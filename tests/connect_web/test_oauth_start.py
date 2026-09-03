"""connect-web ``/oauth2/start/{state}`` ・ ``/slack/oauth/start/{state}`` のテスト。

（実Google0・実Slack0・実DB0・実KMS0）

@Aico(openclaw) の LLM が約 600 字の認可 URL（``?state=…``）を再タイプして state を壊し、
callback で HMAC 不一致になる事故（2026-08-31 / 09-02 実測）の根治。署名 state を path に
載せ、connect-web が検証（**消費はしない**）して mcp と同一の認可 URL へ 302 する。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters import google_oauth_flow, slack_oauth_flow
from teamagent.adapters.google_oauth_flow import OAuthConsentFlow
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.adapters.slack_oauth_flow import SlackOAuthConsentFlow
from teamagent.connect_web.app import _MAX_OAUTH_START_STATE_CHARS, create_app
from teamagent.skills.base import SkillContext
from teamagent.skills.oauth_connect.schema import OAuthConnectInput
from teamagent.skills.oauth_connect.skill import OAuthConnectSkill

_GOOGLE_REDIRECT = "https://connect.example.com/oauth2/callback"
_SLACK_REDIRECT = "https://connect.example.com/slack/oauth/callback"
_BASE = "https://connect.example.com"
_EMAIL = "owner@vectorinc.co.jp"
_SLACK_UID = "U0123456789"
_SLACK_TEAM = "T0123456789"

# mcp(oauth_connect) と connect-web の両方が読む env（本番では両サービスで同一値にする前提）。
_ENV = {
    "OAUTH_STATE_SECRET": "unit-test-state-secret-0123456789",
    "CONNECT_GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "CONNECT_GOOGLE_CLIENT_SECRET": "test-secret",
    "CONNECT_SEARCH_ALLOWED_HD": "vectorinc.co.jp",
    "OAUTH_REDIRECT_URI": _GOOGLE_REDIRECT,
    "SLACK_OAUTH_STATE_SECRET": "unit-test-slack-state-secret-0123456789",
    "CONNECT_SLACK_CLIENT_ID": "123456789.987654321",
    "SLACK_OAUTH_REDIRECT_URI": _SLACK_REDIRECT,
}


class _RecordingConsumer:
    """ワンタイム消費の記録器。start ルートが **呼ばない** ことを証明する。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, key: str) -> bool:
        first = key not in self.calls
        self.calls.append(key)
        return first


class _FakeStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, Any]] = []

    def put(self, user_email: str, token: Any) -> None:
        self.puts.append((user_email, token))


class _Conn:
    """has() だけを持つ最小トークンストア（oauth_connect の連携済み判定に注入）。"""

    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def has(self, _user_email: str) -> bool:
        return self._connected


def _client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, _RecordingConsumer, _RecordingConsumer]:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", raising=False)
    monkeypatch.delenv("USE_OAUTH_START_LINKS", raising=False)
    monkeypatch.delenv("CONNECT_BASE_URL", raising=False)
    google_consumer = _RecordingConsumer()
    slack_consumer = _RecordingConsumer()

    def exchange_fn(code: str) -> OAuthToken:
        return OAuthToken(refresh_token=f"rt-{code}", scopes=("a",), id_token=f"id-{code}")

    def verifier(_token: str, _client_id: str) -> dict[str, Any]:
        return {"email": _EMAIL, "email_verified": True}

    app = create_app(
        redirect_uri=_GOOGLE_REDIRECT,
        slack_redirect_uri=_SLACK_REDIRECT,
        exchange_fn=exchange_fn,
        store=_FakeStore(),
        google_state_consumer=google_consumer,
        oauth_id_token_verifier=verifier,
        slack_state_consumer=slack_consumer,
    )
    return TestClient(app), google_consumer, slack_consumer


def _state_of(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("state")
    assert values and len(values) == 1
    return values[0]


def _retype_one_char(state: str) -> str:
    """LLM の転記事故を模す: 構造は無傷のまま中央の 1 文字だけ変える（HMAC 不一致になる）。"""
    mid = len(state) // 2
    repl = "A" if state[mid] != "A" else "B"
    return state[:mid] + repl + state[mid + 1 :]


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="r",
        user_id=_SLACK_UID,
        metadata={
            "user_email": _EMAIL,
            "verified_slack_user_id": _SLACK_UID,
            "verified_slack_team_id": _SLACK_TEAM,
        },
    )


def _assert_no_input_echo(r: Any, state: str) -> None:
    """Location 以外（本文・他ヘッダ）に利用者入力(state)が出ないこと。"""
    assert state not in r.text
    for name, value in r.headers.items():
        if name.lower() != "location":
            assert state not in value, f"header {name} に state が反射している"


# ── Google ─────────────────────────────────────────────────────────────────────


def test_google_start_redirects_to_identical_authorization_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有効 state → 302。Location は現行 authorization_url と完全一致（state 同一・query 順序含む）。"""
    client, google_consumer, _ = _client(monkeypatch)
    issued_url, state = OAuthConsentFlow(redirect_uri=_GOOGLE_REDIRECT).authorization_url(_EMAIL)

    r = client.get(f"/oauth2/start/{state}", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == issued_url
    assert r.headers["cache-control"] == "no-store"
    assert r.content == b""  # 本文無し（state/URL を反射しない）
    assert google_consumer.calls == []  # start は消費しない
    _assert_no_input_echo(r, state)


def test_google_start_matches_link_issued_by_mcp_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp(oauth_connect・フラグ OFF) が発行した認可 URL と、その state で start が飛ぶ先が一致する。"""
    client, _, _ = _client(monkeypatch)
    out = OAuthConnectSkill(google_store=_Conn(False), slack_store=_Conn(False)).run(
        OAuthConnectInput(), _ctx()
    )
    assert out.url is not None and out.slack_url is not None

    g = client.get(f"/oauth2/start/{_state_of(out.url)}", follow_redirects=False)
    assert g.status_code == 302
    assert g.headers["location"] == out.url

    s = client.get(f"/slack/oauth/start/{_state_of(out.slack_url)}", follow_redirects=False)
    assert s.status_code == 302
    assert s.headers["location"] == out.slack_url


def test_start_links_from_mcp_resolve_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """フラグ ON の mcp 出力（path リンク・query 無し）を connect-web に流すと認可 URL に着地する。"""
    client, google_consumer, slack_consumer = _client(monkeypatch)
    monkeypatch.setenv("USE_OAUTH_START_LINKS", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", _BASE)
    out = OAuthConnectSkill(google_store=_Conn(False), slack_store=_Conn(False)).run(
        OAuthConnectInput(), _ctx()
    )
    assert out.url is not None and out.slack_url is not None
    assert out.url.startswith(f"{_BASE}/oauth2/start/") and "?" not in out.url
    assert out.slack_url.startswith(f"{_BASE}/slack/oauth/start/") and "?" not in out.slack_url

    g = client.get(out.url.removeprefix(_BASE), follow_redirects=False)
    assert g.status_code == 302
    g_loc = g.headers["location"]
    assert g_loc.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert _state_of(g_loc) == out.url.rsplit("/", 1)[1]  # path の state がそのまま認可 URL へ
    assert parse_qs(urlparse(g_loc).query)["login_hint"] == [_EMAIL]

    s = client.get(out.slack_url.removeprefix(_BASE), follow_redirects=False)
    assert s.status_code == 302
    s_loc = s.headers["location"]
    assert s_loc.startswith("https://slack.com/oauth/v2/authorize?")
    assert _state_of(s_loc) == out.slack_url.rsplit("/", 1)[1]

    assert google_consumer.calls == [] and slack_consumer.calls == []


def test_google_start_rejects_retyped_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 転記型の改竄（構造無傷・HMAC 不一致）→ 400。Location 無し・入力を反射しない。"""
    client, _, _ = _client(monkeypatch)
    _, state = OAuthConsentFlow(redirect_uri=_GOOGLE_REDIRECT).authorization_url(_EMAIL)
    tampered = _retype_one_char(state)

    r = client.get(f"/oauth2/start/{tampered}", follow_redirects=False)

    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert "location" not in r.headers
    _assert_no_input_echo(r, tampered)


def test_google_start_rejects_garbage_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch)
    r = client.get("/oauth2/start/not-a-state", follow_redirects=False)
    assert r.status_code == 400
    assert "location" not in r.headers


def test_google_start_rejects_expired_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch)
    expired = google_oauth_flow.make_state(_EMAIL, now=1, nonce="expired")
    r = client.get(f"/oauth2/start/{expired}", follow_redirects=False)
    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert "location" not in r.headers


def test_google_start_rejects_oversize_state_before_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = _client(monkeypatch)
    r = client.get(
        "/oauth2/start/" + "A" * (_MAX_OAUTH_START_STATE_CHARS + 1), follow_redirects=False
    )
    assert r.status_code == 400
    assert "location" not in r.headers


def test_google_start_does_not_consume_state_even_after_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """消費済み state でも start は 302（消費は callback だけ）。start を何度踏んでも消費しない。"""
    client, google_consumer, _ = _client(monkeypatch)
    issued_url, state = OAuthConsentFlow(redirect_uri=_GOOGLE_REDIRECT).authorization_url(_EMAIL)

    cb = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert cb.status_code == 200
    assert google_consumer.calls == [state]

    for _ in range(2):
        r = client.get(f"/oauth2/start/{state}", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == issued_url
    assert google_consumer.calls == [state]  # start は一度も消費していない

    # 消費は依然 callback 側で効いている（2 回目の callback は使用済み）。
    again = client.get("/oauth2/callback", params={"code": "xyz", "state": state})
    assert again.status_code == 400
    assert "使用済み" in again.text


def test_google_start_ignores_extra_query_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """state 以外の入力は受け取らない（open redirect 不可）。"""
    client, _, _ = _client(monkeypatch)
    issued_url, state = OAuthConsentFlow(redirect_uri=_GOOGLE_REDIRECT).authorization_url(_EMAIL)
    r = client.get(
        f"/oauth2/start/{state}",
        params={"next": "https://evil.example/", "redirect_uri": "https://evil.example/cb"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == issued_url
    assert "evil.example" not in r.headers["location"]


def test_google_start_returns_500_when_oauth_client_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """認可 URL を組めない設定不備は利用者操作で直らない旨を返す（リダイレクトしない）。"""
    client, _, _ = _client(monkeypatch)
    _, state = OAuthConsentFlow(redirect_uri=_GOOGLE_REDIRECT).authorization_url(_EMAIL)
    for name in (
        "CONNECT_GOOGLE_CLIENT_ID",
        "CONNECT_GOOGLE_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    r = client.get(f"/oauth2/start/{state}", follow_redirects=False)
    assert r.status_code == 500
    assert "設定不備" in r.text
    assert "location" not in r.headers
    _assert_no_input_echo(r, state)


# ── Slack ──────────────────────────────────────────────────────────────────────


def _bound_slack_url() -> tuple[str, str]:
    return SlackOAuthConsentFlow(redirect_uri=_SLACK_REDIRECT).authorization_url(
        _EMAIL, slack_user_id=_SLACK_UID, slack_team_id=_SLACK_TEAM
    )


def test_slack_start_redirects_to_identical_authorization_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, slack_consumer = _client(monkeypatch)
    issued_url, state = _bound_slack_url()

    r = client.get(f"/slack/oauth/start/{state}", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == issued_url
    assert r.headers["cache-control"] == "no-store"
    assert r.content == b""
    assert slack_consumer.calls == []
    _assert_no_input_echo(r, state)


def test_slack_start_rejects_retyped_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch)
    _, state = _bound_slack_url()
    tampered = _retype_one_char(state)
    r = client.get(f"/slack/oauth/start/{tampered}", follow_redirects=False)
    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert "location" not in r.headers
    _assert_no_input_echo(r, tampered)


def test_slack_start_rejects_expired_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch)
    expired = slack_oauth_flow.make_state(
        _EMAIL, now=1, nonce="expired", slack_user_id=_SLACK_UID, slack_team_id=_SLACK_TEAM
    )
    r = client.get(f"/slack/oauth/start/{expired}", follow_redirects=False)
    assert r.status_code == 400
    assert "location" not in r.headers


def test_slack_start_rejects_unbound_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """検証済み Slack user/team に束縛されていない state は入口で止める（callback と同じ結論）。"""
    client, _, slack_consumer = _client(monkeypatch)
    unbound = slack_oauth_flow.make_state(_EMAIL)
    r = client.get(f"/slack/oauth/start/{unbound}", follow_redirects=False)
    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert "location" not in r.headers
    assert slack_consumer.calls == []


def test_slack_start_rejects_oversize_state_before_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = _client(monkeypatch)
    r = client.get(
        "/slack/oauth/start/" + "A" * (_MAX_OAUTH_START_STATE_CHARS + 1), follow_redirects=False
    )
    assert r.status_code == 400
    assert "location" not in r.headers


def test_slack_start_returns_500_when_client_id_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = _client(monkeypatch)
    _, state = _bound_slack_url()
    monkeypatch.delenv("CONNECT_SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    r = client.get(f"/slack/oauth/start/{state}", follow_redirects=False)
    assert r.status_code == 500
    assert "設定不備" in r.text
    assert "location" not in r.headers


# ── アクセスログ（G8: state は email を含む）────────────────────────────────────


def test_access_log_redacts_oauth_start_state() -> None:
    from teamagent.connect_web.app import (
        _RedactOAuthStartAccessLog,
        build_uvicorn_log_config,
    )

    fmt = '%s - "%s %s HTTP/%s" %d'
    flt = _RedactOAuthStartAccessLog()

    def _record(path: str) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, fmt, ("1.2.3.4:5", "GET", path, "1.1", 302), None
        )

    g = _record("/oauth2/start/SECRET.STATE?x=1")
    assert flt.filter(g) is True
    assert g.args[2] == "/oauth2/start/<redacted>"  # type: ignore[index]
    assert "SECRET" not in g.getMessage()

    s = _record("/slack/oauth/start/SECRET.STATE")
    flt.filter(s)
    assert s.args[2] == "/slack/oauth/start/<redacted>"  # type: ignore[index]

    other = _record("/oauth2/callback?code=abc&state=xyz")
    flt.filter(other)
    assert other.args[2] == "/oauth2/callback?code=abc&state=xyz"  # type: ignore[index]

    cfg = build_uvicorn_log_config()
    assert "redact_oauth_start" in cfg.get("filters", {})
    assert "redact_oauth_start" in cfg["loggers"]["uvicorn.access"].get("filters", [])
