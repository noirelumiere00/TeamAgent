"""DashboardQueries のテスト（実DB0）。read-only ロール接続と行マッピングを検証。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from teamagent.dashboard.queries import DashboardQueries, recent_questions


class _FakeCursor:
    def __init__(self, results: list[list[dict[str, Any]]], executed: list[Any]) -> None:
        self._results = results
        self._executed = executed
        self._last: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._executed.append((sql, params))
        self._last = self._results.pop(0) if self._results else []

    def fetchall(self) -> list[dict[str, Any]]:
        return self._last


class _FakeConn:
    def __init__(self, results: list[list[dict[str, Any]]], executed: list[Any]) -> None:
        self._results = results
        self._executed = executed

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._results, self._executed)


class _FakePg:
    def __init__(self, results: list[list[dict[str, Any]]]) -> None:
        self.results = list(results)
        self.executed: list[Any] = []
        self.conn_kwargs: list[dict[str, Any]] = []

    @contextmanager
    def connection(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.conn_kwargs.append(kwargs)
        yield _FakeConn(self.results, self.executed)


def test_connects_with_readonly_role_and_admin_guc() -> None:
    pg = _FakePg([[{"day": "2026-06-01", "requests": 3, "cost_usd": 0.1}]])
    DashboardQueries(pg).daily_series(7)
    assert pg.conn_kwargs[0] == {"app_role": "teamagent_dashboard", "user_role": "admin"}


def test_kpis_maps_three_selects() -> None:
    pg = _FakePg(
        [
            [{"requests": 12, "active_users": 4}],  # today
            [{"cost": 1.23}],  # month-to-date
            [{"rate": 0.25, "total": 8}],  # 24h error
        ]
    )
    k = DashboardQueries(pg).kpis()
    assert k["today_requests"] == 12
    assert k["active_users"] == 4
    assert k["month_cost_usd"] == 1.23
    assert k["error_rate_24h"] == 0.25
    assert k["requests_24h"] == 8


def test_skill_breakdown_maps_and_handles_null_latency() -> None:
    pg = _FakePg(
        [
            [
                {"skill": "search", "n": 10, "cost_usd": 0.05, "p50_ms": 800, "p95_ms": 1900},
                {"skill": "clientkarte", "n": 2, "cost_usd": 0.02, "p50_ms": None, "p95_ms": None},
            ]
        ]
    )
    out = DashboardQueries(pg).skill_breakdown(7)
    assert out[0] == {"skill": "search", "n": 10, "cost_usd": 0.05, "p50_ms": 800, "p95_ms": 1900}
    assert out[1]["p50_ms"] is None


def test_error_list_maps() -> None:
    pg = _FakePg(
        [
            [
                {
                    "occurred_at": "2026-06-04 10:00:00+00",
                    "request_id": "req-abc",
                    "skill": "search",
                    "status": "error",
                    "error_code": "ValueError",
                    "who": "u@x.com",
                }
            ]
        ]
    )
    out = DashboardQueries(pg).error_list(50)
    assert out[0]["request_id"] == "req-abc"
    assert out[0]["status"] == "error"
    assert out[0]["error_code"] == "ValueError"


def test_runtime_now_none_when_empty() -> None:
    pg = _FakePg([[]])
    assert DashboardQueries(pg).runtime_now() is None


def test_oauth_status_maps_without_ciphertext() -> None:
    pg = _FakePg(
        [[{"user_email": "u@x.com", "created_at": "2026-06-01 00:00:00+00", "n_scopes": 7}]]
    )
    out = DashboardQueries(pg).oauth_status()
    assert out[0] == {
        "user_email": "u@x.com",
        "created_at": "2026-06-01 00:00:00+00",
        "n_scopes": 7,
    }
    # クエリに暗号化列を含めない
    sql = pg.executed[0][0]
    assert "refresh_token_enc" not in sql


def test_recent_questions_uses_named_limit_and_maps_query_text() -> None:
    row = {
        "occurred_at": "2026-08-13 00:00:00+00",
        "who": "u@x.com",
        "skill": "search",
        "query_text": "質問です",
        "status": "ok",
        "latency_ms": 120,
        "cost_usd": 0.02,
    }
    pg = _FakePg([[row]])
    with pg.connection(app_role="teamagent_dashboard", user_role="admin") as conn:
        out = recent_questions(conn, limit=17)
    sql, params = pg.executed[0]
    assert "WHERE query_text IS NOT NULL" in sql
    assert "ORDER BY occurred_at DESC" in sql
    assert "LIMIT %(limit)s" in sql
    assert params == {"limit": 17}
    assert out[0]["query_text"] == "質問です"
    assert out[0]["latency_ms"] == 120
