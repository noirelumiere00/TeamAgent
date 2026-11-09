"""Public OAuth contracts with the real SDK and an explicitly injected issuer-store fake."""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import replace
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import structlog
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from teamagent.dashboard.auth import make_session
from tests.connect_web.mcp_store_fake import FakePgMCPStore, patch_store
from tests.connect_web.test_library_mcp_chatgpt import _rpc
from tests.connect_web.test_library_mcp_spike import (
    _BASE,
    _app,
    _code,
    _consent_form,
    _registration,
    _start,
    _token_data,
    _verifier_mcp,
)
from tests.connect_web.test_search_routes import _EMAIL, _SECRET, _auth_cookie, _config


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> FakePgMCPStore:
    monkeypatch.setenv("CONNECT_MCP_ENABLED", "true")
    monkeypatch.setenv("CONNECT_MCP_PUBLIC_BASE_URL", _BASE)
    monkeypatch.delenv("CONNECT_MCP_SPIKE_REDIRECT_ALLOW", raising=False)
    return patch_store(monkeypatch, resource=_BASE + "/mcp")


@pytest.fixture
def client(enabled: FakePgMCPStore) -> Any:
    with TestClient(_app(), base_url=_BASE, follow_redirects=False) as http:
        yield http


def _pair(client: TestClient) -> tuple[str, dict[str, Any]]:
    client_id, code = _code(client)
    response = client.post("/token", data=_token_data(client_id, code))
    assert response.status_code == 200, response.text
    return client_id, response.json()


def _refresh(client: TestClient, client_id: str, token: str, **changes: str) -> Any:
    return client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": token,
            "resource": _BASE + "/mcp",
            **changes,
        },
    )


def _access(client: TestClient, token: str) -> Any:
    return client.get("/mcp", headers={"Authorization": "Bearer " + token})


def test_client_and_credentials_survive_app_recreation(enabled: FakePgMCPStore) -> None:
    with TestClient(_app(), base_url=_BASE, follow_redirects=False) as first:
        client_id, tokens = _pair(first)
    with TestClient(_app(), base_url=_BASE, follow_redirects=False) as restarted:
        assert _access(restarted, tokens["access_token"]).status_code == 405
        refreshed = _refresh(restarted, client_id, tokens["refresh_token"])
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["refresh_token"] != tokens["refresh_token"]


def test_expired_access_refreshes_without_google_and_keeps_identity(
    client: TestClient, enabled: FakePgMCPStore
) -> None:
    client_id, tokens = _pair(client)
    enabled.expire_access(tokens["access_token"])
    client.cookies.clear()
    assert _access(client, tokens["access_token"]).status_code == 401
    refreshed = _refresh(client, client_id, tokens["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text
    fresh = refreshed.json()
    assert fresh["expires_in"] == 600 and fresh["scope"] == "library:read"
    assert fresh["refresh_token"] != tokens["refresh_token"]
    assert _access(client, fresh["access_token"]).status_code == 405
    ping = _rpc(client, fresh["access_token"], "tools/call", {"name": "ping", "arguments": {}})
    assert "s***@vectorinc.co.jp" in ping.text and _EMAIL not in ping.text
    assert enabled.list_grants(_EMAIL)[0]["expires_at"] > time.time() + 89 * 86400


def test_refresh_replay_revokes_the_entire_grant(client: TestClient) -> None:
    client_id, tokens = _pair(client)
    fresh = _refresh(client, client_id, tokens["refresh_token"]).json()
    replay = _refresh(client, client_id, tokens["refresh_token"])
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert _access(client, tokens["access_token"]).status_code == 401
    assert _access(client, fresh["access_token"]).status_code == 401
    assert _refresh(client, client_id, fresh["refresh_token"]).status_code == 400


def test_wrong_client_scope_and_resource_do_not_consume_refresh(client: TestClient) -> None:
    client_id, tokens = _pair(client)
    other_id = client.post("/register", json=_registration()).json()["client_id"]
    wrong_client = _refresh(client, other_id, tokens["refresh_token"])
    assert wrong_client.status_code == 400 and wrong_client.json()["error"] == "invalid_grant"
    scope = _refresh(client, client_id, tokens["refresh_token"], scope="library:read admin")
    assert scope.status_code == 400 and scope.json()["error"] == "invalid_scope"
    resource = _refresh(
        client, client_id, tokens["refresh_token"], resource="https://evil.example/mcp"
    )
    assert resource.status_code == 400 and "access_token" not in resource.json()
    assert _refresh(client, client_id, tokens["refresh_token"]).status_code == 200


def test_public_revocation_accepts_no_secret_and_expired_access(
    client: TestClient, enabled: FakePgMCPStore
) -> None:
    client_id, tokens = _pair(client)
    enabled.expire_access(tokens["access_token"])
    response = client.post(
        "/revoke", data={"client_id": client_id, "token": tokens["access_token"]}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert _refresh(client, client_id, tokens["refresh_token"]).status_code == 400
    assert (
        client.post("/revoke", data={"client_id": client_id, "token": "unknown"}).status_code == 200
    )


def test_wrong_client_cannot_revoke_another_grant(client: TestClient) -> None:
    client_id, tokens = _pair(client)
    other_id = client.post("/register", json=_registration()).json()["client_id"]
    response = client.post("/revoke", data={"client_id": other_id, "token": tokens["access_token"]})
    assert response.status_code == 200
    assert _access(client, tokens["access_token"]).status_code == 405
    assert _refresh(client, client_id, tokens["refresh_token"]).status_code == 200


def test_updated_authorization_policy_applies_to_persisted_tokens(enabled: FakePgMCPStore) -> None:
    with TestClient(_app(), base_url=_BASE, follow_redirects=False) as initial:
        client_id, tokens = _pair(initial)
    removed = replace(_config(), allowed_emails=frozenset())
    with TestClient(
        create_app(search_config=removed, search_verifier=_verifier_mcp, admin_pg=object()),
        base_url=_BASE,
        follow_redirects=False,
    ) as changed:
        assert _access(changed, tokens["access_token"]).status_code == 401
        response = _refresh(changed, client_id, tokens["refresh_token"])
        assert response.status_code == 400 and "access_token" not in response.json()


def test_database_outage_is_fail_closed_and_sanitized(
    client: TestClient, enabled: FakePgMCPStore
) -> None:
    client_id, tokens = _pair(client)
    enabled.available = False
    with structlog.testing.capture_logs() as logs:
        responses = [
            _access(client, tokens["access_token"]),
            _refresh(client, client_id, tokens["refresh_token"]),
            client.post("/register", json=_registration()),
        ]
    assert all(r.status_code == 503 for r in responses)
    assert all("private" not in r.text and "customer" not in r.text for r in responses)
    assert "password=private" not in str(logs) and tokens["refresh_token"] not in str(logs)


def test_consent_cannot_move_to_another_browser(client: TestClient) -> None:
    _, target = _start(client)
    original = client.get(target)
    page = BeautifulSoup(original.text, "html.parser")
    csrf = str(page.select_one('input[name="csrf"]')["value"])
    nonce = str(page.select_one("#g_id_onload")["data-nonce"])
    tx = parse_qs(urlsplit(target).query)["tx"][0]
    with TestClient(_app(), base_url=_BASE, follow_redirects=False) as other:
        page = other.get(target)
        assert page.status_code in {400, 403}
        response = other.post(
            "/mcp-auth/verify",
            params={"tx": tx},
            data={"csrf": csrf, "credential": nonce},
            headers={"Origin": _BASE},
        )
        assert response.status_code in {400, 403}
    _consent_form(client, target)


def test_google_nonce_and_origin_are_bound_to_browser(client: TestClient) -> None:
    _, target = _start(client)
    page = BeautifulSoup(client.get(target).text, "html.parser")
    csrf = str(page.select_one('input[name="csrf"]')["value"])
    nonce = str(page.select_one("#g_id_onload")["data-nonce"])
    tx = parse_qs(urlsplit(target).query)["tx"][0]
    for credential, origin in [("wrong-nonce", _BASE), (nonce, "https://evil.example")]:
        response = client.post(
            "/mcp-auth/verify",
            params={"tx": tx},
            data={"csrf": csrf, "credential": credential},
            headers={"Origin": origin},
        )
        assert response.status_code in {400, 401, 403}
        assert "location" not in response.headers
    assert client.post(target, data={"csrf": csrf}, headers={"Origin": _BASE}).status_code in {
        400,
        401,
        403,
    }


def test_owner_can_list_and_disconnect_without_showing_credentials(client: TestClient) -> None:
    _, tokens = _pair(client)
    client.cookies.update(_auth_cookie())
    response = client.get("/mcp-auth/connections")
    assert response.status_code == 200
    assert (
        tokens["access_token"] not in response.text and tokens["refresh_token"] not in response.text
    )
    soup = BeautifulSoup(response.text, "html.parser")
    csrf = str(soup.select_one('input[name="csrf"]')["value"])
    grant_id = str(soup.select_one('input[name="grant_id"]')["value"])
    denied = client.post(
        "/mcp-auth/connections",
        data={"csrf": csrf, "grant_id": grant_id},
        headers={"Origin": "https://evil.example"},
    )
    assert denied.status_code == 403
    revoked = client.post(
        "/mcp-auth/connections",
        data={"csrf": csrf, "grant_id": grant_id},
        headers={"Origin": _BASE},
    )
    assert revoked.status_code == 303, revoked.text
    assert _access(client, tokens["access_token"]).status_code == 401


def test_setup_is_public_and_never_touches_the_database(
    client: TestClient, enabled: FakePgMCPStore
) -> None:
    before = enabled.calls
    response = client.get("/mcp/setup")
    assert response.status_code == 200
    assert enabled.calls == before
    assert _BASE + "/mcp" in response.text
    assert "https://chatgpt.com/plugins" in response.text
    assert "https://claude.ai/customize/connectors" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "content-security-policy" in response.headers


@pytest.mark.parametrize(
    "body", ["not-json", "[]", "null", '{"scope":"library:read","scope":"admin"}']
)
def test_registration_rejects_invalid_or_duplicate_json(client: TestClient, body: str) -> None:
    response = client.post("/register", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert "client_id" not in response.json()


def test_token_rejects_duplicate_parameters_and_oversized_body(client: TestClient) -> None:
    duplicate = client.post(
        "/token",
        content="client_id=a&client_id=b&grant_type=refresh_token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert duplicate.status_code == 400
    huge = client.post(
        "/token",
        content="x=" + "a" * 20000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert huge.status_code in {400, 413}


@pytest.mark.parametrize("hd", [None, "missing"])
def test_explicit_email_allowlist_accepts_no_hd_claim(
    enabled: FakePgMCPStore, hd: str | None
) -> None:
    def verifier(credential: str, client_id: str) -> dict[str, Any]:
        claims: dict[str, Any] = {"email": _EMAIL, "email_verified": True, "nonce": credential}
        if hd != "missing":
            claims["hd"] = hd
        return claims

    with TestClient(
        create_app(search_config=_config(), search_verifier=verifier, admin_pg=object()),
        base_url=_BASE,
        follow_redirects=False,
    ) as http:
        _, tokens = _pair(http)
        assert _access(http, tokens["access_token"]).status_code == 405
    assert next(iter(enabled.grants.values()))["verified_hd"] is None


@pytest.mark.parametrize("bad_claim", ["missing_nonce", "unverified_email", "outside_user"])
def test_fresh_google_rejects_invalid_identity(enabled: FakePgMCPStore, bad_claim: str) -> None:
    def verifier(credential: str, client_id: str) -> dict[str, Any]:
        claims: dict[str, Any] = {"email": _EMAIL, "email_verified": True, "nonce": credential}
        if bad_claim == "missing_nonce":
            del claims["nonce"]
        elif bad_claim == "unverified_email":
            claims["email_verified"] = False
        else:
            claims["email"] = "outside@example.net"
        return claims

    with TestClient(
        create_app(search_config=_config(), search_verifier=verifier, admin_pg=object()),
        base_url=_BASE,
        follow_redirects=False,
    ) as http:
        _, target = _start(http)
        page = BeautifulSoup(http.get(target).text, "html.parser")
        tx = parse_qs(urlsplit(target).query)["tx"][0]
        response = http.post(
            "/mcp-auth/verify",
            params={"tx": tx},
            data={
                "csrf": str(page.select_one('input[name="csrf"]')["value"]),
                "credential": str(page.select_one("#g_id_onload")["data-nonce"]),
            },
            headers={"Origin": _BASE},
        )
        assert response.status_code == 403
        assert not enabled.grants


def test_development_bypass_cannot_create_persistent_grants(enabled: FakePgMCPStore) -> None:
    cfg = replace(_config(), dev_bypass=True)
    with TestClient(
        create_app(search_config=cfg, search_verifier=_verifier_mcp, admin_pg=object()),
        base_url=_BASE,
        follow_redirects=False,
    ) as http:
        _, target = _start(http)
        assert http.get(target).status_code == 503
        assert not enabled.grants


def test_untrusted_client_name_is_not_inserted_as_html(client: TestClient) -> None:
    name = '<script>window.stolen="credential"</script>'
    body = {**_registration(), "client_name": name}
    response = client.post("/register", json=body)
    assert response.status_code == 201
    from tests.connect_web.test_library_mcp_spike import _params

    target = client.get("/authorize", params=_params(response.json()["client_id"])).headers[
        "location"
    ]
    csrf = _consent_form(client, target)
    assert client.post(target, data={"csrf": csrf}, headers={"Origin": _BASE}).status_code == 303
    connections = client.get("/mcp-auth/connections")
    assert connections.status_code == 200
    assert "window.stolen" not in connections.text
    assert "Claude" in connections.text


def test_another_user_cannot_list_or_revoke_the_owner_grant(
    client: TestClient, enabled: FakePgMCPStore
) -> None:
    _, tokens = _pair(client)
    original_id = next(iter(enabled.grants))
    other_id = str(uuid.uuid4())
    other = copy.deepcopy(enabled.grants[original_id])
    other.update(grant_id=other_id, subject="other@vectorinc.co.jp")
    enabled.grants[other_id] = other
    client.cookies.set(
        "ta_search_session", make_session("other@vectorinc.co.jp", _SECRET, ttl_s=3600)
    )
    page = client.get("/mcp-auth/connections")
    assert original_id not in page.text and other_id in page.text
    csrf = str(BeautifulSoup(page.text, "html.parser").select_one('input[name="csrf"]')["value"])
    rejected = client.post(
        "/mcp-auth/connections",
        data={"csrf": csrf, "grant_id": original_id},
        headers={"Origin": _BASE},
    )
    assert rejected.status_code == 403
    assert _access(client, tokens["access_token"]).status_code == 405


def test_google_verification_rate_limit_precedes_the_verifier(enabled: FakePgMCPStore) -> None:
    calls = []

    def verifier(credential: str, client_id: str) -> dict[str, Any]:
        calls.append(credential)
        return _verifier_mcp(credential, client_id)

    with TestClient(
        create_app(search_config=_config(), search_verifier=verifier, admin_pg=object()),
        base_url=_BASE,
        follow_redirects=False,
    ) as http:
        _, target = _start(http)
        page = BeautifulSoup(http.get(target).text, "html.parser")
        tx = parse_qs(urlsplit(target).query)["tx"][0]
        enabled.admit = False
        response = http.post(
            "/mcp-auth/verify",
            params={"tx": tx},
            data={
                "csrf": str(page.select_one('input[name="csrf"]')["value"]),
                "credential": str(page.select_one("#g_id_onload")["data-nonce"]),
            },
            headers={"Origin": _BASE},
        )
        assert response.status_code == 429
        assert not calls and not enabled.grants


def test_denial_consumes_the_request_without_granting_access(
    client: TestClient, enabled: FakePgMCPStore
) -> None:
    _, target = _start(client)
    page = client.get(target)
    cookie = SimpleCookie()
    cookie.load(page.headers["set-cookie"])
    browser = cookie["__Host-library-mcp-browser"]
    assert browser["secure"] and browser["httponly"]
    assert browser["samesite"].lower() == "lax" and browser["path"] == "/"
    csrf = str(BeautifulSoup(page.text, "html.parser").select_one('input[name="csrf"]')["value"])
    denied = client.post(target, data={"csrf": csrf, "decision": "deny"}, headers={"Origin": _BASE})
    assert denied.status_code == 303
    query = parse_qs(urlsplit(denied.headers["location"]).query)
    assert query["error"] == ["access_denied"] and query["state"] == ["private-state"]
    assert not enabled.grants
    assert client.post(target, data={"csrf": csrf}, headers={"Origin": _BASE}).status_code == 400
