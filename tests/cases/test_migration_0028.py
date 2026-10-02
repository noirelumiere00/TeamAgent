"""migration 0028（case_records）の静的検証（実 DB 不要）。

store.py が読む/書く列と DDL の列が食い違わないこと、次元が chunks と同じ 1024 であること、
冪等（IF NOT EXISTS）であることを固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

from teamagent.cases.store import EMBEDDING_DIM, RECORD_COLUMNS

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "infra" / "migrations" / "0028_case_records.sql"


def test_migration_exists_and_is_next_number() -> None:
    assert _MIGRATION.is_file()
    versions = sorted(
        p.name[:4] for p in (_ROOT / "infra" / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")
    )
    assert versions[-1] == "0028"
    assert len(versions) == len(set(versions))  # 番号衝突なし


def test_migration_declares_every_store_column() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS case_records" in sql
    for column in (*RECORD_COLUMNS, "embedding", "embedding_backend", "schema_version"):
        assert re.search(rf"^\s+{column}\s", sql, re.MULTILINE), column
    assert "case_id           TEXT PRIMARY KEY" in sql


def test_embedding_dimension_matches_chunks() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert f"vector({EMBEDDING_DIM})" in sql
    chunks_sql = (_ROOT / "infra" / "migrations" / "0001_unified_documents.sql").read_text(
        encoding="utf-8"
    )
    assert f"vector({EMBEDDING_DIM})" in chunks_sql
    assert "hnsw (embedding vector_cosine_ops)" in sql


def test_migration_is_idempotent_and_granted() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    for stmt in re.findall(r"^\s*CREATE (?:TABLE|INDEX)\b[^\n]*", sql, re.MULTILINE):
        assert "IF NOT EXISTS" in stmt, stmt
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON case_records TO teamagent_app" in sql
    assert "external_use IN ('ok', 'ng', 'unknown')" in sql
    assert "USING gin (purpose)" in sql and "USING gin (traits)" in sql
