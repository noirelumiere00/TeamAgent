"""OAuth 同意フローの state 署名テスト（CSRF対策・課金0）。

実 Google 認可は W1 後だが、CSRF の要となる state 署名/検証は stdlib のみで完結＝決定的に検証。
"""

from __future__ import annotations

import base64

from teamagent.adapters.google_oauth_flow import (
    WORKSPACE_SCOPES,
    make_state,
    verify_state,
)

_SECRET = b"unit-test-secret"


def test_make_verify_state_roundtrip() -> None:
    state = make_state("S-Komata@Vectorinc.co.jp ", secret=_SECRET)
    assert verify_state(state, secret=_SECRET) == "s-komata@vectorinc.co.jp"  # 正規化


def test_verify_state_rejects_wrong_secret() -> None:
    state = make_state("a@x.com", secret=_SECRET)
    assert verify_state(state, secret=b"attacker-secret") is None  # 別鍵では検証失敗


def test_verify_state_rejects_garbage() -> None:
    assert verify_state("not-valid-base64-!!!", secret=_SECRET) is None
    assert verify_state("", secret=_SECRET) is None


def test_verify_state_rejects_tampered_email() -> None:
    """email 部分を書き換えても署名が一致しない（なりすまし防止）。"""
    state = make_state("a@x.com", secret=_SECRET)
    raw = base64.urlsafe_b64decode(state.encode()).decode()
    _, sig = raw.rsplit(".", 1)
    tampered = base64.urlsafe_b64encode(f"evil@x.com.{sig}".encode()).decode()
    assert verify_state(tampered, secret=_SECRET) is None


def test_workspace_scopes_gmail_modify_rest_readonly() -> None:
    # Gmail のみ modify（読み+下書き作成）。他6つは readonly。Internal アプリ=審査不要。
    assert len(WORKSPACE_SCOPES) == 7
    assert WORKSPACE_SCOPES[0].endswith("/auth/gmail.modify")
    assert all(s.endswith(".readonly") for s in WORKSPACE_SCOPES[1:])
    assert not any(s.endswith("gmail.readonly") for s in WORKSPACE_SCOPES)
