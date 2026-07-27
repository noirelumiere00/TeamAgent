"""connect_web /oauth2/callback のテスト（実Google0・実DB0・実KMS0）。

state検証(CSRF)→code交換→保存 の経路を、exchange_fn/store を注入して検証する。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters.google_oauth_flow import make_state
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.connect_web.app import _put_verified_oauth_token, create_app

_SECRET = "unit-test-state-secret"


class _FakeStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, OAuthToken]] = []

    def put(self, user_email: str, token: OAuthToken) -> None:
        self.puts.append((user_email, token))


class _OneTimeStateConsumer:
    def __init__(self) -> None:
        self.consumed: set[str] = set()

    def __call__(self, state: str) -> bool:
        if state in self.consumed:
            return False
        self.consumed.add(state)
        return True


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exchange_fn: Any = None,
    store: _FakeStore | None = None,
    verifier: Any = None,
    state_consumer: Any = None,
) -> tuple[TestClient, _FakeStore]:
    monkeypatch.setenv("OAUTH_STATE_SECRET", _SECRET)
    monkeypatch.setenv(
        "CONNECT_GOOGLE_CLIENT_ID",
        "test-client.apps.googleusercontent.com",
    )
    st = store or _FakeStore()
    if exchange_fn is None:

        def exchange_fn(code: str) -> OAuthToken:
            return OAuthToken(
                refresh_token=f"rt-{code}",
                scopes=("a", "b"),
                id_token=f"id-{code}",
            )

    if verifier is None:

        def verifier(token: str, client_id: str) -> dict[str, Any]:
            assert token.startswith("id-")
            assert client_id == "test-client.apps.googleusercontent.com"
            return {
                "email": "owner@vectorinc.co.jp",
                "email_verified": True,
            }

    app = create_app(
        redirect_uri="https://example/oauth2/callback",
        exchange_fn=exchange_fn,
        store=st,
        google_state_consumer=state_consumer or _OneTimeStateConsumer(),
        oauth_id_token_verifier=verifier,
    )
    return TestClient(app), st


def test_healthz(monkeypatch: pytest.MonkeyPatch) -> None:
    # app_html_* フィールドの中身は test_app_html_s3.py で検証（ここでは ok のみ見る）。
    client, _ = _client(monkeypatch)
    assert client.get("/healthz").json()["ok"] is True


def test_callback_success_stores_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    state = make_state("Owner@vectorinc.co.jp")  # OAUTH_STATE_SECRET から署名
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == 200
    assert "連携が完了" in r.text
    # email は正規化(lower)・token は exchange の戻り
    assert len(store.puts) == 1
    assert store.puts[0][0] == "owner@vectorinc.co.jp"
    assert store.puts[0][1].refresh_token == "rt-abc"
    assert store.puts[0][1].id_token is None


def test_callback_rejects_unknown_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    r = client.get("/oauth2/callback", params={"code": "abc", "state": "garbage"})
    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert store.puts == []  # 保存されない


def test_callback_rejects_expired_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    state = make_state("owner@vectorinc.co.jp", now=1, nonce="expired")
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert store.puts == []


def test_callback_rejects_second_use_of_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    state = make_state("owner@vectorinc.co.jp")
    first = client.get("/oauth2/callback", params={"code": "first", "state": state})
    second = client.get("/oauth2/callback", params={"code": "second", "state": state})
    assert first.status_code == 200
    assert second.status_code == 400
    assert "使用済み" in second.text
    assert len(store.puts) == 1
    assert store.puts[0][1].refresh_token == "rt-first"


def test_callback_rejects_mismatched_id_token_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def verifier(_token: str, _client_id: str) -> dict[str, Any]:
        return {
            "email": "other-person@vectorinc.co.jp",
            "email_verified": True,
        }

    client, store = _client(monkeypatch, verifier=verifier)
    state = make_state("owner@vectorinc.co.jp")
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == 403
    assert "別のアカウントで許可されました" in r.text
    assert "owner@vectorinc.co.jp" in r.text
    assert "other-person@vectorinc.co.jp" not in r.text
    assert store.puts == []


def test_callback_rejects_unverified_id_token_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def verifier(_token: str, _client_id: str) -> dict[str, Any]:
        return {
            "email": "owner@vectorinc.co.jp",
            "email_verified": False,
        }

    client, store = _client(monkeypatch, verifier=verifier)
    state = make_state("owner@vectorinc.co.jp")
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == 403
    assert store.puts == []


def test_callback_rejects_missing_id_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def exchange_without_id_token(code: str) -> OAuthToken:
        return OAuthToken(refresh_token=f"rt-{code}", scopes=("a", "b"))

    client, store = _client(monkeypatch, exchange_fn=exchange_without_id_token)
    state = make_state("owner@vectorinc.co.jp")
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == 403
    assert "Googleアカウントを確認できませんでした" in r.text
    assert store.puts == []


def test_store_guard_requires_verified_identity() -> None:
    store = _FakeStore()
    token = OAuthToken(refresh_token="rt", id_token="id")
    with pytest.raises(TypeError):
        _put_verified_oauth_token(store, "owner@vectorinc.co.jp", token)  # type: ignore[call-arg]
    with pytest.raises(PermissionError):
        _put_verified_oauth_token(
            store,
            "owner@vectorinc.co.jp",
            token,
            identity_verified=False,
        )
    assert store.puts == []


def test_callback_missing_params(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    r = client.get("/oauth2/callback", params={"code": "abc"})  # state 欠如
    assert r.status_code == 400
    assert store.puts == []


def test_callback_user_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    r = client.get("/oauth2/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert "キャンセル" in r.text
    assert store.puts == []


def test_callback_exchange_failure_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(code: str) -> OAuthToken:
        raise RuntimeError("token endpoint down")

    client, store = _client(monkeypatch, exchange_fn=boom)
    state = make_state("owner@vectorinc.co.jp")
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == 500
    assert "連携に失敗" in r.text
    assert store.puts == []


def test_callback_does_not_leak_token_in_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功ページに refresh token を出さない（G8）。"""
    client, _ = _client(monkeypatch)
    state = make_state("owner@vectorinc.co.jp")
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert "rt-abc" not in r.text
