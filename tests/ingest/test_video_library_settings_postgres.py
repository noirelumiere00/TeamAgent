"""Live view/RLS contract in a rollback-only schema; requires TEAMAGENT_TEST_DB_DSN."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from teamagent.connect_web.library import search_videos
from teamagent.ingest.video_library import resolve_business_category
from tests.ingest.test_video_library_postgres import _DSN, NOW, db, seed_batch

# Reuse the isolated P1 transaction fixture, never public or production tables.
__all__ = ["db"]
pytestmark = pytest.mark.skipif(
    not _DSN, reason="Actual DB validation requires TEAMAGENT_TEST_DB_DSN"
)
MIGRATION = Path(__file__).resolve().parents[2] / "infra/migrations/0036_video_library_settings.sql"


def prepare(conn: Any, repository: Any) -> None:
    conn.execute(MIGRATION.read_text())
    conn.execute(MIGRATION.read_text())  # Reapplying must preserve human settings.
    batch = seed_batch()
    batch.videos[0].advertiser = "test"
    batch.videos[0].campaign = "test"
    repository.save(batch, request_id="p3", started_at=NOW)


@pytest.mark.parametrize(
    "is_pr,category", [(False, None), (False, "media"), (True, None), (True, "tto")]
)
def test_view_matches_resolver_and_settings_change_without_sync(
    db: tuple[Any, Any], is_pr: bool, category: str | None
) -> None:
    conn, repository = db
    prepare(conn, repository)
    conn.execute("UPDATE vl_videos SET is_pr=%s", (is_pr,))
    initial = conn.execute("SELECT * FROM vl_videos_effective").fetchone()
    assert initial["is_private"] is False
    assert initial["industry"] is None and initial["purpose"] is None
    assert conn.execute("SELECT business_category FROM vl_videos_effective").fetchone()[
        "business_category"
    ] == resolve_business_category(is_pr=is_pr)
    conn.execute(
        "INSERT INTO vl_campaign_settings (advertiser,campaign,business_category,is_private,industry,purpose,updated_by) VALUES ('test','test',%s,true,'industry','purpose','admin')",
        (category,),
    )
    row = conn.execute("SELECT * FROM vl_videos_effective").fetchone()
    assert row["business_category"] == resolve_business_category(
        is_pr=is_pr, campaign_category=category
    )
    assert row["is_private"] is True
    assert row["industry"] == "industry" and row["purpose"] == "purpose"
    conn.execute("UPDATE vl_campaign_settings SET business_category='kirinuki',is_private=false")
    row = conn.execute("SELECT * FROM vl_videos_effective").fetchone()
    assert row["business_category"] == resolve_business_category(
        is_pr=is_pr, campaign_category="kirinuki"
    )
    assert row["is_private"] is False
    repository.save(seed_batch(), request_id="sync-again", started_at=NOW)
    assert (
        conn.execute("SELECT business_category FROM vl_campaign_settings").fetchone()[
            "business_category"
        ]
        == "kirinuki"
    )


def test_settings_rls_and_security_invoker(db: tuple[Any, Any]) -> None:
    conn, repository = db
    prepare(conn, repository)
    conn.execute(
        "INSERT INTO vl_campaign_settings (advertiser,campaign,updated_by) VALUES ('test','test','admin')"
    )
    conn.execute(
        "INSERT INTO vl_account_settings (platform,handle,registered_by) VALUES ('tiktok','sample','admin')"
    )
    conn.execute(
        "INSERT INTO vl_video_analysis (platform,video_id,kind,version,payload) VALUES ('tiktok','1','structure','v1','{}')"
    )
    conn.execute("SELECT set_config('app.user_role','user',true)")
    assert conn.execute("SELECT count(*) AS n FROM vl_videos_effective").fetchone()["n"] == 1
    for table in ("vl_campaign_settings", "vl_account_settings", "vl_video_analysis"):
        assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 1
        assert (
            conn.execute(
                f"UPDATE {table} SET "
                + (
                    "updated_by='bad'"
                    if table == "vl_campaign_settings"
                    else "handle='bad'"
                    if table == "vl_account_settings"
                    else "kind='bad'"
                )
            ).rowcount
            == 0
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(f"DELETE FROM {table}")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute(
            "INSERT INTO vl_campaign_settings (advertiser,campaign,updated_by) VALUES ('x','x','bad')"
        )
    options = conn.execute(
        "SELECT reloptions FROM pg_class WHERE oid='vl_videos_effective'::regclass"
    ).fetchone()["reloptions"]
    assert "security_invoker=true" in options


def test_search_uses_latest_snapshot_and_settings_immediately(db: tuple[Any, Any]) -> None:
    conn, repository = db
    prepare(conn, repository)
    conn.execute(
        "UPDATE vl_metrics SET organic_plays=4000, organic_saves=40, organic_likes=10, organic_comments=2, organic_shares=3"
    )
    conn.execute(
        "INSERT INTO vl_metrics (platform,video_id,captured_on,organic_plays,organic_saves) VALUES ('tiktok','1','2026-10-30',10000,200)"
    )
    for sort in ("new", "plays", "saves", "eng"):
        data = search_videos(conn, {"sort": sort}, admin=False)
        assert data["total"] == 1
        assert data["videos"][0]["organic_plays"] == 10000
        assert data["videos"][0]["rates"] == {
            "save_rate": 0.02,
            "engagement_rate": None,
            "is_reference": False,
        }
    conn.execute(
        "INSERT INTO vl_campaign_settings (advertiser,campaign,business_category,is_private,updated_by) VALUES ('test','test','media',true,'admin')"
    )
    assert search_videos(conn, {}, admin=False)["total"] == 0
    assert search_videos(conn, {"category": "media"}, admin=True)["total"] == 1
    conn.execute("UPDATE vl_campaign_settings SET is_private=false,business_category='tto'")
    assert search_videos(conn, {"category": "media"}, admin=False)["total"] == 0
    assert (
        search_videos(conn, {"category": "tto", "q": "#PR", "pr": "yes"}, admin=False)["total"] == 1
    )
    assert search_videos(conn, {"q": "' OR true--"}, admin=False)["total"] == 0
