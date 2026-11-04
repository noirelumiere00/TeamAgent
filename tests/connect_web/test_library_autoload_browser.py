"""Opt-in, offline headless browser check using real HTML/CSP and a 2,800-video fake.

TEAMAGENT_LIBRARY_BROWSER=1 PYTHONPATH=.:src .venv/bin/pytest -q \
    tests/connect_web/test_library_autoload_browser.py
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from tests.connect_web.test_library_autoload import AutoloadPg
from tests.connect_web.test_search_routes import _EMAIL, _auth_cookie, _config

pytestmark = pytest.mark.skipif(
    os.environ.get("TEAMAGENT_LIBRARY_BROWSER") != "1",
    reason="Opt-in headless browser: TEAMAGENT_LIBRARY_BROWSER=1",
)


def test_scroll_loads_three_pages_and_appended_cards_work(monkeypatch: pytest.MonkeyPatch) -> None:
    from playwright.sync_api import Route, expect, sync_playwright

    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _EMAIL)
    pg = AutoloadPg()
    requests: list[str] = []
    output = Path(
        os.environ.get("TEAMAGENT_LIBRARY_SCREENSHOTS", "/tmp/library-autoload-screenshots")
    )
    output.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(search_config=_config(), admin_pg=pg)) as client:

        def serve(route: Route) -> None:
            url = urlsplit(route.request.url)
            if url.path == "/favicon.ico":
                route.fulfill(status=204)
                return
            if url.path.startswith("/library/thumb/"):
                route.fulfill(
                    content_type="image/svg+xml",
                    body='<svg xmlns="http://www.w3.org/2000/svg" width="540" height="960">'
                    '<rect width="540" height="960" fill="#e1e9f5"/>'
                    '<text x="40" y="450" font-size="28" fill="#234e89">VIDEO / SAMPLE</text></svg>',
                )
                return
            requests.append(url.path + "?" + url.query)
            response = client.get(
                url.path + ("?" + url.query if url.query else ""),
                headers={"cookie": route.request.headers.get("cookie", "")},
                follow_redirects=False,
            )
            route.fulfill(
                status=response.status_code, headers=dict(response.headers), body=response.content
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies(
                    [
                        {
                            "name": k,
                            "value": v,
                            "domain": "library.test",
                            "path": "/",
                            "secure": True,
                        }
                        for k, v in _auth_cookie().items()
                    ]
                )
                context.route("https://library.test/**", serve)
                context.route(
                    "https://www.tiktok.com/**", lambda route: route.fulfill(body="Preview test")
                )
                context.add_init_script(
                    "Object.defineProperty(navigator,'clipboard',{value:{"
                    "writeText:async text=>{window.copiedURLs=text;}}});"
                )
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("https://library.test/library/videos?category=organic&sort=plays")
                cards = page.locator("[data-items] > .card")
                expect(cards).to_have_count(50)
                for count in (100, 150, 200):
                    page.locator("[data-load-more]").scroll_into_view_if_needed()
                    expect(cards).to_have_count(count)
                assert all("category=organic" in url and "sort=plays" in url for url in requests)
                for number in (2, 3, 4):
                    assert sum(url.endswith(f"page={number}") for url in requests) == 1
                expect(page.locator("[data-load-status]")).to_have_text("200件表示（全2,800件）")
                page.screenshot(path=str(output / "videos-200-1440.png"))
                # Both selected videos were appended, not part of the initial 50.
                for index in (50, 100):
                    cards.nth(index).locator("input[data-url]").check()
                page.locator("#copy").click()
                expect(page.locator("#copy-status")).to_have_text("2件コピーしました")
                assert page.evaluate("window.copiedURLs") == "\n".join(
                    pg.videos[index]["url"] for index in (50, 100)
                )
                for index in (50, 100):
                    cards.nth(index).locator(".preview").hover()
                    expect(page.locator("iframe")).to_have_count(1)
                    expect(cards.nth(index).locator("iframe")).to_have_count(1)
                page.locator(".library-header").hover()
                expect(page.locator("iframe")).to_have_count(0)
                assert cards.nth(100).locator(".card-link").get_attribute("href").endswith("10100")
                assert cards.nth(101).locator("img").get_attribute("loading") == "lazy"
                page.goto("https://library.test/library/cases?category=organic&sort=plays")
                for count in (12, 24, 36, 48, 55):
                    expect(page.locator("[data-items] > .card")).to_have_count(count)
                    if count < 55:
                        page.locator("[data-load-more]").scroll_into_view_if_needed()
                expect(page.locator("[data-load-status]")).to_have_text(
                    "すべて表示しました（55件）"
                )
                page.screenshot(path=str(output / "cases-55-1440.png"))
                page.goto("https://library.test/library/c?advertiser=a&campaign=b")
                for count in (50, 100, 150):
                    expect(page.locator("[data-items] > .card")).to_have_count(count)
                    if count < 150:
                        page.locator("[data-load-more]").scroll_into_view_if_needed()
                expect(page.locator("[data-load-status]")).to_have_text(
                    "すべて表示しました（150件）"
                )
                assert len(page.locator("#campaign-urls").text_content().splitlines()) == 150
                page.screenshot(path=str(output / "case-150-1440.png"))
                assert errors == []
                # With JavaScript disabled, the same HTML still navigates via rel=next.
                nojs = browser.new_context(java_script_enabled=False)
                nojs.add_cookies(context.cookies())
                nojs.route("https://library.test/**", serve)
                fallback = nojs.new_page()
                fallback.goto("https://library.test/library/videos?sort=plays")
                fallback.locator('a[rel="next"]').click()
                expect(fallback).to_have_url(
                    "https://library.test/library/videos?sort=plays&page=2"
                )
                expect(fallback.locator("[data-items] > .card")).to_have_count(50)
            finally:
                browser.close()
