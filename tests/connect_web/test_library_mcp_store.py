"""Durable issuer tests. Real PostgreSQL is explicitly disposable and opt-in."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from time import time
from typing import Any

import pytest

from teamagent.connect_web.library_mcp_store import PgMCPStore

_RESOURCE = "https://connect.newstv.co.jp/mcp"
_CLIENT = "client_for_store_tests_123"
_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
_EMAIL = "member@vectorinc.co.jp"
_HD = "vectorinc.co.jp"
_MIGRATION = Path(__file__).resolve().parents[2] / "infra/migrations/0044_library_mcp_oauth.sql"
_TABLES = [
    "library_mcp_clients",
    "library_mcp_auth_requests",
    "library_mcp_grants",
    "library_mcp_credentials",
    "library_mcp_rate_buckets",
]


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _client(client_id: str = _CLIENT) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "redirect_uris": [_CALLBACK],
        "scope": "library:read",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Test client",
    }


class _FakePg:
    def __init__(self, value: Any) -> None:
        self.value = value
        self.calls: list[Any] = []
        self.committed = False

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[Any]:
        assert kwargs == {"app_role": "teamagent_app"}
        yield self
        self.committed = True

    def execute(self, query: Any, params: Any) -> Any:
        self.calls.append((query.as_string(), params))
        return self

    def fetchone(self) -> dict[str, Any]:
        return {"value": self.value}


def test_store_hashes_only_and_replay_status_commits() -> None:
    pg = _FakePg({"status": "replay"})
    store = PgMCPStore(pg, resource=_RESOURCE)
    assert store.rotate_refresh(_h("old"), _CLIENT, ["library:read"], _h("at"), _h("rt")) == {
        "status": "replay"
    }
    assert pg.committed
    query, params = pg.calls[0]
    assert '"library_mcp_rotate_refresh"' in query
    assert params[0] == bytes.fromhex(_h("old")) and params[0] != b"old"


@pytest.mark.parametrize("value", ["", "a" * 63, "G" * 64, "../token"])
def test_invalid_hash_never_reaches_database(value: str) -> None:
    pg = _FakePg(None)
    with pytest.raises(ValueError, match="hash"):
        PgMCPStore(pg, resource=_RESOURCE).get_access(value)
    assert not pg.calls


def test_scope_and_resource_fail_before_database() -> None:
    pg = _FakePg(None)
    store = PgMCPStore(pg, resource=_RESOURCE)
    with pytest.raises(ValueError, match="scope"):
        store.rotate_refresh(_h("old"), _CLIENT, ["library:write"], _h("at"), _h("rt"))
    with pytest.raises(ValueError, match="resource"):
        store.create_request(_h("tx"), _CLIENT, {"resource": "https://evil.example/mcp"})
    with pytest.raises(ValueError, match="public client"):
        store.register_client({**_client(), "client_secret": "synthetic-secret"})
    assert not pg.calls


class _DbPg:
    def __init__(self, dsn: str, database: str) -> None:
        self.dsn, self.database = dsn, database

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[Any]:
        import psycopg
        from psycopg.rows import dict_row

        assert kwargs == {"app_role": "teamagent_app"}
        with psycopg.connect(self.dsn, dbname=self.database, row_factory=dict_row) as conn:
            conn.execute("SET ROLE teamagent_app")
            yield conn


@pytest.fixture(scope="module")
def database(postgres_roles_lock: None) -> Iterator[_DbPg]:
    dsn = os.environ.get("TEAMAGENT_TEST_DB_DSN")
    if not dsn:
        pytest.skip("requires explicitly disposable TEAMAGENT_TEST_DB_DSN")
    import psycopg
    from psycopg import sql

    suffix = uuid.uuid4().hex[:12]
    db, migrator = "library_mcp_test_" + suffix, "library_mcp_migrator_" + suffix
    created_app = False
    with psycopg.connect(dsn, dbname="postgres", autocommit=True) as admin:
        created_app = not admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='teamagent_app'"
        ).fetchone()
        if created_app:
            admin.execute("CREATE ROLE teamagent_app NOLOGIN NOBYPASSRLS")
        existing_owner = bool(
            admin.execute("SELECT 1 FROM pg_roles WHERE rolname='library_mcp_definer'").fetchone()
        )
        admin.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN CREATEROLE NOBYPASSRLS").format(
                sql.Identifier(migrator)
            )
        )
        if existing_owner:
            admin.execute(
                sql.SQL(
                    "GRANT library_mcp_definer TO {} WITH ADMIN TRUE, INHERIT FALSE, SET TRUE"
                ).format(sql.Identifier(migrator))
            )
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(db), sql.Identifier(migrator)
            )
        )
    try:
        with psycopg.connect(dsn, dbname=db) as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(migrator)))
            assert conn.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False,)
            conn.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO teamagent_app"
            )
            conn.execute(_MIGRATION.read_text())
        yield _DbPg(dsn, db)
    finally:
        with psycopg.connect(dsn, dbname="postgres", autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db)))
            if not existing_owner:
                admin.execute("DROP ROLE library_mcp_definer")
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(migrator)))
            if created_app:
                admin.execute("DROP ROLE teamagent_app")


@pytest.fixture
def store(database: _DbPg) -> PgMCPStore:
    import psycopg
    from psycopg import sql

    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        conn.execute(
            sql.SQL("TRUNCATE {} CASCADE").format(
                sql.SQL(",").join(sql.Identifier("public", t) for t in _TABLES)
            )
        )
    result = PgMCPStore(database, resource=_RESOURCE)
    result.register_client(_client())
    return result


def _consent(store: PgMCPStore, suffix: str = "", hd: str | None = _HD) -> tuple[str, str]:
    tx, csrf, browser, code = (_h(x + suffix) for x in ("tx", "csrf", "browser", "code"))
    store.create_request(
        tx,
        _CLIENT,
        {
            "redirect_uri": _CALLBACK,
            "redirect_uri_provided_explicitly": True,
            "state": "client-state",
            "code_challenge": "A" * 43,
            "scopes": ["library:read"],
            "resource": _RESOURCE,
        },
    )
    assert store.bind_request(tx, csrf, None, None, browser)
    assert store.bind_request(tx, csrf, _EMAIL, hd, browser)
    grant = store.authorize_request(tx, csrf, _EMAIL, code, browser)
    assert grant is not None
    return code, grant["grant_id"]


def _issued(store: PgMCPStore) -> tuple[str, str, str]:
    code, grant = _consent(store)
    access, refresh = _h("access"), _h("refresh")
    issued = store.exchange_code(code, _CLIENT, access, refresh)
    assert issued["status"] == "ok" and issued["verified_hd"] == _HD
    assert 0 < issued["expires_at"] - time() <= 601
    assert 89 * 86400 < issued["refresh_expires_at"] - issued["expires_at"] < 91 * 86400
    return access, refresh, grant


def test_restart_rotation_and_replay_revoke_every_access(
    store: PgMCPStore, database: _DbPg
) -> None:
    access, refresh, _ = _issued(store)
    restarted = PgMCPStore(database, resource=_RESOURCE)
    assert restarted.get_access(access)["subject"] == _EMAIL  # type: ignore[index]
    rotated = restarted.rotate_refresh(
        refresh, _CLIENT, ["library:read"], _h("access2"), _h("refresh2")
    )
    assert rotated["status"] == "ok"
    assert restarted.get_refresh(refresh, _CLIENT)["consumed"]  # type: ignore[index]
    assert (
        restarted.rotate_refresh(refresh, _CLIENT, ["library:read"], _h("access3"), _h("refresh3"))[
            "status"
        ]
        == "replay"
    )
    assert restarted.get_access(access) is None and restarted.get_access(_h("access2")) is None
    assert restarted.get_refresh(_h("refresh2"), _CLIENT) is None


def test_browser_and_verified_identity_cannot_be_rebound(store: PgMCPStore) -> None:
    tx, csrf, browser = _h("tx"), _h("csrf"), _h("browser")
    store.create_request(tx, _CLIENT, {"redirect_uri": _CALLBACK, "code_challenge": "A" * 43})
    assert store.bind_request(tx, csrf, None, None, browser)
    assert not store.bind_request(tx, csrf, _EMAIL, _HD, _h("other-browser"))
    assert store.bind_request(tx, csrf, _EMAIL, None, browser)
    assert not store.bind_request(tx, csrf, _EMAIL, _HD, browser)
    assert not store.bind_request(tx, csrf, "other@vectorinc.co.jp", None, browser)
    assert store.authorize_request(tx, csrf, _EMAIL, _h("code"), browser)["verified_hd"] is None  # type: ignore[index]
    assert store.authorize_request(tx, csrf, _EMAIL, _h("code2"), browser) is None


def test_code_exchange_has_one_winner(store: PgMCPStore, database: _DbPg) -> None:
    code, _ = _consent(store)

    def exchange(n: int) -> str:
        other = PgMCPStore(database, resource=_RESOURCE)
        return str(
            other.exchange_code(code, _CLIENT, _h(f"access{n}"), _h(f"refresh{n}"))["status"]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(exchange, range(2))) == ["invalid_grant", "ok"]


def test_parallel_refresh_has_one_chain_then_replay_revokes(
    store: PgMCPStore, database: _DbPg
) -> None:
    _, refresh, _ = _issued(store)

    def rotate(n: int) -> str:
        return str(
            PgMCPStore(database, resource=_RESOURCE).rotate_refresh(
                refresh, _CLIENT, ["library:read"], _h(f"next-access{n}"), _h(f"next-refresh{n}")
            )["status"]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(rotate, range(2))) == ["ok", "replay"]
    assert all(store.get_access(_h(f"next-access{n}")) is None for n in range(2))


def test_revocation_is_owner_bound_and_includes_expired_access(
    store: PgMCPStore, database: _DbPg
) -> None:
    import psycopg

    access, refresh, grant = _issued(store)
    assert store.list_grants("other@vectorinc.co.jp") == []
    assert not store.revoke_grant(grant, "other@vectorinc.co.jp")
    store.revoke_token(refresh, "wrong-client")
    assert store.get_access(access) is not None
    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        conn.execute(
            "UPDATE public.library_mcp_credentials SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s",
            (bytes.fromhex(access),),
        )
    assert store.get_access(access) is None
    store.cleanup()
    store.revoke_token(access, _CLIENT)
    assert store.get_refresh(refresh, _CLIENT) is None and store.list_grants(_EMAIL) == []


def test_acl_rls_and_private_helpers(database: _DbPg) -> None:
    import psycopg

    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        rows = conn.execute(
            "SELECT relrowsecurity,relforcerowsecurity,pg_get_userbyid(relowner) FROM pg_class WHERE relname=ANY(%s)",
            (_TABLES,),
        ).fetchall()
        assert len(rows) == len(_TABLES)
        assert all(row == (True, True, "library_mcp_definer") for row in rows)
        assert conn.execute(
            "SELECT rolcanlogin,rolbypassrls,rolsuper,rolinherit FROM pg_roles WHERE rolname='library_mcp_definer'"
        ).fetchone() == (False, False, False, False)
        for table in _TABLES:
            assert not conn.execute(
                "SELECT has_table_privilege('teamagent_app',%s,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
                ("public." + table,),
            ).fetchone()[0]
        assert not conn.execute(
            "SELECT has_function_privilege('teamagent_app','public.library_mcp_credential_record(bytea,text,text,text,boolean)','EXECUTE')"
        ).fetchone()[0]
        conn.execute("SET ROLE teamagent_app")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM public.library_mcp_credentials")


def test_rate_is_atomic_and_cleanup_is_bounded(store: PgMCPStore, database: _DbPg) -> None:
    import psycopg

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(
                lambda _: PgMCPStore(database, resource=_RESOURCE).rate_limit(_h("rate"), 3, 3600),
                range(12),
            )
        )
    assert sum(outcomes) == 3
    for n in range(8):
        store.create_request(
            _h(f"cleanup{n}"), _CLIENT, {"redirect_uri": _CALLBACK, "code_challenge": "A" * 43}
        )
    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        conn.execute(
            "UPDATE public.library_mcp_auth_requests SET expires_at=clock_timestamp()-interval '1 second'"
        )
    assert store.cleanup(3) == 3
    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        assert (
            conn.execute("SELECT count(*) FROM public.library_mcp_auth_requests").fetchone()[0] == 5
        )


def test_denial_consumes_request_without_google_identity(store: PgMCPStore) -> None:
    tx, csrf, browser = _h("denied-tx"), _h("denied-csrf"), _h("denied-browser")
    store.create_request(tx, _CLIENT, {"redirect_uri": _CALLBACK, "code_challenge": "A" * 43})
    assert store.bind_request(tx, csrf, None, None, browser)
    assert not store.cancel_request(tx, csrf, _h("another-browser"))
    assert not store.cancel_request(tx, _h("another-csrf"), browser)
    assert store.cancel_request(tx, csrf, browser)
    assert store.get_request(tx) is None
    assert not store.cancel_request(tx, csrf, browser)
    assert store.authorize_request(tx, csrf, _EMAIL, _h("denied-code"), browser) is None
    assert store.list_grants(_EMAIL) == []


def test_refresh_slides_inactivity_without_absolute_weekly_expiry(
    store: PgMCPStore, database: _DbPg
) -> None:
    import psycopg

    _, refresh, grant = _issued(store)
    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        conn.execute(
            "UPDATE public.library_mcp_grants SET created_at=clock_timestamp()-interval '180 days', expires_at=clock_timestamp()+interval '1 minute' WHERE grant_id=%s",
            (grant,),
        )
        conn.execute(
            "UPDATE public.library_mcp_credentials SET expires_at=clock_timestamp()+interval '1 minute' WHERE token_hash=%s",
            (bytes.fromhex(refresh),),
        )
    issued = store.rotate_refresh(
        refresh, _CLIENT, ["library:read"], _h("sliding-at"), _h("sliding-rt")
    )
    assert issued["status"] == "ok" and issued["refresh_expires_at"] - time() > 89 * 86400
    assert store.get_access(_h("sliding-at"))["subject"] == _EMAIL


def test_current_client_disable_and_resource_mismatch_fail_closed(
    store: PgMCPStore, database: _DbPg
) -> None:
    import psycopg

    access, refresh, _ = _issued(store)
    wrong_resource = PgMCPStore(database, resource="https://other.example/mcp")
    assert wrong_resource.get_access(access) is None
    assert (
        wrong_resource.rotate_refresh(
            refresh, _CLIENT, ["library:read"], _h("wrong-at"), _h("wrong-rt")
        )["status"]
        == "invalid_grant"
    )
    assert store.get_access(access) is not None
    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        conn.execute(
            "UPDATE public.library_mcp_clients SET disabled_at=clock_timestamp() WHERE client_id=%s",
            (_CLIENT,),
        )
    assert store.get_client(_CLIENT) is None
    assert store.get_access(access) is None and store.get_refresh(refresh, _CLIENT) is None
    assert (
        store.rotate_refresh(
            refresh, _CLIENT, ["library:read"], _h("disabled-at"), _h("disabled-rt")
        )["status"]
        == "invalid_grant"
    )


def test_revoke_racing_refresh_leaves_no_usable_successor(
    store: PgMCPStore, database: _DbPg
) -> None:
    access, refresh, _ = _issued(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rotate = pool.submit(
            PgMCPStore(database, resource=_RESOURCE).rotate_refresh,
            refresh,
            _CLIENT,
            ["library:read"],
            _h("racing-at"),
            _h("racing-rt"),
        )
        revoke = pool.submit(
            PgMCPStore(database, resource=_RESOURCE).revoke_token, refresh, _CLIENT
        )
        assert rotate.result()["status"] in {"ok", "invalid_grant"}
        revoke.result()
    assert store.get_access(access) is None and store.get_access(_h("racing-at")) is None
    assert store.get_refresh(_h("racing-rt"), _CLIENT) is None


def test_public_helpers_and_temp_search_path_cannot_bypass_store(
    store: PgMCPStore, database: _DbPg
) -> None:
    import psycopg

    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        assert not conn.execute(
            "SELECT has_function_privilege('teamagent_app','public.library_mcp_exchange(bytea,text,text,text[],bytea,bytea,text)','EXECUTE')"
        ).fetchone()[0]
        assert conn.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE n.nspname='public' AND left(p.proname,12)='library_mcp_' AND a.grantee=0 AND a.privilege_type='EXECUTE'"
        ).fetchone() == (0,)
        conn.execute("CREATE TEMP TABLE library_mcp_clients(client_id text,client_name text)")
        conn.execute("INSERT INTO library_mcp_clients VALUES(%s,'attacker shadow')", (_CLIENT,))
        conn.execute("SET search_path=pg_temp,public")
        conn.execute("SET ROLE teamagent_app")
        row = conn.execute(
            "SELECT public.library_mcp_get_client(%s,%s)", (_CLIENT, _RESOURCE)
        ).fetchone()[0]
        assert row["client_name"] == "Test client"


def test_cleanup_reclaims_unused_clients_within_total_budget(
    store: PgMCPStore, database: _DbPg
) -> None:
    import psycopg

    with psycopg.connect(database.dsn, dbname=database.database) as conn:
        conn.execute(
            "UPDATE public.library_mcp_clients SET created_at=clock_timestamp()-interval '2 days'"
        )
    assert store.cleanup(1) == 1
    assert store.get_client(_CLIENT) is None


def test_owner_connections_keep_each_selected_client_callback(store: PgMCPStore) -> None:
    _issued(store)
    client_id = "chatgpt_client_for_test_123"
    callback = "https://chatgpt.com/connector/oauth/test-callback"
    store.register_client({**_client(client_id), "redirect_uris": [callback]})
    tx, csrf, browser, code = (
        _h("chatgpt-" + value) for value in ("tx", "csrf", "browser", "code")
    )
    store.create_request(tx, client_id, {"redirect_uri": callback, "code_challenge": "A" * 43})
    assert store.bind_request(tx, csrf, _EMAIL, _HD, browser)
    assert store.authorize_request(tx, csrf, _EMAIL, code, browser)
    assert (
        store.exchange_code(code, client_id, _h("chatgpt-at"), _h("chatgpt-rt"))["status"] == "ok"
    )
    grants = store.list_grants(_EMAIL)
    assert {row["redirect_uri"] for row in grants} == {_CALLBACK, callback}


def test_real_sdk_http_with_postgres_survives_restart_refreshes_and_revokes(
    store: PgMCPStore, database: _DbPg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No issuer-store fake: exercise its actual SQL projections through the SDK."""
    import psycopg
    from fastapi.testclient import TestClient

    from teamagent.connect_web.app import create_app
    from tests.connect_web.test_library_mcp_spike import (
        _BASE,
        _code,
        _token_data,
        _verifier_mcp,
    )
    from tests.connect_web.test_search_routes import _config

    monkeypatch.setenv("CONNECT_MCP_ENABLED", "true")
    monkeypatch.setenv("CONNECT_MCP_PUBLIC_BASE_URL", _BASE)
    monkeypatch.delenv("CONNECT_MCP_SPIKE_REDIRECT_ALLOW", raising=False)

    def app() -> Any:
        return create_app(search_config=_config(), search_verifier=_verifier_mcp, admin_pg=database)

    with TestClient(app(), base_url=_BASE, follow_redirects=False) as first:
        client_id, code = _code(first)
        response = first.post("/token", data=_token_data(client_id, code))
        assert response.status_code == 200, response.text
        tokens = response.json()
        assert tokens["expires_in"] == 600 and tokens["scope"] == "library:read"
        assert tokens["refresh_token"] != tokens["access_token"]

    with TestClient(app(), base_url=_BASE, follow_redirects=False) as restarted:
        assert (
            restarted.get(
                "/mcp", headers={"Authorization": "Bearer " + tokens["access_token"]}
            ).status_code
            == 405
        )
        refreshed = restarted.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "resource": _BASE + "/mcp",
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        fresh = refreshed.json()
        assert fresh["refresh_token"] != tokens["refresh_token"]
        assert (
            restarted.get(
                "/mcp", headers={"Authorization": "Bearer " + fresh["access_token"]}
            ).status_code
            == 405
        )
        with psycopg.connect(database.dsn, dbname=database.database) as conn:
            conn.execute(
                "UPDATE public.library_mcp_credentials SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s",
                (bytes.fromhex(_h(tokens["access_token"])),),
            )
        revoked = restarted.post(
            "/revoke", data={"client_id": client_id, "token": tokens["access_token"]}
        )
        assert revoked.status_code == 200, revoked.text
        assert (
            restarted.get(
                "/mcp", headers={"Authorization": "Bearer " + fresh["access_token"]}
            ).status_code
            == 401
        )
        denied = restarted.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": fresh["refresh_token"],
                "resource": _BASE + "/mcp",
            },
        )
        assert denied.status_code == 400 and denied.json()["error"] == "invalid_grant"

    with TestClient(app(), base_url=_BASE, follow_redirects=False) as after_revoke:
        assert (
            after_revoke.get(
                "/mcp", headers={"Authorization": "Bearer " + fresh["access_token"]}
            ).status_code
            == 401
        )
