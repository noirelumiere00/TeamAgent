"""0029_personal_memory の実 PostgreSQL 回帰テスト（DM 本人メモ v1・M5）。

``TEAMAGENT_TEST_DB_DSN`` が明示された使い捨ての PostgreSQL 16 だけで実行する（CI は postgres:16）。

本番に似せるため、superuser では migration を流さない。試験ごとに次を作る:
- 使い捨ての DATABASE（SECURITY DEFINER 関数は search_path を固定し public を修飾するので schema 分離は使えない）
- 本番の master（teamagent）に似せた「CREATEROLE・BYPASSRLS・非 superuser」の migrator ロール
  （handoff day0-4:265-270 に「master 接続では FORCE RLS でもすり抜けた」とあるため、最悪の BYPASSRLS を仮定する）
- 0002 の既定権限（新しい表に teamagent_app の S/I/U/D が自動で付く）
その migrator で 0029 を流し、権限の行列・RLS・SECURITY DEFINER 関数を実機で確かめる。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from tests.personal_memory.conftest import PM_ROLES, PmDb

_DB_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_MIGRATION = Path(__file__).resolve().parents[2] / "infra/migrations/0029_personal_memory.sql"
_TABLES = ("personal_memory_profiles", "personal_memory_entries", "personal_memory_audit")
_TEAM = "T0123ABCDE"
_USER_A = "U0AAAAAAA1"
_USER_B = "U0BBBBBBB2"

pytestmark = pytest.mark.skipif(
    _DB_DSN is None,
    reason="disposable PostgreSQL validation requires TEAMAGENT_TEST_DB_DSN",
)


def _sha16(principal: str) -> str:
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()[:16]


@pytest.fixture(scope="module")
def env(pm_db: PmDb) -> PmDb:
    return pm_db


def _connect(env: PmDb) -> Any:
    import psycopg

    return psycopg.connect(env.migrator_dsn)


def _as_app(cur: Any, principal: str | None) -> None:
    cur.execute("SET ROLE personal_memory_app")
    if principal is not None:
        cur.execute("SELECT set_config('app.pm_principal', %s, true)", (principal,))


def _seed(env: PmDb, user: str, contents: list[str]) -> None:
    principal = f"{_TEAM}:{user}"
    with _connect(env) as conn, conn.cursor() as cur:
        _as_app(cur, principal)
        cur.execute(
            "INSERT INTO personal_memory_profiles (team_id, slack_user_id, user_email) "
            "VALUES (%s, %s, %s)",
            (_TEAM, user, f"{user.lower()}@example.com"),
        )
        for content in contents:
            cur.execute(
                "INSERT INTO personal_memory_entries (team_id, slack_user_id, target, content) "
                "VALUES (%s, %s, 'user', %s)",
                (_TEAM, user, content),
            )
        conn.commit()


@pytest.fixture(scope="module")
def seeded(env: PmDb) -> PmDb:
    _seed(env, _USER_A, ["返事は結論から", "資料は表形式"])
    _seed(env, _USER_B, ["箇条書きを好む"])
    return env


def test_rls_forced_and_owned_by_definer(env: PmDb) -> None:
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) "
            "FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r' ORDER BY relname",
            (list(_TABLES),),
        )
        rows = cur.fetchall()
    assert len(rows) == 3
    assert all(r[1] is True and r[2] is True and r[3] == "personal_memory_definer" for r in rows)


def test_roles_are_nologin_nobypassrls(env: PmDb) -> None:
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper, rolcreaterole, rolinherit "
            "FROM pg_roles WHERE rolname = ANY(%s)",
            (list(PM_ROLES),),
        )
        rows = {r[0]: r[1:] for r in cur.fetchall()}
    assert set(rows) == set(PM_ROLES)
    assert all(v == (False, False, False, False, False) for v in rows.values())


@pytest.mark.parametrize(
    ("role", "table", "allowed"),
    [
        ("personal_memory_app", "personal_memory_profiles", {"SELECT", "INSERT", "UPDATE"}),
        ("personal_memory_app", "personal_memory_entries", {"SELECT", "INSERT", "DELETE"}),
        ("personal_memory_app", "personal_memory_audit", {"INSERT"}),
        ("teamagent_app", "personal_memory_profiles", set()),
        ("teamagent_app", "personal_memory_entries", set()),
        ("teamagent_app", "personal_memory_audit", set()),
        ("teamagent_dashboard", "personal_memory_entries", set()),
        ("personal_memory_admin_reader", "personal_memory_entries", set()),
        ("personal_memory_retirer", "personal_memory_entries", set()),
        ("public", "personal_memory_entries", set()),
    ],
)
def test_table_privilege_matrix(env: PmDb, role: str, table: str, allowed: set[str]) -> None:
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    with _connect(env) as conn, conn.cursor() as cur:
        granted = set()
        for priv in privileges:
            cur.execute("SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", priv))
            if cur.fetchone()[0]:
                granted.add(priv)
            cur.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (role, f"public.{table}", f"{priv} WITH GRANT OPTION"),
            )
            assert cur.fetchone()[0] is False
    assert granted == allowed


def test_master_without_set_role_cannot_read_even_with_bypassrls(seeded: PmDb) -> None:
    import psycopg

    with _connect(seeded) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone()[0] is True  # 最悪の前提（master が BYPASSRLS）で試している
        cur.execute("SELECT set_config('app.user_role', 'admin', true)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT count(*) FROM personal_memory_entries")


def test_teamagent_app_with_admin_guc_cannot_read(seeded: PmDb) -> None:
    import psycopg

    with _connect(seeded) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE teamagent_app")
        cur.execute("SELECT set_config('app.user_role', 'admin', true)")
        cur.execute("SELECT set_config('app.user_email', %s, true)", ("u0aaaaaaa1@example.com",))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT count(*) FROM personal_memory_entries")


def test_master_cannot_become_definer(seeded: PmDb) -> None:
    import psycopg

    with _connect(seeded) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SET ROLE personal_memory_definer")


def test_app_sees_only_own_rows(seeded: PmDb) -> None:
    with _connect(seeded) as conn, conn.cursor() as cur:
        _as_app(cur, f"{_TEAM}:{_USER_A}")
        cur.execute("SELECT content FROM personal_memory_entries ORDER BY content")
        assert [r[0] for r in cur.fetchall()] == ["資料は表形式", "返事は結論から"]
        cur.execute("SELECT count(*) FROM personal_memory_profiles")
        assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("principal", [None, "", f"{_TEAM}:U0NOBODY00"])
def test_app_without_matching_principal_sees_nothing(seeded: PmDb, principal: str | None) -> None:
    with _connect(seeded) as conn, conn.cursor() as cur:
        _as_app(cur, principal)
        cur.execute("SELECT set_config('app.user_role', 'admin', true)")
        cur.execute("SELECT count(*) FROM personal_memory_entries")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM personal_memory_profiles")
        assert cur.fetchone()[0] == 0


def test_app_cannot_write_other_users_rows(seeded: PmDb) -> None:
    import psycopg

    with _connect(seeded) as conn, conn.cursor() as cur:
        _as_app(cur, f"{_TEAM}:{_USER_A}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "INSERT INTO personal_memory_entries (team_id, slack_user_id, target, content) "
                "VALUES (%s, %s, 'user', 'x')",
                (_TEAM, _USER_B),
            )
    with _connect(seeded) as conn, conn.cursor() as cur:
        _as_app(cur, f"{_TEAM}:{_USER_A}")
        cur.execute("DELETE FROM personal_memory_entries WHERE slack_user_id = %s", (_USER_B,))
        assert cur.rowcount == 0


@pytest.mark.parametrize(
    "content",
    ["区切り§入り", "x" * 201, " 前後に空白 ", ""],
)
def test_entry_content_checks(seeded: PmDb, content: str) -> None:
    import psycopg

    with _connect(seeded) as conn, conn.cursor() as cur:
        _as_app(cur, f"{_TEAM}:{_USER_A}")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO personal_memory_entries (team_id, slack_user_id, target, content) "
                "VALUES (%s, %s, 'user', %s)",
                (_TEAM, _USER_A, content),
            )


def test_self_audit_insert_rules(seeded: PmDb) -> None:
    import psycopg

    principal = f"{_TEAM}:{_USER_A}"
    with _connect(seeded) as conn, conn.cursor() as cur:
        _as_app(cur, principal)
        cur.execute(
            "INSERT INTO personal_memory_audit (actor_kind, profile_sha16, action, item_count, "
            "reason_code) VALUES ('self', %s, 'forget', 1, 'user_command')",
            (_sha16(principal),),
        )
        conn.commit()
    bad_rows = [
        ("self", _sha16(f"{_TEAM}:{_USER_B}"), None),  # 他人の profile を名乗る
        ("admin", _sha16(principal), "admin@example.com"),  # 本人が管理者を名乗る
    ]
    for actor_kind, sha, email in bad_rows:
        with _connect(seeded) as conn, conn.cursor() as cur:
            _as_app(cur, principal)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO personal_memory_audit (actor_kind, actor_email, profile_sha16, "
                    "action, item_count, reason_code) VALUES (%s, %s, %s, 'forget', 1, 'x')",
                    (actor_kind, email, sha),
                )
    with _connect(seeded) as conn, conn.cursor() as cur:
        _as_app(cur, principal)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT count(*) FROM personal_memory_audit")


def test_function_execute_privileges(seeded: PmDb) -> None:
    view = "public.personal_memory_admin_view(text,text,text,text)"
    retire = "public.personal_memory_retire_delete(text,text,text)"
    with _connect(seeded) as conn, conn.cursor() as cur:
        expected = {
            ("public", view): False,
            ("public", retire): False,
            ("teamagent_app", view): False,
            ("personal_memory_app", view): False,
            ("personal_memory_admin_reader", view): True,
            ("personal_memory_admin_reader", retire): False,
            ("personal_memory_retirer", retire): True,
            ("personal_memory_retirer", view): False,
            (seeded.migrator, view): False,  # INHERIT FALSE なので master のままでは実行できない
            (seeded.migrator, retire): False,
        }
        for (role, fn), want in expected.items():
            cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, fn))
            assert cur.fetchone()[0] is want, (role, fn)
        cur.execute(
            "SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig "
            "FROM pg_proc p WHERE p.proname LIKE 'personal_memory_%' ORDER BY p.proname"
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["personal_memory_admin_view", "personal_memory_retire_delete"]
    assert all(r[1] is True and r[2] == "personal_memory_definer" for r in rows)
    assert all(r[3] == ["search_path=pg_catalog, pg_temp"] for r in rows)


def test_admin_view_audits_before_returning_rows(env: PmDb) -> None:
    import psycopg

    user = "U0VIEWED01"
    _seed(env, user, ["返事は短め", "資料は表で"])
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE personal_memory_admin_reader")
        cur.execute(
            "SELECT entry_no, entry_target, entry_content FROM "
            "public.personal_memory_admin_view(%s, %s, %s, %s)",
            ("Komata@Example.com", _TEAM, user, "support_request"),
        )
        rows = cur.fetchall()
        conn.commit()
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE personal_memory_admin_reader")
        # 管理者閲覧ロールは表そのものは読めない
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT count(*) FROM personal_memory_entries")
    # 同じトランザクションで入れた行は created_at が同じなので、順序ではなく中身と番号で比べる
    assert sorted(r[2] for r in rows) == sorted(["返事は短め", "資料は表で"])
    assert [r[0] for r in rows] == [1, 2]
    with psycopg.connect(env.admin_dsn) as su, su.cursor() as cur:
        cur.execute(
            "SELECT actor_kind, actor_email, action, item_count, reason_code FROM "
            "personal_memory_audit WHERE profile_sha16 = %s",
            (_sha16(f"{_TEAM}:{user}"),),
        )
        assert cur.fetchall() == [
            ("admin", "komata@example.com", "admin_view", 2, "support_request")
        ]
        cur.execute(
            "SELECT admin_view_count FROM personal_memory_profiles WHERE slack_user_id = %s",
            (user,),
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.parametrize(
    ("admin", "team", "user", "reason"),
    [
        ("not-an-email", _TEAM, "U0VIEWED02", "support_request"),
        ("a@example.com", "bad", "U0VIEWED02", "support_request"),
        ("a@example.com", _TEAM, "bad", "support_request"),
        ("a@example.com", _TEAM, "U0VIEWED02", "Bad Reason"),
    ],
)
def test_admin_view_rejects_bad_arguments_without_audit(
    env: PmDb, admin: str, team: str, user: str, reason: str
) -> None:
    import psycopg

    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE personal_memory_admin_reader")
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            cur.execute(
                "SELECT * FROM public.personal_memory_admin_view(%s, %s, %s, %s)",
                (admin, team, user, reason),
            )
    with psycopg.connect(env.admin_dsn) as su, su.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM personal_memory_audit WHERE reason_code = %s",
            (reason,),
        )
        assert cur.fetchone()[0] == 0 or reason == "support_request"


def test_retire_delete_removes_profile_and_entries(env: PmDb) -> None:
    import psycopg

    user = "U0RETIRE01"
    _seed(env, user, ["返事は短め", "資料は表で", "結論から"])
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE personal_memory_retirer")
        cur.execute(
            "SELECT public.personal_memory_retire_delete(%s, %s, %s)",
            (_TEAM, user, "slack_deleted"),
        )
        assert cur.fetchone()[0] == 3
        conn.commit()
    with psycopg.connect(env.admin_dsn) as su, su.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM personal_memory_entries WHERE slack_user_id = %s", (user,)
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM personal_memory_profiles WHERE slack_user_id = %s", (user,)
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT actor_kind, action, item_count FROM personal_memory_audit "
            "WHERE profile_sha16 = %s",
            (_sha16(f"{_TEAM}:{user}"),),
        )
        assert cur.fetchall() == [("retire", "retire_delete", 3)]


def test_migration_avoids_on_conflict_and_returning() -> None:
    text = _MIGRATION.read_text(encoding="utf-8").upper()
    # コメント以外の SQL に ON CONFLICT / RETURNING が無いこと（0024 の地雷）
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    assert "ON CONFLICT" not in body
    assert "RETURNING " not in body.replace("RETURNS TABLE", "").replace("RETURNS INTEGER", "")
    assert "OR CURRENT_SETTING('APP.USER_ROLE'" not in body
