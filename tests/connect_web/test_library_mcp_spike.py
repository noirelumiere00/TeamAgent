"""Exercise durable OAuth HTTP boundaries through the real SDK without external services."""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import structlog
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from teamagent.connect_web import app as connect_app
from teamagent.connect_web.app import RedactMCPAccessLog, _safe_next, create_app
from teamagent.connect_web.library_mcp_spike import CONSENT_PATH
from teamagent.dashboard.auth import make_session
from tests.connect_web.mcp_store_fake import FakePgMCPStore, patch_store
from tests.connect_web.test_search_routes import (
    _EMAIL,
    _SECRET,
    _auth_cookie,
    _config,
)

_BASE = "https://connect.example.com"
_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
_VERIFIER = "v" * 43
_CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(_VERIFIER.encode()).digest()).decode().rstrip("=")
)
_SPIKE_PATHS = (
    "/mcp",
    "/mcp/",
    "/authorize",
    "/token",
    "/register",
    CONSENT_PATH,
    "/mcp-auth/verify",
    "/mcp-auth/connections",
    "/mcp/setup",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> FakePgMCPStore:
    monkeypatch.setenv("CONNECT_MCP_ENABLED", "true")
    monkeypatch.setenv("CONNECT_MCP_PUBLIC_BASE_URL", _BASE)
    monkeypatch.delenv("CONNECT_MCP_SPIKE_REDIRECT_ALLOW", raising=False)
    return patch_store(monkeypatch, resource=_BASE + "/mcp")


def _verifier_mcp(credential: str, client_id: str) -> dict[str, Any]:
    return {"email": _EMAIL, "email_verified": True, "hd": "vectorinc.co.jp", "nonce": credential}


def _app() -> Any:
    return create_app(search_config=_config(), search_verifier=_verifier_mcp, admin_pg=object())


@pytest.fixture
def client(enabled: None) -> Iterator[TestClient]:
    with TestClient(_app(), base_url=_BASE, follow_redirects=False) as client:
        yield client


def _registration(callback: str = _CALLBACK) -> dict[str, Any]:
    return {
        "redirect_uris": [callback],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "library:read",
    }


def _params(client_id: str, **overrides: str) -> dict[str, str]:
    return {
        "client_id": client_id,
        "redirect_uri": _CALLBACK,
        "response_type": "code",
        "code_challenge": _CHALLENGE,
        "code_challenge_method": "S256",
        "scope": "library:read",
        "state": "private-state",
        "resource": _BASE + "/mcp",
        **overrides,
    }


def _start(client: TestClient) -> tuple[str, str]:
    registration = client.post("/register", json=_registration())
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]
    authorize = client.get("/authorize", params=_params(client_id))
    assert authorize.status_code == 302
    return client_id, authorize.headers["location"]


def _consent_form(client: TestClient, target: str, *, service: str = "Claude") -> str:
    client.cookies.update(_auth_cookie())
    page = client.get(target)
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "same-origin"
    soup = BeautifulSoup(page.text, "html.parser")
    google = soup.select_one("#g_id_onload")
    assert google is not None, "Even an existing search cookie requires fresh Google verification"
    csrf = str(soup.select_one('input[name="csrf"]')["value"])
    tx = parse_qs(urlsplit(target).query)["tx"][0]
    verified = client.post(
        "/mcp-auth/verify",
        params={"tx": tx},
        data={"csrf": csrf, "credential": str(google["data-nonce"])},
        headers={"Origin": _BASE},
    )
    assert verified.status_code == 303, verified.text
    page = client.get(target)
    assert page.status_code == 200
    soup = BeautifulSoup(page.text, "html.parser")
    assert soup.find("button", string=f"{service} に読み取り接続を許可")
    assert _EMAIL not in page.text
    assert "自動更新" in page.text and "広告費" in page.text
    assert "この試作" not in page.text
    return str(soup.select_one('input[name="csrf"]')["value"])


def _code(client: TestClient) -> tuple[str, str]:
    client_id, target = _start(client)
    csrf = _consent_form(client, target)
    consent = client.post(target, data={"csrf": csrf}, headers={"Origin": _BASE})
    assert consent.status_code == 303
    callback = urlsplit(consent.headers["location"])
    assert callback.hostname == "claude.ai" and callback.path == "/api/mcp/auth_callback"
    query = parse_qs(callback.query)
    assert query["state"] == ["private-state"]
    assert client.post(target, data={"csrf": csrf}, headers={"Origin": _BASE}).status_code == 400
    return client_id, query["code"][0]


def _token_data(client_id: str, code: str, **overrides: str) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": _CALLBACK,
        "code_verifier": _VERIFIER,
        "resource": _BASE + "/mcp",
        **overrides,
    }


def _token(client: TestClient) -> tuple[str, str, str]:
    client_id, code = _code(client)
    response = client.post("/token", data=_token_data(client_id, code))
    assert response.status_code == 200, response.text
    assert response.json()["refresh_token"]
    assert response.json()["expires_in"] == 600
    return response.json()["access_token"], client_id, code


def test_off_has_no_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_MCP_ENABLED", raising=False)
    monkeypatch.delenv("CONNECT_MCP_PUBLIC_BASE_URL", raising=False)
    app = _app()
    assert not any(getattr(route, "path", "") in _SPIKE_PATHS for route in app.routes)
    with TestClient(app) as client:
        for path in (*_SPIKE_PATHS, "/revoke"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}
    assert _safe_next(CONSENT_PATH + "?tx=" + "a" * 43) == "/"


@pytest.mark.parametrize(
    "base",
    [
        "",
        "http://evil.example",
        "https://x/path",
        "https://x?q=s",
        "https://x#s",
        "https://user:password@x",
    ],
)
def test_enabled_requires_fixed_https_origin(
    enabled: None, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    monkeypatch.setenv("CONNECT_MCP_PUBLIC_BASE_URL", base)
    with pytest.raises(ValueError, match="HTTPS origin"):
        _app()


def test_discovery_and_get_authentication(client: TestClient) -> None:
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == _BASE + "/mcp"
    assert prm["authorization_servers"] == [_BASE + "/"]
    assert prm["scopes_supported"] == ["library:read"]
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == _BASE + "/"
    for key, path in [
        ("authorization_endpoint", "/authorize"),
        ("token_endpoint", "/token"),
        ("registration_endpoint", "/register"),
        ("revocation_endpoint", "/revoke"),
    ]:
        assert metadata[key] == _BASE + path
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert set(metadata["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert metadata["revocation_endpoint_auth_methods_supported"] == ["none"]
    for path in ("/mcp", "/mcp/"):
        response = client.get(path, headers={"Host": "attacker.example"})
        assert response.status_code == 401
        assert (
            f'resource_metadata="{_BASE}/.well-known/oauth-protected-resource/mcp"'
            in response.headers["www-authenticate"]
        )
        assert "location" not in response.headers


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_dcr_consent_code_token_ping(client: TestClient, path: str) -> None:
    token, client_id, code = _token(client)
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "Origin": "https://claude.ai",
    }
    get_response = client.get(path, headers=headers)
    assert get_response.status_code == 405
    assert get_response.headers["allow"] == "POST"
    assert client.get(path).status_code == 401
    assert client.get(path, params={"access_token": token}).status_code == 401
    initialize = client.post(
        path,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "spike-test", "version": "1"},
            },
        },
    )
    assert initialize.status_code == 200, initialize.text
    assert "mcp-session-id" not in initialize.headers
    listing = client.post(
        path, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert [tool["name"] for tool in listing.json()["result"]["tools"]] == [
        "ping",
        "search_videos",
        "get_video",
    ]
    ping = client.post(
        path,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
    )
    assert ping.status_code == 200, ping.text
    assert ping.json()["result"]["content"] == [
        {"type": "text", "text": "s***@vectorinc.co.jp: 動画ライブラリとの接続を確認しました。"}
    ]
    assert _EMAIL not in ping.text
    assert (
        client.post("/token", data=_token_data(client_id, code)).json()["error"] == "invalid_grant"
    )


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"code_verifier": "wrong"}, "invalid_request"),
        ({"code_verifier": "w" * 43}, "invalid_grant"),
        ({"redirect_uri": _CALLBACK + "?changed=1"}, "invalid_request"),
        ({"client_id": "unknown"}, "unauthorized_client"),
    ],
)
def test_token_binding(client: TestClient, overrides: dict[str, str], error: str) -> None:
    client_id, code = _code(client)
    response = client.post("/token", data={**_token_data(client_id, code), **overrides})
    assert response.status_code in {400, 401}
    assert response.json()["error"] == error


@pytest.mark.parametrize(
    "callback",
    [
        _CALLBACK + "?secret=do-not-log",
        _CALLBACK + "/",
        "https://claude.ai.evil/api/mcp/auth_callback",
    ],
)
def test_redirect_allowlist_and_observation(client: TestClient, callback: str) -> None:
    with structlog.testing.capture_logs() as logs:
        response = client.post(
            "/register", json=_registration(callback), headers={"X-Request-Id": "spike-123"}
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"
    observed = next(row for row in logs if row["event"] == "mcp_spike_redirect_observed")
    if callback == _CALLBACK + "?secret=do-not-log":
        assert observed["host"] == "claude.ai"
        assert observed["path"] == "/api/mcp/auth_callback"
    else:
        assert observed["path"] == "unsupported"
        assert observed["host"] in {"claude.ai", "unsupported"}
    assert observed["request_id"] == "spike-123"
    assert "do-not-log" not in str(logs)


def test_additional_exact_callback(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = "https://callback.example/claude?trial=1"
    monkeypatch.setenv("CONNECT_MCP_SPIKE_REDIRECT_ALLOW", " " + extra + ", ")
    with TestClient(_app(), base_url=_BASE) as client:
        assert client.post("/register", json=_registration(extra)).status_code == 201
        assert client.post("/register", json=_registration(extra + "x")).status_code == 400


def test_authorize_rejects_unregistered_redirect(client: TestClient) -> None:
    registration = client.post("/register", json=_registration()).json()
    response = client.get(
        "/authorize",
        params=_params(registration["client_id"], redirect_uri="https://evil.example/callback"),
    )
    assert response.status_code == 400
    assert "location" not in response.headers


def test_fresh_google_required_and_safe_return(client: TestClient) -> None:
    _, target = _start(client)
    client.cookies.update(_auth_cookie())
    response = client.get(target)
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select_one("#g_id_onload") is not None
    csrf = str(page.select_one('input[name="csrf"]')["value"])
    rejected = client.post(target, data={"csrf": csrf}, headers={"Origin": _BASE})
    assert rejected.status_code in {400, 401, 403}
    assert "location" not in rejected.headers
    parsed = urlsplit(target)
    internal = parsed.path + "?" + parsed.query
    assert _safe_next(internal) == internal
    assert _safe_next("https://evil.example" + internal) == "/"
    assert _safe_next(CONSENT_PATH + "?tx=" + "x" * 43 + "&next=evil") == "/"


@pytest.mark.parametrize(
    "origin,csrf",
    [
        ("https://evil.example", None),
        (_BASE + "/", None),
        (None, None),
        (_BASE, "wrong"),
        (_BASE, "無効"),
    ],
)
def test_consent_origin_and_csrf(client: TestClient, origin: str | None, csrf: str | None) -> None:
    _, target = _start(client)
    actual_csrf = _consent_form(client, target)
    response = client.post(
        target,
        data={"csrf": actual_csrf if csrf is None else csrf},
        headers={"Origin": origin} if origin else {},
    )
    assert response.status_code == 403
    assert "location" not in response.headers


def test_search_cookie_cannot_replace_fresh_google_identity(client: TestClient) -> None:
    client_id, target = _start(client)
    csrf = _consent_form(client, target)
    client.cookies.set(
        "ta_search_session", make_session("other@vectorinc.co.jp", _SECRET, ttl_s=3600)
    )
    response = client.post(target, data={"csrf": csrf}, headers={"Origin": _BASE})
    assert response.status_code == 303
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    token = client.post("/token", data=_token_data(client_id, code)).json()["access_token"]
    from tests.connect_web.test_library_mcp_chatgpt import _rpc

    ping = _rpc(client, token, "tools/call", {"name": "ping", "arguments": {}})
    assert "s***@vectorinc.co.jp" in ping.text and "o***@vectorinc.co.jp" not in ping.text


def test_existing_routes_unchanged(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = (
        "/",
        "/search",
        "/library",
        "/healthz",
        "/unknown-pr0",
        "/mcp/unknown-pr0",
        "/mcp/authorize",
        "/oauth2/callback",
        "/slack/oauth/callback",
    )

    def snapshots() -> list[tuple[int, str | None, str]]:
        with TestClient(_app(), base_url=_BASE, follow_redirects=False) as client:
            return [
                (r.status_code, r.headers.get("location"), r.text)
                for r in (client.get(path) for path in paths)
            ]

    on = snapshots()
    monkeypatch.setenv("CONNECT_MCP_ENABLED", "false")
    assert snapshots() == on
    assert all(row == (404, None, '{"detail":"Not Found"}') for row in on[4:7])


def test_lifespan_composes_worker(enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    events = []

    @asynccontextmanager
    async def worker(app: Any) -> Any:
        events.append("start")
        yield
        events.append("stop")

    monkeypatch.setattr(connect_app, "worker_lifespan", worker)
    with TestClient(_app(), base_url=_BASE, follow_redirects=False) as client:
        assert events == ["start"]
        _token(client)
    assert events == ["start", "stop"]


@pytest.mark.parametrize(
    "path",
    [
        "/authorize?state=secret&email=private",
        CONSENT_PATH + "?tx=secret",
        "/mcp?token=secret",
        "/mcp-auth/verify?tx=secret",
        "/mcp-auth/connections?code=secret",
        "/revoke?token=secret",
        "/search/login?next=%2Fmcp-auth%2Fconsent%3Ftx%3Dsecret",
    ],
)
def test_access_log_redacts_mcp_secrets(path: str) -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s "%s %s HTTP/%s" %s',
        ("client", "GET", path, "1.1", 200),
        None,
    )
    assert RedactMCPAccessLog().filter(record)
    assert "secret" not in record.getMessage() and "private" not in record.getMessage()
    assert (
        "redact_mcp_spike"
        in connect_app.build_uvicorn_log_config()["loggers"]["uvicorn.access"]["filters"]
    )


async def test_sdk_streamable_client_roundtrip(enabled: None) -> None:
    app = _app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE) as http,
    ):
        registration = (await http.post("/register", json=_registration())).json()
        auth = await http.get("/authorize", params=_params(registration["client_id"]))
        target = auth.headers["location"]
        http.cookies.update(_auth_cookie())
        page = await http.get(target)
        soup = BeautifulSoup(page.text, "html.parser")
        csrf = str(soup.select_one('input[name="csrf"]')["value"])
        nonce = str(soup.select_one("#g_id_onload")["data-nonce"])
        tx = parse_qs(urlsplit(target).query)["tx"][0]
        verified = await http.post(
            "/mcp-auth/verify",
            params={"tx": tx},
            data={"csrf": csrf, "credential": nonce},
            headers={"Origin": _BASE},
        )
        assert verified.status_code == 303
        page = await http.get(target)
        csrf = BeautifulSoup(page.text, "html.parser").select_one('input[name="csrf"]')["value"]
        consent = await http.post(target, data={"csrf": csrf}, headers={"Origin": _BASE})
        code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
        token = (
            await http.post("/token", data=_token_data(registration["client_id"], code))
        ).json()["access_token"]
        http.cookies.clear()
        http.headers["Authorization"] = "Bearer " + token
        async with (
            streamable_http_client(_BASE + "/mcp", http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            assert [tool.name for tool in (await session.list_tools()).tools] == [
                "ping",
                "search_videos",
                "get_video",
            ]
            result = await session.call_tool("ping", {})
            assert not result.isError
            assert "s***@vectorinc.co.jp" in result.model_dump_json()
            assert _EMAIL not in result.model_dump_json()
