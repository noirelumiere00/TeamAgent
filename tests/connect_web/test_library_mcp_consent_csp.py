"""The consent form can return only to its validated client's callback."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from teamagent.connect_web.app import _CONTENT_SECURITY_POLICY, create_app
from teamagent.connect_web.library_mcp_spike import _csp_callback_source
from tests.connect_web.mcp_store_fake import patch_store
from tests.connect_web.test_library_mcp_spike import (
    _BASE,
    _params,
    _registration,
    _verifier_mcp,
)
from tests.connect_web.test_search_routes import _config


@pytest.mark.parametrize(
    "callback",
    [
        "https://chatgpt.com/connector/oauth/test-callback",
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "http://localhost:53682/callback",
    ],
)
def test_only_consent_get_extends_only_form_action_for_its_client(
    monkeypatch: pytest.MonkeyPatch,
    callback: str,
) -> None:
    monkeypatch.setenv("CONNECT_MCP_ENABLED", "true")
    monkeypatch.setenv("CONNECT_MCP_PUBLIC_BASE_URL", _BASE)
    monkeypatch.setenv("CONNECT_MCP_SPIKE_REDIRECT_ALLOW", callback)
    patch_store(monkeypatch, resource=_BASE + "/mcp")
    with TestClient(
        create_app(search_config=_config(), search_verifier=_verifier_mcp, admin_pg=object()),
        base_url=_BASE,
        follow_redirects=False,
    ) as client:
        registered = client.post("/register", json=_registration(callback))
        assert registered.status_code == 201
        client_id = registered.json()["client_id"]
        authorized = client.get("/authorize", params=_params(client_id, redirect_uri=callback))
        assert authorized.status_code == 302
        target = authorized.headers["location"]
        consent = client.get(target)
        assert consent.status_code == 200
        expected = _CONTENT_SECURITY_POLICY.replace(
            "form-action 'self'",
            f"form-action 'self' {callback}",
        )
        assert consent.headers["content-security-policy"] == expected
        assert consent.headers["referrer-policy"] == "same-origin"
        # Neither unrelated pages nor JSON errors inherit the callback exception.
        for response in (
            client.get("/search/login"),
            client.get("/healthz"),
            client.get("/mcp-auth/consent"),
            client.post(target, data={"csrf": "wrong"}, headers={"Origin": "https://evil.example"}),
        ):
            assert response.headers["content-security-policy"] == _CONTENT_SECURITY_POLICY
        assert (
            client.post(target, data={"csrf": "wrong"}, headers={"Origin": _BASE}).status_code
            == 403
        )


@pytest.mark.parametrize(
    "uri",
    [
        "https://chatgpt.com/callback;script-src*",
        "https://chatgpt.com/callback'",
        'https://chatgpt.com/callback"',
        "https://chatgpt.com/callback,https://evil.example",
        "https://chatgpt.com/callback form-action *",
        "https://chatgpt.com/call\nback",
        "https://chatgpt.com/callback\x7f",
        "https://user:password@chatgpt.com/callback",
        "https://*.chatgpt.com/callback",
        "https://chatgpt.com\\@evil.example/callback",
    ],
)
def test_csp_source_rejects_directive_injection_or_ambiguous_authority(uri: str) -> None:
    with pytest.raises(ValueError, match="safe CSP source"):
        _csp_callback_source(uri)


def test_csp_source_keeps_exact_authority_and_path_without_oauth_query_or_fragment() -> None:
    callback = "https://chatgpt.com/connector/oauth/test-callback"
    assert _csp_callback_source(callback + "?existing=value;ignored=source#fragment") == callback
