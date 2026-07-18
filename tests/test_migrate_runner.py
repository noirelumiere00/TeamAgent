"""scripts/migrate.py の純粋関数を検証。実 DB 接続は別途。

検証対象:
  - _list_migrations: ファイル名パターン認識 + version 昇順
  - _sha256: 同一入力で同一ハッシュ（改竄検知の基盤）
  - run() の DSN 未設定時 exit code
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATE_PY = PROJECT_ROOT / "scripts" / "migrate.py"


def _load_migrate_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migrate_runner", MIGRATE_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_list_migrations_discovers_0001() -> None:
    """0001_unified_documents.sql が version='0001' として認識される。"""
    mod = _load_migrate_module()
    migrations = mod._list_migrations()
    versions = [v for v, _ in migrations]
    assert "0001" in versions, f"version 0001 が見つからない: {versions}"


def test_list_migrations_sorted_ascending() -> None:
    """version 昇順で返ること。"""
    mod = _load_migrate_module()
    migrations = mod._list_migrations()
    versions = [v for v, _ in migrations]
    assert versions == sorted(versions), f"昇順でない: {versions}"


def test_migration_versions_are_unique() -> None:
    """version(4桁番号)に重複が無いこと。

    schema_migrations.version は PRIMARY KEY で、同一 version の2ファイル目は
    checksum 不一致 WARN で **無言 skip** される（例: 0016 に chunks と slack_oauth の
    2本 → 後者が本番で永久未適用＝テーブル欠落で機能破損）。採番重複を出荷前に落とす。
    """
    mod = _load_migrate_module()
    versions = [v for v, _ in mod._list_migrations()]
    dupes = sorted({v for v in versions if versions.count(v) > 1})
    assert not dupes, f"migration version が重複しています（要改番）: {dupes}"


def test_sha256_deterministic() -> None:
    """同じ文字列に対して同じハッシュを返す。"""
    mod = _load_migrate_module()
    assert mod._sha256("hello") == mod._sha256("hello")
    assert mod._sha256("hello") != mod._sha256("world")


def test_sha256_hex_length() -> None:
    """SHA-256 は 64 桁の hex を返す。"""
    mod = _load_migrate_module()
    h = mod._sha256("any string")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_run_returns_exit_code_2_when_dsn_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """DATABASE_URL も dsn 引数も無ければ exit code 2。"""
    mod = _load_migrate_module()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = mod.run(dsn=None, dry_run=True)
    assert rc == 2


def test_apply_one_rejects_autocommit_before_executing_sql(tmp_path: Path) -> None:
    mod = _load_migrate_module()
    migration = tmp_path / "9998_autocommit.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")

    class _AutocommitConnection:
        autocommit = True

        def cursor(self):  # type: ignore[no-untyped-def]
            raise AssertionError("autocommit validation must happen before cursor use")

    with pytest.raises(RuntimeError, match="autocommit=False"):
        mod._apply_one(_AutocommitConnection(), "9998", migration)


@pytest.mark.parametrize(
    "transaction_statement",
    (
        "BEGIN;",
        "BEGIN WORK;",
        "BEGIN TRANSACTION;",
        "START TRANSACTION;",
        "COMMIT;",
        "COMMIT WORK;",
        "ROLLBACK;",
        "ROLLBACK WORK;",
    ),
)
def test_apply_one_rejects_embedded_transaction_control(
    tmp_path: Path,
    transaction_statement: str,
) -> None:
    mod = _load_migrate_module()
    migration = tmp_path / "9999_embedded_commit.sql"
    migration.write_text(f"SELECT 1;\n{transaction_statement}\n", encoding="utf-8")

    class _TransactionalConnection:
        autocommit = False

        def cursor(self):  # type: ignore[no-untyped-def]
            raise AssertionError("transaction-control validation must happen before cursor use")

    with pytest.raises(RuntimeError, match="owns the transaction"):
        mod._apply_one(_TransactionalConnection(), "9999", migration)


def test_repository_migrations_do_not_embed_transaction_control() -> None:
    mod = _load_migrate_module()

    offenders = [
        path.name
        for _, path in mod._list_migrations()
        if mod._TRANSACTION_CONTROL_RE.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_dry_run_rolls_back_bootstrap_and_never_executes_migration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mod = _load_migrate_module()
    migration = tmp_path / "9997_dry_run.sql"
    migration.write_text("SELECT 'migration-body';\n", encoding="utf-8")

    class _Cursor:
        def __init__(self, conn):  # type: ignore[no-untyped-def]
            self._conn = conn

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return False

        def execute(self, statement, params=None):  # type: ignore[no-untyped-def]
            self._conn.statements.append(str(statement))

        def fetchall(self):  # type: ignore[no-untyped-def]
            return []

    class _Connection:
        autocommit = False

        def __init__(self) -> None:
            self.statements: list[str] = []
            self.commits = 0
            self.rollbacks = 0

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return False

        def cursor(self):  # type: ignore[no-untyped-def]
            return _Cursor(self)

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

    conn = _Connection()
    monkeypatch.setattr(mod, "_list_migrations", lambda: [("9997", migration)])
    monkeypatch.setattr(mod.psycopg, "connect", lambda *args, **kwargs: conn)

    assert mod.run(dry_run=True, dsn="postgresql://unused") == 0
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert all("migration-body" not in statement for statement in conn.statements)


def test_checksum_drift_is_a_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_migrate_module()
    migration = tmp_path / "9996_changed.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")

    class _Cursor:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return False

        def execute(self, statement, params=None):  # type: ignore[no-untyped-def]
            return None

    class _Connection:
        autocommit = False

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return False

        def cursor(self):  # type: ignore[no-untyped-def]
            return _Cursor()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(mod, "_list_migrations", lambda: [("9996", migration)])
    monkeypatch.setattr(mod, "_applied_versions", lambda conn: {"9996": "0" * 64})
    monkeypatch.setattr(mod.psycopg, "connect", lambda *args, **kwargs: _Connection())

    assert mod.run(dry_run=True, dsn="postgresql://unused") == 1
    assert "[ERROR]" in capsys.readouterr().err
