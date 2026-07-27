"""OAuth 同意フローの state 署名テスト（CSRF対策・課金0）。

実 Google 認可は W1 後だが、CSRF の要となる state 署名/検証は stdlib のみで完結＝決定的に検証。
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from teamagent.adapters.google_oauth_flow import (
    WORKSPACE_SCOPES,
    OAuthConsentFlow,
    consume_state_once,
    make_state,
    verify_state,
)

_SECRET = b"unit-test-secret"


def test_make_verify_state_roundtrip() -> None:
    state = make_state("S-Komata@Vectorinc.co.jp ", secret=_SECRET, now=1000)
    assert (
        verify_state(state, secret=_SECRET, now=1000) == "s-komata@vectorinc.co.jp"
    )  # 正規化


def test_state_is_per_request_nonce() -> None:
    first = make_state("a@x.com", secret=_SECRET, now=1000)
    second = make_state("a@x.com", secret=_SECRET, now=1000)
    assert first != second
    assert verify_state(first, secret=_SECRET, now=1000) == "a@x.com"
    assert verify_state(second, secret=_SECRET, now=1000) == "a@x.com"


def test_verify_state_rejects_wrong_secret() -> None:
    state = make_state("a@x.com", secret=_SECRET, now=1000)
    assert (
        verify_state(state, secret=b"attacker-secret", now=1000) is None
    )  # 別鍵では検証失敗


def test_verify_state_rejects_garbage() -> None:
    assert verify_state("not-valid-base64-!!!", secret=_SECRET) is None
    assert verify_state("", secret=_SECRET) is None


def test_verify_state_rejects_tampered_email() -> None:
    """email 部分を書き換えても署名が一致しない（なりすまし防止）。"""
    state = make_state("a@x.com", secret=_SECRET, now=1000, nonce="fixednonce")
    raw = base64.urlsafe_b64decode(state.encode()).decode()
    body, sig = raw.rsplit("|", 1)
    _email, issued, nonce = body.split("|")
    tampered_body = "|".join(("evil@x.com", issued, nonce))
    tampered = base64.urlsafe_b64encode(f"{tampered_body}|{sig}".encode()).decode()
    assert verify_state(tampered, secret=_SECRET, now=1000) is None


def test_verify_state_default_ttl_is_1800_seconds() -> None:
    state = make_state("a@x.com", secret=_SECRET, now=1000)
    assert verify_state(state, secret=_SECRET, now=2800) == "a@x.com"
    assert verify_state(state, secret=_SECRET, now=2801) is None


def test_verify_state_rejects_future_issued() -> None:
    state = make_state("a@x.com", secret=_SECRET, now=5000)
    assert verify_state(state, secret=_SECRET, now=4880) is None


def test_authorization_url_pins_login_hint_and_company_hd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeFlow:
        def __init__(self) -> None:
            self.params: dict[str, str] = {}

        def authorization_url(self, **kwargs: str) -> tuple[str, str]:
            self.params = kwargs
            return "https://accounts.google.com/o/oauth2/auth?fake=1", kwargs["state"]

    fake = _FakeFlow()
    monkeypatch.setenv("OAUTH_STATE_SECRET", "unit-test-state-secret")
    monkeypatch.setenv("CONNECT_SEARCH_ALLOWED_HD", "VectorInc.co.jp")
    monkeypatch.setattr(OAuthConsentFlow, "_flow", lambda _self: fake)
    url, state = OAuthConsentFlow("https://example.test/oauth2/callback").authorization_url(
        " Owner@VectorInc.co.jp "
    )
    assert url.startswith("https://accounts.google.com/")
    assert fake.params["state"] == state
    assert fake.params["login_hint"] == "owner@vectorinc.co.jp"
    assert fake.params["hd"] == "vectorinc.co.jp"


def test_exchange_returns_id_token_for_callback_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Credentials:
        refresh_token = "refresh-token"
        scopes = ("openid",)
        id_token = "signed-id-token"

    class _FakeFlow:
        credentials = _Credentials()

        def fetch_token(self, *, code: str) -> None:
            assert code == "authorization-code"

    monkeypatch.setattr(OAuthConsentFlow, "_flow", lambda _self: _FakeFlow())
    token = OAuthConsentFlow("https://example.test/oauth2/callback").exchange(
        "authorization-code"
    )
    assert token.refresh_token == "refresh-token"
    assert token.id_token == "signed-id-token"


def test_consume_state_once_uses_conditional_update() -> None:
    class _ConditionalCheckFailed(Exception):
        def __init__(self) -> None:
            self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}

    class _FakeDynamoDb:
        def __init__(self) -> None:
            self.seen: set[str] = set()
            self.calls: list[dict[str, Any]] = []

        def update_item(self, **kwargs: Any) -> None:
            assert kwargs["TableName"] == "teamagent-dev-hmac-state"
            assert kwargs["Key"]["scope"] == {"S": "teamagent/dev"}
            assert kwargs["ConditionExpression"] == "attribute_not_exists(#record)"
            assert kwargs["ExpressionAttributeNames"] == {"#record": "record"}
            assert kwargs["ExpressionAttributeValues"][":expires"] == {"N": "2800"}
            record = str(kwargs["Key"]["record"]["S"])
            if record in self.seen:
                raise _ConditionalCheckFailed
            self.seen.add(record)
            self.calls.append(kwargs)

    dynamodb = _FakeDynamoDb()
    state = make_state("a@x.com", secret=_SECRET, now=1000, nonce="one-use")
    args = {
        "client": dynamodb,
        "now": 1000,
        "table_name": "teamagent-dev-hmac-state",
        "scope": "teamagent/dev",
    }
    assert consume_state_once(state, **args) is True
    assert consume_state_once(state, **args) is False
    assert len(dynamodb.calls) == 1


def test_workspace_scopes_write_is_gmail_and_calendar_only() -> None:
    # 書込スコープは gmail.modify（読み+下書き作成）と calendar.events（v0.3 Task2・
    # insert のみ実装/破壊系は _GCalSafePolicy 物理封鎖）の2つだけ。identity scope を除く
    # Google Workspace API の他スコープは readonly。
    # Internal アプリ=審査不要。
    assert len(WORKSPACE_SCOPES) == 10
    assert "openid" in WORKSPACE_SCOPES
    assert "https://www.googleapis.com/auth/userinfo.email" in WORKSPACE_SCOPES
    non_readonly_scopes = {s for s in WORKSPACE_SCOPES if not s.endswith(".readonly")}
    assert non_readonly_scopes == {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar.events",
    }
    write_scopes = non_readonly_scopes - {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    }
    assert write_scopes == {
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar.events",
    }
    assert not any(s.endswith("gmail.readonly") for s in WORKSPACE_SCOPES)
    # calendar は readonly（freebusy/list 用）と events（insert 用）の両方を持つ。
    assert "https://www.googleapis.com/auth/calendar.readonly" in WORKSPACE_SCOPES
