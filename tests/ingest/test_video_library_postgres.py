"""Actual migration/upsert/RLS checks; runs only with explicit TEAMAGENT_TEST_DB_DSN.

Each test uses a disposable schema in a rolled-back transaction, never public tables.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library import Account, Campaign, Metrics, SyncBatch, Video
from teamagent.ingest.video_library_repository import VideoLibraryRepository

NOW = datetime(2026, 10, 29, tzinfo=UTC)

_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_MIGRATION = Path(__file__).resolve().parents[2] / "infra/migrations/0035_video_library.sql"
pytestmark = pytest.mark.skipif(not _DSN, reason="Actual DB validation requires TEAMAGENT_TEST_DB_DSN")


class TransactionPg:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[Any]:
        # The actual production SET ROLE/GUC logic, without committing the fixture transaction.
        PgVectorClient._apply_session(
            self.conn,
            kwargs.get("app_role"),
            kwargs.get("user_email"),
            kwargs.get("user_groups"),
            kwargs.get("user_role"),
            kwargs.get("application_name"),
        )
        yield self.conn


@pytest.fixture
def db() -> Iterator[tuple[Any, VideoLibraryRepository]]:
    assert _DSN
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        schema = f"vl_test_{uuid.uuid4().hex}"
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO teamagent_app").format(sql.Identifier(schema))
        )
        conn.execute(_MIGRATION.read_text())
        repository = VideoLibraryRepository(TransactionPg(conn))  # type: ignore[arg-type]
        try:
            yield conn, repository
        finally:
            conn.rollback()


def seed_batch() -> SyncBatch:
    return SyncBatch(
        rows_read=1,
        accounts=[Account(platform="tiktok", handle="test", operator_label="NewsTV")],
        campaigns=[
            Campaign(advertiser="test", campaign="test", business_category="tto", is_private=True)
        ],
        videos=[
            Video(
                platform="tiktok",
                video_id="1",
                url="https://www.tiktok.com/@test/video/1",
                business_category="tto",
                caption="#PR",
                is_pr=True,
                is_private=True,
            )
        ],
        metrics=[
            Metrics(
                platform="tiktok", video_id="1", captured_on=date(2026, 10, 29), organic_plays=0
            )
        ],
    )


def test_postgres_idempotence_and_daily_snapshot_overwrite(
    db: tuple[Any, VideoLibraryRepository],
) -> None:
    conn, repository = db
    batch = seed_batch()
    assert repository.business_categories()["organic"] == "オーガニック"
    assert repository.save(batch, request_id="first", started_at=NOW) == 1
    assert conn.execute("SELECT operator_label FROM vl_accounts").fetchone() == {
        "operator_label": "NewsTV",
    }
    # An old timestamp exposes accidental updates even within this one transaction.
    conn.execute(
        "UPDATE vl_videos SET updated_at='2000-01-01T00:00:00Z', thumbnail_s3_key='future-thumbnail', video_s3_key='future-video'"
    )
    tables = ("vl_videos", "vl_metrics", "vl_accounts", "vl_campaigns", "vl_business_categories")
    before = {
        t: conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(t))).fetchall()
        for t in tables
    }
    assert repository.save(batch, request_id="second", started_at=NOW) == 0
    after = {
        t: conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(t))).fetchall()
        for t in tables
    }
    assert before == after
    assert conn.execute("SELECT count(*) AS n FROM vl_ingest_runs").fetchone()["n"] == 2
    batch.metrics[0].organic_plays = 100
    repository.save(batch, request_id="overwrite", started_at=NOW)
    rows = conn.execute("SELECT organic_plays, total_plays FROM vl_metrics").fetchall()
    assert rows == [{"organic_plays": 100, "total_plays": None}]
    batch.metrics[0].captured_on = date(2026, 10, 30)
    repository.save(batch, request_id="next-day", started_at=NOW)
    assert conn.execute("SELECT count(*) AS n FROM vl_metrics").fetchone()["n"] == 2
    repository.record_failure(request_id="failed", started_at=NOW, error="ValueError")
    assert (
        conn.execute("SELECT error FROM vl_ingest_runs WHERE request_id='failed'").fetchone()[
            "error"
        ]
        == "ValueError"
    )


def test_postgres_rls_internal_read_and_admin_write(db: tuple[Any, VideoLibraryRepository]) -> None:
    conn, repository = db
    repository.save(seed_batch(), request_id="first", started_at=NOW)
    conn.execute("SELECT set_config('app.user_role', 'viewer', true)")
    assert conn.execute("SELECT current_user AS role").fetchone()["role"] == "teamagent_app"
    assert conn.execute("SELECT count(*) AS n FROM vl_videos").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM vl_ingest_runs").fetchone()["n"] == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute(
            "INSERT INTO vl_videos (platform, video_id, url) VALUES ('tiktok', '2', 'url')"
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("DELETE FROM vl_videos")
    assert conn.execute("UPDATE vl_videos SET is_private=false").rowcount == 0
    conn.execute("SELECT set_config('app.user_role', 'admin', true)")
    conn.execute("INSERT INTO vl_business_categories VALUES ('new_category', '追加区分', 50)")
    assert repository.business_categories()["new_category"] == "追加区分"
    conn.execute("UPDATE vl_videos SET business_category='new_category'")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("UPDATE vl_videos SET business_category='does-not-exist'")
