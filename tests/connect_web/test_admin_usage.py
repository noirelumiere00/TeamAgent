"""connect-web /admin の認可・read-only 集計・質問本文 escaping（実DBなし）。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teamagent.connect_web import app as connect_app
from teamagent.connect_web.app import create_app
from teamagent.dashboard.auth import make_session
from teamagent.dashboard.config import DashboardConfig

_SECRET = b"admin-usage-test-secret-32-bytes"
_OWNER = "s-komata@vectorinc.co.jp"
_OTHER = "x@vectorinc.co.jp"
_OVERRIDE = "a@vectorinc.co.jp"


def _config() -> DashboardConfig:
    return DashboardConfig(
        allowed_emails=frozenset({_OWNER, _OTHER, _OVERRIDE}),
        allowed_hd="vectorinc.co.jp",
        google_client_id="cid",
        session_secret=_SECRET,
        dev_bypass=False,
        cookie_secure=False,
        allowed_hd_opens_domain=True,
    )


class _FakeCursor:
    def __init__(self, pg: _FakePg) -> None:
        self._pg = pg
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._pg.executed.append((sql, params))
        if "COUNT(*) AS total_events" in sql:
            self._rows = [
                {
                    "total_events": 0 if self._pg.empty else 1,
                    "today_requests": 0 if self._pg.empty else 1,
                    "today_users": 0 if self._pg.empty else 1,
                    "today_cost_usd": 0 if self._pg.empty else 0.01,
                    "today_errors": 0,
                    "seven_day_requests": 0 if self._pg.empty else 1,
                    "seven_day_users": 0 if self._pg.empty else 1,
                    "seven_day_cost_usd": 0 if self._pg.empty else 0.01,
                    "seven_day_errors": 0,
                }
            ]
        elif "query_text" in sql:
            self._rows = (
                []
                if self._pg.empty
                else [
                    {
                        "occurred_at": datetime(2026, 8, 13, 0, 30, tzinfo=UTC),
                        "who": _OWNER,
                        "skill": "search",
                        "query_text": '<script>alert("x")</script>',
                        "status": "ok",
                        "latency_ms": 123,
                        "cost_usd": 0.01,
                    }
                ]
            )
        else:
            self._rows = []

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(self, pg: _FakePg) -> None:
        self._pg = pg

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._pg)


class _FakePg:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.connection_kwargs: list[dict[str, Any]] = []
        self.executed: list[tuple[str, Any]] = []

    @contextmanager
    def connection(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.connection_kwargs.append(kwargs)
        yield _FakeConn(self)


def _client(pg: _FakePg | None = None) -> tuple[TestClient, _FakePg]:
    fake_pg = pg or _FakePg()
    return TestClient(create_app(search_config=_config(), admin_pg=fake_pg)), fake_pg


def _cookies(email: str) -> dict[str, str]:
    return {
        "ta_search_session": make_session(email, _SECRET, ttl_s=3600),
    }


def test_admin_unauthenticated_redirects_to_login_with_safe_next() -> None:
    client, _ = _client()
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/search/login?next=/admin"
    assert connect_app._safe_next("/admin") == "/admin"
    assert connect_app._safe_next("https://example.com/admin") == "/app"


def test_admin_authenticated_non_admin_gets_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, pg = _client()
    response = client.get("/admin", cookies=_cookies(_OTHER))
    assert response.status_code == 404
    assert pg.connection_kwargs == []


def test_admin_unset_defaults_to_owner_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, pg = _client()
    assert client.get("/admin", cookies=_cookies(_OWNER)).status_code == 200
    assert client.get("/admin", cookies=_cookies(_OTHER)).status_code == 404
    assert pg.connection_kwargs
    assert all(
        kwargs == {"app_role": "teamagent_dashboard", "user_role": "admin"}
        for kwargs in pg.connection_kwargs
    )


def test_admin_empty_allowlist_does_not_open_to_everyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    client, _ = _client()
    assert client.get("/admin", cookies=_cookies(_OWNER)).status_code == 200
    assert client.get("/admin", cookies=_cookies(_OTHER)).status_code == 404


def test_admin_override_replaces_default_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _OVERRIDE)
    client, _ = _client()
    assert client.get("/admin", cookies=_cookies(_OVERRIDE)).status_code == 200
    assert client.get("/admin", cookies=_cookies(_OWNER)).status_code == 404


def test_admin_invalid_nonempty_allowlist_denies_everyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "*")
    client, pg = _client()
    assert client.get("/admin", cookies=_cookies(_OWNER)).status_code == 404
    assert client.get("/admin", cookies=_cookies(_OTHER)).status_code == 404
    assert pg.connection_kwargs == []


def test_admin_escapes_question_text_and_renders_jst(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, _ = _client()
    response = client.get("/admin", cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert '<script>alert("x")</script>' not in response.text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in response.text
    assert "2026-08-13 09:30:00 JST" in response.text
    assert "NewsTV AI 利用状況（管理）" in response.text


def test_admin_empty_usage_events_renders_deployment_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, _ = _client(_FakePg(empty=True))
    response = client.get("/admin", cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert "まだ記録がありません。記録は mcp の次回デプロイから始まります" in response.text
