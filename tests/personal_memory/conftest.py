"""personal_memory の実 PostgreSQL 試験で共有する使い捨て DB（migration 0029 を本番に似せた役で流したもの）。

``TEAMAGENT_TEST_DB_DSN`` が無ければ、この fixture を使う試験は skip する。
superuser では migration を流さない（SECURITY DEFINER と RLS が偽の緑になるため）。本番の master（teamagent）に
似せた「CREATEROLE・BYPASSRLS・非 superuser」の migrator を試験モジュールごとに作り、使い捨ての DATABASE に流す。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

_DB_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_MIGRATION = Path(__file__).resolve().parents[2] / "infra/migrations/0029_personal_memory.sql"
PM_ROLES = (
    "personal_memory_app",
    "personal_memory_definer",
    "personal_memory_admin_reader",
    "personal_memory_retirer",
)


@dataclass(frozen=True)
class PmDb:
    migrator_dsn: str
    admin_dsn: str
    dbname: str
    migrator: str


def _dsn_with(dsn: str, **overrides: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(overrides)
    return make_conninfo(**params)


@pytest.fixture(scope="module")
def pm_db() -> Iterator[PmDb]:
    import psycopg
    from psycopg import sql

    if _DB_DSN is None:
        pytest.skip("disposable PostgreSQL validation requires TEAMAGENT_TEST_DB_DSN")
    suffix = uuid.uuid4().hex[:12]
    dbname = f"pm0029_{suffix}"
    migrator = f"pm0029_master_{suffix}"
    password = uuid.uuid4().hex
    with psycopg.connect(_DB_DSN, autocommit=True) as su:
        su.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} CREATEROLE BYPASSRLS NOSUPERUSER NOCREATEDB INHERIT"
            ).format(sql.Identifier(migrator), sql.Literal(password))
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
        # 本番の master は teamagent_app へ SET ROLE できる（0002）。その関係も再現する
        su.execute(sql.SQL("GRANT teamagent_app TO {}").format(sql.Identifier(migrator)))
        # 以前の試験で personal_memory_* が残っていれば、本番で「作成者に付く ADMIN」と同じ状態にする
        for role in PM_ROLES:
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
    with psycopg.connect(admin_dsn, autocommit=True) as su:
        # PG15+ の public は pg_database_owner 所有。念のため migrator に CREATE を付ける
        su.execute(sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(sql.Identifier(migrator)))
    with psycopg.connect(migrator_dsn) as conn:
        with conn.cursor() as cur:
            # 0002 の既定権限を再現（新しい表に teamagent_app の S/I/U/D が自動で付く）
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO teamagent_app"
            )
            cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        conn.commit()
    try:
        yield PmDb(migrator_dsn=migrator_dsn, admin_dsn=admin_dsn, dbname=dbname, migrator=migrator)
    finally:
        with psycopg.connect(_DB_DSN, autocommit=True) as su:
            su.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
            su.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(migrator)))
