"""migration 0007(usage_events) / 0008(runtime_metrics) の静的契約テスト。

管理画面の一次データなので「管理者だけが読める・Bot は書くだけ・暗号化列は dashboard ロールに
渡さない・本文を保存しない」というセキュリティ契約を**静的に**検証する（実 DB 不要・CI で必ず通る）。
動的(実DB)検証は TEAMAGENT_DB_DSN がある時のみの別テストで行う。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MIG_0007 = PROJECT_ROOT / "infra" / "migrations" / "0007_usage_events.sql"
MIG_0008 = PROJECT_ROOT / "infra" / "migrations" / "0008_runtime_metrics.sql"


def test_migrations_exist() -> None:
    assert MIG_0007.exists(), f"missing: {MIG_0007}"
    assert MIG_0008.exists(), f"missing: {MIG_0008}"


def test_0007_defines_usage_events_with_security() -> None:
    sql = MIG_0007.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS usage_events" in sql
    assert "CREATE TABLE IF NOT EXISTS usage_event_calls" in sql
    # status は限定値（CHECK）
    assert "status IN ('ok', 'error', 'queue_full', 'timeout')" in sql
    # 二重書込に安全な UNIQUE(request_id)
    assert "usage_events_request_id_unique UNIQUE (request_id)" in sql
    # RLS: 管理者のみ読める・FORCE
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE  ROW LEVEL SECURITY" in sql or "FORCE ROW LEVEL SECURITY" in sql
    assert "current_setting('app.user_role', true) = 'admin'" in sql
    # Bot は INSERT のみ（SELECT 権限を与えない＝二重防御）
    assert "GRANT INSERT ON usage_events      TO teamagent_app" in sql
    assert "GRANT SELECT ON usage_events" not in sql.replace(
        "GRANT SELECT ON usage_events      TO teamagent_dashboard", ""
    )


def test_0007_creates_readonly_dashboard_role() -> None:
    sql = MIG_0007.read_text(encoding="utf-8")
    assert "CREATE ROLE teamagent_dashboard NOLOGIN NOBYPASSRLS" in sql
    assert "GRANT teamagent_dashboard TO teamagent" in sql
    assert "GRANT USAGE ON SCHEMA public TO teamagent_dashboard" in sql
    assert "GRANT SELECT ON usage_events      TO teamagent_dashboard" in sql


def test_0007_oauth_tokens_grant_excludes_ciphertext_column() -> None:
    """dashboard ロールは oauth_tokens の暗号化列 refresh_token_enc を SELECT できないこと。"""
    sql = MIG_0007.read_text(encoding="utf-8")
    # 列単位 GRANT に refresh_token_enc を含めない
    assert (
        "GRANT SELECT (user_email, scopes, created_at, updated_at) "
        "ON oauth_tokens TO teamagent_dashboard" in sql
    )
    assert "refresh_token_enc" not in sql.split("GRANT SELECT (")[1].split(")")[0]


def test_0007_does_not_store_message_bodies() -> None:
    """本文/PII を保存しないこと（query_chars=文字数のみ）。"""
    sql = MIG_0007.read_text(encoding="utf-8")
    assert "query_chars" in sql
    # 平文の本文列を作らない
    for forbidden in ("query_text", "message_text", "answer_text", "response_text"):
        assert forbidden not in sql, f"本文列 {forbidden} を持ってはいけない"


def test_0007_is_idempotent() -> None:
    sql = MIG_0007.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS usage_events" in sql
    assert "DROP POLICY IF EXISTS usage_events_admin_read" in sql
    assert "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'teamagent_dashboard')" in sql


def test_0008_defines_runtime_metrics_with_security() -> None:
    sql = MIG_0008.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS runtime_metrics" in sql
    # GateMetrics / PoolStats の主要列
    for col in (
        "gate_in_flight",
        "gate_peak_in_flight",
        "gate_waiting",
        "gate_rejected_queue_full",
        "pool_in_use",
        "pool_timeouts",
    ):
        assert col in sql, f"missing column {col}"
    assert "current_setting('app.user_role', true) = 'admin'" in sql
    assert "GRANT INSERT ON runtime_metrics TO teamagent_app" in sql
    assert "GRANT SELECT ON runtime_metrics TO teamagent_dashboard" in sql


def test_0008_pool_columns_nullable_for_direct_mode() -> None:
    """プール無効（直結）時に pool_* が NULL で入る想定＝NOT NULL を付けない。"""
    sql = MIG_0008.read_text(encoding="utf-8")
    # pool_in_use は NOT NULL を持たない（NULL 許容）
    pool_line = next(line for line in sql.splitlines() if "pool_in_use" in line)
    assert "NOT NULL" not in pool_line


# -----------------------------------------------------------
# 動的検証（実 DB ある時のみ。CI ではスキップ）= go-live ゲート
#   TEAMAGENT_DB_DSN=postgresql://teamagent:...@localhost:15433/teamagent \
#       pytest tests/adapters/test_usage_metrics_migrations.py -v
# -----------------------------------------------------------
_DB_DSN = os.environ.get("TEAMAGENT_DB_DSN")
pytestmark_db = pytest.mark.skipif(
    _DB_DSN is None,
    reason="実 DB 検証は TEAMAGENT_DB_DSN を設定した時のみ実行",
)


@pytestmark_db
def test_db_usage_tables_and_role_exist() -> None:
    """0007/0008 適用後、テーブルと read-only ロールが実在すること。"""
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn, conn.cursor() as cur:
        for t in ("usage_events", "usage_event_calls", "runtime_metrics"):
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (t,))
            assert cur.fetchone() is not None, f"table {t} 未作成"
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'teamagent_dashboard'")
        assert cur.fetchone() is not None, "role teamagent_dashboard 未作成"


@pytestmark_db
def test_db_grants_app_insert_only_dashboard_select_only() -> None:
    """Bot(teamagent_app)=INSERT のみ / 管理画面(teamagent_dashboard)=SELECT のみ（最小権限）。"""
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
            "WHERE table_name = 'usage_events' "
            "AND grantee IN ('teamagent_app', 'teamagent_dashboard')"
        )
        grants = {(r[0], r[1]) for r in cur.fetchall()}
        assert ("teamagent_app", "INSERT") in grants
        assert ("teamagent_app", "SELECT") not in grants  # Bot は読めない（二重防御）
        assert ("teamagent_dashboard", "SELECT") in grants
        assert ("teamagent_dashboard", "INSERT") not in grants  # 画面は書けない


@pytestmark_db
def test_db_oauth_tokens_ciphertext_not_granted_to_dashboard() -> None:
    """管理画面ロールは oauth_tokens の暗号化列 refresh_token_enc を SELECT できない（列単位GRANT）。"""
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.role_column_grants "
            "WHERE table_name = 'oauth_tokens' AND grantee = 'teamagent_dashboard' "
            "AND privilege_type = 'SELECT'"
        )
        cols = {r[0] for r in cur.fetchall()}
        assert "user_email" in cols  # 連携状況の表示に必要な列は読める
        assert "scopes" in cols
        assert "refresh_token_enc" not in cols  # 暗号化列は読む権限すら無い


@pytestmark_db
def test_db_usage_events_rls_behavior() -> None:
    """振る舞い: Bot は INSERT 可・SELECT 不可 / 管理者GUCで読める / 暗号化列は拒否。"""
    import psycopg

    assert _DB_DSN is not None

    # 1) teamagent_app は INSERT 可（rollback で残さない）
    with psycopg.connect(_DB_DSN) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE teamagent_app")
                cur.execute(
                    "INSERT INTO usage_events (request_id, skill, status) VALUES (%s, %s, %s)",
                    ("rlschk-app", "_rls_test_", "ok"),
                )
        finally:
            conn.rollback()  # テスト行を永続化しない（append-only テーブルを汚さない）

    # 2) teamagent_app は SELECT 不可（権限が無い）
    with psycopg.connect(_DB_DSN) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE teamagent_app")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("SELECT count(*) FROM usage_events")
        finally:
            conn.rollback()

    # 3) teamagent_dashboard + admin GUC は usage_events を SELECT 可
    with psycopg.connect(_DB_DSN) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE teamagent_dashboard")
                cur.execute("SELECT set_config('app.user_role', 'admin', true)")
                cur.execute("SELECT count(*) FROM usage_events")
                assert cur.fetchone() is not None  # エラーなく読める
        finally:
            conn.rollback()

    # 4) teamagent_dashboard は oauth_tokens の暗号化列を読めない（列権限が無い）
    with psycopg.connect(_DB_DSN) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SET ROLE teamagent_dashboard")
                cur.execute("SELECT set_config('app.user_role', 'admin', true)")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute("SELECT refresh_token_enc FROM oauth_tokens LIMIT 1")
        finally:
            conn.rollback()
