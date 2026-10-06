"""朝ダイジェストの本人設定ストア（digest_preferences）— 本番の失敗モードを再現するフェイクで検査。

フェイクが再現すること（migration 0030 の実体どおり）:
  - RLS: ``app.user_email`` と違う行は見えない・書けない
  - 新規 INSERT ... ON CONFLICT DO NOTHING は既に行があれば rowcount **0**
  - UPDATE ... WHERE version = expected は版が違えば rowcount **0**
  - 表が無い・接続断は **例外**
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.digest_preferences_store import DigestPreferencesStore

ME = "komata@vectorinc.co.jp"


class _Cursor:
    def __init__(self, db: _Pg, guc: str) -> None:
        self.db, self.guc = db, guc
        self.rowcount = 0
        self._row: Any = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        if self.db.fail:
            raise RuntimeError('relation "digest_preferences" does not exist')
        verb = sql.strip().split()[0]
        self.db.statements.append(verb)
        email = params["email"]
        visible = email == self.guc  # RLS（USING / WITH CHECK）
        if verb == "SELECT":
            row = self.db.rows.get(email) if visible else None
            self._row = None if row is None else {"prefs": row[0], "version": row[1]}
        elif verb == "INSERT":
            if not visible:
                raise RuntimeError("new row violates row-level security policy")
            if email in self.db.rows:
                self.rowcount = 0
            else:
                self.db.rows[email] = (json.loads(params["prefs"]), 1)
                self.rowcount = 1
        elif verb == "UPDATE":
            row = self.db.rows.get(email) if visible else None
            if row is None or row[1] != params["expected"]:
                self.rowcount = 0
            else:
                self.db.rows[email] = (json.loads(params["prefs"]), row[1] + 1)
                self.rowcount = 1
        elif verb == "DELETE":
            self.rowcount = 1 if visible and self.db.rows.pop(email, None) else 0

    def fetchone(self) -> Any:
        return self._row


class _Conn:
    def __init__(self, db: _Pg, guc: str) -> None:
        self.db, self.guc = db, guc

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.db, self.guc)

    def commit(self) -> None:
        self.db.commits += 1


class _Pg:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: dict[str, tuple[dict[str, Any], int]] = {}
        self.statements: list[str] = []
        self.commits = 0
        self.fail = fail
        self.roles: list[tuple[str | None, str | None]] = []

    def connection(self, *, app_role: str | None = None, user_email: str | None = None) -> _Conn:
        self.roles.append((app_role, user_email))
        return _Conn(self, user_email or "")


def test_missing_row_reads_as_none_version_zero() -> None:
    assert DigestPreferencesStore(_Pg()).get(ME, request_id="r") == (None, 0)


def test_create_then_update_bumps_the_version() -> None:
    pg = _Pg()
    store = DigestPreferencesStore(pg)
    assert store.save(ME, {"delivery": False}, expected_version=0, request_id="r") == 1
    assert store.get(ME, request_id="r") == ({"delivery": False}, 1)
    assert store.save(ME, {"auto_drafts": False}, expected_version=1, request_id="r") == 2
    assert store.get(ME, request_id="r") == ({"auto_drafts": False}, 2)


def test_stale_version_is_a_conflict_not_a_silent_overwrite() -> None:
    """同時に 2 か所から変えたとき、後から来た古い版を黙って勝たせない。"""
    pg = _Pg()
    store = DigestPreferencesStore(pg)
    store.save(ME, {"delivery": False}, expected_version=0, request_id="r")
    assert store.save(ME, {"delivery": True}, expected_version=0, request_id="r") is None
    store.save(ME, {"auto_drafts": False}, expected_version=1, request_id="r")
    assert store.save(ME, {"reminders": False}, expected_version=1, request_id="r") is None
    assert pg.rows[ME] == ({"auto_drafts": False}, 2)


def test_every_call_binds_rls_to_the_owner_and_normalises_email() -> None:
    pg = _Pg()
    store = DigestPreferencesStore(pg)
    store.save(" Komata@VectorInc.co.jp ", {"delivery": False}, expected_version=0, request_id="r")
    store.get(ME, request_id="r")
    store.delete(ME, request_id="r")
    assert set(pg.roles) == {("teamagent_app", ME)}


def test_delete_resets_and_is_idempotent() -> None:
    pg = _Pg()
    store = DigestPreferencesStore(pg)
    store.save(ME, {"delivery": False}, expected_version=0, request_id="r")
    assert store.delete(ME, request_id="r") is True
    assert store.get(ME, request_id="r") == (None, 0)
    assert store.delete(ME, request_id="r") is True


def test_db_failures_are_raised_not_swallowed() -> None:
    """読めない/書けないを「設定なし」「書けた」に潰さない（倒し方は呼び出し側が決める）。"""
    store = DigestPreferencesStore(_Pg(fail=True))
    with pytest.raises(RuntimeError):
        store.get(ME, request_id="r")
    with pytest.raises(RuntimeError):
        store.save(ME, {}, expected_version=0, request_id="r")
    with pytest.raises(RuntimeError):
        store.delete(ME, request_id="r")


def test_invalid_arguments_never_touch_the_db() -> None:
    pg = _Pg()
    store = DigestPreferencesStore(pg)
    with pytest.raises(ValueError):
        store.get("", request_id="r")
    with pytest.raises(ValueError):
        store.save("no-at-mark", {}, expected_version=0, request_id="r")
    with pytest.raises(ValueError):
        store.save(ME, {}, expected_version=-1, request_id="r")
    assert pg.statements == []


def test_row_from_a_driver_returning_text_jsonb_is_parsed() -> None:
    class _TextPg(_Pg):
        pass

    pg = _TextPg()
    pg.rows[ME] = ({"delivery": False}, 3)
    store = DigestPreferencesStore(pg)
    # 行を文字列で返すドライバ設定を模す
    orig = _Cursor.fetchone

    def _as_text(self: _Cursor) -> Any:
        row = orig(self)
        return None if row is None else (json.dumps(row["prefs"]), row["version"])

    _Cursor.fetchone = _as_text  # type: ignore[method-assign]
    try:
        assert store.get(ME, request_id="r") == ({"delivery": False}, 3)
    finally:
        _Cursor.fetchone = orig  # type: ignore[method-assign]


def test_sql_matches_the_migration() -> None:
    """ON CONFLICT の arbiter・列名・RLS・権限が migration 0030 と一致している。"""
    import teamagent.adapters.digest_preferences_store as m

    root = Path(__file__).resolve().parents[2]
    ddl = (root / "infra" / "migrations" / "0030_digest_preferences.sql").read_text(
        encoding="utf-8"
    )
    assert "user_email   TEXT PRIMARY KEY" in ddl
    assert "ON CONFLICT (user_email) DO NOTHING" in m._INSERT_SQL
    assert "version = %(expected)s" in m._UPDATE_SQL
    assert "FORCE ROW LEVEL SECURITY" in ddl
    assert "current_setting('app.user_email', true)" in ddl
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON digest_preferences TO teamagent_app" in ddl
