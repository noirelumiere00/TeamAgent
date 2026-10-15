"""history_view_audit の実 PostgreSQL 試験で共有する使い捨て DB（migration 0033 を本番に似せた役で流したもの）。

tests/personal_memory/conftest.py（0029）の複製。

``TEAMAGENT_TEST_DB_DSN`` が無ければ、この fixture を使う試験は skip する。
superuser では migration を流さない（SECURITY DEFINER と RLS が偽の緑になるため）。本番の master（teamagent）に
似せた「CREATEROLE・NOBYPASSRLS・非 superuser」の migrator を試験モジュールごとに作り、使い捨ての DATABASE に流す。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.migration_postgres import apply_with_acl_evidence

_DB_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_MIGRATION = Path(__file__).resolve().parents[2] / "infra/migrations/0033_history_view_audit.sql"
HV_ROLES = (
    "history_view_definer",
    "history_view_auditor",
)


@dataclass(frozen=True)
class HvDb:
    migrator_dsn: str
    admin_dsn: str
    dbname: str
    migrator: str
    bypassrls: bool


def _dsn_with(dsn: str, **overrides: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(overrides)
    return make_conninfo(**params)


@pytest.fixture(scope="module", params=[False, True], ids=["production", "bypassrls"])
def hv_db(request: pytest.FixtureRequest) -> Iterator[HvDb]:
    import psycopg
    from psycopg import sql

    if _DB_DSN is None:
        pytest.skip("disposable PostgreSQL validation requires TEAMAGENT_TEST_DB_DSN")
    bypassrls = request.param
    suffix = uuid.uuid4().hex[:12]
    dbname = f"hv0033_{suffix}"
    migrator = f"hv0033_master_{suffix}"
    password = uuid.uuid4().hex
    rds_surrogate = f"hv0033_rds_{suffix}"
    with psycopg.connect(_DB_DSN, autocommit=True) as su:
        su.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} CREATEROLE {} NOSUPERUSER NOCREATEDB INHERIT"
            ).format(
                sql.Identifier(migrator),
                sql.Literal(password),
                sql.SQL("BYPASSRLS" if bypassrls else "NOBYPASSRLS"),
            )
        )
        for role in ("teamagent_app", "teamagent_dashboard"):
            su.execute(
                sql.SQL(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {}) THEN "
                    "EXECUTE {}; END IF; END $$"
                ).format(
                    sql.Literal(role),
                    sql.Literal(f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS"),
                )
            )
        # RDS master の全表権限を再現する（AWS 固有の管理権限は再現しない）。
        su.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN NOBYPASSRLS NOSUPERUSER").format(
                sql.Identifier(rds_surrogate)
            )
        )
        su.execute(
            sql.SQL(
                "GRANT pg_read_all_data, pg_write_all_data TO {} WITH INHERIT TRUE, SET TRUE"
            ).format(sql.Identifier(rds_surrogate))
        )
        for role in ("teamagent_app", "teamagent_dashboard", rds_surrogate):
            su.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(
                    sql.Identifier(role), sql.Identifier(migrator)
                )
            )
        # 以前の試験で history_view_* が残っていれば、本番で「作成者に付く ADMIN」と同じ状態にする
        for role in HV_ROLES:
            su.execute(
                sql.SQL(
                    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {}) THEN "
                    "EXECUTE {}; END IF; END $$"
                ).format(
                    sql.Literal(role),
                    # PG16 で作成者に自動で付く grant と同じ形（ADMIN だけ・受け継がない・SET できない）
                    sql.Literal(
                        f'GRANT {role} TO "{migrator}" WITH ADMIN OPTION, INHERIT FALSE, SET FALSE'
                    ),
                )
            )
        su.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(dbname), sql.Identifier(migrator)
            )
        )
    admin_dsn = _dsn_with(_DB_DSN, dbname=dbname)
    migrator_dsn = _dsn_with(_DB_DSN, dbname=dbname, user=migrator, password=password)
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as su:
            # PG15+ の public は pg_database_owner 所有。念のため migrator に CREATE を付ける
            su.execute(
                sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(sql.Identifier(migrator))
            )
        with psycopg.connect(migrator_dsn) as conn:
            with conn.cursor() as cur:
                # 0002 の既定権限を再現（新しい表に teamagent_app の S/I/U/D が自動で付く）
                cur.execute(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO teamagent_app"
                )
                cur.execute(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO teamagent_app"
                )
                cur.execute(
                    "SELECT rolsuper, rolcreaterole, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
                assert cur.fetchone() == (False, True, bypassrls)
                for role in ("teamagent_app", "teamagent_dashboard", rds_surrogate):
                    cur.execute(
                        "SELECT pg_has_role(current_user, %s, 'USAGE'), "
                        "pg_has_role(current_user, %s, 'SET')",
                        (role, role),
                    )
                    assert cur.fetchone() == (True, True), role
                for role in ("pg_read_all_data", "pg_write_all_data"):
                    cur.execute("SELECT pg_has_role(current_user, %s, 'USAGE')", (role,))
                    assert cur.fetchone() == (True,), role
                apply_with_acl_evidence(
                    conn, _MIGRATION.read_text(encoding="utf-8"), ["history_view_audit"]
                )
            conn.commit()
        yield HvDb(
            migrator_dsn=migrator_dsn,
            admin_dsn=admin_dsn,
            dbname=dbname,
            migrator=migrator,
            bypassrls=bypassrls,
        )
    finally:
        with psycopg.connect(_DB_DSN, autocommit=True) as su:
            su.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
            su.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(migrator)))
            su.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(rds_surrogate)))
