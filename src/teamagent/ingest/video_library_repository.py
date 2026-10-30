"""Atomic P1 library upserts using the existing RLS-aware connection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library import SyncBatch


def _upsert(
    conn: Connection[dict[str, Any]],
    table: str,
    record: BaseModel,
    keys: tuple[str, ...],
) -> int:
    values = record.model_dump()
    columns = list(values)
    changed = [c for c in columns if c not in keys]
    assignments: list[sql.Composable] = [
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in changed
    ]
    if table == "vl_videos":
        assignments.append(sql.SQL("updated_at = now()"))
    query = sql.SQL("""
        INSERT INTO {table} ({columns}) VALUES ({values})
        ON CONFLICT ({keys}) DO UPDATE SET {assignments}
        WHERE ({old}) IS DISTINCT FROM ({new})
    """).format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
        values=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        keys=sql.SQL(", ").join(map(sql.Identifier, keys)),
        assignments=sql.SQL(", ").join(assignments),
        old=sql.SQL(", ").join(sql.Identifier(table, c) for c in changed),
        new=sql.SQL(", ").join(sql.Identifier("excluded", c) for c in changed),
    )
    return conn.execute(query, list(values.values())).rowcount


class VideoLibraryRepository:
    def __init__(self, pg: PgVectorClient) -> None:
        self.pg = pg

    def business_categories(self) -> dict[str, str]:
        with self.pg.connection(
            app_role="teamagent_app",
            user_email=None,
            user_role="admin",
            application_name="teamagent-video-library-sync",
        ) as conn:
            return {
                row["code"]: row["display_name"]
                for row in conn.execute(
                    "SELECT code, display_name FROM vl_business_categories"
                ).fetchall()
            }

    def save(self, batch: SyncBatch, *, request_id: str, started_at: datetime) -> int:
        """One transaction; unchanged rows (including video timestamps) are untouched."""
        with self.pg.connection(
            app_role="teamagent_app",
            user_email=None,
            user_role="admin",
            application_name="teamagent-video-library-sync",
        ) as conn:
            for campaign in batch.campaigns:
                _upsert(conn, "vl_campaigns", campaign, ("advertiser", "campaign"))
            for account in batch.accounts:
                _upsert(conn, "vl_accounts", account, ("platform", "handle"))
            changed = 0
            for video in batch.videos:
                changed += _upsert(conn, "vl_videos", video, ("platform", "video_id"))
            for metrics in batch.metrics:
                _upsert(conn, "vl_metrics", metrics, ("platform", "video_id", "captured_on"))
            self._record_run(conn, batch, request_id, started_at, changed, None)
            return changed

    def record_failure(
        self, *, request_id: str, started_at: datetime, error: str, batch: SyncBatch | None = None
    ) -> None:
        with self.pg.connection(
            app_role="teamagent_app",
            user_email=None,
            user_role="admin",
            application_name="teamagent-video-library-sync",
        ) as conn:
            self._record_run(conn, batch or SyncBatch(), request_id, started_at, 0, error)

    @staticmethod
    def _record_run(
        conn: Connection[dict[str, Any]],
        batch: SyncBatch,
        request_id: str,
        started_at: datetime,
        changed: int,
        error: str | None,
    ) -> None:
        conn.execute(
            """INSERT INTO vl_ingest_runs
                (request_id, started_at, rows_read, videos_upserted, skipped, warnings, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (request_id) DO NOTHING""",
            (
                request_id,
                started_at,
                batch.rows_read,
                changed,
                Jsonb(batch.skipped),
                Jsonb(batch.warnings),
                error,
            ),
        )
