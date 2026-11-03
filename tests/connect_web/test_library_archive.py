"""Media archive presentation contracts; security still goes through real middleware."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.connect_web import library
from tests.connect_web.test_library import Pg, setup
from tests.connect_web.test_library_catalog import case_row
from tests.connect_web.test_library_catalog_review import LargeCatalogPg
from tests.connect_web.test_search_routes import _auth_cookie

__all__ = ["setup"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "再生数未集計"),
        (0, "0回再生"),
        (9999, "9,999回再生"),
        (10000, "1万回再生"),
        (17800, "1.7万回再生"),
        (1234567, "123.4万回再生"),
        (99999999, "9999.9万回再生"),
        (100000000, "1億回再生"),
        (123456789, "1.2億回再生"),
    ],
)
def test_compact_plays_boundaries(value: float | None, expected: str) -> None:
    assert library.compact_plays(value) == expected


def test_home_shelves_thumbnail_priority_recency_and_category_order() -> None:
    pg = Pg()
    rows = []
    for i, code in enumerate([None, "organic", "media", "tto", "kirinuki", "media", None]):
        row = deepcopy(case_row(pg))
        row.update(
            campaign=f"事例{i}",
            categories=[code],
            primary_category=code,
            total_plays=10000 - i,
            last_posted=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=i),
        )
        row["representative"]["has_thumbnail"] = i > 1
        row["representative"]["thumbnail_s3_key"] = None
        rows.append(row)
    before = deepcopy(rows)
    soup = BeautifulSoup(library.archive_home(rows, []), "html.parser")
    assert [h.text for h in soup.select("#pickup h3")] == [f"事例{i}" for i in [2, 3, 4, 5, 6]]
    assert [h.text for h in soup.select("#new h3")] == [f"事例{i}" for i in range(6, -1, -1)]
    assert [s["data-category"] for s in soup.select(".category-section")] == [
        "media",
        "tto",
        "kirinuki",
        "organic",
        "unset",
    ]
    assert not soup.select('.category-section[data-category="other"]')
    assert "種別未設定" in soup.select(".category-section")[-1].text
    assert len(soup.select('.category-section[data-category="media"] .card')) == 2
    assert rows == before


def test_large_home_two_queries_and_limited_shelves(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    large = LargeCatalogPg()
    pg.execute = large.execute  # type: ignore[method-assign]
    assert len(large.videos) == 2800 and len(large.cases) == 55
    assert sum(not v["thumbnail_s3_key"] for v in large.videos) == 280
    assert sum(c["categories"][0] in (None, "organic") for c in large.cases) == 48
    soup = BeautifulSoup(client.get("/library", cookies=_auth_cookie()).text, "html.parser")
    assert len(large.calls) == 2
    assert sum("GROUP BY v.advertiser" in q for q, _ in large.calls) == 1
    assert len(soup.select("#pickup .card")) == 5
    assert len(soup.select("#new .card")) == 8
    assert len(soup.select("#trending .card")) == 12
    assert all(len(s.select(".card")) <= 4 for s in soup.select(".category-section"))
    assert soup.select_one("main .filters") is None
    assert "すべての事例を見る（55件）" in soup.text
    assert "数値は毎平日 18 時に更新" in soup.footer.text
    assert "社内限定" in soup.footer.text


@pytest.mark.parametrize("query", ["all=1", "all=1&category=organic&q=a%26b&page=2", "sort=new"])
def test_legacy_catalog_redirect_preserves_filters(
    setup: tuple[TestClient, Pg], query: str
) -> None:
    client, pg = setup
    response = client.get("/library?" + query, cookies=_auth_cookie(), follow_redirects=False)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == "/library/cases"
    assert parse_qs(target.query) == {k: v for k, v in parse_qs(query).items() if k != "all"}
    assert pg.calls == []


def test_whole_card_links_selection_and_mobile_navigation(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    for rendered in (
        library._campaign_card(case_row(pg)),
        library._video_card(pg.row, selectable=True),
    ):
        soup = BeautifulSoup(rendered, "html.parser")
        assert len(soup.select("a")) == 1
        link = soup.select_one("a.card-link")
        assert link["aria-label"] and link.select_one(".preview") and link.select_one("h3")
        assert not link.select("a,button,input")
        assert "詳細を見る" not in soup.text
        assert link["href"].startswith("/library/")
    listing = BeautifulSoup(
        client.get("/library/videos", cookies=_auth_cookie()).text, "html.parser"
    )
    assert listing.select_one(".card-select input[data-url]").find_parent("a") is None
    detail = BeautifulSoup(
        client.get("/library/v/tiktok/123", cookies=_auth_cookie()).text, "html.parser"
    )
    assert detail.select_one("button[data-play]") is not None
    assert detail.find("a", string="TikTokで開く")["target"] == "_blank"
    assert ".video-shelf{grid-template-columns:none;" in library._STYLE
    assert "prefers-reduced-motion" in library._STYLE
    assert ".card-link:hover .preview img{transform:none}" in library._STYLE


@pytest.mark.parametrize("path", ["/library/cases", "/library/videos"])
def test_category_navigation_current_and_header_search(
    setup: tuple[TestClient, Pg], path: str
) -> None:
    client, _ = setup
    soup = BeautifulSoup(
        client.get(path + "?category=tto", cookies=_auth_cookie()).text, "html.parser"
    )
    nav = soup.select_one("header .category-nav")
    assert len(nav.select("a[aria-current]")) == 1
    assert nav.select_one("[aria-current]").text == "TTO"
    assert nav.select_one("[aria-current]")["href"] == path + "?category=tto"
    assert len(nav.select("a")) == 6
    search = soup.select_one(".header-search")
    assert search.select_one("summary[aria-label]") is not None
    assert search.select_one('form[role="search"]')["action"] == "/library/videos"
    assert not search.has_attr("open")  # Mobile/no-JS defaults to a native disclosure.


def test_related_video_query_is_scoped_private_safe_and_bounded(
    setup: tuple[TestClient, Pg], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, pg = setup
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "other@example.test")
    pg.row.update(advertiser="a' OR true--", campaign="案件 & /")
    original = pg.execute

    def execute(query: str, params: Any = None) -> MagicMock:
        result = original(query, params)
        if "AND NOT (v.platform=%s" in query:
            result.fetchall.return_value = [
                {**pg.row, "video_id": "456", "url": "https://www.tiktok.com/@sample/video/456"}
            ]
        return result

    pg.execute = execute  # type: ignore[method-assign]
    soup = BeautifulSoup(
        client.get("/library/v/tiktok/123", cookies=_auth_cookie()).text, "html.parser"
    )
    assert soup.select_one("#related .card-link")["href"] == "/library/v/tiktok/456"
    query, params = pg.calls[-1]
    assert "NOT v.is_private" in query and query.endswith("LIMIT 6")
    assert "v.advertiser = %s" in query and "v.campaign = %s" in query
    assert "AND NOT (v.platform=%s AND v.video_id=%s)" in query
    assert params == ["a' OR true--", "案件 & /", "tiktok", "123"]
    assert "a' OR true--" not in query
    pg.calls.clear()
    pg.row.update(advertiser=None, campaign=None)
    client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    assert len(pg.calls) == 2


def test_card_labels_hide_unset_and_separate_known_categories() -> None:
    pg = Pg()
    row = case_row(pg)
    row["categories"] = ["organic", None, "media"]
    soup = BeautifulSoup(library._campaign_card(row), "html.parser")
    body = soup.select_one(".card-body")
    assert body.get_text().startswith("ORGANIC · MEDIA")
    assert "UNCLASSIFIED" not in body.text
    assert [e["title"] for e in body.select(".category-label")] == ["オーガニック", "メディア"]
    row["categories"] = [None]
    assert not BeautifulSoup(library._campaign_card(row), "html.parser").select(".category-label")
    video = BeautifulSoup(
        library._video_card({**pg.row, "business_category": None, "is_pr": True}), "html.parser"
    )
    assert not video.select(".category-label")
    assert video.select_one(".video-card-context").text == "PR · campaign"


def test_category_shelves_use_primary_category_once_and_named_headings() -> None:
    pg = Pg()
    codes = ["media", "tto", "kirinuki", "organic", "other", None]
    rows = [
        {**case_row(pg), "campaign": f"case{i}", "categories": codes, "primary_category": code}
        for i, code in enumerate(codes)
    ]
    soup = BeautifulSoup(library.archive_home(rows, []), "html.parser")
    sections = soup.select(".category-section")
    assert [s.h2.contents[0] for s in sections] == [
        "MEDIA",
        "TTO",
        "CLIP",
        "ORGANIC",
        "OTHER",
        "UNCLASSIFIED",
    ]
    assert [s.h2.span.text for s in sections] == [
        "メディア",
        "TTO",
        "切り抜き",
        "オーガニック",
        "その他",
        "種別未設定",
    ]
    assert [[h.text for h in s.select(".card h3")] for s in sections] == [
        [f"case{i}"] for i in range(6)
    ]


def test_primary_category_is_counted_in_one_parameterized_aggregate() -> None:
    pg = Pg()
    library.search_campaigns(pg, {"q": "' OR true--"}, admin=False)
    assert len(pg.calls) == 1
    query, params = pg.calls[0]
    sql = " ".join(query.split())
    assert "count(*) OVER ( PARTITION BY v.advertiser, v.campaign, v.business_category)" in sql
    assert "array_agg(v.business_category ORDER BY v.category_count DESC," in sql
    assert (
        "CASE v.business_category WHEN 'media' THEN 0 WHEN 'tto' THEN 1 "
        "WHEN 'kirinuki' THEN 2 WHEN 'other' THEN 3 WHEN 'organic' THEN 4 "
        "ELSE 5 END))[1] AS primary_category"
    ) in sql
    assert sql.count("FROM vl_videos_effective") == 1
    assert sql.count("GROUP BY v.advertiser, v.campaign") == 1
    assert "NOT v.is_private" in sql and "' OR true--" not in sql
    assert params == ["%'%", "%OR%", "%true--%"]


@pytest.mark.parametrize("count", [1, 3, 4, 5])
def test_sparse_category_uses_new_list_layout(count: int) -> None:
    rows = [{**case_row(Pg()), "campaign": str(i)} for i in range(count)]
    soup = BeautifulSoup(library.archive_home(rows, []), "html.parser")
    section = soup.select_one(".category-section")
    grid = section.select_one(".campaign-grid")
    assert ("new-grid" in grid["class"]) == (count < 4)
    assert ("grid" in grid["class"]) == (count >= 4)
    assert len(grid.select(".card")) == min(count, 4)


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("最初の行 <script>\n次の行", "最初の行 <script>"),
        ("", "account_full_name"),
        (None, "account_full_name"),
        ("  \n本文", "本文"),
        (" \n\t", "account_full_name"),
    ],
)
@pytest.mark.parametrize(
    "path",
    ["/library", "/library/videos", "/library/c?advertiser=a&campaign=b", "/library/v/tiktok/123"],
)
def test_video_titles_use_caption_first_nonempty_line_or_account(
    setup: tuple[TestClient, Pg], caption: str | None, expected: str, path: str
) -> None:
    from tests.connect_web.test_library_catalog import catalog_pg

    client, pg = setup
    pg.row.update(caption=caption, campaign="全動画で同じ案件名", account_name="account_full_name")
    catalog_pg(pg)
    soup = BeautifulSoup(client.get(path, cookies=_auth_cookie()).text, "html.parser")
    card = soup.select_one(".video-grid .card")
    assert card.h3.text == expected
    assert card.select_one(".card-link")["aria-label"].endswith(expected + "の動画詳細")
    assert card.select_one("script") is None
    assert card.select_one(".account-name").text == "account_full_name"
    assert "2026-10-30" in card.select_one(".meta-source").text
    assert [item.text for item in card.select(".meta-plays")] == [
        "この動画 1.7万回再生",
        "施策計 312万回再生",
    ]
    assert card.select_one(".video-card-context").text == "MEDIA · PR · 全動画で同じ案件名"
    assert "次の行" not in card.text


def test_pickup_shelf_aligns_with_container_and_keeps_focus_inside() -> None:
    from tests.connect_web.test_library_catalog_review import css_rule

    rule = css_rule(".pickup-grid")
    assert "margin:0" in rule and "padding:0 0 20px" in rule
    assert "outline-offset:-3px" in css_rule(".pickup-grid .card-link:focus-visible")


@pytest.mark.parametrize(
    "overrides",
    [{"campaign": None}, {"advertiser": ""}, {"campaign_total_plays": None}],
)
def test_video_card_omits_unavailable_campaign_total(overrides: dict[str, Any]) -> None:
    card = BeautifulSoup(library._video_card({**Pg().row, **overrides}), "html.parser")
    assert card.select_one(".video-card-plays").text == "この動画 1.7万回再生"
    if not overrides.get("campaign", True):
        assert card.select_one(".video-card-context").text == "MEDIA · PR"


def test_video_card_escapes_context_title_and_selection_label() -> None:
    row = {**Pg().row, "campaign": '施策 <img src=x>"', "caption": '<script>"&\n次の行'}
    card = BeautifulSoup(library._video_card(row, selectable=True), "html.parser")
    context = card.select_one(".video-card-context")
    assert context.text == 'MEDIA · PR · 施策 <img src=x>"'
    assert context["title"] == row["campaign"]
    assert card.h3.text == card.h3["title"] == '<script>"&'
    assert (
        card.select_one("input")["aria-label"] == row["account_name"] + ': <script>"&の動画を選択'
    )
    assert not card.select(".card-body img, script, [onerror]")


def test_video_card_zero_and_unknown_plays() -> None:
    card = BeautifulSoup(
        library._video_card({**Pg().row, "total_plays": None, "campaign_total_plays": 0}),
        "html.parser",
    )
    assert [item.text for item in card.select(".meta-plays")] == [
        "この動画 再生数未集計",
        "施策計 0回再生",
    ]


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize(
    "path",
    [
        "/library",
        "/library/videos?q=needle",
        "/library/c?advertiser=a&campaign=b",
        "/library/v/tiktok/123",
    ],
)
def test_video_card_queries_aggregate_visible_campaign_before_filtering(
    setup: tuple[TestClient, Pg], monkeypatch: pytest.MonkeyPatch, admin: bool, path: str
) -> None:
    from tests.connect_web.test_library_catalog import catalog_pg
    from tests.connect_web.test_search_routes import _EMAIL

    client, pg = setup
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _EMAIL if admin else "")
    catalog_pg(pg)
    assert client.get(path, cookies=_auth_cookie()).status_code == 200
    queries = [sql for sql, _ in pg.calls if sql.startswith("WITH campaign_totals AS (")]
    assert len(queries) == 1
    totals, selection = queries[0].split("GROUP BY advertiser, campaign)", 1)
    assert "WHERE true" in totals if admin else "WHERE NOT v.is_private" in totals
    assert "count(m.total_plays)=count(*)" in totals
    assert "ORDER BY captured_on DESC LIMIT 1" in totals
    assert "%s" not in totals and "needle" not in queries[0]
    assert "ct.campaign_total_plays" in selection
    assert "LEFT JOIN campaign_totals ct" in selection
