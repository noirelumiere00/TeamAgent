"""oauth_tokens（per-user OAuth トークン）の RLS / スキーマ契約テスト。

最も機微なテーブルなので「他人のトークン行に触れない」を**実 DB で**証明する
（migration 0006 のポリシーが本当に効いているか）。静的検証は実 DB 不要で CI で必ず通る。
実 DB 検証は TEAMAGENT_DB_DSN がある時のみ（go-live ゲート）:

    TEAMAGENT_DB_DSN=postgresql://teamagent:...@localhost:15432/teamagent \
        pytest tests/adapters/test_oauth_tokens_rls.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATION_0006 = PROJECT_ROOT / "infra" / "migrations" / "0006_oauth_tokens.sql"


# -----------------------------------------------------------
# 静的検証（実 DB 不要 / CI で必ず通る）
# -----------------------------------------------------------
def test_migration_0006_exists() -> None:
    assert MIGRATION_0006.exists(), f"missing migration: {MIGRATION_0006}"


def test_migration_0006_has_security_objects() -> None:
    """0006 に RLS/FORCE/policy/CHECK/BYTEA（平文列無し）が含まれること。"""
    sql = MIGRATION_0006.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS oauth_tokens" in sql
    assert "refresh_token_enc   BYTEA NOT NULL" in sql  # 暗号化列（平文列は持たない）
    assert "refresh_token TEXT" not in sql  # 平文 refresh token 列が無いこと
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql  # owner も RLS 対象（越権防止）
    assert "CREATE POLICY oauth_tokens_self" in sql
    assert "current_setting('app.user_email', true)" in sql
    # 空 user_email を構造的に禁止（fail-closed / RLS が空文字で崩れるのを防ぐ）
    assert "position('@' IN user_email) > 0" in sql
    assert "current_setting('app.user_email', true) <> ''" in sql


def test_migration_0006_is_idempotent() -> None:
    sql = MIGRATION_0006.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS oauth_tokens" in sql
    assert "DROP POLICY IF EXISTS oauth_tokens_self" in sql


# -----------------------------------------------------------
# 動的検証（実 DB ある時のみ。CI ではスキップ）= go-live ゲート
# -----------------------------------------------------------
_DB_DSN = os.environ.get("TEAMAGENT_DB_DSN")
pytestmark_db = pytest.mark.skipif(
    _DB_DSN is None,
    reason="実 DB 検証は TEAMAGENT_DB_DSN を設定した時のみ実行",
)

_ALICE = "alice-rlstest@example.com"
_BOB = "bob-rlstest@example.com"


def _cleanup(conn: object) -> None:
    import psycopg  # noqa: F401

    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute("RESET ROLE")
        cur.execute("SET LOCAL app.user_role = 'admin'")
        cur.execute("DELETE FROM oauth_tokens WHERE user_email IN (%s, %s)", (_ALICE, _BOB))


@pytestmark_db
def test_db_oauth_tokens_rls_blocks_other_users() -> None:
    """RLS: 本人行のみ可視・他人のトークンは read/update 不可・GUC 未設定/空は fail-safe。"""
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'teamagent_app'")
            if cur.fetchone() is None:
                pytest.skip("migration 0002 (teamagent_app role) 未適用のため skip")
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'oauth_tokens'")
            if cur.fetchone() is None:
                pytest.skip("migration 0006 (oauth_tokens) 未適用のため skip")

        # admin GUC でテストデータ INSERT（FORCE RLS なので WITH CHECK の admin 分岐で通す）
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.user_role = 'admin'")
            for email in (_ALICE, _BOB):
                cur.execute(
                    "INSERT INTO oauth_tokens (user_email, refresh_token_enc, scopes) "
                    "VALUES (%s, %s, %s) ON CONFLICT (user_email) DO UPDATE "
                    "SET refresh_token_enc = EXCLUDED.refresh_token_enc",
                    (email, b"\x01\x02\x03", ["gmail.readonly"]),
                )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SET ROLE teamagent_app")
        try:
            # 1) GUC 未設定 → 何も見えない（fail-safe）
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM oauth_tokens WHERE user_email IN (%s, %s)",
                    (_ALICE, _BOB),
                )
                row = cur.fetchone()
                assert row is not None and row[0] == 0, "RLS 失効：GUC 未設定でも見えている"

            # 2) alice 本人 → 自分の行のみ可視、bob の行は不可視
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.user_email', %s, true)", (_ALICE,))
                cur.execute("SELECT count(*) FROM oauth_tokens WHERE user_email = %s", (_ALICE,))
                assert cur.fetchone()[0] == 1, "本人の行が見えない"  # type: ignore[index]
                cur.execute("SELECT count(*) FROM oauth_tokens WHERE user_email = %s", (_BOB,))
                assert cur.fetchone()[0] == 0, "他人(bob)のトークンが見えている"  # type: ignore[index]

            # 3) alice のまま bob 行を UPDATE → 0 行（他人のトークンを書き換え不可）
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.user_email', %s, true)", (_ALICE,))
                cur.execute(
                    "UPDATE oauth_tokens SET scopes = %s WHERE user_email = %s",
                    (["drive.readonly"], _BOB),
                )
                assert cur.rowcount == 0, "他人(bob)のトークンを書き換えられてしまう"

            # 4) 空文字 GUC → 何も見えない（空 email スロットの悪用防止）
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.user_email = ''")
                cur.execute(
                    "SELECT count(*) FROM oauth_tokens WHERE user_email IN (%s, %s)",
                    (_ALICE, _BOB),
                )
                assert cur.fetchone()[0] == 0, "空 GUC で行が見えている"  # type: ignore[index]
        finally:
            _cleanup(conn)
            conn.commit()


@pytestmark_db
def test_db_oauth_tokens_check_rejects_invalid_email() -> None:
    """CHECK 制約: 空文字 / @ 無しの user_email は INSERT 不可（構造的 fail-closed）。"""
    import psycopg

    assert _DB_DSN is not None
    for bad in ("", "no-at-sign"):
        with psycopg.connect(_DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.user_role = 'admin'")
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO oauth_tokens (user_email, refresh_token_enc) VALUES (%s, %s)",
                        (bad, b"\x01"),
                    )
            conn.rollback()
