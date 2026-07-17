"""invalid source observability と title-only DB guard の repository 回帰テスト。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from structlog.testing import capture_logs

from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert, IngestRepository


class _Cursor:
    def __init__(self, conn: _Connection, row_factory: Any) -> None:
        self._conn = conn
        self._row_factory = row_factory
        self._last_sql = ""

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._last_sql = sql
        self._conn.executed.append((sql, params, self._row_factory))
        if self._conn.raise_table and self._conn.raise_table in sql:
            raise RuntimeError("relation unavailable")

    def fetchone(self) -> Any:
        if "FROM ingest_source_health" in self._last_sql:
            return (
                {"reason": self._conn.invalid_reason}
                if self._conn.invalid_reason is not None
                else None
            )
        if "SELECT EXISTS" in self._last_sql:
            return {"has_content": self._conn.has_content}
        if "RETURNING id" in self._last_sql:
            return {"id": "document-uuid"}
        return None


class _Connection:
    def __init__(
        self,
        *,
        invalid_reason: str | None = None,
        has_content: bool = False,
        raise_table: str | None = None,
    ) -> None:
        self.invalid_reason = invalid_reason
        self.has_content = has_content
        self.raise_table = raise_table
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

    assert repo.find_invalid_source_reason("gdrive", "FILE1", md5, 52_427_482) == "corrupt_zip"
    sql, params, row_factory = conn.executed[0]
    assert "status = 'invalid_source'" in sql
    assert params == ("gdrive", "FILE1", md5, 52_427_482)
    assert row_factory is not None
    assert pgvector.connection_calls[0]["user_role"] == "admin"


def test_source_health_old_schema_fails_open_without_logging_full_id() -> None:
    conn = _Connection(raise_table="ingest_source_health")
    repo, _ = _repo(conn)
    external_id = "never-log-this-drive-id"
    md5 = "0123456789abcdef0123456789abcdef"

    with capture_logs() as logs:
        assert repo.find_invalid_source_reason("gdrive", external_id, md5, 10) is None
        assert (
            repo.record_invalid_source(
                "gdrive",
                external_id,
                md5_checksum=md5,
                size_bytes=10,
                reason="corrupt_zip",
                mime_type="application/test",
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
            request_id="req",
        )
        is True
    )
    sql, params, _ = conn.executed[0]
    assert "INSERT INTO ingest_source_health" in sql
    assert "observation_count = ingest_source_health.observation_count + 1" in sql
    assert params[:6] == ("gdrive", "FILE1", md5, 123, "corrupt_zip", "application/test")


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


def test_migration_is_additive_and_has_explicit_rollback() -> None:
    root = Path(__file__).resolve().parents[2]
    sql = (root / "infra/migrations/0019_ingest_source_health.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS ingest_source_health" in sql
    assert "CREATE TABLE IF NOT EXISTS ingest_connector_runs" in sql
    assert "success_with_warnings" in sql
    assert "ingest_source_health FORCE ROW LEVEL SECURITY" in sql
    assert "ingest_connector_runs FORCE ROW LEVEL SECURITY" in sql
    assert "app.user_role" in sql
    assert "DROP TABLE IF EXISTS ingest_source_health" in sql
    assert "ALTER TYPE" not in sql
