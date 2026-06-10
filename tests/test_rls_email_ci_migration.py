"""WS-C.2: RLS email/group 比較の lower() 化 migration の静的検査（実DB不要）。

実DB適用と RLS 動的検証（2ユーザで他人行0）は P0(要承認)で行う。ここでは migration が
冪等(DROP IF EXISTS→CREATE)で email/group を両側 lower() 比較することを固定する。
"""

from __future__ import annotations

from pathlib import Path

_MIG = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "migrations"
    / "0010_rls_email_case_insensitive.sql"
)


def test_migration_file_exists() -> None:
    assert _MIG.is_file()


def test_recreates_documents_policies_idempotently() -> None:
    sql = _MIG.read_text(encoding="utf-8")
    assert "DROP POLICY IF EXISTS documents_user_acl ON documents" in sql
    assert "CREATE POLICY documents_user_acl ON documents" in sql
    assert "DROP POLICY IF EXISTS documents_owner_insert ON documents" in sql
    assert "CREATE POLICY documents_owner_insert ON documents" in sql


def test_email_and_group_compared_case_insensitively() -> None:
    sql = _MIG.read_text(encoding="utf-8")
    # email: GUC 側・DB 側の両方を lower()
    assert "lower(current_setting('app.user_email', true)) = lower(owner_email)" in sql
    assert "SELECT lower(e) FROM unnest(acl_emails) AS e" in sql
    # group: GUC 側・DB 側の両方を lower()
    assert "WHERE lower(g) = ANY" in sql
    assert "string_to_array(lower(current_setting('app.user_groups', true)), ',')" in sql
    # admin バイパスは温存（意図不変）
    assert "current_setting('app.user_role', true) = 'admin'" in sql
