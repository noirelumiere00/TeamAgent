"""RLS-aware, idempotent decisions; the sheet-owned tables are read-only here."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import structlog
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

logger = structlog.get_logger(__name__)


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
        # Session lock, then end the transaction: holding a transaction open across Haiku calls
        # hit the RDS idle-in-transaction timeout (OperationalError after ~1-2 min on 11/04).
        # The lock still serializes replicas and is released with the session if the run dies.
        with self.connection() as conn:
            row = conn.execute("SELECT pg_try_advisory_lock(864238) AS acquired").fetchone()
            acquired = bool(row and row["acquired"])
            conn.commit()
            try:
                yield acquired
            finally:
                if acquired and not conn.closed:
                    try:
                        conn.execute("SELECT pg_advisory_unlock(864238)")
                        conn.commit()
                    except psycopg.Error:
                        logger.warning("case_inference_unlock_failed")  # session end releases it

    @staticmethod
    def cases(conn: Connection[dict[str, Any]]) -> list[Case]:
        return [
            Case(**r)
            for r in conn.execute(
                """SELECT DISTINCT COALESCE(a.to_advertiser,c.advertiser) AS advertiser,
                   COALESCE(a.to_campaign,c.campaign) AS campaign
                   FROM vl_campaigns c LEFT JOIN vl_case_aliases a
                   ON a.from_advertiser=c.advertiser AND a.from_campaign=c.campaign
                   ORDER BY advertiser, campaign"""
            ).fetchall()
        ]

    @staticmethod
    def evaluation_rows(conn: Connection[dict[str, Any]]) -> list[dict[str, Any]]:
        # Only sheet-labelled rows are truth; never evaluate against an inferred/human label.
        return list(
            conn.execute(
                """SELECT v.* FROM vl_videos_effective v
               JOIN vl_videos sheet ON sheet.platform=v.platform AND sheet.video_id=v.video_id
               JOIN vl_campaigns c ON c.advertiser=sheet.advertiser AND c.campaign=sheet.campaign
               WHERE sheet.is_pr AND v.caption IS NOT NULL AND v.caption<>''
               ORDER BY v.platform, v.video_id"""
            ).fetchall()
        )

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
