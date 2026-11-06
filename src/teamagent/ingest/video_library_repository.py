"""Atomic P1 library upserts using the existing RLS-aware connection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library import Account, SyncBatch


def _upsert(
    conn: Connection[dict[str, Any]],
    table: str,
    record: BaseModel,
    keys: tuple[str, ...],
) -> int:
    values = record.model_dump()
    columns = list(values)
    changed = [c for c in columns if c not in keys]
    action: sql.Composable = sql.SQL("DO NOTHING")
    if changed:
        assignments: list[sql.Composable] = [
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
            for c in changed
        ]
        if table == "vl_videos":
            assignments.append(sql.SQL("updated_at = now()"))
        action = sql.SQL("""
            DO UPDATE SET {assignments}
            WHERE ({old}) IS DISTINCT FROM ({new})
        """).format(
            assignments=sql.SQL(", ").join(assignments),
            old=sql.SQL(", ").join(sql.Identifier(table, c) for c in changed),
            new=sql.SQL(", ").join(sql.Identifier("excluded", c) for c in changed),
        )
    query = sql.SQL("""
        INSERT INTO {table} ({columns}) VALUES ({values})
        ON CONFLICT ({keys}) {action}
    """).format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
        values=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        keys=sql.SQL(", ").join(map(sql.Identifier, keys)),
        action=action,
    )
    return conn.execute(query, list(values.values())).rowcount


_ACCOUNT_LABELS = ("display_name", "operator_label", "classification")


def _save_account(conn: Connection[dict[str, Any]], account: Account) -> None:
    # Accounts seen only in video rows carry no labels; NULL must not erase a ledger/manual 運営
    # (11/06: 110 NewsTV accounts were blanked nightly this way).
    # shortcut: the ledger can no longer clear a label by emptying it; clear by hand if ever needed.
    kept = [
        sql.SQL("COALESCE(EXCLUDED.{c}, {t}.{c})").format(
            c=sql.Identifier(c), t=sql.Identifier("vl_accounts")
        )
        for c in _ACCOUNT_LABELS
    ]
    conn.execute(
        sql.SQL("""
            INSERT INTO vl_accounts (platform, handle, {cols}) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (platform, handle) DO UPDATE SET ({cols}) = ({kept})
            WHERE ({old}) IS DISTINCT FROM ({kept})
        """).format(
            cols=sql.SQL(", ").join(map(sql.Identifier, _ACCOUNT_LABELS)),
            kept=sql.SQL(", ").join(kept),
            old=sql.SQL(", ").join(sql.Identifier("vl_accounts", c) for c in _ACCOUNT_LABELS),
        ),
        (account.platform, account.handle, *(getattr(account, c) for c in _ACCOUNT_LABELS)),
    )


class VideoLibraryRepository:
    def __init__(self, pg: PgVectorClient) -> None:
        self.pg = pg

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
                _save_account(conn, account)
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
