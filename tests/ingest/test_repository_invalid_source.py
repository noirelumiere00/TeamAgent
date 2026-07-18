"""invalid source observability と title-only DB guard の repository 回帰テスト。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from teamagent.ingest.repository import (
    ChunkUpsert,
    DocumentUpsert,
    IngestRepository,
    SourceRetryUnavailableError,
)


class _Cursor:
    def __init__(self, conn: _Connection, row_factory: Any) -> None:
        self._conn = conn
        self._row_factory = row_factory
        self._last_sql = ""
        self.rowcount = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._last_sql = sql
        self._conn.executed.append((sql, params, self._row_factory))
        if self._conn.raise_table and self._conn.raise_table in sql:
            raise RuntimeError("relation unavailable")
        self.rowcount = self._conn.rowcount

    def fetchone(self) -> Any:
        if "FROM ingest_source_health" in self._last_sql:
            return (
                {"reason": self._conn.invalid_reason}
                if self._conn.invalid_reason is not None
                else None
            )
        if "SELECT EXISTS" in self._last_sql:
            return {"has_content": self._conn.has_content}
        if "FROM ingest_source_retries" in self._last_sql and "FOR UPDATE" in self._last_sql:
            return {"id": "00000000-0000-0000-0000-000000000001"}
        if "RETURNING id" in self._last_sql:
            return {"id": "document-uuid"}
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        if "UPDATE ingest_source_retries AS retry" in self._last_sql:
            return list(self._conn.retry_rows)
        if "FROM ingest_reconciliation_gaps" in self._last_sql:
            return list(self._conn.reconciliation_rows)
        return []


class _Connection:
    def __init__(
        self,
        *,
        invalid_reason: str | None = None,
        has_content: bool = False,
        raise_table: str | None = None,
        retry_rows: list[dict[str, Any]] | None = None,
        reconciliation_rows: list[dict[str, Any]] | None = None,
        rowcount: int = 1,
    ) -> None:
        self.invalid_reason = invalid_reason
        self.has_content = has_content
        self.raise_table = raise_table
        self.retry_rows = retry_rows or []
        self.reconciliation_rows = reconciliation_rows or []
        self.rowcount = rowcount
        self.executed: list[tuple[str, Any, Any]] = []

    def cursor(self, *, row_factory: Any = None) -> _Cursor:
        return _Cursor(self, row_factory)


class _ConnectionContext:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn

    def __enter__(self) -> _Connection:
        return self._conn

    def __exit__(self, *exc: object) -> bool:
        return False


class _PgVector:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn
        self.connection_calls: list[dict[str, Any]] = []

    def connection(self, **kwargs: Any) -> _ConnectionContext:
        self.connection_calls.append(kwargs)
        return _ConnectionContext(self.conn)


def _repo(conn: _Connection) -> tuple[IngestRepository, _PgVector]:
    pgvector = _PgVector(conn)
    repo = IngestRepository(pgvector, owner_email="bot@example.jp")  # type: ignore[arg-type]
    return repo, pgvector


def _doc() -> DocumentUpsert:
    return DocumentUpsert(
        source_type="gdrive",
        external_id="drive-file-secret",
        source_uri="gdrive://drive-file-secret",
        title="title",
        owner_email="bot@example.jp",
    )


def _title_chunk() -> ChunkUpsert:
    return ChunkUpsert(
        chunk_idx=0,
        content="title",
        embedding=[0.1] * 4,
        metadata={"title_only": True},
    )


def test_find_invalid_source_uses_exact_fingerprint_and_dict_row() -> None:
    conn = _Connection(invalid_reason="corrupt_zip")
    repo, pgvector = _repo(conn)
    md5 = "0123456789abcdef0123456789abcdef"

    assert (
        repo.find_invalid_source_reason(
            "gdrive",
            "FILE1",
            md5,
            52_427_482,
            "application/test",
            "ooxml-safe-v2",
        )
        == "corrupt_zip"
    )
    sql, params, row_factory = conn.executed[0]
    assert "status = 'invalid_source'" in sql
    assert params == (
        "gdrive",
        "FILE1",
        md5,
        52_427_482,
        "application/test",
        "ooxml-safe-v2",
    )
    assert row_factory is not None
    assert pgvector.connection_calls[0]["user_role"] == "admin"


def test_source_health_old_schema_fails_open_without_logging_full_id() -> None:
    conn = _Connection(raise_table="ingest_source_health")
    repo, _ = _repo(conn)
    external_id = "never-log-this-drive-id"
    md5 = "0123456789abcdef0123456789abcdef"

    with capture_logs() as logs:
        assert (
            repo.find_invalid_source_reason(
                "gdrive",
                external_id,
                md5,
                10,
                "application/test",
                "ooxml-safe-v2",
            )
            is None
        )
        assert (
            repo.record_invalid_source(
                "gdrive",
                external_id,
                md5_checksum=md5,
                size_bytes=10,
                reason="corrupt_zip",
                mime_type="application/test",
                validator_schema_version="ooxml-safe-v2",
                request_id="req",
            )
            is False
        )
    assert external_id not in str(logs)
    assert len(conn.executed) == 1  # 同一processでは旧schemaへの反復SQLを抑止


def test_record_invalid_source_upserts_observation_count() -> None:
    conn = _Connection()
    repo, _ = _repo(conn)
    md5 = "0123456789abcdef0123456789abcdef"

    assert (
        repo.record_invalid_source(
            "gdrive",
            "FILE1",
            md5_checksum=md5,
            size_bytes=123,
            reason="corrupt_zip",
            mime_type="application/test",
            validator_schema_version="ooxml-safe-v2",
            request_id="req",
        )
        is True
    )
    sql, params, _ = conn.executed[0]
    assert "INSERT INTO ingest_source_health" in sql
    assert "observation_count = ingest_source_health.observation_count + 1" in sql
    assert params[:7] == (
        "gdrive",
        "FILE1",
        md5,
        123,
        "corrupt_zip",
        "application/test",
        "ooxml-safe-v2",
    )


def test_source_health_unavailable_cache_reprobes_after_backoff() -> None:
    conn = _Connection(raise_table="ingest_source_health", invalid_reason="corrupt_zip")
    repo, _ = _repo(conn)
    md5 = "0123456789abcdef0123456789abcdef"
    args = ("gdrive", "FILE1", md5, 123, "application/test", "ooxml-safe-v2")

    assert repo.find_invalid_source_reason(*args) is None
    assert len(conn.executed) == 1
    conn.raise_table = None
    assert repo.find_invalid_source_reason(*args) is None
    assert len(conn.executed) == 1

    repo._source_health_retry_after = 0.0
    assert repo.find_invalid_source_reason(*args) == "corrupt_zip"
    assert len(conn.executed) == 2


def test_claim_due_retries_uses_skip_locked_and_maps_dict_rows() -> None:
    conn = _Connection(
        retry_rows=[
            {
                "external_id": "FILE1",
                "md5_checksum": "0123456789abcdef0123456789abcdef",
                "size_bytes": 123,
                "mime_type": "application/test",
                "validator_schema_version": "ooxml-safe-v2",
                "attempt_count": 2,
                "reason": "office_download_failed",
                "lease_owner": "req-2",
                "lease_token": "opaque-claim-token",
            }
        ]
    )
    repo, pgvector = _repo(conn)

    retries = repo.claim_due_source_retries(
        source_kind="gdrive",
        source_id="FOLDER1",
        request_id="req-2",
    )

    assert len(retries) == 1
    assert retries[0].external_id == "FILE1"
    assert retries[0].attempt_count == 2
    assert retries[0].lease_owner == "req-2"
    assert retries[0].lease_token == "opaque-claim-token"
    sql, params, row_factory = conn.executed[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "lease_token = gen_random_uuid()::text" in sql
    assert "lease_expires_at" in sql
    assert params[:4] == ("gdrive", "FOLDER1", 1000, "req-2")
    assert row_factory is not None
    assert pgvector.connection_calls[0]["user_role"] == "admin"


def test_claim_failure_is_not_indistinguishable_from_an_empty_retry_queue() -> None:
    conn = _Connection(raise_table="ingest_source_retries")
    repo, _ = _repo(conn)

    with pytest.raises(SourceRetryUnavailableError):
        repo.claim_due_source_retries(
            source_kind="gdrive",
            source_id="FOLDER1",
            request_id="req",
        )
    with pytest.raises(SourceRetryUnavailableError):
        repo.claim_due_source_retries(
            source_kind="gdrive",
            source_id="FOLDER1",
            request_id="req-2",
        )

    assert len(conn.executed) == 1


def test_record_retry_is_request_idempotent_and_has_exponential_backoff() -> None:
    conn = _Connection()
    repo, _ = _repo(conn)
    md5 = "0123456789abcdef0123456789abcdef"

    assert repo.record_source_retry(
        source_kind="gdrive",
        source_id="FOLDER1",
        source_type="gdrive",
        external_id="FILE1",
        md5_checksum=md5,
        size_bytes=123,
        mime_type="application/test",
        validator_schema_version="ooxml-safe-v2",
        reason="office_download_failed",
        request_id="req",
        allow_unclaimed=True,
    )

    sql, params, _ = conn.executed[0]
    assert "last_request_id = EXCLUDED.last_request_id" in sql
    assert "ingest_source_retries.status = 'resolved'" in sql
    assert "IS NOT DISTINCT FROM EXCLUDED.md5_checksum" in sql
    assert "power(" in sql
    assert "lease_owner = NULL" in sql
    assert "lease_token = NULL" in sql
    assert "ingest_source_retries.lease_owner IS NULL" in sql
    assert "ingest_source_retries.lease_token IS NULL" in sql
    assert "ingest_source_retries.lease_expires_at IS NULL" in sql
    assert params[:10] == (
        "gdrive",
        "FOLDER1",
        "gdrive",
        "FILE1",
        md5,
        123,
        "application/test",
        "ooxml-safe-v2",
        "office_download_failed",
        "req",
    )
    assert params[11] is None


def test_record_retry_from_claim_requires_exact_active_lease_owner() -> None:
    conn = _Connection(rowcount=1)
    repo, _ = _repo(conn)

    assert repo.record_source_retry(
        source_kind="gdrive",
        source_id="FOLDER1",
        source_type="gdrive",
        external_id="FILE1",
        md5_checksum="0123456789abcdef0123456789abcdef",
        size_bytes=123,
        mime_type="application/test",
        validator_schema_version="ooxml-safe-v2",
        reason="office_download_failed",
        request_id="worker-a",
        expected_lease_owner="claim-a",
        expected_lease_token="token-a",
    )

    sql, params, _ = conn.executed[0]
    assert "claimed.lease_owner = %s" in sql
    assert "claimed.lease_token = %s" in sql
    assert "claimed.lease_expires_at > clock_timestamp()" in sql
    assert "ingest_source_retries.status = 'pending'" in sql
    assert "ingest_source_retries.lease_owner = %s" in sql
    assert "ingest_source_retries.lease_token = %s" in sql
    assert "ingest_source_retries.lease_expires_at > clock_timestamp()" in sql
    assert params[11] == "claim-a"
    assert params[12] == "token-a"
    assert params[-4:] == ("claim-a", "token-a", "claim-a", "token-a")


def test_record_retry_rejects_implicit_or_conflicting_unclaimed_path() -> None:
    conn = _Connection(rowcount=1)
    repo, _ = _repo(conn)
    kwargs = {
        "source_kind": "gdrive",
        "source_id": "FOLDER1",
        "source_type": "gdrive",
        "external_id": "FILE1",
        "md5_checksum": "0123456789abcdef0123456789abcdef",
        "size_bytes": 123,
        "mime_type": "application/test",
        "validator_schema_version": "ooxml-safe-v2",
        "reason": "office_download_failed",
        "request_id": "worker-a",
    }

    assert not repo.record_source_retry(**kwargs)
    assert not repo.record_source_retry(
        **kwargs,
        expected_lease_owner="claim-a",
    )
    assert not repo.record_source_retry(
        **kwargs,
        expected_lease_token="token-a",
    )
    assert not repo.record_source_retry(
        **kwargs,
        expected_lease_owner="claim-a",
        expected_lease_token="token-a",
        allow_unclaimed=True,
    )
    assert conn.executed == []


def test_record_retry_returns_false_when_fence_rejects_write() -> None:
    conn = _Connection(rowcount=0)
    repo, _ = _repo(conn)

    assert not repo.record_source_retry(
        source_kind="gdrive",
        source_id="FOLDER1",
        source_type="gdrive",
        external_id="FILE1",
        md5_checksum="0123456789abcdef0123456789abcdef",
        size_bytes=123,
        mime_type="application/test",
        validator_schema_version="ooxml-safe-v2",
        reason="office_download_failed",
        request_id="worker-a",
        expected_lease_owner="claim-a",
        expected_lease_token="token-a",
    )


def test_retry_lease_renewal_requires_current_owner_and_pending_status() -> None:
    conn = _Connection(rowcount=1)
    repo, _ = _repo(conn)

    assert repo.renew_source_retry_lease(
        source_kind="gdrive",
        source_id="FOLDER1",
        source_type="gdrive",
        external_id="FILE1",
        request_id="trace-request",
        expected_lease_owner="lease-owner",
        expected_lease_token="lease-token",
        lease_seconds=321,
    )

    sql, params, _ = conn.executed[0]
    assert "lease_expires_at = now()" in sql
    assert "status = 'pending'" in sql
    assert "lease_owner = %s" in sql
    assert "lease_token = %s" in sql
    assert "lease_expires_at > clock_timestamp()" in sql
    assert params == (
        321,
        "gdrive",
        "FOLDER1",
        "gdrive",
        "FILE1",
        "lease-owner",
        "lease-token",
    )


def test_resolve_claimed_retry_requires_exact_active_owner_without_delete() -> None:
    conn = _Connection(rowcount=1)
    repo, _ = _repo(conn)
    md5 = "0123456789abcdef0123456789abcdef"

    assert repo.resolve_source_retry(
        source_kind="gdrive",
        source_id="FOLDER1",
        source_type="gdrive",
        external_id="FILE1",
        md5_checksum=md5,
        size_bytes=123,
        mime_type="application/test",
        validator_schema_version="ooxml-safe-v2",
        request_id="req",
        expected_lease_owner="claim-owner",
        expected_lease_token="claim-token",
    )
    sql, params, _ = conn.executed[0]
    assert "UPDATE ingest_source_retries" in sql
    assert "status = 'resolved'" in sql
    assert "%s::text IS NOT NULL" in sql
    assert "lease_owner = %s" in sql
    assert "lease_token = %s" in sql
    assert "lease_expires_at > clock_timestamp()" in sql
    assert params[5:9] == (
        "claim-owner",
        "claim-token",
        "claim-owner",
        "claim-token",
    )
    assert "DELETE" not in sql


def test_resolve_unclaimed_retry_requires_empty_lease_and_exact_fingerprint() -> None:
    conn = _Connection(rowcount=1)
    repo, _ = _repo(conn)

    assert repo.resolve_source_retry(
        source_kind="gdrive",
        source_id="FOLDER1",
        source_type="gdrive",
        external_id="FILE1",
        md5_checksum="0123456789abcdef0123456789abcdef",
        size_bytes=123,
        mime_type="application/test",
        validator_schema_version="ooxml-safe-v2",
        request_id="unclaimed-observer",
        allow_unclaimed=True,
    )

    sql, params, _ = conn.executed[0]
    assert "%s::text IS NULL" in sql
    assert "lease_owner IS NULL" in sql
    assert "lease_token IS NULL" in sql
    assert "lease_expires_at IS NULL" in sql
    assert "md5_checksum IS NOT DISTINCT FROM %s" in sql
    assert params[5:11] == (None, None, None, None, None, None)


def test_resolve_retry_rejects_implicit_or_conflicting_unclaimed_path() -> None:
    conn = _Connection(rowcount=1)
    repo, _ = _repo(conn)
    kwargs = {
        "source_kind": "gdrive",
        "source_id": "FOLDER1",
        "source_type": "gdrive",
        "external_id": "FILE1",
        "md5_checksum": "0123456789abcdef0123456789abcdef",
        "size_bytes": 123,
        "mime_type": "application/test",
        "validator_schema_version": "ooxml-safe-v2",
        "request_id": "worker-a",
    }

    assert not repo.resolve_source_retry(**kwargs)
    assert not repo.resolve_source_retry(
        **kwargs,
        expected_lease_owner="claim-a",
    )
    assert not repo.resolve_source_retry(
        **kwargs,
        expected_lease_token="token-a",
    )
    assert not repo.resolve_source_retry(
        **kwargs,
        expected_lease_owner="claim-a",
        expected_lease_token="token-a",
        allow_unclaimed=True,
    )
    assert conn.executed == []


def test_reconciliation_returns_counts_only_and_resolves_by_sha256_ref() -> None:
    conn = _Connection(
        reconciliation_rows=[
            {"gap_kind": "unindexed_pdf", "gap_count": 3},
            {"gap_kind": "source_original_missing", "gap_count": 9},
        ],
        rowcount=1,
    )
    repo, _ = _repo(conn)

    assert repo.unresolved_reconciliation_counts("gdrive") == {
        "unindexed_pdf": 3,
        "source_original_missing": 9,
    }
    assert (
        repo.resolve_reconciliation_gaps(
            source_kind="gdrive",
            external_id="SENSITIVE-DRIVE-ID",
            request_id="req",
        )
        == 1
    )
    count_sql, _, _ = conn.executed[0]
    resolve_sql, resolve_params, _ = conn.executed[1]
    assert "external_id" not in count_sql
    assert "source_ref_hashes" in resolve_sql
    assert "SENSITIVE-DRIVE-ID" not in resolve_params
    assert resolve_params[-1] == hashlib.sha256(b"SENSITIVE-DRIVE-ID").hexdigest()


def test_reconciliation_unavailable_is_warning_and_reprobes() -> None:
    conn = _Connection(raise_table="ingest_reconciliation_gaps")
    repo, _ = _repo(conn)

    assert repo.unresolved_reconciliation_counts("gdrive") == {"reconciliation_unavailable": 1}
    assert repo.unresolved_reconciliation_counts("gdrive") == {"reconciliation_unavailable": 1}
    assert len(conn.executed) == 1

    conn.raise_table = None
    conn.reconciliation_rows = [{"gap_kind": "unindexed_pdf", "gap_count": 3}]
    repo._reconciliation_retry_after = 0.0
    assert repo.unresolved_reconciliation_counts("gdrive") == {"unindexed_pdf": 3}


def test_record_connector_run_supports_success_with_warnings_and_old_schema() -> None:
    conn = _Connection()
    repo, _ = _repo(conn)
    assert repo.record_connector_run(
        request_id="req",
        source_kind="gdrive",
        source_id="FOLDER1",
        outcome="success_with_warnings",
        documents_upserted=3,
        chunks_inserted=8,
        warning_reasons={"corrupt_zip": 2},
        suppressed_retry_count=1,
    )
    sql, params, _ = conn.executed[0]
    assert "INSERT INTO ingest_connector_runs" in sql
    assert params[3] == "success_with_warnings"
    assert params[6] == 2
    assert params[8] == 1

    old_conn = _Connection(raise_table="ingest_connector_runs")
    old_repo, _ = _repo(old_conn)
    assert (
        old_repo.record_connector_run(
            request_id="req",
            source_kind="gdrive",
            source_id="FOLDER1",
            outcome="success",
            documents_upserted=0,
            chunks_inserted=0,
        )
        is False
    )


def test_title_only_guard_skips_without_document_or_chunk_mutation() -> None:
    conn = _Connection(has_content=True)
    repo, pgvector = _repo(conn)

    assert repo.upsert_title_only_if_no_content(_doc(), [_title_chunk()], "req") is None
    rendered_sql = "\n".join(sql for sql, _, _ in conn.executed)
    assert "pg_advisory_xact_lock" in rendered_sql
    assert "SELECT EXISTS" in rendered_sql
    assert "INSERT INTO documents" not in rendered_sql
    assert "DELETE FROM chunks" not in rendered_sql
    assert pgvector.connection_calls[0]["user_role"] == "admin"


def test_title_only_guard_checks_and_upserts_in_one_locked_transaction() -> None:
    conn = _Connection(has_content=False)
    repo, _ = _repo(conn)

    assert repo.upsert_title_only_if_no_content(_doc(), [_title_chunk()], "req") == "document-uuid"
    statements = [sql for sql, _, _ in conn.executed]
    lock_idx = next(i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql)
    check_idx = next(i for i, sql in enumerate(statements) if "SELECT EXISTS" in sql)
    upsert_idx = next(i for i, sql in enumerate(statements) if "INSERT INTO documents" in sql)
    delete_idx = next(i for i, sql in enumerate(statements) if "DELETE FROM chunks" in sql)
    insert_idx = next(i for i, sql in enumerate(statements) if "INSERT INTO chunks" in sql)
    assert lock_idx < check_idx < upsert_idx < delete_idx < insert_idx


def test_normal_content_upsert_takes_same_source_lock_before_replace() -> None:
    conn = _Connection()
    repo, _ = _repo(conn)
    chunk = ChunkUpsert(chunk_idx=0, content="body", embedding=[0.1] * 4)

    repo.upsert_document_with_chunks(_doc(), [chunk], "req")
    statements = [sql for sql, _, _ in conn.executed]
    assert "pg_advisory_xact_lock" in statements[0]
    assert "INSERT INTO documents" in statements[1]


def test_claimed_retry_upsert_locks_exact_token_and_resolves_in_same_connection() -> None:
    conn = _Connection(rowcount=1)
    repo, pgvector = _repo(conn)
    chunk = ChunkUpsert(chunk_idx=0, content="body", embedding=[0.1] * 4)

    assert (
        repo.upsert_document_with_chunks_and_resolve_retry(
            _doc(),
            [chunk],
            "trace-request",
            source_kind="gdrive",
            source_id="FOLDER1",
            expected_lease_owner="worker-a",
            expected_lease_token="opaque-token-a",
        )
        == "document-uuid"
    )

    statements = [sql for sql, _, _ in conn.executed]
    retry_lock_idx = next(
        i
        for i, sql in enumerate(statements)
        if "FROM ingest_source_retries" in sql and "FOR UPDATE" in sql
    )
    document_lock_idx = next(
        i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql
    )
    upsert_idx = next(i for i, sql in enumerate(statements) if "INSERT INTO documents" in sql)
    resolve_idx = next(
        i
        for i, sql in enumerate(statements)
        if "UPDATE ingest_source_retries" in sql and "status = 'resolved'" in sql
    )
    assert retry_lock_idx < document_lock_idx < upsert_idx < resolve_idx
    lock_params = conn.executed[retry_lock_idx][1]
    assert lock_params[-2:] == ("worker-a", "opaque-token-a")
    assert "lease_expires_at > clock_timestamp()" in statements[retry_lock_idx]
    assert "lease_token = %s" in statements[resolve_idx]
    assert pgvector.connection_calls == [
        {
            "app_role": "teamagent_app",
            "user_email": "bot@example.jp",
            "user_role": "admin",
            "application_name": "teamagent-ingest",
        }
    ]


def test_migration_checksums_and_lease_token_upgrade_are_pinned() -> None:
    root = Path(__file__).resolve().parents[2]
    sql_0019 = (root / "infra/migrations/0019_ingest_source_health.sql").read_text(encoding="utf-8")
    sql_0020 = (root / "infra/migrations/0020_ingest_source_retry_upgrade.sql").read_text(
        encoding="utf-8"
    )
    sql_0021 = (root / "infra/migrations/0021_ingest_source_retry_lease_token.sql").read_text(
        encoding="utf-8"
    )
    assert hashlib.sha256(sql_0019.encode()).hexdigest() == (
        "fcbd206703afe955f17c8c7b951e3bb0fc0be698e4a179e19e065c7a144a2afd"
    )
    assert hashlib.sha256(sql_0020.encode()).hexdigest() == (
        "c186984a554147b62c8caf4b519dbd7cfcd5d0e90d8dd75ca9d766e29c98e623"
    )
    assert hashlib.sha256(sql_0021.encode()).hexdigest() == (
        "6f28d1eedbb6e3f4c6e3cd229fbc736b57da9f9d06f600dc52336d00d2c8acba"
    )
    assert "CREATE TABLE IF NOT EXISTS ingest_source_health" in sql_0019
    assert "CREATE TABLE IF NOT EXISTS ingest_connector_runs" in sql_0019
    assert "CREATE TABLE IF NOT EXISTS ingest_source_retries" not in sql_0019
    assert "validator_schema_version" not in sql_0019
    assert "CREATE TABLE IF NOT EXISTS ingest_source_retries" in sql_0020
    assert "CREATE TABLE IF NOT EXISTS ingest_reconciliation_gaps" in sql_0020
    assert "REVOKE DELETE" in sql_0020
    assert "validator_schema_version" in sql_0020
    assert sql_0020.count("'audit-20260717-unindexed-pdf-") == 3
    assert sql_0020.count("'audit-20260717-source-original-missing-") == 9
    assert len(re.findall(r"'[0-9a-f]{64}'", sql_0020)) == 19
    assert "ADD COLUMN IF NOT EXISTS lease_token TEXT" in sql_0021
    assert "ingest_source_retries_lease_fields_consistent" in sql_0021
    assert "status = 'pending'" in sql_0021


def test_upgrade_migration_preserves_legacy_rows_and_forces_revalidation() -> None:
    root = Path(__file__).resolve().parents[2]
    sql = (root / "infra/migrations/0020_ingest_source_retry_upgrade.sql").read_text(
        encoding="utf-8"
    )
    assert "ooxml-legacy-v1" in sql
    assert "DROP CONSTRAINT IF EXISTS ingest_source_health_payload_unique" in sql
    assert "ingest_source_health_fingerprint_unique" in sql
    assert "REVOKE DELETE" in sql
    assert "ON CONFLICT (gap_key) DO NOTHING" in sql
    assert sql.count("'audit-20260717-unindexed-pdf-") == 3
    assert sql.count("'audit-20260717-source-original-missing-") == 9


def test_runbook_defines_warning_exit_monitoring_and_admin_only_cleanup() -> None:
    root = Path(__file__).resolve().parents[2]
    runbook = (root / "docs/runbooks/ingest_invalid_office.md").read_text(encoding="utf-8")

    assert "`2`: completed with warnings" in runbook
    assert "Pending retries and unresolved reconciliation" in runbook
    assert "completed operational" in runbook
    assert "older than 90 days" in runbook
    assert "older than 180 days" in runbook
    assert "SET LOCAL app.user_role = 'admin'" in runbook
    assert "ROLLBACK" in runbook
