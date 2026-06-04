"""connect_web /oauth2/callback のテスト（実Google0・実DB0・実KMS0）。

state検証(CSRF)→code交換→保存 の経路を、exchange_fn/store を注入して検証する。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters.google_oauth_flow import make_state
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.connect_web.app import create_app

_SECRET = "unit-test-state-secret"


class _FakeStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, OAuthToken]] = []

    def put(self, user_email: str, token: OAuthToken) -> None:
        self.puts.append((user_email, token))


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exchange_fn: Any = None,
    store: _FakeStore | None = None,
) -> tuple[TestClient, _FakeStore]:
    monkeypatch.setenv("OAUTH_STATE_SECRET", _SECRET)
    st = store or _FakeStore()
    if exchange_fn is None:

        def exchange_fn(code: str) -> OAuthToken:
            return OAuthToken(refresh_token=f"rt-{code}", scopes=("a", "b"))

    app = create_app(
        redirect_uri="https://example/oauth2/callback",
        exchange_fn=exchange_fn,
        store=st,
    )
    return TestClient(app), st


def test_healthz(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(monkeypatch)
    assert client.get("/healthz").json() == {"ok": True}


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


def test_callback_rejects_tampered_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    r = client.get("/oauth2/callback", params={"code": "abc", "state": "garbage"})
    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert store.puts == []  # 保存されない


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
