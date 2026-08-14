"""0019/0020/0021 migration の実 PostgreSQL 回帰テスト。

``TEAMAGENT_TEST_DB_DSN`` が明示された disposable PostgreSQL だけで実行する。
各テストは専用 schema を使う。並行性テストは worker ごとに独立した接続/transactionを
commitし、最後に schema を CASCADE cleanup する。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from teamagent.ingest.repository import (
    ChunkUpsert,
    DocumentUpsert,
    IngestRepository,
    SourceRetryLeaseLostError,
)

_DB_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_0019 = _ROOT / "infra/migrations/0019_ingest_source_health.sql"
_MIGRATION_0020 = _ROOT / "infra/migrations/0020_ingest_source_retry_upgrade.sql"
_MIGRATION_0021 = _ROOT / "infra/migrations/0021_ingest_source_retry_lease_token.sql"

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


def _set_retry_test_session(conn: Any, schema: str) -> None:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        cur.execute("SELECT set_config('app.user_role', 'admin', true)")
        cur.execute("SET LOCAL statement_timeout = '5s'")


@contextmanager
def _committed_retry_schema() -> Iterator[str]:
    import psycopg
    from psycopg import sql

    assert _DB_DSN is not None
    schema = f"ingest_source_concurrent_{uuid.uuid4().hex}"
    with psycopg.connect(_DB_DSN, autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'teamagent_app'")
            if cur.fetchone() is None:
                pytest.skip("teamagent_app role is required")
            cur.execute("SELECT pg_has_role(current_user, 'teamagent_app', 'MEMBER')")
            if not bool(cur.fetchone()[0]):
                pytest.skip("test role cannot SET ROLE teamagent_app")
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cur.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO teamagent_app").format(sql.Identifier(schema))
            )
    try:
        with psycopg.connect(_DB_DSN) as conn:
            _set_retry_test_session(conn, schema)
            with conn.cursor() as cur:
                cur.execute(_MIGRATION_0019.read_text(encoding="utf-8"))
                cur.execute(_MIGRATION_0020.read_text(encoding="utf-8"))
                cur.execute(_MIGRATION_0021.read_text(encoding="utf-8"))
                cur.execute(
                    """
                    CREATE DOMAIN vector AS double precision[];
                    CREATE TYPE document_source_type AS ENUM ('gdrive');
                    CREATE TABLE documents (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        source_type document_source_type NOT NULL,
                        external_id TEXT NOT NULL,
                        source_uri TEXT,
                        title TEXT,
                        owner_email TEXT NOT NULL,
                        acl_emails TEXT[] NOT NULL DEFAULT '{}',
                        acl_groups TEXT[] NOT NULL DEFAULT '{}',
                        client_code TEXT,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        modified_at TIMESTAMPTZ,
                        UNIQUE (source_type, external_id)
                    );
                    CREATE TABLE chunks (
                        id BIGSERIAL PRIMARY KEY,
                        document_id UUID NOT NULL REFERENCES documents(id),
                        chunk_idx INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        contextualized TEXT,
                        embedding vector,
                        page_num INTEGER,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb
                    );
                    """
                )
        yield schema
    finally:
        with psycopg.connect(_DB_DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


class _CommittedPgVector:
    """Repository operation ごとに独立接続を開き、成功時だけ commit する test vector。"""

    def __init__(self, dsn: str, schema: str) -> None:
        self._dsn = dsn
        self._schema = schema
        self.transactions = 0

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[Any]:
        import psycopg

        with psycopg.connect(self._dsn) as conn:
            _set_retry_test_session(conn, self._schema)
            self.transactions += 1
            yield conn


def _execute_committed(schema: str, statement: str, params: tuple[Any, ...] = ()) -> None:
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        _set_retry_test_session(conn, schema)
        conn.execute(statement, params)


def _retry_state(schema: str, external_id: str) -> tuple[Any, ...]:
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        _set_retry_test_session(conn, schema)
        row = conn.execute(
            """
            SELECT status, lease_owner, lease_token, lease_expires_at > now(),
                   resolved_at IS NOT NULL, last_request_id
            FROM ingest_source_retries
            WHERE external_id = %s
            """,
            (external_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def test_fresh_migrations_enforce_rls_and_revoke_destructive_privileges() -> None:
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        schema = _prepare_schema(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(_MIGRATION_0019.read_text(encoding="utf-8"))
                # Fresh/upgrade DB both run 0020; a retry after uncertain migration status is safe.
                cur.execute(_MIGRATION_0020.read_text(encoding="utf-8"))
                cur.execute(_MIGRATION_0020.read_text(encoding="utf-8"))
                cur.execute(_MIGRATION_0021.read_text(encoding="utf-8"))
                cur.execute(_MIGRATION_0021.read_text(encoding="utf-8"))

                for table in (
                    "ingest_source_health",
                    "ingest_source_retries",
                    "ingest_reconciliation_gaps",
                    "ingest_connector_runs",
                ):
                    qualified = f"{schema}.{table}"
                    cur.execute(
                        """
                        SELECT relrowsecurity, relforcerowsecurity
                        FROM pg_class
                        WHERE oid = %s::regclass
                        """,
                        (qualified,),
                    )
                    assert cur.fetchone() == (True, True)
                    for privilege in ("SELECT", "INSERT", "UPDATE"):
                        cur.execute(
                            "SELECT has_table_privilege('teamagent_app', %s, %s)",
                            (qualified, privilege),
                        )
                        assert cur.fetchone()[0] is True
                    for privilege in (
                        "DELETE",
                        "TRUNCATE",
                        "REFERENCES",
                        "TRIGGER",
                    ):
                        cur.execute(
                            "SELECT has_table_privilege('teamagent_app', %s, %s)",
                            (qualified, privilege),
                        )
                        assert cur.fetchone()[0] is False
                    for privilege in (
                        "SELECT",
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "TRUNCATE",
                        "REFERENCES",
                        "TRIGGER",
                    ):
                        cur.execute(
                            "SELECT has_table_privilege('teamagent_app', %s, %s)",
                            (qualified, f"{privilege} WITH GRANT OPTION"),
                        )
                        assert cur.fetchone()[0] is False

                    cur.execute(
                        """
                        SELECT polname,
                               polpermissive,
                               polcmd,
                               polroles,
                               pg_get_expr(polqual, polrelid),
                               pg_get_expr(polwithcheck, polrelid)
                        FROM pg_policy
                        WHERE polrelid = %s::regclass
                        """,
                        (qualified,),
                    )
                    assert cur.fetchall() == [
                        (
                            f"{table}_admin",
                            True,
                            "*",
                            [0],
                            ("(current_setting('app.user_role'::text, true) = 'admin'::text)"),
                            ("(current_setting('app.user_role'::text, true) = 'admin'::text)"),
                        )
                    ]

                cur.execute(
                    """
                    SELECT privilege_type, is_grantable
                    FROM information_schema.role_table_grants
                    WHERE grantee = 'teamagent_app'
                      AND table_schema = %s
                      AND table_name = 'ingest_source_retries'
                    ORDER BY privilege_type
                    """,
                    (schema,),
                )
                assert cur.fetchall() == [
                    ("INSERT", "NO"),
                    ("SELECT", "NO"),
                    ("UPDATE", "NO"),
                ]
                cur.execute(
                    """
                    SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = 'teamagent_app'
                    """
                )
                assert cur.fetchone() == (False, False, False, False)
                cur.execute("SELECT pg_has_role(current_user, 'teamagent_app', 'MEMBER')")
                assert cur.fetchone() == (True,)
                cur.execute(
                    """
                    SELECT conname, convalidated
                    FROM pg_constraint
                    WHERE conrelid = %s::regclass
                    ORDER BY conname
                    """,
                    (f"{schema}.ingest_source_retries",),
                )
                retry_constraints = dict(cur.fetchall())
                assert retry_constraints["ingest_source_retries_lease_token_nonempty"] is True
                assert retry_constraints["ingest_source_retries_lease_fields_consistent"] is True

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
                cur.execute(
                    """
                    INSERT INTO ingest_connector_runs
                        (request_id, source_kind, source_id, outcome)
                    VALUES ('rls-probe', 'gdrive', 'rls-probe', 'success')
                    """
                )
                cur.execute(
                    """
                    INSERT INTO ingest_source_retries
                        (source_kind, source_id, source_type, external_id,
                         mime_type, validator_schema_version, reason)
                    VALUES
                        ('gdrive', 'rls-probe', 'gdrive', 'rls-probe',
                         'application/test', 'ooxml-safe-v2', 'rls_probe')
                    """
                )
                cur.execute(
                    """
                    INSERT INTO ingest_reconciliation_gaps
                        (gap_key, source_kind, gap_kind, source_ref_hashes)
                    VALUES
                        ('rls-probe', 'gdrive', 'unindexed_pdf', ARRAY['rls-probe'])
                    """
                )
                cur.execute("SET LOCAL app.user_role = ''")
                denied_inserts = (
                    """
                    INSERT INTO ingest_source_health
                        (source_type, external_id, md5_checksum, size_bytes,
                         mime_type, validator_schema_version, reason)
                    VALUES
                        ('gdrive', 'rls-denied',
                         'abcdef0123456789abcdef0123456789', 1,
                         'application/test', 'ooxml-safe-v2', 'rls_probe')
                    """,
                    """
                    INSERT INTO ingest_connector_runs
                        (request_id, source_kind, source_id, outcome)
                    VALUES ('rls-denied', 'gdrive', 'rls-denied', 'success')
                    """,
                    """
                    INSERT INTO ingest_source_retries
                        (source_kind, source_id, source_type, external_id,
                         mime_type, validator_schema_version, reason)
                    VALUES
                        ('gdrive', 'rls-denied', 'gdrive', 'rls-denied',
                         'application/test', 'ooxml-safe-v2', 'rls_probe')
                    """,
                    """
                    INSERT INTO ingest_reconciliation_gaps
                        (gap_key, source_kind, gap_kind, source_ref_hashes)
                    VALUES
                        ('rls-denied', 'gdrive', 'unindexed_pdf', ARRAY['rls-denied'])
                    """,
                )
                for denied_insert in denied_inserts:
                    cur.execute("SAVEPOINT rls_denied")
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        cur.execute(denied_insert)
                    cur.execute("ROLLBACK TO SAVEPOINT rls_denied")
                    cur.execute("RELEASE SAVEPOINT rls_denied")
                cur.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM ingest_source_health
                         WHERE external_id = 'opaque-test-id'),
                        (SELECT count(*) FROM ingest_connector_runs
                         WHERE request_id = 'rls-probe'),
                        (SELECT count(*) FROM ingest_source_retries
                         WHERE external_id = 'rls-probe'),
                        (SELECT count(*) FROM ingest_reconciliation_gaps
                         WHERE gap_key = 'rls-probe')
                    """
                )
                assert cur.fetchone() == (0, 0, 0, 0)
                cur.execute("SET LOCAL app.user_role = 'admin'")
                cur.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM ingest_source_health
                         WHERE external_id = 'opaque-test-id'),
                        (SELECT count(*) FROM ingest_connector_runs
                         WHERE request_id = 'rls-probe'),
                        (SELECT count(*) FROM ingest_source_retries
                         WHERE external_id = 'rls-probe'),
                        (SELECT count(*) FROM ingest_reconciliation_gaps
                         WHERE gap_key = 'rls-probe')
                    """
                )
                assert cur.fetchone() == (1, 1, 1, 1)
                cur.execute("RESET ROLE")

                cur.execute(
                    """
                        SELECT gap_kind, count(*)
                        FROM ingest_reconciliation_gaps
                        WHERE status = 'unresolved'
                          AND gap_key <> 'rls-probe'
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
                    INSERT INTO ingest_source_retries
                        (source_kind, source_id, source_type, external_id,
                         mime_type, validator_schema_version, reason,
                         lease_owner, lease_expires_at)
                    VALUES
                        ('gdrive', 'legacy-folder', 'gdrive', 'legacy-retry',
                         'application/test', 'ooxml-safe-v2', 'legacy_retry',
                         'legacy-owner', now() + interval '5 minutes')
                    """
                )
                cur.execute(_MIGRATION_0021.read_text(encoding="utf-8"))
                cur.execute(
                    """
                    SELECT lease_owner, lease_token, lease_expires_at
                    FROM ingest_source_retries
                    WHERE external_id = 'legacy-retry'
                    """
                )
                assert cur.fetchone() == (None, None, None)
                cur.execute(
                    """
                    UPDATE ingest_source_retries
                    SET lease_owner = 'tokenized-owner',
                        lease_token = 'tokenized-lease',
                        lease_expires_at = now() + interval '5 minutes'
                    WHERE external_id = 'legacy-retry'
                    """
                )
                cur.execute(_MIGRATION_0021.read_text(encoding="utf-8"))
                cur.execute(
                    """
                    SELECT lease_owner, lease_token, lease_expires_at > now()
                    FROM ingest_source_retries
                    WHERE external_id = 'legacy-retry'
                    """
                )
                assert cur.fetchone() == ("tokenized-owner", "tokenized-lease", True)
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
                cur.execute(_MIGRATION_0020.read_text(encoding="utf-8"))
                cur.execute(_MIGRATION_0021.read_text(encoding="utf-8"))
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
                allow_unclaimed=True,
            )
            assert repository.record_source_retry(
                **fingerprint,
                request_id="request-1",
                allow_unclaimed=True,
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
            lease_token = claims[0].lease_token
            assert lease_token
            assert repository.renew_source_retry_lease(
                source_kind="gdrive",
                source_id="folder",
                source_type="gdrive",
                external_id="opaque-retry-id",
                request_id="request-2",
                expected_lease_owner="request-2",
                expected_lease_token=lease_token,
                lease_seconds=900,
            )
            assert not repository.renew_source_retry_lease(
                source_kind="gdrive",
                source_id="folder",
                source_type="gdrive",
                external_id="opaque-retry-id",
                request_id="wrong-owner",
                expected_lease_owner="wrong-owner",
                expected_lease_token=lease_token,
                lease_seconds=900,
            )
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
                expected_lease_owner="request-2",
                expected_lease_token=lease_token,
            )
            assert not repository.record_source_retry(
                **fingerprint,
                request_id="request-2",
                expected_lease_owner="request-2",
                expected_lease_token=lease_token,
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
                assert cur.fetchone() == (3, True, True)  # enqueue=1+claim=1+再record=1（claim加算は2026-08-14毒ループ対策）

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
                allow_unclaimed=True,
            )

            assert repository.record_source_retry(
                **fingerprint,
                request_id="request-3",
                allow_unclaimed=True,
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
                allow_unclaimed=True,
            )

            changed = {
                **fingerprint,
                "md5_checksum": "abcdef0123456789abcdef0123456789",
                "size_bytes": 11,
            }
            assert repository.record_source_retry(
                **changed,
                request_id="request-4",
                allow_unclaimed=True,
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


def test_retry_fence_across_committed_workers_blocks_every_stale_write() -> None:
    """A expiry → B takeover/resolve の前後で stale A は一切の状態を変更できない。"""
    assert _DB_DSN is not None
    with _committed_retry_schema() as schema:
        seed_vector = _CommittedPgVector(_DB_DSN, schema)
        worker_a_vector = _CommittedPgVector(_DB_DSN, schema)
        worker_b_vector = _CommittedPgVector(_DB_DSN, schema)
        seed_repository = IngestRepository(seed_vector, app_role=None)  # type: ignore[arg-type]
        worker_a = IngestRepository(worker_a_vector, app_role=None)  # type: ignore[arg-type]
        worker_b = IngestRepository(worker_b_vector, app_role=None)  # type: ignore[arg-type]
        retry = {
            "source_kind": "gdrive",
            "source_id": "folder-fence",
            "source_type": "gdrive",
            "external_id": "opaque-fenced-retry",
            "md5_checksum": "0123456789abcdef0123456789abcdef",
            "size_bytes": 10,
            "mime_type": "application/test",
            "validator_schema_version": "ooxml-safe-v2",
            "reason": "office_download_failed",
        }
        resolve = {key: value for key, value in retry.items() if key != "reason"}

        assert seed_repository.record_source_retry(
            **retry,
            request_id="seed",
            allow_unclaimed=True,
        )
        _execute_committed(
            schema,
            """
            UPDATE ingest_source_retries
            SET next_attempt_at = now()
            WHERE external_id = %s
            """,
            (retry["external_id"],),
        )
        claims_a = worker_a.claim_due_source_retries(
            source_kind="gdrive",
            source_id="folder-fence",
            request_id="shared-owner",
            lease_seconds=60,
        )
        assert [claim.lease_owner for claim in claims_a] == ["shared-owner"]
        token_a = claims_a[0].lease_token
        assert token_a

        _execute_committed(
            schema,
            """
            UPDATE ingest_source_retries
            SET lease_expires_at = now() - interval '1 second'
            WHERE external_id = %s
            """,
            (retry["external_id"],),
        )

        # Expired A cannot resolve, renew, or record even before B takes over.
        assert not worker_a.resolve_source_retry(
            **resolve,
            request_id="worker-a-expired",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
        )
        assert not worker_a.renew_source_retry_lease(
            source_kind="gdrive",
            source_id="folder-fence",
            source_type="gdrive",
            external_id=str(retry["external_id"]),
            request_id="worker-a-expired",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
            lease_seconds=60,
        )
        assert not worker_a.record_source_retry(
            **retry,
            request_id="worker-a-expired",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
        )
        assert _retry_state(schema, str(retry["external_id"])) == (
            "pending",
            "shared-owner",
            token_a,
            False,
            False,
            "seed",
        )

        claims_b = worker_b.claim_due_source_retries(
            source_kind="gdrive",
            source_id="folder-fence",
            request_id="shared-owner",
            lease_seconds=60,
        )
        assert [claim.lease_owner for claim in claims_b] == ["shared-owner"]
        token_b = claims_b[0].lease_token
        assert token_b
        assert token_b != token_a

        # B's active lease cannot be resolved, renewed, or cleared by stale A.
        assert not worker_a.resolve_source_retry(
            **resolve,
            request_id="worker-a-stale",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
        )
        assert not worker_a.renew_source_retry_lease(
            source_kind="gdrive",
            source_id="folder-fence",
            source_type="gdrive",
            external_id=str(retry["external_id"]),
            request_id="worker-a-stale",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
            lease_seconds=60,
        )
        assert not worker_a.record_source_retry(
            **retry,
            request_id="worker-a-stale",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
        )
        assert _retry_state(schema, str(retry["external_id"])) == (
            "pending",
            "shared-owner",
            token_b,
            True,
            False,
            "seed",
        )

        assert worker_b.renew_source_retry_lease(
            source_kind="gdrive",
            source_id="folder-fence",
            source_type="gdrive",
            external_id=str(retry["external_id"]),
            request_id="worker-b-heartbeat",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_b,
            lease_seconds=60,
        )
        assert worker_b.resolve_source_retry(
            **resolve,
            request_id="worker-b-resolved",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_b,
        )
        resolved_state = (
            "resolved",
            None,
            None,
            None,
            True,
            "worker-b-resolved",
        )
        assert _retry_state(schema, str(retry["external_id"])) == resolved_state

        # B resolve後も stale A cannot reopen/close the row or alter last_request_id.
        assert not worker_a.resolve_source_retry(
            **resolve,
            request_id="worker-a-after-resolve",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
        )
        assert not worker_a.renew_source_retry_lease(
            source_kind="gdrive",
            source_id="folder-fence",
            source_type="gdrive",
            external_id=str(retry["external_id"]),
            request_id="worker-a-after-resolve",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
            lease_seconds=60,
        )
        assert not worker_a.record_source_retry(
            **retry,
            request_id="worker-a-after-resolve",
            expected_lease_owner="shared-owner",
            expected_lease_token=token_a,
        )
        assert _retry_state(schema, str(retry["external_id"])) == resolved_state

        # A current owner can still record an ordinary retry and release its own lease.
        same_owner_retry = {
            **retry,
            "external_id": "ordinary-same-owner-retry",
        }
        assert seed_repository.record_source_retry(
            **same_owner_retry,
            request_id="same-owner-seed",
            allow_unclaimed=True,
        )
        _execute_committed(
            schema,
            """
            UPDATE ingest_source_retries
            SET next_attempt_at = now()
            WHERE external_id = %s
            """,
            (same_owner_retry["external_id"],),
        )
        same_owner_claims = worker_a.claim_due_source_retries(
            source_kind="gdrive",
            source_id="folder-fence",
            request_id="current-owner",
            lease_seconds=60,
        )
        assert same_owner_claims
        same_owner_token = same_owner_claims[0].lease_token
        assert same_owner_token
        assert worker_a.record_source_retry(
            **same_owner_retry,
            request_id="current-owner",
            expected_lease_owner="current-owner",
            expected_lease_token=same_owner_token,
        )
        assert _retry_state(schema, str(same_owner_retry["external_id"])) == (
            "pending",
            None,
            None,
            None,
            False,
            "current-owner",
        )

        assert worker_a_vector.transactions >= 10
        assert worker_b_vector.transactions >= 3


def test_stale_worker_cannot_modify_documents_or_chunks_after_takeover_commit() -> None:
    """Bのtoken付き成功commit後、旧tokenのA transactionはdocument/chunkともwrite 0。"""
    import psycopg

    assert _DB_DSN is not None
    with _committed_retry_schema() as schema:
        seed_repository = IngestRepository(  # type: ignore[arg-type]
            _CommittedPgVector(_DB_DSN, schema),
            app_role=None,
        )
        worker_a = IngestRepository(  # type: ignore[arg-type]
            _CommittedPgVector(_DB_DSN, schema),
            app_role=None,
        )
        worker_b = IngestRepository(  # type: ignore[arg-type]
            _CommittedPgVector(_DB_DSN, schema),
            app_role=None,
        )
        retry = {
            "source_kind": "gdrive",
            "source_id": "folder-atomic",
            "source_type": "gdrive",
            "external_id": "atomic-document",
            "md5_checksum": "0123456789abcdef0123456789abcdef",
            "size_bytes": 10,
            "mime_type": "application/test",
            "validator_schema_version": "ooxml-safe-v2",
            "reason": "office_download_failed",
        }
        assert seed_repository.record_source_retry(
            **retry,
            request_id="seed",
            allow_unclaimed=True,
        )
        _execute_committed(
            schema,
            """
            UPDATE ingest_source_retries
            SET next_attempt_at = now()
            WHERE external_id = %s
            """,
            (retry["external_id"],),
        )

        claim_a = worker_a.claim_due_source_retries(
            source_kind="gdrive",
            source_id="folder-atomic",
            request_id="shared-owner",
            lease_seconds=60,
        )[0]
        assert claim_a.lease_token
        _execute_committed(
            schema,
            """
            UPDATE ingest_source_retries
            SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE external_id = %s
            """,
            (retry["external_id"],),
        )
        claim_b = worker_b.claim_due_source_retries(
            source_kind="gdrive",
            source_id="folder-atomic",
            request_id="shared-owner",
            lease_seconds=60,
        )[0]
        assert claim_b.lease_token
        assert claim_b.lease_token != claim_a.lease_token

        doc_b = DocumentUpsert(
            source_type="gdrive",
            external_id="atomic-document",
            source_uri="gdrive://atomic-document",
            title="worker-b-title",
            owner_email="bot@example.jp",
            metadata={"writer": "worker-b"},
        )
        chunks_b = [
            ChunkUpsert(
                chunk_idx=0,
                content="worker-b-content",
                embedding=[0.2, 0.3],
                metadata={"writer": "worker-b"},
            )
        ]
        assert (
            worker_b.upsert_document_with_chunks_and_resolve_retry(
                doc_b,
                chunks_b,
                "worker-b-success",
                source_kind="gdrive",
                source_id="folder-atomic",
                expected_lease_owner="shared-owner",
                expected_lease_token=claim_b.lease_token,
            )
            is not None
        )

        doc_a = DocumentUpsert(
            source_type="gdrive",
            external_id="atomic-document",
            source_uri="gdrive://atomic-document",
            title="stale-worker-a-title",
            owner_email="bot@example.jp",
            metadata={"writer": "stale-worker-a"},
        )
        chunks_a = [
            ChunkUpsert(
                chunk_idx=0,
                content="stale-worker-a-content",
                embedding=[0.8, 0.9],
                metadata={"writer": "stale-worker-a"},
            )
        ]
        with pytest.raises(SourceRetryLeaseLostError):
            worker_a.upsert_document_with_chunks_and_resolve_retry(
                doc_a,
                chunks_a,
                "worker-a-stale-success",
                source_kind="gdrive",
                source_id="folder-atomic",
                expected_lease_owner="shared-owner",
                expected_lease_token=claim_a.lease_token,
            )

        with psycopg.connect(_DB_DSN) as conn:
            _set_retry_test_session(conn, schema)
            document_row = conn.execute(
                """
                SELECT title, metadata->>'writer'
                FROM documents
                WHERE source_type = 'gdrive' AND external_id = 'atomic-document'
                """
            ).fetchone()
            chunk_rows = conn.execute(
                """
                SELECT c.content, c.metadata->>'writer'
                FROM chunks AS c
                JOIN documents AS d ON d.id = c.document_id
                WHERE d.source_type = 'gdrive' AND d.external_id = 'atomic-document'
                ORDER BY c.chunk_idx
                """
            ).fetchall()
        assert document_row == ("worker-b-title", "worker-b")
        assert chunk_rows == [("worker-b-content", "worker-b")]
        assert _retry_state(schema, "atomic-document") == (
            "resolved",
            None,
            None,
            None,
            True,
            "worker-b-success",
        )


def test_retry_claim_uses_skip_locked_across_overlapping_transactions() -> None:
    import psycopg

    assert _DB_DSN is not None
    with _committed_retry_schema() as schema:
        seed_repository = IngestRepository(  # type: ignore[arg-type]
            _CommittedPgVector(_DB_DSN, schema),
            app_role=None,
        )
        worker_a = IngestRepository(  # type: ignore[arg-type]
            _CommittedPgVector(_DB_DSN, schema),
            app_role=None,
        )
        worker_b = IngestRepository(  # type: ignore[arg-type]
            _CommittedPgVector(_DB_DSN, schema),
            app_role=None,
        )
        base_retry = {
            "source_kind": "gdrive",
            "source_id": "folder-skip-locked",
            "source_type": "gdrive",
            "md5_checksum": "0123456789abcdef0123456789abcdef",
            "size_bytes": 10,
            "mime_type": "application/test",
            "validator_schema_version": "ooxml-safe-v2",
            "reason": "office_download_failed",
        }
        for external_id in ("locked-row", "free-row"):
            assert seed_repository.record_source_retry(
                **base_retry,
                external_id=external_id,
                request_id=f"seed-{external_id}",
                allow_unclaimed=True,
            )
        _execute_committed(
            schema,
            """
            UPDATE ingest_source_retries
            SET next_attempt_at = now()
            WHERE source_id = 'folder-skip-locked'
            """,
        )

        # Keep the first row locked while B's separate committed transaction claims.
        with psycopg.connect(_DB_DSN) as lock_conn:
            _set_retry_test_session(lock_conn, schema)
            locked = lock_conn.execute(
                """
                SELECT external_id
                FROM ingest_source_retries
                WHERE external_id = 'locked-row'
                FOR UPDATE
                """
            ).fetchone()
            assert locked == ("locked-row",)

            claims_b = worker_b.claim_due_source_retries(
                source_kind="gdrive",
                source_id="folder-skip-locked",
                request_id="worker-b",
                limit=2,
                lease_seconds=60,
            )
            assert [claim.external_id for claim in claims_b] == ["free-row"]

        claims_a = worker_a.claim_due_source_retries(
            source_kind="gdrive",
            source_id="folder-skip-locked",
            request_id="worker-a",
            limit=2,
            lease_seconds=60,
        )
        assert [claim.external_id for claim in claims_a] == ["locked-row"]
        assert _retry_state(schema, "free-row")[1] == "worker-b"
        assert _retry_state(schema, "locked-row")[1] == "worker-a"
