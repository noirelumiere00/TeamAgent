"""表示済み資料の永続化・本人RLS・期限切れ・入力制限。外部DBは使わない。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.document_reference_store import DocumentReferenceStore

EMAIL = "pilot@example.test"
KEY = "a" * 64
REF = {"title": "事例資料", "drive_file_id": "ORIGINAL", "kind": "drive"}


class _Cursor:
    def __init__(self, db: _Pg, owner: str) -> None:
        self.db, self.owner = db, owner
        self.row: dict[str, Any] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        if self.db.fail:
            raise RuntimeError("reference storage unavailable")
        self.db.statements.append(sql)
        key = (params["email"], params["key"])
        visible = self.owner == params["email"]
        if sql.strip().startswith("SELECT"):
            value = self.db.rows.get(key) if visible else None
            if value and value[1] > self.db.now:
                self.row = {"refs": json.loads(json.dumps(value[0]))}
        elif sql.strip().startswith("DELETE"):
            for old_key, value in list(self.db.rows.items()):
                if old_key[0] == self.owner and value[1] <= self.db.now:
                    del self.db.rows[old_key]
        elif sql.strip().startswith("INSERT"):
            if not visible:
                raise PermissionError("row level security")
            self.db.rows[key] = (json.loads(params["refs"]), self.db.now + 600)

    def fetchone(self) -> dict[str, Any] | None:
        return self.row


class _Conn:
    def __init__(self, db: _Pg, owner: str) -> None:
        self.db, self.owner = db, owner

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def cursor(self) -> _Cursor:
        return _Cursor(self.db, self.owner)


class _Pg:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[list[dict[str, Any]], int]] = {}
        self.statements: list[str] = []
        self.roles: list[tuple[str, str]] = []
        self.now = 0
        self.fail = False
        self.forced_owner: str | None = None

    def connection(self, *, app_role: str, user_email: str) -> _Conn:
        self.roles.append((app_role, user_email))
        return _Conn(self, self.forced_owner or user_email)


def test_independent_workers_read_the_same_owner_selection() -> None:
    pg = _Pg()
    DocumentReferenceStore(pg).save(EMAIL, KEY, [REF])
    assert DocumentReferenceStore(pg).get(EMAIL, KEY) == [REF]
    assert set(pg.roles) == {("teamagent_app", EMAIL)}


def test_expired_selection_is_not_restored_and_next_write_prunes_only_its_owner() -> None:
    pg = _Pg()
    store = DocumentReferenceStore(pg)
    store.save(EMAIL, KEY, [REF])
    store.save("other@example.test", KEY, [REF])
    pg.now = 600
    assert store.get(EMAIL, KEY) == []
    store.save(EMAIL, "b" * 64, [])
    assert (EMAIL, KEY) not in pg.rows
    assert ("other@example.test", KEY) in pg.rows
    assert any("expires_at > NOW()" in sql for sql in pg.statements)


def test_owner_and_conversation_are_separate_even_when_the_key_is_known() -> None:
    pg = _Pg()
    store = DocumentReferenceStore(pg)
    store.save(EMAIL, KEY, [REF])
    assert store.get("other@example.test", KEY) == []
    assert store.get(EMAIL, "b" * 64) == []
    pg.forced_owner = "other@example.test"
    assert store.get(EMAIL, KEY) == []
    with pytest.raises(PermissionError):
        store.save(EMAIL, KEY, [REF])


@pytest.mark.parametrize("refs", [[REF] * 6, ["bad"], {"bad": 1}, [{"title": "x" * 70_000}]])
def test_invalid_reference_payload_never_reaches_the_database(refs: Any) -> None:
    pg = _Pg()
    with pytest.raises(ValueError):
        DocumentReferenceStore(pg).save(EMAIL, KEY, refs)
    assert pg.roles == []


@pytest.mark.parametrize("email,key", [("", KEY), (EMAIL, "bad"), (EMAIL, "a" * 65)])
def test_invalid_owner_or_key_never_reaches_the_database(email: str, key: str) -> None:
    pg = _Pg()
    with pytest.raises(ValueError):
        DocumentReferenceStore(pg).get(email, key)
    assert pg.roles == []


def test_unavailable_storage_is_not_reported_as_success_or_an_empty_selection() -> None:
    pg = _Pg()
    pg.fail = True
    with pytest.raises(RuntimeError):
        DocumentReferenceStore(pg).get(EMAIL, KEY)
    with pytest.raises(RuntimeError):
        DocumentReferenceStore(pg).save(EMAIL, KEY, [REF])


def test_migration_forces_owner_rls_and_bounds_only_the_reference_json() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "infra/migrations/0034_document_followup_refs.sql"
    )
    sql = migration.read_text()
    assert "ENABLE ROW LEVEL SECURITY" in sql and "FORCE ROW LEVEL SECURITY" in sql
    assert "USING (user_email = current_setting('app.user_email', true))" in sql
    assert "WITH CHECK (user_email = current_setting('app.user_email', true))" in sql
    assert "jsonb_array_length(refs) <= 5" in sql
    assert "REVOKE ALL ON document_followup_refs FROM PUBLIC" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON document_followup_refs TO teamagent_app" in sql
