"""RLS-aware, idempotent decisions; the sheet-owned tables are read-only here."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library_case_models import (
    PROMPT_VERSION,
    Case,
    Post,
    Prediction,
    catalog_hash,
)


class CaseRepository:
    def __init__(self, pg: PgVectorClient) -> None:
        self.pg = pg

    @contextmanager
    def connection(self) -> Iterator[Connection[dict[str, Any]]]:
        with self.pg.connection(
            app_role="teamagent_app",
            user_email=None,
            user_role="admin",
            application_name="teamagent-case-inference",
        ) as conn:
            yield conn

    @contextmanager
    def run_lock(self) -> Iterator[bool]:
        # Transaction-scoped and released on exception; serializes replicas' daily budgets.
        with self.connection() as conn:
            row = conn.execute("SELECT pg_try_advisory_xact_lock(864238) AS acquired").fetchone()
            yield bool(row and row["acquired"])

    @staticmethod
    def cases(conn: Connection[dict[str, Any]]) -> list[Case]:
        return [
            Case(**r)
            for r in conn.execute(
                "SELECT advertiser, campaign FROM vl_campaigns ORDER BY advertiser, campaign"
            ).fetchall()
        ]

    def load(self) -> tuple[list[Case], list[dict[str, Any]]]:
        with self.connection() as conn:
            return self.cases(conn), list(
                conn.execute(
                    """SELECT v.*, l.input_hash FROM vl_videos v
                LEFT JOIN vl_video_campaign_links l
                    ON l.platform=v.platform AND l.video_id=v.video_id AND l.source='ai'
                WHERE COALESCE(v.campaign,'')=''
                  AND (v.is_pr OR COALESCE(v.advertiser,'')<>'')
                  AND NOT EXISTS (SELECT 1 FROM vl_video_campaign_links h
                    WHERE h.platform=v.platform AND h.video_id=v.video_id AND h.source='human')
                ORDER BY l.decided_at NULLS FIRST, v.platform, v.video_id"""
                ).fetchall()
            )

    def invalidate(self, row: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE vl_video_campaign_links SET status='unknown'
                WHERE platform=%s AND video_id=%s AND source='ai' AND status='applied'""",
                (row["platform"], row["video_id"]),
            )

    def save(
        self,
        row: dict[str, Any],
        result: Prediction,
        status: str,
        fingerprint: str,
        model: str,
        cases: list[Case],
    ) -> bool:
        with self.connection() as conn:
            current = conn.execute(
                "SELECT * FROM vl_videos WHERE platform=%s AND video_id=%s FOR UPDATE",
                (row["platform"], row["video_id"]),
            ).fetchone()
            if (
                not current
                or current["campaign"]
                or Post.from_row(current) != Post.from_row(row)
                or current["is_pr"] != row["is_pr"]
                or catalog_hash(self.cases(conn)) != catalog_hash(cases)
            ):
                return False
            if conn.execute(
                """SELECT 1 FROM vl_video_campaign_links
                WHERE platform=%s AND video_id=%s AND source='human'""",
                (row["platform"], row["video_id"]),
            ).fetchone():
                return False
            conn.execute(
                """INSERT INTO vl_video_campaign_links
                (platform, video_id, advertiser, campaign, source, status, match_type,
                 confidence, evidence, model, prompt_version, input_hash, decided_by)
                VALUES (%s,%s,%s,%s,'ai',%s,%s,%s,%s,%s,%s,%s,'bedrock-haiku')
                ON CONFLICT (platform,video_id,source) DO UPDATE SET
                 advertiser=EXCLUDED.advertiser, campaign=EXCLUDED.campaign,
                 status=EXCLUDED.status, match_type=EXCLUDED.match_type,
                 confidence=EXCLUDED.confidence, evidence=EXCLUDED.evidence,
                 model=EXCLUDED.model, prompt_version=EXCLUDED.prompt_version,
                 input_hash=EXCLUDED.input_hash, decided_by=EXCLUDED.decided_by,
                 decided_at=now()""",
                (
                    row["platform"],
                    row["video_id"],
                    result.advertiser,
                    result.campaign,
                    status,
                    result.match_type,
                    result.confidence,
                    Jsonb([e.model_dump() for e in result.evidence]),
                    model,
                    PROMPT_VERSION,
                    fingerprint,
                ),
            )
            return True
