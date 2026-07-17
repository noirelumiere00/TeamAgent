"""0019/0020 migration の実 PostgreSQL 回帰テスト。

``TEAMAGENT_TEST_DB_DSN`` が明示された disposable PostgreSQL だけで実行する。
各テストは専用 schema を transaction 内に作り、最後に rollback する。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from teamagent.ingest.repository import IngestRepository

_DB_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_0019 = _ROOT / "infra/migrations/0019_ingest_source_health.sql"
_MIGRATION_0020 = _ROOT / "infra/migrations/0020_ingest_source_retry_upgrade.sql"

pytestmark = pytest.mark.skipif(
    _DB_DSN is None,
    reason="disposable PostgreSQL validation requires TEAMAGENT_TEST_DB_DSN",
)


def _prepare_schema(conn: object) -> str:
    from psycopg import sql

    schema = f"ingest_source_test_{uuid.uuid4().hex}"
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'teamagent_app'")
        if cur.fetchone() is None:
            pytest.skip("teamagent_app role is required")
        cur.execute("SELECT pg_has_role(current_user, 'teamagent_app', 'MEMBER')")
        if not bool(cur.fetchone()[0]):
            pytest.skip("test role cannot SET ROLE teamagent_app")
        cur.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO teamagent_app").format(sql.Identifier(schema))
        )
    return schema


def test_fresh_migrations_enforce_rls_and_revoke_destructive_privileges() -> None:
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        schema = _prepare_schema(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(_MIGRATION_0019.read_text(encoding="utf-8"))
                # A fresh database runs 0020 too; it must remain idempotent after the new 0019.
                cur.execute(_MIGRATION_0020.read_text(encoding="utf-8"))

                for table in (
                    "ingest_source_health",
                    "ingest_source_retries",
                    "ingest_reconciliation_gaps",
                    "ingest_connector_runs",
                ):
                    qualified = f"{schema}.{table}"
                    for privilege in ("SELECT", "INSERT", "UPDATE"):
                        cur.execute(
                            "SELECT has_table_privilege('teamagent_app', %s, %s)",
                            (qualified, privilege),
                        )
                        assert cur.fetchone()[0] is True
                    for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                        cur.execute(
                            "SELECT has_table_privilege('teamagent_app', %s, %s)",
                            (qualified, privilege),
                        )
                        assert cur.fetchone()[0] is False

                cur.execute("SET ROLE teamagent_app")
                cur.execute("SET LOCAL app.user_role = 'admin'")
                cur.execute(
                    """
                    INSERT INTO ingest_source_health
                        (source_type, external_id, md5_checksum, size_bytes,
                         mime_type, validator_schema_version, reason)
                    VALUES
                        ('gdrive', 'opaque-test-id',
                         '0123456789abcdef0123456789abcdef', 10,
                         'application/test', 'ooxml-safe-v2', 'corrupt_zip')
                    """
                )
                cur.execute("SET LOCAL app.user_role = ''")
                cur.execute("SELECT count(*) FROM ingest_source_health")
                assert cur.fetchone()[0] == 0
                cur.execute("SET LOCAL app.user_role = 'admin'")
                cur.execute("SELECT count(*) FROM ingest_source_health")
                assert cur.fetchone()[0] == 1
                cur.execute("RESET ROLE")

                cur.execute(
                    """
                    SELECT gap_kind, count(*)
                    FROM ingest_reconciliation_gaps
                    WHERE status = 'unresolved'
                    GROUP BY gap_kind
                    ORDER BY gap_kind
                    """
                )
                assert cur.fetchall() == [
                    ("source_original_missing", 9),
                    ("unindexed_pdf", 3),
                ]
        finally:
            conn.rollback()


def test_upgrade_migration_preserves_legacy_row_and_forces_v2_revalidation() -> None:
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        _prepare_schema(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE ingest_source_health (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        source_type TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        md5_checksum TEXT NOT NULL,
                        size_bytes BIGINT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'invalid_source',
                        reason TEXT NOT NULL,
                        mime_type TEXT,
                        CONSTRAINT ingest_source_health_payload_unique
                            UNIQUE (source_type, external_id, md5_checksum, size_bytes)
                    );
                    CREATE TABLE ingest_connector_runs (id UUID);
                    INSERT INTO ingest_source_health
                        (source_type, external_id, md5_checksum, size_bytes, reason)
                    VALUES
                        ('gdrive', 'legacy-id',
                         '0123456789abcdef0123456789abcdef', 10, 'corrupt_zip');
                    """
                )
                cur.execute(_MIGRATION_0020.read_text(encoding="utf-8"))
                cur.execute(
                    """
                    SELECT mime_type, validator_schema_version
                    FROM ingest_source_health
                    WHERE external_id = 'legacy-id'
                    """
                )
                assert cur.fetchone() == (
                    "application/octet-stream",
                    "ooxml-legacy-v1",
                )
                cur.execute(
                    """
                    SELECT count(*)
                    FROM ingest_source_health
                    WHERE external_id = 'legacy-id'
                      AND mime_type = 'application/test'
                      AND validator_schema_version = 'ooxml-safe-v2'
                    """
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid = 'ingest_source_health'::regclass
                    """
                )
                constraints = {row[0] for row in cur.fetchall()}
                assert "ingest_source_health_payload_unique" not in constraints
                assert "ingest_source_health_fingerprint_unique" in constraints
                assert "ingest_source_health_mime_type_nonempty" in constraints
                assert "ingest_source_health_validator_schema_nonempty" in constraints
        finally:
            conn.rollback()


def test_retry_sql_is_idempotent_leased_and_resets_on_fingerprint_change() -> None:
    import psycopg

    class _TransactionPgVector:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        @contextmanager
        def connection(self, **kwargs: Any) -> Iterator[Any]:
            yield self._conn

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        _prepare_schema(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(_MIGRATION_0019.read_text(encoding="utf-8"))
            repository = IngestRepository(  # type: ignore[arg-type]
                _TransactionPgVector(conn),
                app_role=None,
            )
            fingerprint = {
                "source_kind": "gdrive",
                "source_id": "folder",
                "source_type": "gdrive",
                "external_id": "opaque-retry-id",
                "md5_checksum": "0123456789abcdef0123456789abcdef",
                "size_bytes": 10,
                "mime_type": "application/test",
                "validator_schema_version": "ooxml-safe-v2",
                "reason": "office_download_failed",
            }

            assert repository.record_source_retry(
                **fingerprint,
                request_id="request-1",
            )
            assert repository.record_source_retry(
                **fingerprint,
                request_id="request-1",
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT attempt_count, status
                    FROM ingest_source_retries
                    WHERE external_id = 'opaque-retry-id'
                    """
                )
                assert cur.fetchone() == (1, "pending")
                cur.execute(
                    """
                    UPDATE ingest_source_retries
                    SET next_attempt_at = now()
                    WHERE external_id = 'opaque-retry-id'
                    """
                )

            claims = repository.claim_due_source_retries(
                source_kind="gdrive",
                source_id="folder",
                request_id="request-2",
            )
            assert [claim.external_id for claim in claims] == ["opaque-retry-id"]
            assert (
                repository.claim_due_source_retries(
                    source_kind="gdrive",
                    source_id="folder",
                    request_id="parallel-request",
                )
                == []
            )

            assert repository.record_source_retry(
                **fingerprint,
                request_id="request-2",
            )
            assert repository.record_source_retry(
                **fingerprint,
                request_id="request-2",
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT attempt_count, lease_owner IS NULL,
                           next_attempt_at > now()
                    FROM ingest_source_retries
                    WHERE external_id = 'opaque-retry-id'
                    """
                )
                assert cur.fetchone() == (2, True, True)

            assert repository.resolve_source_retry(
                source_kind="gdrive",
                source_id="folder",
                source_type="gdrive",
                external_id="opaque-retry-id",
                md5_checksum=str(fingerprint["md5_checksum"]),
                size_bytes=10,
                mime_type="application/test",
                validator_schema_version="ooxml-safe-v2",
                request_id="request-2",
            )

            assert repository.record_source_retry(
                **fingerprint,
                request_id="request-3",
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT attempt_count, status
                    FROM ingest_source_retries
                    WHERE external_id = 'opaque-retry-id'
                    """
                )
                assert cur.fetchone() == (1, "pending")
            assert repository.resolve_source_retry(
                source_kind="gdrive",
                source_id="folder",
                source_type="gdrive",
                external_id="opaque-retry-id",
                md5_checksum=str(fingerprint["md5_checksum"]),
                size_bytes=10,
                mime_type="application/test",
                validator_schema_version="ooxml-safe-v2",
                request_id="request-3",
            )

            changed = {
                **fingerprint,
                "md5_checksum": "abcdef0123456789abcdef0123456789",
                "size_bytes": 11,
            }
            assert repository.record_source_retry(
                **changed,
                request_id="request-4",
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT attempt_count, status, md5_checksum, size_bytes
                    FROM ingest_source_retries
                    WHERE external_id = 'opaque-retry-id'
                    """
                )
                assert cur.fetchone() == (
                    1,
                    "pending",
                    "abcdef0123456789abcdef0123456789",
                    11,
                )
        finally:
            conn.rollback()
