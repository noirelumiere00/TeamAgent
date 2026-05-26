"""統合 documents + chunks スキーマの spec 検証テスト。

Sprint 3 / PR-1 — `infra/migrations/0001_unified_documents.sql` の適用結果を
contract test として固定する。実 DB が無い CI でも import + 静的検証は通る。

実 DB を使う検証は環境変数 TEAMAGENT_DB_DSN がある時のみ実行（pytest skip マーカー）。
ローカル開発で:
    docker compose up -d
    DATABASE_URL=postgresql://teamagent:teamagent@localhost:5432/teamagent \
        python scripts/migrate.py
    TEAMAGENT_DB_DSN=postgresql://teamagent:teamagent@localhost:5432/teamagent \
        pytest tests/adapters/test_pgvector_schema.py -v
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATION_FILE = PROJECT_ROOT / "infra" / "migrations" / "0001_unified_documents.sql"


# -----------------------------------------------------------
# 静的検証（実 DB 不要 / CI で必ず通る）
# -----------------------------------------------------------
def test_migration_file_exists() -> None:
    assert MIGRATION_FILE.exists(), f"missing migration: {MIGRATION_FILE}"


def test_migration_contains_required_objects() -> None:
    """migration SQL に必須オブジェクト（ENUM / table / policy）が含まれていること。"""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    # ENUM
    assert "document_source_type" in sql
    for v in ("'pdf'", "'gdrive'", "'gmail'", "'slack'", "'other'"):
        assert v in sql, f"ENUM value {v} missing"
    # tables
    assert re.search(r"CREATE TABLE IF NOT EXISTS documents", sql)
    assert re.search(r"CREATE TABLE IF NOT EXISTS chunks", sql)
    # ACL 列
    for col in ("acl_emails", "acl_groups", "owner_email", "external_id", "source_uri"):
        assert col in sql, f"column {col} missing in migration"
    # RLS
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY documents_user_acl" in sql
    assert "CREATE POLICY chunks_via_document" in sql
    # 後方互換
    assert "ALTER TABLE proposals_chunks" in sql


def test_migration_is_idempotent() -> None:
    """`IF NOT EXISTS` / `IF EXISTS` で 2 回適用しても壊れない記述になっていること。"""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    # 全ての CREATE TABLE が IF NOT EXISTS を伴う（無装飾の CREATE TABLE はゼロ）
    ct_total = len(re.findall(r"CREATE\s+TABLE\b", sql))
    ct_safe = len(re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\b", sql))
    assert ct_total == ct_safe, (
        f"CREATE TABLE が IF NOT EXISTS なしで {ct_total - ct_safe} 件残っています"
    )
    # 全ての CREATE INDEX も IF NOT EXISTS 付き
    ci_total = len(re.findall(r"CREATE\s+INDEX\b", sql))
    ci_safe = len(re.findall(r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\b", sql))
    assert ci_total == ci_safe, (
        f"CREATE INDEX が IF NOT EXISTS なしで {ci_total - ci_safe} 件残っています"
    )
    # ENUM は DO ブロックで存在チェック
    assert "IF NOT EXISTS (SELECT 1 FROM pg_type" in sql
    # POLICY は DROP POLICY IF EXISTS が先にある
    assert sql.count("DROP POLICY IF EXISTS") >= 2
    # 既存テーブル拡張も DO ブロック内で IF EXISTS チェック
    assert "table_name = 'proposals_chunks'" in sql


def test_migration_uses_vector_1024() -> None:
    """embedding 列が multilingual-e5-large の 1024 次元になっていること。"""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "vector(1024)" in sql


def test_migration_has_hnsw_index() -> None:
    """chunks の HNSW index が cosine ops で作成されていること。"""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    assert re.search(r"chunks_embedding_hnsw_idx.*USING hnsw.*vector_cosine_ops", sql, re.S)


def test_migration_has_acl_gin_indexes() -> None:
    """ACL 列に GIN index（配列検索高速化）が設定されていること。"""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "documents_acl_emails_idx" in sql
    assert "USING gin (acl_emails)" in sql


def test_migration_has_unique_external_id() -> None:
    """source_type + external_id の複合 UNIQUE があること（idempotency）。"""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "UNIQUE (source_type, external_id)" in sql


def test_rls_policy_uses_current_setting() -> None:
    """RLS policy が current_setting('app.user_email', true) を使っていること。

    SET LOCAL でリクエストごとに上書きできる前提。第二引数 true で missing_ok。
    """
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    assert "current_setting('app.user_email', true)" in sql
    assert "current_setting('app.user_role', true)" in sql
    assert "current_setting('app.user_groups', true)" in sql


# -----------------------------------------------------------
# 動的検証（実 DB ある時のみ。CI ではスキップ）
# -----------------------------------------------------------
_DB_DSN = os.environ.get("TEAMAGENT_DB_DSN")
_HAS_DB = _DB_DSN is not None

pytestmark_db = pytest.mark.skipif(
    not _HAS_DB,
    reason="実 DB 検証は TEAMAGENT_DB_DSN を設定した時のみ実行（pytest tests/adapters/test_pgvector_schema.py）",
)


@pytestmark_db
def test_db_documents_table_exists() -> None:
    """migration 適用後に documents テーブルが存在すること。"""
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'documents'"
        )
        assert cur.fetchone() is not None


@pytestmark_db
def test_db_chunks_table_has_embedding_column() -> None:
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'chunks' AND column_name = 'embedding'"
        )
        row = cur.fetchone()
        assert row is not None
        # pgvector の型は USER-DEFINED として現れる
        assert row[0] in ("USER-DEFINED", "vector")


@pytestmark_db
def test_db_rls_enforces_acl() -> None:
    """RLS が ACL を強制すること：app.user_email 未設定なら何も見えない。"""
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        # admin role でテストデータを INSERT（後始末あり）
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.user_role = 'admin'")
            cur.execute("SET LOCAL app.user_email = 'admin@vectorinc.co.jp'")
            cur.execute(
                "INSERT INTO documents "
                "(source_type, external_id, owner_email, acl_emails, title) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (source_type, external_id) DO NOTHING "
                "RETURNING id",
                ("other", "rls-test-1", "owner@example.com", ["alice@example.com"], "rls test"),
            )
        conn.commit()

        # 1) user_email セットなし → 何も見えない（fail-safe）
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents WHERE external_id = 'rls-test-1'")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 0, "RLS 失効：user_email 未設定でも見えている"

        # 2) ACL に含まれる alice なら見える
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.user_email = 'alice@example.com'")
            cur.execute("SELECT count(*) FROM documents WHERE external_id = 'rls-test-1'")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 1, "ACL に含まれるユーザーが見えていない"

        # cleanup
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.user_role = 'admin'")
            cur.execute("DELETE FROM documents WHERE external_id = 'rls-test-1'")
        conn.commit()
