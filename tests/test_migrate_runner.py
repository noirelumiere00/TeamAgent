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
