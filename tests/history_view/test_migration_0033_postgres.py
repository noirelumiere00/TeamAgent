"""0033_history_view_audit の実 PostgreSQL 回帰テスト（質問と回答の履歴・管理者閲覧の監査）。

``TEAMAGENT_TEST_DB_DSN`` が明示された使い捨ての PostgreSQL 16 だけで実行する（CI は postgres:16）。
0029 と同じく superuser では流さず、本番の master に似せた「CREATEROLE・全表読み書き権限・非 superuser」の
migrator で流す（tests/history_view/conftest.py）。

固定すること:
- 監査の表は FORCE RLS・所有者は history_view_definer。アプリ・監査ロールは直接読めない・書けない（RDS master の全表権限は対象外）
- 監査を書けるのは関数 1 本だけで、EXECUTE は history_view_auditor だけ
- 関数は形の不正を拒み、そのときは何も書かない。対象は U… そのものではなく SHA-256 先頭 16 hex
- adapters/history_audit は管理者でなければ DB に触れずに拒否し、通ったときだけ 1 行書く
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.history_view.conftest import HV_ROLES, HvDb

_DB_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_MIGRATION = Path(__file__).resolve().parents[2] / "infra/migrations/0033_history_view_audit.sql"
_FN = "public.history_view_record(text,text,text,integer,text)"
_TEAM = "T0123ABCDE"
_USER = "U0AAAAAAA1"

pytestmark = pytest.mark.skipif(
    _DB_DSN is None,
    reason="disposable PostgreSQL validation requires TEAMAGENT_TEST_DB_DSN",
)


def _sha16(principal: str) -> str:
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()[:16]


@pytest.fixture(scope="module")
def env(hv_db: HvDb) -> HvDb:
    return hv_db


def _connect(env: HvDb) -> Any:
    import psycopg

    return psycopg.connect(env.migrator_dsn)


def _audit_rows(env: HvDb) -> list[tuple[Any, ...]]:
    import psycopg

    with psycopg.connect(env.admin_dsn) as su, su.cursor() as cur:
        cur.execute(
            "SELECT actor_email, target_sha16, days, reason_code FROM history_view_audit "
            "ORDER BY occurred_at, audit_id"
        )
        return list(cur.fetchall())


def test_table_is_forced_rls_and_owned_by_definer(env: HvDb) -> None:
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) "
            "FROM pg_class WHERE relname = 'history_view_audit' AND relkind = 'r'"
        )
        assert cur.fetchone() == (True, True, "history_view_definer")


def test_roles_are_nologin_nobypassrls(env: HvDb) -> None:
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper, rolinherit FROM pg_roles "
            "WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(HV_ROLES),),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == sorted(HV_ROLES)
    assert all(r[1:] == (False, False, False, False) for r in rows)


@pytest.mark.parametrize("role", ["teamagent_app", "teamagent_dashboard", "history_view_auditor"])
@pytest.mark.parametrize("privilege", ["SELECT", "INSERT", "UPDATE", "DELETE"])
def test_no_role_touches_the_table_directly(env: HvDb, role: str, privilege: str) -> None:
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone() is None:
            pytest.skip(f"{role} が無い環境")
        cur.execute(
            "SELECT has_table_privilege(%s, 'public.history_view_audit', %s)", (role, privilege)
        )
        assert cur.fetchone()[0] is False


def test_master_has_rds_table_privileges_but_cannot_become_definer(env: HvDb) -> None:
    import psycopg

    with _connect(env) as conn, conn.cursor() as cur:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            cur.execute(
                "SELECT has_table_privilege(current_user, 'public.history_view_audit', %s)",
                (privilege,),
            )
            assert cur.fetchone()[0] is True
        for role in HV_ROLES:
            cur.execute("SELECT pg_has_role(current_user, %s, 'USAGE')", (role,))
            assert cur.fetchone()[0] is False
        cur.execute("SELECT pg_has_role(current_user, 'history_view_definer', 'SET')")
        assert cur.fetchone()[0] is False
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SET ROLE history_view_definer")


def test_function_privileges_and_definition(env: HvDb) -> None:
    with _connect(env) as conn, conn.cursor() as cur:
        expected = {
            "public": False,
            "teamagent_app": False,
            "teamagent_dashboard": False,
            "history_view_auditor": True,
            env.migrator: False,  # INHERIT FALSE なので master のままでは実行できない
        }
        for role, want in expected.items():
            cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, _FN))
            assert cur.fetchone()[0] is want, role
        cur.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig "
            "FROM pg_proc p WHERE p.proname = 'history_view_record'"
        )
        assert cur.fetchone() == (True, "history_view_definer", ["search_path=pg_catalog, pg_temp"])


def test_auditor_records_one_row_with_hashed_target(env: HvDb) -> None:
    before = len(_audit_rows(env))
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE history_view_auditor")
        cur.execute(
            f"SELECT {_FN.split('(')[0]}(%s, %s, %s, %s, %s)",
            ("Owner@Vectorinc.co.jp", _TEAM, _USER, 30, "admin_page"),
        )
        assert cur.fetchone()[0] is True
        conn.commit()
        # 監査ロールでも表は読めない
        import psycopg

        cur.execute("SET ROLE history_view_auditor")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT count(*) FROM history_view_audit")
    rows = _audit_rows(env)
    assert len(rows) == before + 1
    assert rows[-1] == ("owner@vectorinc.co.jp", _sha16(f"{_TEAM}:{_USER}"), 30, "admin_page")
    assert _USER not in repr(rows)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("not-an-email", _TEAM, _USER, 30, "admin_page"), "hv_bad_admin"),
        (("a@vectorinc.co.jp", "team", _USER, 30, "admin_page"), "hv_bad_principal"),
        (("a@vectorinc.co.jp", _TEAM, "someone@example.com", 30, "admin_page"), "hv_bad_principal"),
        (("a@vectorinc.co.jp", _TEAM, _USER, 0, "admin_page"), "hv_bad_days"),
        (("a@vectorinc.co.jp", _TEAM, _USER, 31, "admin_page"), "hv_bad_days"),
        (("a@vectorinc.co.jp", _TEAM, _USER, 30, "Bad Reason"), "hv_bad_reason"),
    ],
)
def test_bad_arguments_are_rejected_without_writing(
    env: HvDb, args: tuple[Any, ...], code: str
) -> None:
    import psycopg

    before = len(_audit_rows(env))
    with _connect(env) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE history_view_auditor")
        with pytest.raises(psycopg.errors.InvalidParameterValue, match=code):
            cur.execute("SELECT public.history_view_record(%s, %s, %s, %s, %s)", args)
    assert len(_audit_rows(env)) == before


def test_migration_avoids_on_conflict_and_returning() -> None:
    text = _MIGRATION.read_text(encoding="utf-8").upper()
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    assert "ON CONFLICT" not in body
    assert "RETURNING " not in body
    assert "CURRENT_SETTING('APP.USER_ROLE'" not in body


# ── adapters/history_audit を実 DB で ─────────────────────────────────────────


def _audit_adapter(env: HvDb) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    from teamagent.adapters.history_audit import HistoryAudit

    @contextmanager
    def factory(role: str) -> Any:
        # 本番の PgVectorClient.connection(app_role=…) と同じく SET ROLE してから使う。
        # 行の形も本番（_connect_pg の row_factory=dict_row）に合わせる（tuple だと row[0] の誤りを見逃す）。
        with psycopg.connect(env.migrator_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET ROLE {role}")
            yield conn
            conn.commit()

    return HistoryAudit(connection_factory=factory)


def test_adapter_records_only_for_the_admin(env: HvDb, monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.history_audit import HistoryAuditError

    user = "U0ADAPTER1"
    audit = _audit_adapter(env)
    monkeypatch.setenv("HISTORY_ADMIN_EMAILS", "owner@vectorinc.co.jp")
    before = len(_audit_rows(env))
    with pytest.raises(HistoryAuditError) as denied:
        audit.record(
            admin_email="other@vectorinc.co.jp", team_id=_TEAM, slack_user_id=user, days=30
        )
    assert denied.value.code == "not_admin"
    with pytest.raises(HistoryAuditError) as bad:
        audit.record(
            admin_email="owner@vectorinc.co.jp", team_id=_TEAM, slack_user_id=user, days=31
        )
    assert bad.value.code == "bad_argument"
    assert len(_audit_rows(env)) == before  # 拒否した 2 回は DB に触れない
    audit.record(admin_email="Owner@vectorinc.co.jp", team_id=_TEAM, slack_user_id=user, days=30)
    rows = _audit_rows(env)
    assert len(rows) == before + 1
    assert rows[-1] == ("owner@vectorinc.co.jp", _sha16(f"{_TEAM}:{user}"), 30, "admin_page")


def test_master_privilege_guard_fails_before_fix(env: HvDb) -> None:
    """旧 SQL の最後の検査は RDS の全表権限だけで必ず失敗する。"""
    import psycopg

    with _connect(env) as conn:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match="current_user must not touch history_view_audit without the function",
        ):
            conn.execute(
                "DO $$ BEGIN IF has_table_privilege(current_user, 'public.history_view_audit', 'SELECT') OR has_table_privilege(current_user, 'public.history_view_audit', 'INSERT') "
                "THEN RAISE EXCEPTION 'current_user must not touch history_view_audit without the function'; "
                "END IF; END $$;"
            )


@pytest.mark.parametrize("role", ["teamagent_app", "teamagent_dashboard", "history_view_auditor"])
@pytest.mark.parametrize("inherited_role", ["pg_read_all_data", "pg_write_all_data"])
def test_final_guard_rejects_inherited_table_access(
    env: HvDb, role: str, inherited_role: str
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
    "grantee", ["PUBLIC", "teamagent_app", "teamagent_dashboard", "history_view_auditor", "master"]
)
@pytest.mark.parametrize("privilege", ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"])
def test_final_guard_rejects_non_definer_acl(env: HvDb, grantee: str, privilege: str) -> None:
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
                sql.SQL("GRANT {} ON public.history_view_audit TO {}").format(
                    sql.SQL(privilege), target
                )
            )
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(env.migrator)))
            conn.execute(guard)
            conn.rollback()
