"""IngestRepository の connector_state / ingest_jobs メソッドの SQL 組立を検証する。

実 DB は使わず、PgVectorClient.connection を fake に差し替えて execute された SQL/params を
キャプチャする（pgvector_client.connection は context manager + conn.cursor も context manager）。
"""

from __future__ import annotations

from typing import Any

from teamagent.ingest.repository import ConnectorState, IngestRepository


class _FakeCursor:
    def __init__(self, fetch_result: Any | None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._fetch = fetch_result

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any | None:
        return self._fetch


class _FakeConn:
    def __init__(self, fetch_result: Any | None) -> None:
        self.cursor_obj = _FakeCursor(fetch_result)

    def cursor(self, *, row_factory: Any = None) -> _FakeCursor:
        return self.cursor_obj


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeConn:
        return self._conn

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakePgVector:
    def __init__(self, fetch_result: Any | None = None) -> None:
        self.conn = _FakeConn(fetch_result)
        self.connection_calls: list[dict[str, Any]] = []

    def connection(self, **kwargs: Any) -> _FakeConnCtx:
        self.connection_calls.append(kwargs)
        return _FakeConnCtx(self.conn)


def _repo(fetch_result: Any | None = None) -> tuple[IngestRepository, _FakePgVector]:
    pg = _FakePgVector(fetch_result)
    repo = IngestRepository(pg, owner_email="bot@x.jp")  # type: ignore[arg-type]
    return repo, pg


# -----------------------------------------------------------
# connector_state
# -----------------------------------------------------------
def test_load_connector_state_returns_none_when_absent() -> None:
    repo, pg = _repo(fetch_result=None)
    assert repo.load_connector_state("gdrive", "F1") is None
    sql, params = pg.conn.cursor_obj.executed[0]
    assert "FROM connector_state" in sql
    assert params == ("gdrive", "F1")
    # ops テーブルは teamagent_app role 経由で接続している
    assert pg.connection_calls[0]["app_role"] == "teamagent_app"


def test_load_connector_state_maps_row() -> None:
    row = {
        "source_kind": "gdrive",
        "source_id": "F1",
        "cursor": "TOK",
        "oldest": None,
        "revision": None,
        "attempt_count": 2,
        "last_error": "boom",
    }
    repo, _pg = _repo(fetch_result=row)
    state = repo.load_connector_state("gdrive", "F1")
    assert state == ConnectorState(
        source_kind="gdrive",
        source_id="F1",
        cursor="TOK",
        oldest=None,
        revision=None,
        attempt_count=2,
        last_error="boom",
    )


def test_save_connector_state_success_resets_attempt() -> None:
    repo, pg = _repo()
    repo.save_connector_state("gdrive", "F1", cursor="NEW", success=True)
    sql, params = pg.conn.cursor_obj.executed[0]
    assert "last_success_at = now()" in sql
    assert "attempt_count = 0" in sql
    assert params[0] == "gdrive"
    assert params[1] == "F1"
    assert params[2] == "NEW"


def test_save_connector_state_failure_increments_attempt() -> None:
    repo, pg = _repo()
    repo.save_connector_state("slack", "C0", success=False, error="boom")
    sql, params = pg.conn.cursor_obj.executed[0]
    assert "attempt_count = connector_state.attempt_count + 1" in sql
    assert params[0] == "slack"
    assert params[1] == "C0"
    assert params[2] == "boom"


# -----------------------------------------------------------
# ingest_jobs
# -----------------------------------------------------------
def test_record_ingest_job_success_marks_committed() -> None:
    repo, pg = _repo()
    repo.record_ingest_job("gdrive", "F1", state="COMMITTED", batch_id="b1")
    sql, params = pg.conn.cursor_obj.executed[0]
    assert "INSERT INTO ingest_jobs" in sql
    assert "committed_at" in sql
    assert params[0] == "gdrive"
    assert params[1] == "F1"
    assert params[3] == "b1"  # batch_id


def test_record_ingest_job_failure_uses_poison_threshold() -> None:
    repo, pg = _repo()
    repo.record_ingest_job("gdrive", "F1", success=False, error="boom", max_attempts=3)
    sql, params = pg.conn.cursor_obj.executed[0]
    assert "FAILED_TRANSIENT" in sql
    assert "POISON" in sql
    assert params[0] == "gdrive"
    assert params[1] == "F1"
    assert params[-1] == 3  # max_attempts threshold
