"""connect_web /slack/oauth/callback のテスト（実Slack0・実DB0・実KMS0）。

state検証(CSRF・nonce+TTL)→code交換→xoxp保存 の経路を、slack_exchange_fn/slack_store を
注入して検証する（Google 版 test_callback.py と対称）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters.oauth_token_store import SlackOAuthToken
from teamagent.adapters.slack_oauth_flow import make_state
from teamagent.connect_web.app import create_app

_SECRET = "unit-test-slack-state-secret"


class _FakeSlackStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, SlackOAuthToken]] = []

    def put(self, user_email: str, token: SlackOAuthToken) -> None:
        self.puts.append((user_email, token))


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slack_exchange_fn: Any = None,
    slack_store: _FakeSlackStore | None = None,
) -> tuple[TestClient, _FakeSlackStore]:
    monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", _SECRET)
    st = slack_store or _FakeSlackStore()
    if slack_exchange_fn is None:

        def slack_exchange_fn(code: str) -> SlackOAuthToken:
            return SlackOAuthToken(
                access_token=f"xoxp-{code}",
                scopes=("search:read", "users:read"),
                slack_user_id="U123",
                team_id="T456",
            )

    app = create_app(
        slack_redirect_uri="https://example/slack/oauth/callback",
        slack_exchange_fn=slack_exchange_fn,
        slack_store=st,
    )
    return TestClient(app), st


def test_slack_callback_success_stores_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    state = make_state("Owner@vectorinc.co.jp")  # SLACK_OAUTH_STATE_SECRET から署名
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 200
    assert "Slack連携が完了" in r.text
    # email は正規化(lower)・token は exchange の戻り
    assert len(store.puts) == 1
    assert store.puts[0][0] == "owner@vectorinc.co.jp"
    assert store.puts[0][1].access_token == "xoxp-abc"


def test_slack_callback_rejects_tampered_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": "garbage"})
    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert store.puts == []  # 保存されない


def test_slack_callback_missing_params(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    r = client.get("/slack/oauth/callback", params={"code": "abc"})  # state 欠如
    assert r.status_code == 400
    assert store.puts == []


def test_slack_callback_user_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _client(monkeypatch)
    r = client.get("/slack/oauth/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert "キャンセル" in r.text
    assert store.puts == []


def test_slack_callback_exchange_failure_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(code: str) -> SlackOAuthToken:
        raise RuntimeError("oauth_v2_access down")

    client, store = _client(monkeypatch, slack_exchange_fn=boom)
    state = make_state("owner@vectorinc.co.jp")
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 500
    assert "連携に失敗" in r.text
    assert store.puts == []


def test_slack_callback_does_not_leak_token_in_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功ページに xoxp を出さない（G8）。"""
    client, _ = _client(monkeypatch)
    state = make_state("owner@vectorinc.co.jp")
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert "xoxp-abc" not in r.text
