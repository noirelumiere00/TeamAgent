"""`/oauth2/callback` の state ワンタイム消費失敗を **原因別に出し分ける**ことのテスト。

## 塞いだ実害（2026-08）

旧実装は `_consume_google_state` の例外を全部握って `state_consumed=False` に倒し、
「リンクが古いか使用済みです。もう一度『連携』と話しかけてください」と表示していた。
ところが本番の失敗は **state 保管先の env 未設定**（`consume_state_once` が `RuntimeError`）で、
利用者が何度リンクを取り直しても永久に直らない。実際に 8 回連打された。

**再利用（DynamoDB の条件付き書込が False を返す）だけが「使用済み」**であり、例外は違う。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters.google_oauth_flow import make_state
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.connect_web.app import create_app
from teamagent.hmac_durable_state import HMAC_STATE_SCOPE_ENV, HMAC_STATE_TABLE_ENV

_SECRET = "unit-test-state-secret"


class _Store:
    def __init__(self) -> None:
        self.puts: list[tuple[str, OAuthToken]] = []

    def put(self, user_email: str, token: OAuthToken) -> None:
        self.puts.append((user_email, token))


def _exchange(code: str) -> OAuthToken:
    return OAuthToken(refresh_token=f"rt-{code}", scopes=("a",), id_token=f"id-{code}")


def _verifier(token: str, client_id: str) -> dict[str, Any]:
    return {"email": "owner@vectorinc.co.jp", "email_verified": True}


def _client(monkeypatch: pytest.MonkeyPatch, *, state_consumer: Any) -> TestClient:
    monkeypatch.setenv("OAUTH_STATE_SECRET", _SECRET)
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    app = create_app(
        redirect_uri="https://example/oauth2/callback",
        exchange_fn=_exchange,
        store=_Store(),
        google_state_consumer=state_consumer,
        oauth_id_token_verifier=_verifier,
    )
    return TestClient(app)


def _callback(client: TestClient) -> Any:
    state = make_state("owner@vectorinc.co.jp")
    return client.get("/oauth2/callback", params={"code": "abc", "state": state})


def test_env_missing_is_reported_as_a_configuration_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 欠落は「設定不備・管理者へ」。**「使用済み」とは絶対に言わない**。"""

    def _raise(state: str) -> bool:
        raise RuntimeError("OAuth state のワンタイム消費先が未設定です")

    r = _callback(_client(monkeypatch, state_consumer=_raise))
    assert r.status_code == 500
    assert "システム側の設定不備です" in r.text
    assert "管理者" in r.text
    assert "使用済み" not in r.text
    assert "リンクが古い" not in r.text


def test_real_consume_state_once_without_env_hits_the_configuration_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """フェイクではなく **本番実装** を通す（渡辺さんが踏んだ経路そのもの）。

    `consume_state_once` は table/scope の env が無いと boto3 に触る前に `RuntimeError`
    を投げる。ここを握り潰して「使用済み」に化けさせない。
    """
    monkeypatch.delenv(HMAC_STATE_TABLE_ENV, raising=False)
    monkeypatch.delenv(HMAC_STATE_SCOPE_ENV, raising=False)
    r = _callback(_client(monkeypatch, state_consumer=None))
    assert r.status_code == 500
    assert "システム側の設定不備です" in r.text
    assert "使用済み" not in r.text


def test_transient_failure_is_reported_as_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    """DynamoDB のスロットリング等は「一時的なエラー」。設定不備とも使用済みとも言わない。"""

    def _raise(state: str) -> bool:
        raise TimeoutError("ProvisionedThroughputExceededException")

    r = _callback(_client(monkeypatch, state_consumer=_raise))
    assert r.status_code == 503
    assert "一時的なエラー" in r.text
    assert "使用済み" not in r.text
    assert "システム側の設定不備です" not in r.text


def test_genuine_reuse_still_says_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """本物の再利用（consumer が False を返す）だけが従来どおり「使用済み」。"""
    r = _callback(_client(monkeypatch, state_consumer=lambda state: False))
    assert r.status_code == 400
    assert "使用済み" in r.text
    assert "システム側の設定不備です" not in r.text


def test_success_path_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _callback(_client(monkeypatch, state_consumer=lambda state: True))
    assert r.status_code == 200
    assert "連携が完了" in r.text
