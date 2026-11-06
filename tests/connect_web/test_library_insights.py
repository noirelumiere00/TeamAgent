"""Latest TikTok analytics on the authenticated video detail page."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.connect_web.test_library import Pg, setup
from tests.connect_web.test_search_routes import _auth_cookie

__all__ = ["setup"]


def insight(**changes: Any) -> dict[str, Any]:
    return {
        "captured_at": datetime(2026, 11, 5, 16, 30, tzinfo=UTC),
        "reach": 12345,
        "completion_rate": Decimal("0.125"),
        "avg_watch_seconds": Decimal("12.34"),
        "profile_views": 1234,
        "male_ratio": Decimal("0.2924"),
        "female_ratio": Decimal("0.7076"),
        "age_bands": [
            {"band": "55+", "ratio": 0.1},
            {"band": "18-24", "ratio": 0.2},
            {"band": "35-44", "ratio": 0.3},
            {"band": "25-34", "ratio": 0.4},
        ],
        "retention": [1000, 800, 500, 490, 200],
        **changes,
    }


def detail(client: TestClient) -> BeautifulSoup:
    response = client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    assert response.status_code == 200
    return BeautifulSoup(response.text, "html.parser")


def test_no_insights_hides_entire_section(setup: tuple[TestClient, Pg]) -> None:
    client, _ = setup
    page = detail(client)
    assert not page.select(".video-insights")
    assert "視聴データ（TikTok 分析）" not in page.text


def test_latest_numbers_chart_audience_and_single_bounded_query(
    setup: tuple[TestClient, Pg], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, pg = setup
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    pg.insights = [insight()]
    section = detail(client).select_one(".video-insights")
    assert section is not None
    assert [node.text for node in section.select(".stats dt")] == [
        "リーチ",
        "完了率",
        "平均視聴時間",
        "プロフィール閲覧",
    ]
    assert [node.text for node in section.select(".stats dd")] == [
        "12,345",
        "12.5%",
        "12.34秒",
        "1,234",
    ]
    chart = section.select_one('svg.retention-chart[role="img"]')
    assert chart is not None
    assert len(chart.select_one("polyline")["points"].split()) == 5
    assert chart.select_one("circle")["cx"] == "354.00"
    assert chart.select_one("circle")["cy"] == "101.40"
    description = section.find(id=chart["aria-describedby"])
    assert description.text == (
        "0秒の視聴者数を100%とした残っている割合。"
        "0秒: 100.0%、1秒: 80.0%、2秒: 50.0%、3秒: 49.0%、4秒: 20.0%"
    )
    for text in ("3秒で半分が離脱", "男性", "29.2%", "女性", "70.8%", "取得日時: 2026/11/06 01:30"):
        assert text in section.text
    assert [node["width"] for node in section.select(".ratio-fill")] == ["29.24", "70.76"]
    assert [node.text for node in section.select("ol li")] == [
        "25-34：40.0%",
        "35-44：30.0%",
        "18-24：20.0%",
    ]
    assert "前回" not in section.text
    queries = [(query, params) for query, params in pg.calls if "FROM vl_video_insights" in query]
    assert len(queries) == 1
    query, params = queries[0]
    assert "WHERE platform=%s AND video_id=%s ORDER BY captured_at DESC LIMIT 2" in query
    assert params == ("tiktok", "123")
    assert pg.sessions[0]["app_role"] == "teamagent_app"
    assert pg.sessions[0]["user_role"] == "user"


@pytest.mark.parametrize("previous,expected", [(10000, "+2,345"), (13000, "-655"), (12345, "+0")])
def test_previous_reach_difference(
    setup: tuple[TestClient, Pg], previous: int, expected: str
) -> None:
    client, pg = setup
    pg.insights = [
        insight(),
        insight(reach=previous, captured_at=datetime(2026, 11, 4, tzinfo=UTC)),
    ]
    assert f"前回（2026/11/04）から リーチ {expected}" in detail(client).text


@pytest.mark.parametrize("retention", [[], [1000], [0, 0]])
def test_insufficient_retention_is_text_only(
    setup: tuple[TestClient, Pg], retention: list[int]
) -> None:
    client, pg = setup
    pg.insights = [insight(retention=retention)]
    section = detail(client).select_one(".video-insights")
    assert "データ不足" in section.text
    assert not section.select(".retention-chart")
    assert "半分が離脱" not in section.text
    assert "12,345" in section.text


@pytest.mark.parametrize("retention", [[1000, 500], [1000, 1200, 600]])
def test_no_half_drop_and_increasing_counts(
    setup: tuple[TestClient, Pg], retention: list[int]
) -> None:
    client, pg = setup
    pg.insights = [insight(retention=retention)]
    section = detail(client).select_one(".video-insights")
    assert "半分以上が視聴を続けています" in section.text
    assert not section.select(".chart-marker")
    points = section.select_one("polyline")["points"].split()
    assert all(30 <= float(point.split(",")[1]) <= 170 for point in points)


def test_missing_values_are_not_zero_and_band_text_is_escaped(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    pg.insights = [
        insight(
            reach=None,
            completion_rate=None,
            avg_watch_seconds=None,
            profile_views=None,
            male_ratio=None,
            female_ratio=None,
            age_bands=[{"band": '<img src=x onerror="alert(1)"> &', "ratio": 0.5}],
        ),
        insight(),
    ]
    section = detail(client).select_one(".video-insights")
    assert [node.text for node in section.select(".stats dd")] == ["データなし"] * 4
    assert not section.select("img, script, .ratio-fill")
    assert section.select_one("li").text == '<img src=x onerror="alert(1)"> &：50.0%'
    assert "&lt;img" in str(section)
    assert "リーチ差: データ不足" in section.text


def test_zero_values_remain_visible(setup: tuple[TestClient, Pg]) -> None:
    client, pg = setup
    pg.insights = [
        insight(
            reach=0,
            completion_rate=0,
            avg_watch_seconds=0,
            profile_views=0,
            male_ratio=0,
            female_ratio=0,
            age_bands=[],
        )
    ]
    section = detail(client).select_one(".video-insights")
    assert [node.text for node in section.select(".stats dd")] == ["0", "0.0%", "0.00秒", "0"]
    assert [node["width"] for node in section.select(".ratio-fill")] == ["0.00", "0.00"]
    assert not section.select("ol")


@pytest.mark.skipif(os.environ.get("TEAMAGENT_LIBRARY_BROWSER") != "1", reason="Opt-in browser")
@pytest.mark.parametrize("width", [375, 1440])
def test_insights_fit_viewport_with_real_csp(setup: tuple[TestClient, Pg], width: int) -> None:
    from playwright.sync_api import sync_playwright

    client, pg = setup
    pg.insights = [insight(reach=2147483647)]
    response = client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            page.route("**/*", lambda route: route.fulfill(status=204))
            page.route(
                "https://library.test/library/v/tiktok/123",
                lambda route: route.fulfill(
                    status=response.status_code,
                    headers=dict(response.headers),
                    body=response.content,
                ),
            )
            page.goto("https://library.test/library/v/tiktok/123")
            assert page.locator(".retention-chart").is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator(".video-insights").evaluate(
                "el => el.scrollWidth <= el.clientWidth"
            )
            assert (
                page.locator(".ratio-fill").first.evaluate("el => getComputedStyle(el).fill")
                == "rgb(35, 78, 137)"
            )
            page.locator(".video-insights").screenshot(path=f"/tmp/library-insights-{width}.png")
        finally:
            browser.close()
