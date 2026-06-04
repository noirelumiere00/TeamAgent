"""管理画面 FastAPI ルートのテスト（TestClient・認証ゲート・dev_bypass）。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from teamagent.dashboard.app import create_app
from teamagent.dashboard.config import DashboardConfig


def _cfg(**kw: Any) -> DashboardConfig:
    base: dict[str, Any] = {
        "allowed_emails": frozenset({"owner@x.com"}),
        "allowed_hd": None,
        "google_client_id": "cid",
        "session_secret": b"secret",
        "dev_bypass": False,
    }
    base.update(kw)
    return DashboardConfig(**base)


class _FakeQueries:
    """DashboardQueries を duck-type した最小 fake（canned data）。"""

    def kpis(self) -> dict[str, Any]:
        return {
            "today_requests": 42,
            "active_users": 7,
            "month_cost_usd": 3.21,
            "error_rate_24h": 0.0,
            "requests_24h": 50,
        }

    def daily_series(self, days: int = 30) -> list[dict[str, Any]]:
        return [{"day": "2026-06-01", "requests": 10, "cost_usd": 0.1}]

    def skill_breakdown(self, days: int = 7) -> list[dict[str, Any]]:
        return [{"skill": "search", "n": 10, "cost_usd": 0.05, "p50_ms": 800, "p95_ms": 1900}]

    def user_breakdown(self, **kw: Any) -> list[dict[str, Any]]:
        return [{"who": "u@x.com", "requests": 5, "cost_usd": 0.03}]

    def runtime_now(self) -> dict[str, Any] | None:
        return {"gate_in_flight": 1, "gate_concurrency": 4, "pool_in_use": 1, "pool_max_size": 8}

    def runtime_peaks(self, hours: int = 1) -> dict[str, Any]:
        return {"peak_parallel": 4, "peak_queue": 0, "queue_full": 0, "timeouts": 0}

    def oauth_status(self) -> list[dict[str, Any]]:
        return [{"user_email": "u@x.com", "created_at": "2026-06-01T00:00:00", "n_scopes": 7}]

    def error_list(self, limit: int = 50) -> list[dict[str, Any]]:
        return []


def test_healthz_ok() -> None:
    client = TestClient(create_app(_cfg()))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_index_redirects_to_login_when_unauthenticated() -> None:
    client = TestClient(create_app(_cfg()))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_errors_redirects_to_login_when_unauthenticated() -> None:
    client = TestClient(create_app(_cfg()))
    r = client.get("/errors", follow_redirects=False)
    assert r.status_code == 303


def test_login_page_renders() -> None:
    client = TestClient(create_app(_cfg()))
    r = client.get("/login")
    assert r.status_code == 200
    assert "g_id_signin" in r.text


def test_dev_bypass_shows_dashboard() -> None:
    app = create_app(_cfg(dev_bypass=True), queries=_FakeQueries())  # type: ignore[arg-type]
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "42" in r.text  # KPI value
    assert "search" in r.text  # skill table


def test_dev_bypass_errors_page() -> None:
    app = create_app(_cfg(dev_bypass=True), queries=_FakeQueries())  # type: ignore[arg-type]
    client = TestClient(app)
    r = client.get("/errors")
    assert r.status_code == 200


def test_auth_verify_sets_session_for_allowed_user() -> None:
    """許可ユーザの id_token 検証成功 → 303 + セッション Cookie 発行。"""

    def verifier(token: str, client_id: str) -> dict[str, Any]:
        return {"email": "owner@x.com", "email_verified": True}

    client = TestClient(create_app(_cfg(), verifier=verifier))
    r = client.post("/auth/verify", data={"credential": "tok"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "ta_dash_session" in r.headers.get("set-cookie", "")


def test_auth_verify_denies_disallowed_user() -> None:
    """allowlist 外のユーザは 403。"""

    def verifier(token: str, client_id: str) -> dict[str, Any]:
        return {"email": "stranger@x.com", "email_verified": True}

    client = TestClient(create_app(_cfg(), verifier=verifier))
    r = client.post("/auth/verify", data={"credential": "tok"}, follow_redirects=False)
    assert r.status_code == 403


def test_authenticated_session_can_view_dashboard() -> None:
    """発行されたセッション Cookie で / が閲覧できる（E2E の認証往復）。"""

    def verifier(token: str, client_id: str) -> dict[str, Any]:
        return {"email": "owner@x.com", "email_verified": True}

    app = create_app(_cfg(), queries=_FakeQueries(), verifier=verifier)  # type: ignore[arg-type]
    client = TestClient(app)
    client.post("/auth/verify", data={"credential": "tok"})  # Cookie が client に保存される
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "42" in r.text
