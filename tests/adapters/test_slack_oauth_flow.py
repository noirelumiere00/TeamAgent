"""Slack OAuth 同意フローの state 署名テスト（CSRF/リプレイ対策・課金0）。

実 Slack 認可は app 設定後だが、CSRF の要となる state 署名/検証（nonce + TTL 付き）は
stdlib のみで完結＝決定的に検証する。Google 版との違い（per-request nonce・TTL 失効）を確認。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest

from teamagent.adapters import slack_oauth_flow
from teamagent.adapters.slack_oauth_flow import (
    SLACK_USER_SCOPES,
    SlackOAuthConsentFlow,
    expected_bind_tag,
    make_state,
    verify_state,
    verify_state_detailed,
)

_SECRET = b"unit-test-secret"


def test_make_verify_state_roundtrip() -> None:
    state = make_state("S-Komata@Vectorinc.co.jp ", secret=_SECRET, now=1000)
    assert verify_state(state, secret=_SECRET, now=1000) == "s-komata@vectorinc.co.jp"  # 正規化


def test_state_is_per_request_nonce() -> None:
    """同一 email でも毎回異なる state になる（Google 版の決定論署名との違い）。"""
    s1 = make_state("a@x.com", secret=_SECRET, now=1000)
    s2 = make_state("a@x.com", secret=_SECRET, now=1000)
    assert s1 != s2  # nonce により毎回変わる
    assert verify_state(s1, secret=_SECRET, now=1000) == "a@x.com"
    assert verify_state(s2, secret=_SECRET, now=1000) == "a@x.com"


def test_verify_state_rejects_wrong_secret() -> None:
    state = make_state("a@x.com", secret=_SECRET, now=1000)
    assert verify_state(state, secret=b"attacker-secret", now=1000) is None


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


def test_verify_state_expired() -> None:
    """発行から TTL 超過で失効（リプレイ耐性）。"""
    state = make_state("a@x.com", secret=_SECRET, now=1000)
    assert verify_state(state, secret=_SECRET, now=1000 + 601, max_age_s=600) is None  # 失効
    assert verify_state(state, secret=_SECRET, now=1000 + 599, max_age_s=600) == "a@x.com"  # 有効


def test_default_state_ttl_is_1800_seconds() -> None:
    state = make_state("a@x.com", secret=_SECRET, now=1000)
    assert verify_state(state, secret=_SECRET, now=2800) == "a@x.com"
    assert verify_state(state, secret=_SECRET, now=2801) is None


def test_verify_state_rejects_future_issued() -> None:
    """発行時刻が未来すぎる（時計ズレ 60s 超）state は拒否。"""
    state = make_state("a@x.com", secret=_SECRET, now=5000)
    assert verify_state(state, secret=_SECRET, now=5000 - 120) is None


def test_slack_user_scopes_read_only() -> None:
    # 最小権限・読み取り先行。書込系(chat:write 等)は当初含めない。
    assert "search:read" in SLACK_USER_SCOPES
    assert "users:read" in SLACK_USER_SCOPES
    assert all(s.endswith(":read") or s.endswith(":history") for s in SLACK_USER_SCOPES)
    assert not any("write" in s for s in SLACK_USER_SCOPES)


def test_bound_state_roundtrip_keeps_three_field_body() -> None:
    state = make_state(
        "A@X.com",
        secret=_SECRET,
        now=1000,
        nonce="fixednonce",
        slack_user_id="U123",
        slack_team_id="T123",
    )

    detailed = verify_state_detailed(state, secret=_SECRET, now=1000)
    assert detailed is not None
    assert detailed.email == "a@x.com"
    expected = hmac.new(
        _SECRET,
        b"slackbind:v1:T123:U123",
        hashlib.sha256,
    ).hexdigest()[:32]
    assert detailed.bind_tag == expected
    assert detailed.bind_tag == expected_bind_tag("T123", "U123", secret=_SECRET)

    raw = base64.urlsafe_b64decode(state.encode()).decode()
    body, sig = raw.rsplit("|", 1)
    assert body.split("|") == ["a@x.com", "1000", f"fixednonce~{expected}"]
    assert detailed.sig == sig
    assert verify_state(state, secret=_SECRET, now=1000) == "a@x.com"


def test_detailed_legacy_state_has_no_bind_tag() -> None:
    state = make_state("a@x.com", secret=_SECRET, now=1000, nonce="fixednonce")
    detailed = verify_state_detailed(state, secret=_SECRET, now=1000)
    assert detailed is not None
    assert detailed.email == "a@x.com"
    assert detailed.bind_tag is None


@pytest.mark.parametrize(
    ("email", "slack_user_id", "slack_team_id"),
    [
        ("bad|email@example.com", None, None),
        ("bad~email@example.com", None, None),
        ("a@x.com", "U|123", "T123"),
        ("a@x.com", "U~123", "T123"),
        ("a@x.com", "U123", "T|123"),
        ("a@x.com", "U123", "T~123"),
    ],
)
def test_make_state_rejects_reserved_delimiters(
    email: str,
    slack_user_id: str | None,
    slack_team_id: str | None,
) -> None:
    with pytest.raises(ValueError):
        make_state(
            email,
            secret=_SECRET,
            now=1000,
            nonce="fixednonce",
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
        )


def test_make_state_output_unchanged_without_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """authorization_url の引数なし state は変更前の固定ベクトルと1バイトも変わらない。"""
    monkeypatch.setenv("CONNECT_SLACK_CLIENT_ID", "client-id")
    monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", "unit-test-secret")
    monkeypatch.setattr(slack_oauth_flow.time, "time", lambda: 1000)
    monkeypatch.setattr(slack_oauth_flow.secrets, "token_urlsafe", lambda _size: "fixednonce")

    url, state = SlackOAuthConsentFlow("https://example.com/callback").authorization_url("A@X.com ")

    assert state == (
        "YUB4LmNvbXwxMDAwfGZpeGVkbm9uY2V8YTM3ODc3YzM4NmYzNTQ5ZGU0ZTBmMDZlNjNi"
        "ZWFjZGZlMDE5M2QxYzQ0YzdhZTYzOGExNjYxZmZmMDA0MWFmMA=="
    )
    assert parse_qs(urlparse(url).query)["state"] == [state]


def test_authorization_url_binds_verified_slack_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONNECT_SLACK_CLIENT_ID", "client-id")
    monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", "unit-test-secret")
    monkeypatch.setattr(slack_oauth_flow.time, "time", lambda: 1000)
    monkeypatch.setattr(slack_oauth_flow.secrets, "token_urlsafe", lambda _size: "fixednonce")

    _url, state = SlackOAuthConsentFlow("https://example.com/callback").authorization_url(
        "a@x.com",
        slack_user_id="U123",
        slack_team_id="T123",
    )

    detailed = verify_state_detailed(state, secret=_SECRET, now=1000)
    assert detailed is not None
    assert detailed.bind_tag == expected_bind_tag("T123", "U123", secret=_SECRET)


def test_exchange_reads_identity_from_real_slack_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authed_user.id / team.id を含む Slack 実応答形から identity を構築する。"""
    from slack_sdk import web as slack_web

    response = {
        "ok": True,
        "authed_user": {
            "id": "U-CONSENTED",
            "access_token": "xoxp-test-token",
            "scope": "search:read,users:read",
        },
        "team": {"id": "T-WORKSPACE", "name": "Example"},
    }
    request: dict[str, str] = {}

    class _FakeWebClient:
        def oauth_v2_access(self, **kwargs: str) -> dict[str, object]:
            request.update(kwargs)
            return response

    monkeypatch.setattr(slack_web, "WebClient", _FakeWebClient)
    monkeypatch.setenv("CONNECT_SLACK_CLIENT_ID", "client-id")
    monkeypatch.setenv("CONNECT_SLACK_CLIENT_SECRET", "client-secret")

    token = SlackOAuthConsentFlow("https://example.com/callback").exchange("oauth-code")

    assert token.access_token == "xoxp-test-token"
    assert token.slack_user_id == "U-CONSENTED"
    assert token.team_id == "T-WORKSPACE"
    assert token.scopes == ("search:read", "users:read")
    assert request == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "code": "oauth-code",
        "redirect_uri": "https://example.com/callback",
    }
