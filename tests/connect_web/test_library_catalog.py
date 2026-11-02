"""Catalog journeys and untrusted analysis display, using the real route middleware."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.connect_web import library
from teamagent.connect_web.library_analysis import EXAMPLE_PAYLOAD, render_analysis
from tests.connect_web.test_library import Pg, setup
from tests.connect_web.test_search_routes import _auth_cookie

__all__ = ["setup"]


def case_row(pg: Pg) -> dict[str, Any]:
    return {
        **pg.row,
        "video_count": 2,
        "first_posted": pg.row["posted_at"],
        "last_posted": pg.row["posted_at"],
        "categories": ["media"],
        "primary_category": "media",
        "average_save_rate": 0.02,
        "representative": pg.row.copy(),
    }


def catalog_pg(pg: Pg) -> None:
    original = pg.execute

    def execute(query: str, params: Any = None) -> MagicMock:
        result = original(query, params)
        if "GROUP BY v.advertiser" in query:
            result.fetchall.return_value = [case_row(pg)]
        return result

    pg.execute = execute  # type: ignore[method-assign]


def test_catalog_case_links_copy_and_all_videos(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    pg.row.update(advertiser='広告主 / ? & " <script>', campaign='案件 / ? & " <img>')
    catalog_pg(pg)
    response = client.get("/library", cookies=_auth_cookie())
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    case_link = soup.select_one(".campaign-grid a")
    assert case_link is not None
    path = str(case_link["href"])
    assert parse_qs(urlsplit(path).query) == {
        "advertiser": [pg.row["advertiser"]],
        "campaign": [pg.row["campaign"]],
    }
    assert "<script>" not in response.text
    assert soup.select_one("nav [aria-current]")["href"] == "/library"
    assert soup.select_one("main .filters") is None
    assert "オーガニック投稿を見る" in response.text
    assert len(pg.calls) == 2  # aggregate + diversified videos, never N+1
    response = client.get(path, cookies=_auth_cookie())
    soup = BeautifulSoup(response.text, "html.parser")
    summary = soup.select_one("#campaign-copy").text
    for expected in (
        pg.row["campaign"],
        "動画: 2本",
        "合計再生: 17,800",
        "合計保存: 178",
        "保存率: 1.00%",
        "ENG率: 算出不可",
        pg.row["url"],
        "再生 17,800",
        "2026-10-30",
    ):
        assert expected in summary
    assert soup.select_one("#campaign-urls").text == pg.row["url"]
    assert soup.select_one(".card-link .preview") is not None
    assert soup.select_one(".case-hero [data-copy=campaign-copy]") is not None
    assert soup.select_one('a[href="/library/v/tiktok/123"]') is not None
    assert "<img>" not in response.text
    assert "LIMIT %s" not in pg.calls[-1][0]  # case copy/grid include every video
    assert pg.row["campaign"] not in pg.calls[-1][0]
    assert pg.row["campaign"] in pg.calls[-1][1]


def test_catalog_navigation_and_security(
    setup: tuple[TestClient, Pg], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, pg = setup
    catalog_pg(pg)
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "other@example.test")
    for path in (
        "/library",
        "/library/videos",
        "/library/cases",
        "/library/c?advertiser=a&campaign=b",
        "/library/v/tiktok/123",
    ):
        response = client.get(path, cookies=_auth_cookie())
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        assert not soup.select('nav a[href^="/library/admin"]')
        assert "'unsafe-inline'" not in response.headers["content-security-policy"]
        assert "https://www.tiktok.com" in response.headers["content-security-policy"]
        assert response.headers["referrer-policy"] == "same-origin"
        assert all(tag.get("nonce") for tag in soup.select("script, style"))
        assert not soup.select("[style]")
    assert all("NOT v.is_private" in q for q, _ in pg.calls if "vl_videos_effective" in q)


def test_empty_catalog_and_missing_case(setup: tuple[TestClient, Pg]) -> None:
    client, _ = setup
    assert "条件に合う事例がありません" in client.get("/library", cookies=_auth_cookie()).text
    assert client.get("/library/c", cookies=_auth_cookie()).status_code == 400
    assert (
        client.get("/library/c?advertiser=a&campaign=b", cookies=_auth_cookie()).status_code == 404
    )
    assert client.get("/library/cases?sort=bad", cookies=_auth_cookie()).status_code == 400


def test_diversify_never_repeats_campaign_or_account_and_does_not_mutate() -> None:
    rows = [
        dict(platform="tiktok", video_id=str(i), advertiser="a", campaign=c, account_handle=a)
        for i, (c, a) in enumerate(
            [("A", "a"), ("A", "b"), ("B", "a"), ("B", "b"), ("C", "c"), ("C", "d")]
        )
    ]
    before = deepcopy(rows)
    result = library.diversify(rows, 4)
    assert [r["video_id"] for r in result] == ["0", "3", "4", "1"]
    assert rows == before
    assert library.diversify(rows[:2]) == rows[:1]
    assert library.diversify([]) == []
    organic = [dict(platform="tiktok", video_id=str(i), account_handle=str(i)) for i in range(14)]
    assert len(library.diversify(organic)) == 12


def test_copy_excludes_corrupted_url() -> None:
    pg = Pg()
    pg.row["url"] = "javascript:alert(1)"
    assert "javascript:" not in library.campaign_copy_text(case_row(pg), [pg.row])


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"segments": [{"start": 2, "end": 1, "role": "hook"}]},
        {"segments": [{"start": float("nan"), "end": 3, "role": "hook"}]},
        {"tags": {"hook_type": "bad"}},
        {
            "segments": [
                {"start": 0, "end": 3, "role": "hook"},
                {"start": 2, "end": 4, "role": "demo"},
            ]
        },
    ],
)
def test_analysis_invalid_or_absent_shows_disabled_example(payload: object) -> None:
    soup = BeautifulSoup(render_analysis(payload), "html.parser")
    assert "見本" in soup.text and "この動画の分析結果ではありません" in soup.text
    assert not soup.select("a.tag")
    assert len(soup.select('.tag[aria-disabled="true"]')) > 3
    assert len(soup.select("button[data-segment]")) == 4
    assert all(t.get("tabindex") == "0" for t in soup.select(".tag"))


def test_analysis_contract_grouped_tags_timeline_and_escaping() -> None:
    payload = deepcopy(EXAMPLE_PAYLOAD)
    payload["segments"][0]["telop"] = "<img src=x onerror=alert(1)>"
    payload["tags"]["pain"] = ["<script>alert(1)</script>"]
    rendered = render_analysis(payload)
    soup = BeautifulSoup(rendered, "html.parser")
    assert "見本" not in soup.text
    assert not soup.select("img,script")
    assert "<img src=x" in soup.select_one("#segment-0").text
    assert len(soup.select(".tag-group")) == 7
    assert "冒頭の型（フック）" in soup.select_one(".hook_type h3").text
    link = soup.select_one(".hook_type a.tag")
    assert parse_qs(urlsplit(str(link["href"])).query) == {
        "tag_group": ["hook_type"],
        "tag": ["question"],
    }
    assert "問いかける" in link.select_one(".tooltip").text
    assert "#segment-button-0{flex:1.8}" in rendered
    assert "#segment-button-2{flex:8}" in rendered
    assert not soup.select("[style]")  # nonce on style tags, no unsafe inline styles
    assert "テロップ" in soup.select_one("#segment-0").text


def test_tag_search_is_parameterized_and_latest_version_only(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    tag = "x' OR true--"
    response = client.get(
        "/library/videos", params={"tag_group": "hook_type", "tag": tag}, cookies=_auth_cookie()
    )
    assert response.status_code == 200
    query, values = pg.calls[1]
    assert tag not in query and tag in values[2]
    assert "ORDER BY a.analyzed_at DESC LIMIT 1" in query
    assert values[:2] == ["structure", "v1"]
    assert (
        client.get("/library/videos?tag_group=evil&tag=x", cookies=_auth_cookie()).status_code
        == 400
    )


def test_catalog_industry_chips_and_card_tag_labels(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    pg.row.update(industry="食品 & 飲料", purpose="認知")
    catalog_pg(pg)
    soup = BeautifulSoup(client.get("/library", cookies=_auth_cookie()).text, "html.parser")
    assert soup.select_one("main .filters") is None
    listing = BeautifulSoup(
        client.get("/library/cases", cookies=_auth_cookie()).text, "html.parser"
    )
    assert listing.select_one('[name="industry"]') is not None
    card = soup.select_one(".video-grid .card")
    assert [label.text for label in card.select(".card-tag .filter-label")] == ["業種", "目的"]
    assert card.select_one(".category-label")["aria-label"] == "メディア"
    assert len(card.select("a")) == 1
    detail = BeautifulSoup(
        client.get("/library/v/tiktok/123", cookies=_auth_cookie()).text, "html.parser"
    )
    assert any(
        parse_qs(urlsplit(str(tag["href"])).query).get("industry") == ["食品 & 飲料"]
        for tag in detail.select(".tag-group a")
    )


def test_detail_has_clickable_player_and_only_supported_analysis(
    setup: tuple[TestClient, Pg],
) -> None:
    client, pg = setup
    original = pg.execute

    def execute(query: str, params: Any = None) -> MagicMock:
        result = original(query, params)
        if "SELECT kind, version" in query:
            result.fetchall.return_value = [
                {
                    "kind": "structure",
                    "version": "v2",
                    "payload": {"tags": {"hook_type": ["wrong-version"]}},
                },
                {"kind": "structure", "version": "v1", "payload": EXAMPLE_PAYLOAD},
            ]
        return result

    pg.execute = execute  # type: ignore[method-assign]
    response = client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one('#detail-player button[data-play="detail-player"]') is not None
    assert soup.select_one(".hook_type a.tag") is not None
    assert "wrong-version" not in response.text
    assert "この動画の分析結果ではありません" not in response.text
    assert all(tag.get("nonce") for tag in soup.select("style,script"))


def test_featured_videos_put_thumbnailed_videos_first() -> None:
    rows = [
        {
            "platform": "tiktok",
            "video_id": "1",
            "account_handle": "a",
            "total_plays": 900,
            "thumbnail_s3_key": None,
        },
        {
            "platform": "tiktok",
            "video_id": "2",
            "account_handle": "b",
            "total_plays": 100,
            "thumbnail_s3_key": "video-library/thumbs/tiktok/2.jpg",
        },
    ]

    class _Conn:
        def execute(self, sql: str, values: object) -> _Conn:
            return self

        def fetchall(self) -> list[dict[str, object]]:
            return [dict(row) for row in rows]

    result = library.featured_videos(_Conn(), {}, admin=True)
    assert [row["video_id"] for row in result] == ["2", "1"]


@pytest.mark.parametrize("inferred", [False, True])
def test_ai_inference_badge_on_case_card_and_video_detail(
    setup: tuple[TestClient, Pg], inferred: bool
) -> None:
    client, pg = setup
    pg.row["case_inferred"] = inferred
    card = BeautifulSoup(library._campaign_card(case_row(pg)), "html.parser")
    assert ("AI推定" in card.text) is inferred
    assert ("AI推定" in library._video_card(pg.row)) is inferred
    detail = client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    assert detail.status_code == 200
    assert ("AI推定" in detail.text) is inferred


def test_advertiser_only_inference_is_visible_without_invented_case(
    setup: tuple[TestClient, Pg],
) -> None:
    client, pg = setup
    pg.row.update(case_inferred=True, advertiser="Brand <script>", campaign=None)
    response = client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    assert response.status_code == 200
    assert "広告主: Brand &lt;script&gt;" in response.text
    assert "AI推定" in response.text
    assert "の事例を見る" not in response.text
