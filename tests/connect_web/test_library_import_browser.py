"""Offline browser review of intake/results at desktop and phone widths.

TEAMAGENT_LIBRARY_BROWSER=1 PYTHONPATH=.:src .venv/bin/pytest -q \
    tests/connect_web/test_library_import_browser.py
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import uvicorn

from teamagent.connect_web.app import create_app
from tests.connect_web.test_library_import import ImportPg
from tests.connect_web.test_search_routes import _auth_cookie, _config
from tests.ingest.test_video_library_url_import import URL

pytestmark = pytest.mark.skipif(
    os.environ.get("TEAMAGENT_LIBRARY_BROWSER") != "1", reason="Opt-in offline browser"
)


@pytest.fixture
def live_import_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, ImportPg]]:
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    monkeypatch.delenv("VIDEO_LIBRARY_IMPORT_WORKER_ENABLED", raising=False)
    pg = ImportPg()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        monkeypatch.setenv("CONNECT_BASE_URL", base)
        app = create_app(search_config=_config(), admin_pg=pg)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "uvicorn did not start"
            app.state.video_library_import_worker = MagicMock()
            yield base, pg
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "uvicorn did not stop"


@pytest.mark.parametrize("width", [1440, 375])
def test_import_confirm_result_and_poll_completion(
    live_import_server: tuple[str, ImportPg], width: int
) -> None:
    from playwright.sync_api import expect, sync_playwright

    base, pg = live_import_server
    output = Path(
        os.environ.get("TEAMAGENT_LIBRARY_SCREENSHOTS", "/tmp/library-import-screenshots")
    )
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": width, "height": 1000})
            context.add_cookies(
                [{"name": k, "value": v, "url": base} for k, v in _auth_cookie().items()]
            )
            tab = context.new_page()
            errors: list[str] = []
            tab.on("pageerror", lambda error: errors.append(str(error)))
            tab.goto(base + "/library/import")
            expect(tab.get_by_role("heading", name="URL取り込み", exact=True)).to_be_visible()
            tab.screenshot(path=str(output / f"import-input-{width}.png"), full_page=True)
            tab.get_by_label("URL（1行1本・100本まで）").fill(
                URL
                + "\n"
                + URL
                + "?q=1\nhttps://vt.tiktok.com/short/\nhttps://youtu.be/test\ninvalid"
            )
            tab.get_by_role("button", name="確認", exact=True).click()
            expect(tab.get_by_role("heading", name="行ごとの検査結果")).to_be_visible()
            tab.screenshot(path=str(output / f"import-confirm-{width}.png"), full_page=True)
            tab.get_by_role("button", name="取り込む", exact=True).click()
            expect(tab.get_by_role("heading", name="受付", exact=True)).to_be_visible()
            batch = next(iter(pg.rows))
            expect(tab).to_have_url(base + "/library/import/" + batch)
            assert tab.locator("[data-import-poll]").get_attribute("data-import-poll") == "true"
            # Complete the fake worker; the real five-second poll must pick up the result.
            pg.rows[batch][0].update(status="succeeded", registration_result="登録済み（統合済み）")
            pg.rows[batch][2].update(status="failed", error_code="network_error")
            expect(tab.get_by_role("heading", name="一部失敗", exact=True)).to_be_visible(
                timeout=8000
            )
            assert tab.locator("[data-import-poll]").get_attribute("data-import-poll") == "false"
            expect(tab.get_by_role("button", name="失敗分を再実行")).to_be_visible()
            tab.screenshot(path=str(output / f"import-result-{width}.png"), full_page=True)
            assert tab.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert not errors
        finally:
            browser.close()
