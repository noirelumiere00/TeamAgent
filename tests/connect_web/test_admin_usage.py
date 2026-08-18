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
        if "GROUP BY skill" in sql and "percentile_cont" not in sql:
            # work_type_breakdown（束の畳み込み元データ）
            self._rows = (
                []
                if self._pg.empty
                else [
                    {"skill": "search", "n": 6, "cost_usd": 0.06},
                    {"skill": "mail_draft", "n": 2, "cost_usd": 0.02},
                    {"skill": "slack_summary", "n": 1, "cost_usd": 0.01},
                    {"skill": "mail_summary", "n": 1, "cost_usd": 0.01},
                ]
            )
        elif "COUNT(*) AS total_events" in sql:
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
            who = (params or {}).get("who") if isinstance(params, dict) else None
            self._pg.question_who.append(who)
            self._rows = (
                []
                if (self._pg.empty or (who is not None and who.lower() != _OWNER))
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
        elif "GROUP BY who" in sql:
            self._rows = (
                []
                if self._pg.empty
                else [
                    {"who": _OWNER, "requests": 9, "cost_usd": 0.09},
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
        self.question_who: list[Any] = []

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


def test_admin_renders_work_type_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    """作業束の横棒バーが出る（外部ライブラリ非依存の CSS 幅指定で描く）。"""
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, _ = _client()
    response = client.get("/admin", cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert "作業の内訳（直近7日）" in response.text
    assert "作業の内訳（直近30日）" in response.text
    for work_type in ("調べる", "作る", "整える", "秘書"):
        assert work_type in response.text
    # search 6 / mail_draft 2 / slack_summary 1 / mail_summary 1 = 10 件
    assert 'class="bar-fill" style="width:60.0%"' in response.text
    assert "60.0% / 6件" in response.text
    assert "20.0% / 2件" in response.text
    assert "10.0% / 1件" in response.text
    # 未知ツールが無いので「その他」の行は出さない。
    assert "その他" not in response.text
    # 外部 CDN を新たに引き込んでいない（/admin はスクリプト無しのまま）。
    assert "cdn.jsdelivr.net" not in response.text


def test_admin_user_rows_link_to_drilldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, _ = _client()
    response = client.get("/admin", cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert '<a href="/admin?user=s-komata%40vectorinc.co.jp">' in response.text


def test_admin_drilldown_filters_questions_by_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, pg = _client()
    response = client.get(f"/admin?user={_OWNER}", cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert pg.question_who == [_OWNER]
    # 絞り込み値は bind パラメータで渡り、SQL 文字列には現れない。
    question_sql = [sql for sql, _ in pg.executed if "query_text" in sql]
    assert question_sql and all(_OWNER not in sql for sql in question_sql)
    assert "で絞り込み中" in response.text
    assert '<a href="/admin">絞り込みを解除</a>' in response.text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in response.text


def test_admin_drilldown_other_user_shows_empty_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, pg = _client()
    response = client.get(f"/admin?user={_OTHER}", cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert pg.question_who == [_OTHER]
    assert "この利用者の質問はまだ記録がありません。" in response.text
    assert "&lt;script&gt;" not in response.text


def test_admin_drilldown_escapes_and_does_not_reflect_raw_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """?user= の値は絞り込み表示にも出るので、生 HTML として反射させない。"""
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, _ = _client()
    response = client.get('/admin?user=<img src=x onerror="alert(1)">', cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert "<img src=x" not in response.text
    assert "&lt;img src=x" in response.text


def test_admin_drilldown_rejects_absurdly_long_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, pg = _client()
    response = client.get("/admin?user=" + "a" * 300, cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert pg.question_who == [None]
    assert "で絞り込み中" not in response.text


def test_admin_drilldown_is_not_reachable_for_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """ドリルダウン付きでも非 admin は 404 偽装のまま（DB に触れない）。"""
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    client, pg = _client()
    response = client.get(f"/admin?user={_OWNER}", cookies=_cookies(_OTHER))
    assert response.status_code == 404
    assert pg.connection_kwargs == []
    assert pg.question_who == []


def test_admin_drilldown_does_not_log_the_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """G8: URL の email を構造化ログへ出さない（クエリ失敗時も含む）。"""
    monkeypatch.delenv("CONNECT_ADMIN_EMAILS", raising=False)
    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _capture(*args: Any, **kwargs: Any) -> None:
        captured.append((args, kwargs))

    class _BoomPg(_FakePg):
        @contextmanager
        def connection(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            self.connection_kwargs.append(kwargs)
            raise RuntimeError("boom")
            yield  # pragma: no cover

    monkeypatch.setattr(connect_app.logger, "warning", _capture)
    client, _ = _client(_BoomPg())
    response = client.get(f"/admin?user={_OWNER}", cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert captured, "クエリ失敗の warning が出ていない＝テストが空振り"
    assert _OWNER not in repr(captured)


def test_admin_query_string_is_redacted_in_uvicorn_access_log() -> None:
    """G8: ?user=<email> をアクセスログへ平文で残さない（/r/<token> と同じ流儀）。"""
    import logging

    from teamagent.connect_web.app import (
        _RedactAdminUserAccessLog,
        build_uvicorn_log_config,
    )

    flt = _RedactAdminUserAccessLog()
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', None, None
    )
    record.args = ("1.2.3.4:5", "GET", f"/admin?user={_OWNER}", "1.1", 200)
    assert flt.filter(record) is True
    assert isinstance(record.args, tuple)
    assert _OWNER not in str(record.args)
    assert record.args[2] == "/admin?<redacted>"

    # 絞り込み無しの /admin と他ルートは通常どおり残す（観測性を落とさない）。
    plain = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", None, None)
    plain.args = ("1.2.3.4:5", "GET", "/admin", "1.1", 200)
    flt.filter(plain)
    assert isinstance(plain.args, tuple)
    assert plain.args[2] == "/admin"

    cfg = build_uvicorn_log_config()
    assert "redact_admin_user" in cfg.get("filters", {})
    assert "redact_admin_user" in cfg["loggers"]["uvicorn.access"].get("filters", [])
    # 既存の短縮リンク秘匿は残っている。
    assert "redact_shortlink" in cfg["loggers"]["uvicorn.access"].get("filters", [])
