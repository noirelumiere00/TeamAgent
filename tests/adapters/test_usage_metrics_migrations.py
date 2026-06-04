"""migration 0007(usage_events) / 0008(runtime_metrics) の静的契約テスト。

管理画面の一次データなので「管理者だけが読める・Bot は書くだけ・暗号化列は dashboard ロールに
渡さない・本文を保存しない」というセキュリティ契約を**静的に**検証する（実 DB 不要・CI で必ず通る）。
動的(実DB)検証は TEAMAGENT_DB_DSN がある時のみの別テストで行う。
"""

from __future__ import annotations

from pathlib import Path

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
