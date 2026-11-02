"""Durable URL queue, shared fetch budget, and conservative URL-only writes."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import timedelta, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library_url_models import (
    ImportResult,
    InputRow,
    Observation,
    Source,
    URLImportError,
    inspect_input,
)

JST = timezone(timedelta(hours=9))
# Session lock covers HTTP and row recovery across replicas, without an open transaction.
FETCH_LOCK = 8675309037
Row = dict[str, Any]


class URLImportRepository:
    def __init__(self, pg: PgVectorClient) -> None:
        self.pg = pg

    def connection(self) -> AbstractContextManager[Connection[Row]]:
        return self.pg.connection(
            app_role="teamagent_app",
            user_email=None,
            user_role="admin",
            application_name="teamagent-url-import",
        )

    def submit(
        self, text: str, *, requested_by: str, idempotency_key: str, source: Source = "url_manual"
    ) -> str:
        rows = inspect_input(text)
        if not requested_by.strip() or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise URLImportError("invalid_request_identity")
        if source not in {"url_manual", "p3b"}:
            raise URLImportError("invalid_source")
        batch_id = str(uuid.uuid4())
        with self.pg.connection(
            app_role="teamagent_app", user_email=requested_by, user_role="viewer"
        ) as conn:
            inserted = conn.execute(
                """INSERT INTO vl_url_import_batches
                   (batch_id, requested_by, idempotency_key, source) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (requested_by, idempotency_key) DO NOTHING RETURNING batch_id""",
                (batch_id, requested_by, idempotency_key, source),
            ).fetchone()
            if not inserted:
                existing = conn.execute(
                    """SELECT batch_id FROM vl_url_import_batches
                       WHERE requested_by=%s AND idempotency_key=%s""",
                    (requested_by, idempotency_key),
                ).fetchone()
                assert existing is not None
                return str(existing["batch_id"])
            for row in rows:
                self._insert_row(conn, batch_id, row)
        return batch_id

    @staticmethod
    def _insert_row(conn: Connection[Row], batch_id: str, row: InputRow) -> None:
        existing = (
            row.video_id
            and conn.execute(
                "SELECT 1 FROM vl_videos WHERE platform='tiktok' AND video_id=%s", (row.video_id,)
            ).fetchone()
        )
        conn.execute(
            """INSERT INTO vl_url_import_rows
               (batch_id,line_no,input_url,canonical_url,video_id,duplicate_of,status,error_code,
                registration_result) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                batch_id,
                row.line_no,
                row.input_url,
                row.canonical_url,
                row.video_id,
                row.duplicate_of,
                row.status,
                row.error_code,
                "登録済み（統合済み）" if existing else None,
            ),
        )

    @contextmanager
    def fetch_lock(self) -> Iterator[bool]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS locked", (FETCH_LOCK,)
            ).fetchone()
            assert row is not None
            locked = bool(row["locked"])
            conn.commit()
            try:
                yield locked
            finally:
                if locked:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (FETCH_LOCK,))

    def claim(self, *, stale_seconds: int = 600) -> Row | None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE vl_url_import_rows SET status='queued', lease_token=NULL,
                   error_code='processing_recovered' WHERE status='processing'
                   AND started_at < clock_timestamp() - %s * interval '1 second'""",
                (stale_seconds,),
            )
            return conn.execute(
                """WITH candidate AS (
                    SELECT r.batch_id,r.line_no FROM vl_url_import_rows r
                    JOIN vl_url_import_batches b USING(batch_id)
                    WHERE r.status IN ('queued','deferred') AND r.eligible_at<=clock_timestamp()
                    AND NOT (SELECT paused FROM vl_public_fetch_control WHERE platform='tiktok')
                    ORDER BY b.created_at,r.line_no FOR UPDATE OF r SKIP LOCKED LIMIT 1
                ), claimed AS (
                    UPDATE vl_url_import_rows r SET status='processing',
                    started_at=clock_timestamp(),
                    lease_token=%s FROM candidate c
                    WHERE r.batch_id=c.batch_id AND r.line_no=c.line_no RETURNING r.*
                ) SELECT c.*,b.source,b.requested_by FROM claimed c
                  JOIN vl_url_import_batches b USING(batch_id)""",
                (uuid.uuid4(),),
            ).fetchone()

    def before_request(self, limit: int, row: Row | None = None) -> None:
        """Commit every page/redirect/retry attempt before HTTP; failed requests count too."""
        with self.connection() as conn:
            control = conn.execute(
                """SELECT paused, GREATEST(0, EXTRACT(EPOCH FROM
                   last_request_at + interval '1 second' - clock_timestamp())) AS delay
                   FROM vl_public_fetch_control WHERE platform='tiktok' FOR UPDATE"""
            ).fetchone()
            assert control is not None
            if control["paused"]:
                raise URLImportError("fetch_paused")
            quota = conn.execute(
                "SELECT (clock_timestamp() AT TIME ZONE INTERVAL '+09:00')::date AS today"
            ).fetchone()
            assert quota is not None
            conn.execute(
                """INSERT INTO vl_public_fetch_daily_usage(platform,quota_on)
                   VALUES ('tiktok',%s) ON CONFLICT DO NOTHING""",
                (quota["today"],),
            )
            used = conn.execute(
                """UPDATE vl_public_fetch_daily_usage SET used_count=used_count+1,
                   updated_at=clock_timestamp() WHERE platform='tiktok' AND quota_on=%s
                   AND used_count<%s RETURNING used_count""",
                (quota["today"], limit),
            ).fetchone()
            if not used:
                raise URLImportError("daily_limit")
            if row:
                updated = conn.execute(
                    """UPDATE vl_url_import_rows SET attempt_count=attempt_count+1,
                       started_at=clock_timestamp() WHERE batch_id=%s AND line_no=%s
                       AND lease_token=%s AND status='processing'""",
                    (row["batch_id"], row["line_no"], row["lease_token"]),
                ).rowcount
                if not updated:
                    raise URLImportError("lease_lost")
            delay = float(control["delay"] or 0)
            conn.execute(
                """UPDATE vl_public_fetch_control
                   SET last_request_at=clock_timestamp()+%s*interval '1 second'
                   WHERE platform='tiktok'""",
                (delay,),
            )
        if delay:
            time.sleep(delay)

    def resolve_row(self, row: Row, url: str, video_id: str) -> int | None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE vl_url_import_rows SET canonical_url=%s,video_id=%s
                   WHERE batch_id=%s AND line_no=%s AND lease_token=%s""",
                (url, video_id, row["batch_id"], row["line_no"], row["lease_token"]),
            )
            duplicate = conn.execute(
                """SELECT line_no FROM vl_url_import_rows WHERE batch_id=%s AND video_id=%s
                   AND line_no<>%s AND status NOT IN ('duplicate','invalid','unsupported')
                   ORDER BY line_no LIMIT 1""",
                (row["batch_id"], video_id, row["line_no"]),
            ).fetchone()
            return int(duplicate["line_no"]) if duplicate else None

    def save(
        self,
        observation: Observation,
        *,
        source: Source,
        requested_by: str,
        thumbnail: str | None,
        result: ImportResult,
        row: Row | None = None,
        stop: str | None = None,
    ) -> ImportResult:
        o = observation
        with self.connection() as conn:
            if row:
                self._check_lease(conn, row)
            inserted = None
            if o.caption is not None:
                inserted = conn.execute(
                    """INSERT INTO vl_videos (platform,video_id,url,caption,account_handle,
                       posted_at,hashtags,is_pr,origin,ingest_sources)
                       VALUES ('tiktok',%s,%s,%s,%s,%s,%s,%s,'external',%s)
                       ON CONFLICT (platform,video_id) DO NOTHING RETURNING video_id""",
                    (
                        o.video_id,
                        o.url,
                        o.caption,
                        o.account_handle,
                        o.posted_at,
                        o.hashtags,
                        o.is_pr,
                        [source],
                    ),
                ).fetchone()
            existing = conn.execute(
                "SELECT video_id FROM vl_videos WHERE platform='tiktok' AND video_id=%s",
                (o.video_id,),
            ).fetchone()
            if not existing:
                result.status = "classification_pending"
                result.error_code = "caption_missing"
            else:
                result.registration_result = "新規登録" if inserted else "登録済み（統合済み）"
                conn.execute(
                    """UPDATE vl_videos SET page_is_ad=%s,page_ad_label_version=%s,
                       page_disclosure=%s,page_observed_at=%s,page_last_success_at=%s,
                       page_last_attempted_at=%s,page_fetch_status=%s,page_fetch_error_code=NULL,
                       thumbnail_s3_key=COALESCE(thumbnail_s3_key,%s),
                       ingest_sources=CASE WHEN %s=ANY(ingest_sources) THEN ingest_sources
                           ELSE array_append(ingest_sources,%s) END
                       WHERE platform='tiktok' AND video_id=%s
                       AND (page_observed_at IS NULL OR page_observed_at<=%s)""",
                    (
                        o.page_is_ad,
                        Jsonb(o.page_ad_label_version),
                        Jsonb(
                            {
                                "caption": o.caption,
                                "hashtags": o.hashtags,
                                "is_pr": o.is_pr if o.caption is not None else None,
                                "disclosures": o.disclosures if o.caption is not None else None,
                                "version": "v1",
                            }
                        ),
                        o.captured_at,
                        o.captured_at,
                        o.captured_at,
                        result.status,
                        thumbnail,
                        source,
                        source,
                        o.video_id,
                        o.captured_at,
                    ),
                )
                if any(value is not None for value in o.metrics):
                    conn.execute(
                        """INSERT INTO vl_public_metrics
                           (platform,video_id,captured_on,captured_at,total_plays,total_likes,
                            total_comments,total_shares,total_saves,metric_observation)
                           VALUES ('tiktok',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (platform,video_id,captured_on) DO UPDATE SET
                           captured_at=EXCLUDED.captured_at,total_plays=EXCLUDED.total_plays,
                           total_likes=EXCLUDED.total_likes,total_comments=EXCLUDED.total_comments,
                           total_shares=EXCLUDED.total_shares,total_saves=EXCLUDED.total_saves,
                           metric_observation=EXCLUDED.metric_observation
                           WHERE vl_public_metrics.captured_at<=EXCLUDED.captured_at
                           AND (vl_public_metrics.total_plays IS NULL
                                OR EXCLUDED.total_plays IS NOT NULL)
                           AND (vl_public_metrics.total_likes IS NULL
                                OR EXCLUDED.total_likes IS NOT NULL)
                           AND (vl_public_metrics.total_comments IS NULL
                                OR EXCLUDED.total_comments IS NOT NULL)
                           AND (vl_public_metrics.total_shares IS NULL
                                OR EXCLUDED.total_shares IS NOT NULL)
                           AND (vl_public_metrics.total_saves IS NULL
                                OR EXCLUDED.total_saves IS NOT NULL)""",
                        (
                            o.video_id,
                            o.captured_at.astimezone(JST).date(),
                            o.captured_at,
                            *o.metrics,
                            Jsonb({"source": "public_page", "warnings": o.warnings}),
                        ),
                    )
                if o.caption is not None:
                    conn.execute(
                        """INSERT INTO vl_video_analysis_requests
                           (platform,video_id,kind,version,requested_by,source)
                           SELECT 'tiktok',%s,'structure','v1',%s,%s WHERE NOT EXISTS (
                             SELECT 1 FROM vl_video_analysis WHERE platform='tiktok'
                             AND video_id=%s AND kind='structure' AND version='v1')
                           ON CONFLICT DO NOTHING""",
                        (o.video_id, requested_by, source, o.video_id),
                    )
            if row:
                self._finish_row(conn, row, result, o.model_dump(mode="json"))
            self._record_outcome(conn, success=True, stop=stop)
        return result

    @staticmethod
    def _check_lease(conn: Connection[Row], row: Row) -> None:
        if not conn.execute(
            """SELECT 1 FROM vl_url_import_rows WHERE batch_id=%s AND line_no=%s
               AND lease_token=%s AND status='processing' FOR UPDATE""",
            (row["batch_id"], row["line_no"], row["lease_token"]),
        ).fetchone():
            raise URLImportError("lease_lost")

    @staticmethod
    def _finish_row(
        conn: Connection[Row],
        row: Row,
        result: ImportResult,
        observation: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """UPDATE vl_url_import_rows SET status=%s,error_code=%s,
               registration_result=COALESCE(%s,registration_result),
               observation=%s,duplicate_of=%s,completed_at=clock_timestamp(),lease_token=NULL,
               eligible_at=CASE WHEN %s='deferred' THEN
                   (date_trunc('day',clock_timestamp() AT TIME ZONE INTERVAL '+09:00')
                     + interval '1 day') AT TIME ZONE INTERVAL '+09:00' ELSE eligible_at END
               WHERE batch_id=%s AND line_no=%s AND lease_token=%s""",
            (
                result.status,
                result.error_code,
                result.registration_result,
                Jsonb(observation) if observation is not None else None,
                result.duplicate_of,
                result.status,
                row["batch_id"],
                row["line_no"],
                row["lease_token"],
            ),
        )

    @staticmethod
    def _record_outcome(conn: Connection[Row], *, success: bool, stop: str | None = None) -> None:
        conn.execute(
            """UPDATE vl_public_fetch_control SET
               consecutive_failures=CASE WHEN %s THEN 0 ELSE consecutive_failures+1 END,
               paused=paused OR %s::text IS NOT NULL OR (NOT %s AND consecutive_failures>=4),
               pause_reason=CASE WHEN %s::text IS NOT NULL THEN %s
                 WHEN NOT %s AND consecutive_failures>=4 THEN 'consecutive_failures'
                 ELSE pause_reason END WHERE platform='tiktok'""",
            (success, stop, success, stop, stop, success),
        )

    def finish_failure(self, row: Row, result: ImportResult, *, stop: str | None = None) -> None:
        with self.connection() as conn:
            self._check_lease(conn, row)
            self._finish_row(conn, row, result)
            if result.video_id and result.status == "failed":
                conn.execute(
                    """UPDATE vl_videos SET page_fetch_status='failed',
                       page_last_attempted_at=clock_timestamp(),page_fetch_error_code=%s
                       WHERE platform='tiktok' AND video_id=%s""",
                    (result.error_code, result.video_id),
                )
            if result.status == "failed":
                self._record_outcome(conn, success=False, stop=stop)

    def thumbnail_missing(self, video_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT thumbnail_s3_key FROM vl_videos WHERE platform='tiktok' AND video_id=%s",
                (video_id,),
            ).fetchone()
            return row is None or row["thumbnail_s3_key"] is None

    def record_failure(self, result: ImportResult, *, stop: str | None = None) -> None:
        with self.connection() as conn:
            if result.video_id:
                conn.execute(
                    """UPDATE vl_videos SET page_fetch_status='failed',
                       page_last_attempted_at=clock_timestamp(),page_fetch_error_code=%s
                       WHERE platform='tiktok' AND video_id=%s""",
                    (result.error_code, result.video_id),
                )
            self._record_outcome(conn, success=False, stop=stop)
