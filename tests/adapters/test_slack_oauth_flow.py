"""Slack OAuth 同意フローの state 署名テスト（CSRF/リプレイ対策・課金0）。

実 Slack 認可は app 設定後だが、CSRF の要となる state 署名/検証（nonce + TTL 付き）は
stdlib のみで完結＝決定的に検証する。Google 版との違い（per-request nonce・TTL 失効）を確認。
"""

from __future__ import annotations

import base64

from teamagent.adapters.slack_oauth_flow import (
    SLACK_USER_SCOPES,
    make_state,
    verify_state,
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
