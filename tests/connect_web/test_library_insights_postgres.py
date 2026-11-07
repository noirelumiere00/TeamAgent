"""Latest two analytics snapshots through the detail route and actual viewer RLS."""

from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from tests.connect_web.test_search_routes import _auth_cookie, _config
from tests.ingest.test_video_library_account_add_postgres import accounts, db
from tests.ingest.test_video_library_postgres import _DSN

__all__ = ["accounts", "db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")


def test_latest_two_snapshots_for_video_as_viewer(
    accounts: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, repository = accounts
    conn.execute(
        """INSERT INTO vl_videos(platform,video_id,url) VALUES
           ('tiktok','123','https://www.tiktok.com/@sample/video/123'),
           ('tiktok','456','https://www.tiktok.com/@sample/video/456')"""
    )
    conn.execute(
        """INSERT INTO vl_video_insights(platform,video_id,captured_at,reach) VALUES
           ('tiktok','123','2026-11-06 10:00+09',12345),
           ('tiktok','123','2026-11-05 18:00+09',10000),
           ('tiktok','123','2026-11-06 01:00+09',11000),
           ('tiktok','456','2026-11-07 10:00+09',999999)"""
    )
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    client = TestClient(create_app(search_config=_config(), admin_pg=repository.pg))
    response = client.get("/library/v/tiktok/123", cookies=_auth_cookie())
    assert response.status_code == 200
    section = BeautifulSoup(response.text, "html.parser").select_one(".video-insights")
    assert section.select_one(".stats dd").text == "12,345"
    assert "取得日時: 2026/11/06 10:00" in section.text
    assert "前回（2026/11/06）から リーチ +1,345" in section.text
    assert "999,999" not in section.text
    assert conn.execute("SELECT current_user AS role").fetchone()["role"] == "teamagent_app"
    assert (
        conn.execute("SELECT current_setting('app.user_role') AS role").fetchone()["role"] == "user"
    )
