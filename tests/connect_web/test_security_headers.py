"""Browser security policy regression tests for the public connect-web surface."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from teamagent.dashboard.config import DashboardConfig


def _config() -> DashboardConfig:
    return DashboardConfig(
        allowed_emails=frozenset({"user@vectorinc.co.jp"}),
        allowed_hd="vectorinc.co.jp",
        google_client_id="client-id.apps.googleusercontent.com",
        session_secret=b"s" * 32,
        dev_bypass=False,
        cookie_secure=True,
    )


def test_security_headers_cover_html_api_redirect_and_health() -> None:
    client = TestClient(create_app(search_config=_config()), follow_redirects=False)

    responses = (
        client.get("/search/login"),
        client.get("/api/v1/graph"),
        client.get("/app"),
        client.get("/healthz"),
    )
    for response in responses:
        assert response.headers["strict-transport-security"] == (
            "max-age=31536000; includeSubDomains"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["cross-origin-opener-policy"] == "same-origin-allow-popups"
        assert response.headers["cross-origin-resource-policy"] == "same-site"


def test_csp_is_default_deny_but_keeps_google_sign_in_compatible() -> None:
    response = TestClient(create_app(search_config=_config())).get("/search/login")
    csp = response.headers["content-security-policy"]

    assert "default-src 'self'" in csp
    assert "base-uri 'none'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "https://accounts.google.com" in csp
    assert "'unsafe-eval'" not in csp
    assert "script-src *" not in csp


def test_uvicorn_does_not_advertise_its_server_banner(
    monkeypatch: Any,
) -> None:
    from teamagent.connect_web import __main__ as entrypoint

    captured: dict[str, Any] = {}

    def _run(_app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(entrypoint.uvicorn, "run", _run)
    entrypoint.main()

    assert captured["server_header"] is False
