"""IngestRepository の ACL-only snapshot / transaction SQL 契約を検証する。"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.ingest.repository import (
    GDriveAclOptimisticLockError,
    GDriveAclSnapshot,
    GDriveAclUpdate,
    IngestRepository,
)


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []
        self.rowcount = 1

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))
        if "SELECT" in sql and self._conn.fetchall_results:
            self._rows = self._conn.fetchall_results.pop(0)
        if "UPDATE documents" in sql and self._conn.update_rowcounts:
            self.rowcount = self._conn.update_rowcounts.pop(0)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(
        self,
        fetchall_results: list[list[dict[str, Any]]] | None = None,
        update_rowcounts: list[int] | None = None,
    ) -> None:
        self.fetchall_results = list(fetchall_results or [])
        self.update_rowcounts = list(update_rowcounts or [])
        self.executed: list[tuple[str, Any]] = []

    def cursor(self, *, row_factory: Any = None) -> _FakeCursor:
        return _FakeCursor(self)


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def __enter__(self) -> _FakeConn:
        return self.conn

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakePgVector:
    def __init__(
        self,
        fetchall_results: list[list[dict[str, Any]]] | None = None,
        update_rowcounts: list[int] | None = None,
    ) -> None:
        self.conn = _FakeConn(fetchall_results, update_rowcounts)
        self.connection_calls: list[dict[str, Any]] = []

    def connection(self, **kwargs: Any) -> _FakeConnCtx:
        self.connection_calls.append(kwargs)
        return _FakeConnCtx(self.conn)


def _repo(pg: _FakePgVector) -> IngestRepository:
    return IngestRepository(pg, owner_email="bot@x.jp")  # type: ignore[arg-type]


def _snapshot_row(*, document_id: str = "00000000-0000-0000-0000-000000000001") -> dict[str, Any]:
    return {
        "document_id": document_id,
        "external_id": "drive-file-1",
        "owner_email": "owner@x.jp",
        "acl_emails": ["reader@x.jp"],
        "acl_groups": ["x.jp"],
        "row_version": "1234",
    }


def _update(*, document_id: str = "00000000-0000-0000-0000-000000000001") -> GDriveAclUpdate:
    return GDriveAclUpdate(
        document_id=document_id,
        external_id="drive-file-1",
        expected_row_version="1234",
        owner_email="new-owner@x.jp",
        acl_emails=("new-owner@x.jp", "reader@x.jp"),
        acl_groups=("x.jp",),
    )


def test_snapshot_selects_only_nonstale_gdrive_acl_fields() -> None:
    pg = _FakePgVector(fetchall_results=[[_snapshot_row()]])
    rows = _repo(pg).list_nonstale_gdrive_acl_snapshot()

    assert rows == [
        GDriveAclSnapshot(
            document_id="00000000-0000-0000-0000-000000000001",
            external_id="drive-file-1",
            owner_email="owner@x.jp",
            acl_emails=("reader@x.jp",),
            acl_groups=("x.jp",),
            row_version="1234",
        )
    ]
    sql, params = pg.conn.executed[0]
    assert params is None
    assert "source_type = 'gdrive'" in sql
    assert "metadata->>'stale'" in sql
    assert "xmin::text AS row_version" in sql
    for forbidden_projection in ("title", "source_uri", "modified_at", "ingested_at", "chunks"):
        assert forbidden_projection not in sql


def test_acl_update_locks_all_rows_then_sets_only_three_acl_columns() -> None:
    lock_row = {
        "document_id": "00000000-0000-0000-0000-000000000001",
        "external_id": "drive-file-1",
        "row_version": "1234",
    }
    pg = _FakePgVector(fetchall_results=[[lock_row]])
    updated = _repo(pg).update_gdrive_acls([_update()])

    assert updated == 1
    assert len(pg.connection_calls) == 1  # lock と全 UPDATE が同じ transaction
    lock_sql, lock_params = pg.conn.executed[0]
    assert "FOR UPDATE" in lock_sql
    assert "xmin::text AS row_version" in lock_sql
    assert lock_params == (["00000000-0000-0000-0000-000000000001"],)

    update_sql, params = pg.conn.executed[1]
    set_clause = update_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "owner_email = %s" in set_clause
    assert "acl_emails = %s" in set_clause
    assert "acl_groups = %s" in set_clause
    for forbidden_column in (
        "content",
        "metadata",
        "stale",
        "modified_at",
        "ingested_at",
        "title",
        "source_uri",
    ):
        assert forbidden_column not in set_clause
    assert params == (
        "new-owner@x.jp",
        ["new-owner@x.jp", "reader@x.jp"],
        ["x.jp"],
        "00000000-0000-0000-0000-000000000001",
        "drive-file-1",
    )


def test_acl_update_optimistic_conflict_happens_before_first_write() -> None:
    changed_lock_row = {
        "document_id": "00000000-0000-0000-0000-000000000001",
        "external_id": "drive-file-1",
        "row_version": "9999",
    }
    pg = _FakePgVector(fetchall_results=[[changed_lock_row]])

    with pytest.raises(GDriveAclOptimisticLockError, match="no rows updated"):
        _repo(pg).update_gdrive_acls([_update()])

    assert len(pg.conn.executed) == 1
    assert "FOR UPDATE" in pg.conn.executed[0][0]
    assert all("UPDATE documents" not in sql for sql, _params in pg.conn.executed)


def test_acl_update_rejects_duplicate_ids_before_connection() -> None:
    pg = _FakePgVector()
    with pytest.raises(ValueError, match="duplicate document_id"):
        _repo(pg).update_gdrive_acls([_update(), _update()])
    assert pg.connection_calls == []


def test_acl_update_rowcount_failure_raises_for_transaction_rollback() -> None:
    lock_row = {
        "document_id": "00000000-0000-0000-0000-000000000001",
        "external_id": "drive-file-1",
        "row_version": "1234",
    }
    pg = _FakePgVector(fetchall_results=[[lock_row]], update_rowcounts=[0])
    with pytest.raises(GDriveAclOptimisticLockError, match="transaction rolled back"):
        _repo(pg).update_gdrive_acls([_update()])
