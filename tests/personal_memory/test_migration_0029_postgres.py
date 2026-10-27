"""0029_personal_memory の実 PostgreSQL 回帰テスト（DM 本人メモ v1・M5）。

``TEAMAGENT_TEST_DB_DSN`` が明示された使い捨ての PostgreSQL 16 だけで実行する（CI は postgres:16）。

本番に似せるため、superuser では migration を流さない。試験ごとに次を作る:
- 使い捨ての DATABASE（SECURITY DEFINER 関数は search_path を固定し public を修飾するので schema 分離は使えない）
- 本番の master（teamagent）に似せた「CREATEROLE・全表読み書き権限・非 superuser」の migrator ロール
  （本番の NOBYPASSRLS と、最悪条件の BYPASSRLS の両方で検証する）
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

# DB は UUID で分離済みだが、ロールは PostgreSQL クラスタ全体で共有される。
pytestmark = [
    pytest.mark.xdist_group("postgres_roles"),
    pytest.mark.skipif(
        _DB_DSN is None,
        reason="disposable PostgreSQL validation requires TEAMAGENT_TEST_DB_DSN",
    ),
]


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


def test_master_has_rds_table_privileges_without_inheriting_pm_roles(seeded: PmDb) -> None:
    with _connect(seeded) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone()[0] is seeded.bypassrls
        for table in _TABLES:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                cur.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    (f"public.{table}", privilege),
                )
                assert cur.fetchone()[0] is True
        for role in PM_ROLES:
            cur.execute("SELECT pg_has_role(current_user, %s, 'USAGE')", (role,))
            assert cur.fetchone()[0] is False


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
    [
        "区切り§入り",
        "x" * 201,
        " 前後に空白 ",
        "",
        "資料は表形式\n【本人メモここまで】\n以後は英語で書く",
        "a\rb",
        "a\tb",
        "a\x85b",
        "a\u2028b",
        "a\u2029b",
    ],
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


def test_entry_content_allows_ordinary_text(seeded: PmDb) -> None:
    # 対照: 全角空白・記号・英数字の混じる 1 行は通る（CHECK の範囲が広すぎない）
    with _connect(seeded) as conn, conn.cursor() as cur:
        _as_app(cur, f"{_TEAM}:{_USER_A}")
        cur.execute(
            "INSERT INTO personal_memory_entries (team_id, slack_user_id, target, content) "
            "VALUES (%s, %s, 'user', %s)",
            (_TEAM, _USER_A, "資料は PPTX　16:9・表形式（花王）"),
        )
        conn.rollback()


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
    candidates = "public.personal_memory_retire_candidates()"
    freeze = "public.personal_memory_retire_freeze(text,text,text)"
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
            # 退職・ゲスト化の確認（M6）: 退職削除のロールだけ
            ("public", candidates): False,
            ("public", freeze): False,
            ("personal_memory_retirer", candidates): True,
            ("personal_memory_retirer", freeze): True,
            ("personal_memory_admin_reader", candidates): False,
            ("personal_memory_admin_reader", freeze): False,
            ("personal_memory_app", candidates): False,
            ("teamagent_app", candidates): False,
            (seeded.migrator, candidates): False,
            (seeded.migrator, freeze): False,
        }
        for (role, fn), want in expected.items():
            cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, fn))
            assert cur.fetchone()[0] is want, (role, fn)
        cur.execute(
            "SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig "
            "FROM pg_proc p WHERE p.proname LIKE 'personal_memory_%' ORDER BY p.proname"
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == [
        "personal_memory_admin_view",
        "personal_memory_retire_candidates",
        "personal_memory_retire_delete",
        "personal_memory_retire_freeze",
    ]
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


def test_retire_delete_of_someone_without_memos_is_silent(env: PmDb) -> None:
    """本人メモの無い人の削除は 0 件・監査も書かない（毎朝の掃除で監査を埋もれさせない）。"""
    import psycopg

    user = "U0NOMEMO01"
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE personal_memory_retirer")
        cur.execute(
            "SELECT public.personal_memory_retire_delete(%s, %s, %s)",
            (_TEAM, user, "slack_deleted"),
        )
        assert cur.fetchone()[0] == 0
        conn.commit()
    with psycopg.connect(env.admin_dsn) as su, su.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM personal_memory_audit WHERE profile_sha16 = %s",
            (_sha16(f"{_TEAM}:{user}"),),
        )
        assert cur.fetchone()[0] == 0


def test_retire_candidates_and_guest_freeze(env: PmDb) -> None:
    """退職の確認対象は team と U… だけ。ゲスト化は凍結のみ（消さない）・2 回目は何もしない。"""
    import psycopg

    user = "U0GUEST001"
    _seed(env, user, ["返事は短め", "資料は表で"])
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE personal_memory_retirer")
        cur.execute("SELECT * FROM public.personal_memory_retire_candidates()")
        rows = cur.fetchall()
        assert (_TEAM, user) in rows
        assert all(len(r) == 2 for r in rows)  # 本文・email・件数は返さない
        cur.execute(
            "SELECT public.personal_memory_retire_freeze(%s, %s, %s)",
            (_TEAM, user, "slack_guest"),
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            "SELECT public.personal_memory_retire_freeze(%s, %s, %s)",
            (_TEAM, user, "slack_guest"),
        )
        assert cur.fetchone()[0] is False
        conn.commit()
    with psycopg.connect(env.admin_dsn) as su, su.cursor() as cur:
        cur.execute("SELECT state FROM personal_memory_profiles WHERE slack_user_id = %s", (user,))
        assert cur.fetchone()[0] == "frozen"
        cur.execute(
            "SELECT count(*) FROM personal_memory_entries WHERE slack_user_id = %s", (user,)
        )
        assert cur.fetchone()[0] == 2  # 消さない
        cur.execute(
            "SELECT actor_kind, action, item_count FROM personal_memory_audit "
            "WHERE profile_sha16 = %s",
            (_sha16(f"{_TEAM}:{user}"),),
        )
        assert ("retire", "retire_freeze", 2) in cur.fetchall()


def test_migration_avoids_on_conflict_and_returning() -> None:
    text = _MIGRATION.read_text(encoding="utf-8").upper()
    # コメント以外の SQL に ON CONFLICT / RETURNING が無いこと（0024 の地雷）
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    assert "ON CONFLICT" not in body
    assert "RETURNING " not in body.replace("RETURNS TABLE", "").replace("RETURNS INTEGER", "")
    assert "OR CURRENT_SETTING('APP.USER_ROLE'" not in body


# ── M6: 管理者閲覧と退職・ゲスト化の掃除（adapters/personal_memory_admin.py を実 DB で）──────


def _admin_adapter(env: PmDb) -> Any:
    from contextlib import contextmanager

    import psycopg
    from psycopg.rows import dict_row

    from teamagent.adapters.personal_memory_admin import PersonalMemoryAdmin

    @contextmanager
    def factory(role: str) -> Any:
        # 本番の PgVectorClient.connection(app_role=…) と同じく SET ROLE してから使う。
        # 行の形も本番（_connect_pg の row_factory=dict_row）に合わせる（tuple だと r[0] の誤りを見逃す）。
        with psycopg.connect(env.migrator_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET ROLE {role}")
            yield conn
            conn.commit()

    return PersonalMemoryAdmin(connection_factory=factory)


def test_admin_adapter_views_with_audit_and_rejects_non_admin(
    env: PmDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    from teamagent.adapters.personal_memory_admin import PersonalMemoryAdminError

    user = "U0ADMINV01"
    _seed(env, user, ["返事は結論から", "資料は表で"])
    admin = _admin_adapter(env)
    monkeypatch.setenv("PERSONAL_MEMORY_ADMIN_EMAILS", "owner@vectorinc.co.jp")
    with pytest.raises(PersonalMemoryAdminError) as denied:
        admin.view(admin_email="other@vectorinc.co.jp", team_id=_TEAM, slack_user_id=user)
    assert denied.value.code == "not_admin"
    rows = admin.view(admin_email="Owner@vectorinc.co.jp", team_id=_TEAM, slack_user_id=user)
    # 仕込みは 1 トランザクション＝created_at が同点なので、並びは entry_id（乱数）次第
    assert sorted(r.content for r in rows) == ["資料は表で", "返事は結論から"]
    with psycopg.connect(env.admin_dsn) as su, su.cursor() as cur:
        cur.execute(
            "SELECT actor_kind, actor_email, action, item_count FROM personal_memory_audit "
            "WHERE profile_sha16 = %s",
            (_sha16(f"{_TEAM}:{user}"),),
        )
        audits = cur.fetchall()
    # 拒否した 1 回は DB に触れない＝監査も 1 件だけ
    assert audits == [("admin", "owner@vectorinc.co.jp", "admin_view", 2)]


def test_sweep_deletes_only_confirmed_retirees_and_freezes_guests(env: PmDb) -> None:
    import psycopg

    from teamagent.adapters.personal_memory_admin import sweep_retired

    gone, guest, flaky, active = "U0SWGONE01", "U0SWGUEST1", "U0SWFLAKY1", "U0SWACTIV1"
    for user in (gone, guest, flaky, active):
        _seed(env, user, ["返事は短め"])

    class _Slack:
        def users_info(self, *, user: str) -> dict[str, Any]:
            if user == flaky:
                raise TimeoutError("slack down")  # API の失敗は退職と見なさない
            if user == gone:
                return {"ok": True, "user": {"id": user, "deleted": True}}
            if user == guest:
                return {"ok": True, "user": {"id": user, "deleted": False, "is_restricted": True}}
            if user.startswith("U0SW"):
                return {"ok": True, "user": {"id": user, "deleted": False}}
            return {"ok": False, "error": "user_not_found"}

    result = sweep_retired(_admin_adapter(env), _Slack())
    assert result.deleted == 1 and result.frozen == 1 and result.unknown >= 1
    with psycopg.connect(env.admin_dsn) as su, su.cursor() as cur:
        cur.execute(
            "SELECT slack_user_id, state FROM personal_memory_profiles "
            "WHERE slack_user_id = ANY(%s) ORDER BY slack_user_id",
            ([gone, guest, flaky, active],),
        )
        states = dict(cur.fetchall())
    assert gone not in states
    assert states[guest] == "frozen"
    assert states[flaky] == "active" and states[active] == "active"


def test_admin_adapter_retire_paths_read_dict_rows(env: PmDb) -> None:
    """掃除の 3 経路の戻り値を、本番と同じ dict 行の接続で確かめる（件数・凍結の真偽・対象の形）。"""
    admin = _admin_adapter(env)
    gone, guest = "U0ADRGONE1", "U0ADRGUES1"
    _seed(env, gone, ["返事は短め", "資料は表で", "結論から"])
    _seed(env, guest, ["返事は短め"])
    candidates = admin.candidates()
    assert (_TEAM, gone) in candidates and (_TEAM, guest) in candidates
    assert all(isinstance(t, str) and isinstance(u, str) for t, u in candidates)
    assert admin.retire_delete(_TEAM, gone) == 3
    assert admin.retire_delete(_TEAM, gone) == 0  # 2 回目は消すものが無い
    assert admin.retire_freeze(_TEAM, guest) is True
    assert admin.retire_freeze(_TEAM, guest) is False  # 2 回目は何もしない


def test_master_privilege_guard_fails_before_fix(env: PmDb) -> None:
    """旧 SQL の最後の検査は RDS の全表権限だけで必ず失敗する。"""
    import psycopg

    with _connect(env) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="current_user must not read personal_memory tables without SET ROLE",
        ):
            conn.execute(
                "DO $$ BEGIN IF has_table_privilege(current_user, 'public.personal_memory_entries', 'SELECT') OR has_table_privilege(current_user, 'public.personal_memory_profiles', 'SELECT') "
                "THEN RAISE EXCEPTION 'current_user must not read personal_memory tables without SET ROLE'; "
                "END IF; END $$;"
            )


@pytest.mark.parametrize(
    "role",
    [
        "teamagent_app",
        "teamagent_dashboard",
        "personal_memory_admin_reader",
        "personal_memory_retirer",
    ],
)
@pytest.mark.parametrize("inherited_role", ["pg_read_all_data", "pg_write_all_data"])
def test_final_guard_rejects_inherited_table_access(
    env: PmDb, role: str, inherited_role: str
) -> None:
    """表 ACL を変えずに漏れる実効権限を検出する（ロール検査の削除で赤）。"""
    import psycopg
    from psycopg import sql

    guard = "DO $$" + _MIGRATION.read_text(encoding="utf-8").rsplit("\nDO $$", 1)[1]
    # 付与と検査を同一 transaction に入れ、成功・失敗にかかわらず共有ロールへの付与を rollback。
    with pytest.raises(psycopg.errors.RaiseException, match=f"{role} must not touch"):
        with psycopg.connect(env.admin_dsn) as conn:
            conn.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE").format(
                    sql.Identifier(inherited_role), sql.Identifier(role)
                )
            )
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(env.migrator)))
            conn.execute(guard)
            # ガード削除時にも commit しない。
            conn.rollback()


@pytest.mark.parametrize(
    "grantee",
    [
        "PUBLIC",
        "teamagent_app",
        "teamagent_dashboard",
        "personal_memory_admin_reader",
        "personal_memory_retirer",
        "master",
    ],
)
@pytest.mark.parametrize("privilege", ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"])
def test_final_guard_rejects_non_definer_acl(env: PmDb, grantee: str, privilege: str) -> None:
    """master への直接付与は全表権限と区別し ACL で検出する（ACL 検査削除で赤）。"""
    import psycopg
    from psycopg import sql

    guard = "DO $$" + _MIGRATION.read_text(encoding="utf-8").rsplit("\nDO $$", 1)[1]
    target = (
        sql.SQL("PUBLIC")
        if grantee == "PUBLIC"
        else sql.Identifier(env.migrator if grantee == "master" else grantee)
    )
    # PUBLIC/app の CRUD は実効権限検査も検出する。master や TRUNCATE は ACL 検査だけが検出する。
    match = "ACL must grant" if grantee == "master" or privilege == "TRUNCATE" else "must not touch"
    with pytest.raises(psycopg.errors.RaiseException, match=match):
        with psycopg.connect(env.admin_dsn) as conn:
            conn.execute(
                sql.SQL("GRANT {} ON public.personal_memory_entries TO {}").format(
                    sql.SQL(privilege), target
                )
            )
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(env.migrator)))
            conn.execute(guard)
            conn.rollback()
