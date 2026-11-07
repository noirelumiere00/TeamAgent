"""0043 persistence and RLS in a disposable schema; no real analytics rows."""

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from teamagent.adapters.gsheets_client import GSheetsClient
from teamagent.ingest import video_library_insights_sync as sync
from teamagent.ingest.video_library_account_repository import AccountAddRepository
from teamagent.ingest.video_library_url_repository import JST
from tests.ingest.test_video_library_account_add_postgres import MIGRATIONS, accounts, db
from tests.ingest.test_video_library_insights_sync import enabled, row, sheet
from tests.ingest.test_video_library_postgres import _DSN

__all__ = ["accounts", "db", "enabled"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")


def test_merge_insert_only_owned_retry_and_latest_public_metrics(
    accounts: tuple[Any, AccountAddRepository], enabled: None
) -> None:
    conn, repository = accounts
    conn.execute(
        """INSERT INTO vl_accounts(platform,handle,operator_label) VALUES
           ('tiktok','@SAMPLE','NewsTV'), ('tiktok','foreign','他社')"""
    )
    conn.execute(
        """INSERT INTO vl_videos(platform,video_id,url,caption,account_handle,origin,is_pr)
           VALUES ('tiktok','123','existing-url','existing-caption','existing','external',false)"""
    )
    before_video = conn.execute("SELECT * FROM vl_videos").fetchone()
    conn.execute(
        """INSERT INTO vl_public_metrics
           (platform,video_id,captured_on,captured_at,total_plays,metric_observation)
           VALUES ('tiktok','123','2026-06-11','2026-06-11 20:00:00+09',9000,'{"source":"public_page"}')"""
    )
    before_metric = conn.execute("SELECT * FROM vl_public_metrics").fetchone()
    public_only = row(**dict.fromkeys(sync.PRIVATE_COLUMNS, ""), データ取得日時="2026/06/12 00:01")
    newest = row(URL="https://www.tiktok.com/@sample/video/456", 視聴数="2000")
    older = row(
        URL="https://www.tiktok.com/@sample/video/456",
        データ取得日時="2026/06/11 18:00",
        視聴数="500",
    )
    old_day = row(データ取得日時="2026/06/10 19:00")
    sheets = MagicMock(spec=GSheetsClient)
    sheets.get_tab_rows.return_value = sheet(
        row(),
        newest,
        older,
        old_day,
        public_only,
        row(URL="https://www.tiktok.com/@foreign/video/789"),
    )
    first = sync.run(repository.pg, sheets, request_id="postgres-first")
    assert first.rows == 6 and first.videos == 2
    assert first.inserted_videos == 1 and first.insights == 4
    assert first.skipped == {"no_insights": 1, "unregistered_external_handle": 1}
    assert conn.execute("SELECT * FROM vl_videos WHERE video_id='123'").fetchone() == before_video
    new = conn.execute("SELECT * FROM vl_videos WHERE video_id='456'").fetchone()
    assert new["origin"] == "own" and new["is_pr"]
    assert new["caption"] == newest["タイトル"]
    assert new["account_handle"] == "sample" and new["hashtags"] == ["PR", "テスト"]
    assert new["duration_seconds"] == 31
    assert new["posted_at"] == datetime(2026, 6, 5, tzinfo=JST)
    assert new["url"] == "https://www.tiktok.com/@sample/video/456"
    assert conn.execute("SELECT count(*) AS n FROM vl_videos").fetchone()["n"] == 2
    assert conn.execute("SELECT count(*) AS n FROM vl_video_insights").fetchone()["n"] == 4
    stats = conn.execute(
        "SELECT * FROM vl_video_insights WHERE video_id='123' ORDER BY captured_at DESC"
    ).fetchall()
    assert len(stats) == 2
    assert stats[0]["captured_at"] == datetime(2026, 6, 11, 19, tzinfo=JST)
    assert [stats[0][k] for k in ("views", "likes", "comments", "shares", "saves")] == [
        1234,
        23,
        4,
        5,
        6,
    ]
    assert float(stats[0]["completion_rate"]) == pytest.approx(0.062)
    assert float(stats[0]["male_ratio"]) == pytest.approx(0.2924)
    assert stats[0]["age_bands"] == [
        {"band": "25-34", "ratio": 0.258},
        {"band": "18-24", "ratio": 0.242},
        {"band": "55+", "ratio": 0.2},
    ]
    assert stats[0]["retention"] == [1234, 1000]
    assert stats[0]["source"] == "analytics_sheet"
    second = sync.run(repository.pg, sheets, request_id="postgres-repeat")
    assert second.inserted_videos == 0
    assert conn.execute("SELECT count(*) AS n FROM vl_video_insights").fetchone()["n"] == 4
    assert conn.execute("SELECT count(*) AS n FROM vl_public_metrics").fetchone()["n"] == 4
    assert (
        conn.execute(
            "SELECT * FROM vl_public_metrics WHERE video_id='123' AND captured_on='2026-06-11'"
        ).fetchone()
        == before_metric
    )
    latest = conn.execute("SELECT * FROM vl_public_metrics WHERE video_id='456'").fetchone()
    assert latest["total_plays"] == 2000
    assert latest["captured_at"] == datetime(2026, 6, 11, 19, tzinfo=JST)
    assert latest["metric_observation"] == {"source": "analytics_sheet"}
    assert (
        conn.execute(
            "SELECT total_plays FROM vl_public_metrics WHERE captured_on='2026-06-12'"
        ).fetchone()["total_plays"]
        == 1234
    )
    sheets.get_tab_rows.return_value = sheet({**newest, "視聴数": "2500", "完了率": "12%"})
    sync.run(repository.pg, sheets, request_id="postgres-corrected")
    corrected = conn.execute(
        "SELECT views,completion_rate FROM vl_video_insights WHERE video_id='456' ORDER BY captured_at DESC"
    ).fetchone()
    assert corrected["views"] == 2500
    assert float(corrected["completion_rate"]) == pytest.approx(0.12)
    assert conn.execute("SELECT count(*) AS n FROM vl_video_insights").fetchone()["n"] == 4
    assert conn.execute("SELECT count(*) AS n FROM vl_metrics").fetchone()["n"] == 0


def test_migration_replay_acl_constraints_and_employee_read_only(
    accounts: tuple[Any, AccountAddRepository], enabled: None
) -> None:
    conn, repository = accounts
    conn.execute(
        "INSERT INTO vl_accounts(platform,handle,operator_label) VALUES ('tiktok','sample','NewsTV')"
    )
    sheets = MagicMock(spec=GSheetsClient)
    sheets.get_tab_rows.return_value = sheet(row())
    sync.run(repository.pg, sheets, request_id="rls-seed")
    before = conn.execute("SELECT * FROM vl_video_insights").fetchall()
    conn.execute("RESET ROLE")
    migration = (MIGRATIONS / "0043_video_library_insights.sql").read_text()
    conn.execute(migration)
    conn.execute(migration)
    assert conn.execute("SELECT * FROM vl_video_insights").fetchall() == before
    assert conn.execute(
        """SELECT relrowsecurity,relforcerowsecurity FROM pg_class
           WHERE oid='vl_video_insights'::regclass"""
    ).fetchone() == {"relrowsecurity": True, "relforcerowsecurity": True}
    assert (
        conn.execute(
            """SELECT count(*) AS n FROM pg_class, LATERAL aclexplode(relacl) acl
           WHERE oid='vl_video_insights'::regclass AND acl.grantee=0"""
        ).fetchone()["n"]
        == 0
    )
    with repository.pg.connection(
        app_role="teamagent_app", user_role="viewer", user_email="member@example.test"
    ):
        assert conn.execute("SELECT * FROM vl_video_insights").fetchall() == before
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                """INSERT INTO vl_video_insights(platform,video_id,captured_at)
                   VALUES ('tiktok','123','2026-06-12 00:00+09')"""
            )
        assert conn.execute("UPDATE vl_video_insights SET views=0").rowcount == 0
        assert conn.execute("SELECT * FROM vl_video_insights").fetchall() == before
    with repository.pg.connection(app_role="teamagent_app", user_role="admin", user_email=None):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE vl_video_insights SET completion_rate=1.01")
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute(
                """INSERT INTO vl_video_insights(platform,video_id,captured_at)
                   VALUES ('tiktok','missing',now())"""
            )
        assert conn.execute("UPDATE vl_video_insights SET views=42").rowcount == 1
