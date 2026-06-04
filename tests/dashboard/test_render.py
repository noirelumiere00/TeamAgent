"""管理画面レンダリングのテスト（値の埋め込み・XSS エスケープ）。"""

from __future__ import annotations

from typing import Any

from teamagent.dashboard.render import render_dashboard, render_errors, render_login


def test_login_with_client_id_shows_google_button() -> None:
    html = render_login(client_id="cid-123")
    assert "g_id_signin" in html
    assert "cid-123" in html
    assert "/auth/verify" in html


def test_login_dev_bypass_shows_notice() -> None:
    html = render_login(client_id=None, dev_bypass=True)
    assert "開発モード" in html


def test_login_without_client_id_shows_setup_notice() -> None:
    html = render_login(client_id=None)
    assert "未設定" in html
    assert "g_id_signin" not in html


def _dashboard_data() -> dict[str, Any]:
    return {
        "email": "owner@x.com",
        "kpis": {
            "today_requests": 42,
            "active_users": 7,
            "month_cost_usd": 3.21,
            "error_rate_24h": 0.05,
            "requests_24h": 100,
        },
        "daily": [{"day": "2026-06-01", "requests": 10, "cost_usd": 0.1}],
        "skills": [{"skill": "search", "n": 10, "cost_usd": 0.05, "p50_ms": 800, "p95_ms": 1900}],
        "users": [{"who": "u@x.com", "requests": 5, "cost_usd": 0.03}],
        "runtime_now": {
            "gate_in_flight": 2,
            "gate_concurrency": 4,
            "pool_in_use": 2,
            "pool_max_size": 8,
        },
        "runtime_peaks": {"peak_parallel": 4, "peak_queue": 6, "queue_full": 1, "timeouts": 0},
        "oauth": [{"user_email": "u@x.com", "created_at": "2026-06-01T00:00:00", "n_scopes": 7}],
    }


def test_dashboard_contains_kpi_and_table_values() -> None:
    html = render_dashboard(_dashboard_data())
    assert "42" in html  # today_requests
    assert "$3.21" in html  # month cost
    assert "5.0%" in html  # error rate (0.05*100)
    assert "search" in html  # skill row
    assert "dailyChart" in html  # chart canvas
    assert "owner@x.com" in html  # nav email


def test_dashboard_escapes_xss_in_values() -> None:
    data = _dashboard_data()
    data["users"] = [{"who": "<script>alert(1)</script>", "requests": 1, "cost_usd": 0.0}]
    html = render_dashboard(data)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_errors_page_lists_rows_without_bodies() -> None:
    rows = [
        {
            "occurred_at": "2026-06-04 10:00:00+00",
            "request_id": "req-xyz",
            "skill": "search",
            "status": "error",
            "error_code": "ValueError",
            "who": "u@x.com",
        }
    ]
    html = render_errors(rows, email="owner@x.com")
    assert "req-xyz" in html
    assert "error" in html
    assert "ValueError" in html
