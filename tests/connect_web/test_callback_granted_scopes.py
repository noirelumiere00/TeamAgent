"""connect-web: 実際に許可された範囲（granted_scopes）の保存と一部許可の画面（F0・既定 OFF）。

背景: ``OAuthConsentFlow.exchange`` は ``creds.scopes``（google_auth_oauthlib の
``session.scope``＝**要求した**範囲）を保存しており、許可画面で項目のチェックを外した人も
「全部許可」に見えていた。フラグ（CONNECT_STORE_GRANTED_SCOPES）ON のときだけ、Google が
返した ``scope``（``session.token["scope"]``→``creds.granted_scopes``）を保存する。

フェイクは token endpoint の HTTP 応答だけ。``OAuthConsentFlow._flow()`` の**本物の**
google_auth_oauthlib Flow（requests_oauthlib の OAuth2Session・oauthlib の応答解析・
``credentials_from_session``）をそのまま通す。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from teamagent.adapters.google_oauth_flow import (
    WORKSPACE_SCOPES,
    OAuthConsentFlow,
    make_state,
    missing_workspace_scopes,
    normalize_granted_scopes,
    scope_labels,
)
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.connect_web.app import create_app

_SECRET = "unit-test-state-secret"
_CLIENT_ID = "test-client.apps.googleusercontent.com"
_EMAIL = "owner@vectorinc.co.jp"
_CAL_EVENTS = "https://www.googleapis.com/auth/calendar.events"
_USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"


class _TokenEndpoint(requests.adapters.BaseAdapter):
    """oauth2.googleapis.com/token の代わり（requests のアダプタとして本物の Session に挿す）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload
        self.bodies: list[str] = []

    def send(self, request: requests.PreparedRequest, **_kwargs: Any) -> requests.Response:
        body = request.body
        self.bodies.append(body.decode() if isinstance(body, bytes) else str(body or ""))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps(self.payload).encode()
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.url = str(request.url)
        resp.request = request
        return resp

    def close(self) -> None:
        return None


def _grant(scope: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "access_token": "ya29.test",
        "expires_in": 3599,
        "refresh_token": "1//granted-refresh-token",
        "token_type": "Bearer",
        "id_token": "id-token-for-test",
    }
    if scope is not None:
        payload["scope"] = scope
    return payload


def _without(*drop: str) -> str:
    return " ".join(s for s in WORKSPACE_SCOPES if s not in drop)


@pytest.fixture
def token_endpoint(monkeypatch: pytest.MonkeyPatch) -> Any:
    """本物の Flow を作り、その OAuth2Session の https 通信だけをフェイクへ向ける。"""
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_SECRET", "test-secret")
    # exchange() が os.environ.setdefault で立てる値を、テスト後に元へ戻す。
    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    real_flow = OAuthConsentFlow._flow
    holder: dict[str, _TokenEndpoint] = {}

    def _install(payload: dict[str, Any]) -> _TokenEndpoint:
        adapter = _TokenEndpoint(payload)
        holder["adapter"] = adapter

        def _flow(self: OAuthConsentFlow) -> Any:
            flow = real_flow(self)
            flow.oauth2session.mount("https://", adapter)
            return flow

        monkeypatch.setattr(OAuthConsentFlow, "_flow", _flow)
        return adapter

    return _install


# ── exchange（adapters/google_oauth_flow）──────────────────────────────────────


def test_flag_off_keeps_saving_the_requested_scopes(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any
) -> None:
    """既定 OFF は今と同じ（一部許可でも要求した範囲を保存する＝従来の穴のまま）。"""
    monkeypatch.delenv("CONNECT_STORE_GRANTED_SCOPES", raising=False)
    adapter = token_endpoint(_grant(_without(_CAL_EVENTS)))
    token = OAuthConsentFlow("https://example.test/oauth2/callback").exchange("auth-code")
    assert token.scopes == WORKSPACE_SCOPES
    assert token.refresh_token == "1//granted-refresh-token"
    assert "code=auth-code" in adapter.bodies[0]


@pytest.mark.parametrize("flag", ["1", "true", "on"])
def test_flag_on_saves_what_google_actually_granted(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any, flag: str
) -> None:
    monkeypatch.setenv("CONNECT_STORE_GRANTED_SCOPES", flag)
    token_endpoint(_grant(_without(_CAL_EVENTS)))
    token = OAuthConsentFlow("https://example.test/oauth2/callback").exchange("auth-code")
    assert _CAL_EVENTS not in token.scopes
    assert set(token.scopes) == set(WORKSPACE_SCOPES) - {_CAL_EVENTS}
    assert token.id_token == "id-token-for-test"


def test_flag_on_reads_the_email_alias_as_userinfo_email(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any
) -> None:
    monkeypatch.setenv("CONNECT_STORE_GRANTED_SCOPES", "1")
    token_endpoint(_grant(_without(_USERINFO_EMAIL) + " email"))
    token = OAuthConsentFlow("https://example.test/oauth2/callback").exchange("auth-code")
    assert "email" not in token.scopes
    assert missing_workspace_scopes(token.scopes) == ()


def test_flag_on_without_scope_in_the_response_falls_back_to_requested(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any
) -> None:
    """応答に scope が無い＝根拠が無い。「何も許可されていない」とは保存しない。"""
    monkeypatch.setenv("CONNECT_STORE_GRANTED_SCOPES", "1")
    token_endpoint(_grant(None))
    with capture_logs() as logs:
        token = OAuthConsentFlow("https://example.test/oauth2/callback").exchange("auth-code")
    assert token.scopes == WORKSPACE_SCOPES
    assert any(e["event"] == "google_oauth_granted_scopes_missing" for e in logs)


def test_normalize_granted_scopes_shapes() -> None:
    assert normalize_granted_scopes(None) is None
    assert normalize_granted_scopes(123) is None
    assert normalize_granted_scopes("openid  email\n") == ("openid", _USERINFO_EMAIL)
    assert normalize_granted_scopes(["email", _USERINFO_EMAIL, "openid"]) == (
        _USERINFO_EMAIL,
        "openid",
    )
    assert normalize_granted_scopes(frozenset({"openid", "email"})) == (
        _USERINFO_EMAIL,
        "openid",
    )


def test_scope_labels_name_the_feature_not_the_url() -> None:
    assert scope_labels([_CAL_EVENTS]) == ["カレンダーへの予定登録"]
    assert scope_labels(["openid", _USERINFO_EMAIL]) == ["アカウントの確認"]
    assert scope_labels(["https://www.googleapis.com/auth/unknown.thing"]) == ["unknown.thing"]


# ── callback（connect_web）──────────────────────────────────────────────────────


class _Store:
    def __init__(self) -> None:
        self.puts: list[tuple[str, OAuthToken]] = []

    def put(self, user_email: str, token: OAuthToken) -> None:
        self.puts.append((user_email, token))


class _Consumer:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def __call__(self, state: str) -> bool:
        if state in self.seen:
            return False
        self.seen.add(state)
        return True


def _app(monkeypatch: pytest.MonkeyPatch, *, consented: str = _EMAIL) -> tuple[TestClient, _Store]:
    """exchange_fn は注入しない（本物の OAuthConsentFlow.exchange を通す）。"""
    monkeypatch.setenv("OAUTH_STATE_SECRET", _SECRET)
    store = _Store()

    def _verifier(token: str, client_id: str) -> dict[str, Any]:
        assert token == "id-token-for-test"
        assert client_id == _CLIENT_ID
        return {"email": consented, "email_verified": True}

    app = create_app(
        redirect_uri="https://example.test/oauth2/callback",
        store=store,
        google_state_consumer=_Consumer(),
        oauth_id_token_verifier=_verifier,
    )
    return TestClient(app), store


def test_partial_grant_shows_what_will_not_work_and_saves_the_granted_scopes(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any
) -> None:
    monkeypatch.setenv("CONNECT_STORE_GRANTED_SCOPES", "1")
    token_endpoint(_grant(_without(_CAL_EVENTS)))
    client, store = _app(monkeypatch)
    with capture_logs() as logs:
        r = client.get("/oauth2/callback", params={"code": "c1", "state": make_state(_EMAIL)})
    assert r.status_code == 200
    assert "連携しました（一部の権限が未許可です）" in r.text
    assert "次の機能は使えません: カレンダーへの予定登録" in r.text
    assert "「連携」と送り" in r.text
    assert "連携が完了しました" not in r.text
    assert len(store.puts) == 1
    saved_email, saved = store.puts[0]
    assert saved_email == _EMAIL
    assert _CAL_EVENTS not in saved.scopes
    assert saved.id_token is None  # id_token は永続化しない（既存の守りのまま）
    partial = [e for e in logs if e["event"] == "connect_callback_scope_partial"]
    assert len(partial) == 1
    assert partial[0]["missing_count"] == 1
    assert partial[0]["missing"] == ["calendar.events"]
    assert "user_email" not in partial[0]


def test_full_grant_shows_the_usual_completion_page(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any
) -> None:
    monkeypatch.setenv("CONNECT_STORE_GRANTED_SCOPES", "1")
    token_endpoint(_grant(" ".join(WORKSPACE_SCOPES)))
    client, store = _app(monkeypatch)
    with capture_logs() as logs:
        r = client.get("/oauth2/callback", params={"code": "c2", "state": make_state(_EMAIL)})
    assert r.status_code == 200
    assert "✅ 連携が完了しました" in r.text
    assert "一部の権限" not in r.text
    assert set(store.puts[0][1].scopes) == set(WORKSPACE_SCOPES)
    assert not [e for e in logs if e["event"] == "connect_callback_scope_partial"]


def test_flag_off_partial_grant_keeps_todays_page_and_saved_scopes(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any
) -> None:
    monkeypatch.delenv("CONNECT_STORE_GRANTED_SCOPES", raising=False)
    token_endpoint(_grant(_without(_CAL_EVENTS)))
    client, store = _app(monkeypatch)
    r = client.get("/oauth2/callback", params={"code": "c3", "state": make_state(_EMAIL)})
    assert r.status_code == 200
    assert "✅ 連携が完了しました" in r.text
    assert "一部の権限" not in r.text
    assert store.puts[0][1].scopes == WORKSPACE_SCOPES


def test_flag_on_does_not_weaken_the_account_check(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any
) -> None:
    """一部許可の判定は本人照合の後ろ。別アカウントで許可されたら保存も画面も従来どおり拒否。"""
    monkeypatch.setenv("CONNECT_STORE_GRANTED_SCOPES", "1")
    token_endpoint(_grant(_without(_CAL_EVENTS)))
    client, store = _app(monkeypatch, consented="someone-else@vectorinc.co.jp")
    r = client.get("/oauth2/callback", params={"code": "c4", "state": make_state(_EMAIL)})
    assert r.status_code == 403
    assert "一致しません" in r.text
    assert store.puts == []


def test_flag_on_does_not_weaken_the_one_time_state(
    monkeypatch: pytest.MonkeyPatch, token_endpoint: Any
) -> None:
    monkeypatch.setenv("CONNECT_STORE_GRANTED_SCOPES", "1")
    token_endpoint(_grant(_without(_CAL_EVENTS)))
    client, store = _app(monkeypatch)
    state = make_state(_EMAIL)
    assert client.get("/oauth2/callback", params={"code": "c5", "state": state}).status_code == 200
    again = client.get("/oauth2/callback", params={"code": "c5", "state": state})
    assert again.status_code == 400
    assert len(store.puts) == 1
