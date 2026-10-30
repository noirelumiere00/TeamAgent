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
    assert soup.find("button", disabled=True).text == "構成・フックで探す（近日公開）"
    assert "オーガニック投稿を見る" in response.text
    assert len(pg.calls) == 3  # aggregate + diversified videos + categories, never N+1
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
    assert soup.select_one("a.preview")["target"] == "_blank"
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
    assert client.get("/library?sort=bad", cookies=_auth_cookie()).status_code == 400


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
    chip = soup.select_one('.filters [aria-label="業種"] a')
    assert chip is not None
    assert parse_qs(urlsplit(str(chip["href"])).query)["industry"] == ["食品 & 飲料"]
    card = soup.select_one(".video-grid .card")
    assert [label.text for label in card.select(".card-tag .filter-label")] == [
        "区分",
        "業種",
        "目的",
    ]
    assert len(card.select(".badge")) == 3
    for field, value in (("category", "media"), ("industry", "食品 & 飲料"), ("purpose", "認知")):
        assert any(
            parse_qs(urlsplit(str(tag["href"])).query).get(field) == [value]
            for tag in card.select("a.badge")
        )
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
