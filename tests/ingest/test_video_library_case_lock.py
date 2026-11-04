"""The run lock must not keep a transaction open while Haiku is called (RDS idle-in-transaction)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from teamagent.ingest.video_library_case_repository import CaseRepository


def _repo(conn: MagicMock) -> CaseRepository:
    repo = CaseRepository(MagicMock())

    @contextmanager
    def connection() -> Any:
        yield conn

    repo.connection = connection  # type: ignore[method-assign]
    return repo


def _conn(acquired: bool) -> MagicMock:
    conn = MagicMock(closed=False)
    conn.execute.return_value.fetchone.return_value = {"acquired": acquired}
    return conn


def test_lock_commits_before_work_and_unlocks_after() -> None:
    conn = _conn(True)
    with _repo(conn).run_lock() as acquired:
        assert acquired
        calls = [c[0] for c in conn.method_calls]
        assert calls == ["execute", "commit"]  # the transaction is already closed during the work
        assert "pg_try_advisory_lock" in conn.execute.call_args_list[0].args[0]
    assert "pg_advisory_unlock" in conn.execute.call_args_list[-1].args[0]
    assert conn.commit.call_count == 2


def test_not_acquired_does_not_unlock_and_unlock_failure_is_swallowed() -> None:
    conn = _conn(False)
    with _repo(conn).run_lock() as acquired:
        assert not acquired
    assert all("unlock" not in c.args[0] for c in conn.execute.call_args_list)

    conn = _conn(True)
    with _repo(conn).run_lock():
        conn.execute.side_effect = psycopg.OperationalError("server closed")
    # The session's end releases the lock; a dead connection must not fail the whole run.


def test_work_errors_still_propagate() -> None:
    conn = _conn(True)
    with pytest.raises(RuntimeError), _repo(conn).run_lock():
        raise RuntimeError("boom")
    assert "pg_advisory_unlock" in conn.execute.call_args_list[-1].args[0]
