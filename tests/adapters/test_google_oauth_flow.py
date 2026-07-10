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


def test_workspace_scopes_write_is_gmail_and_calendar_only() -> None:
    # 書込スコープは gmail.modify（読み+下書き作成）と calendar.events（v0.3 Task2・
    # insert のみ実装/破壊系は _GCalSafePolicy 物理封鎖）の2つだけ。他は readonly。
    # Internal アプリ=審査不要。
    assert len(WORKSPACE_SCOPES) == 8
    assert WORKSPACE_SCOPES[0].endswith("/auth/gmail.modify")
    write_scopes = {s for s in WORKSPACE_SCOPES if not s.endswith(".readonly")}
    assert write_scopes == {
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar.events",
    }
    assert not any(s.endswith("gmail.readonly") for s in WORKSPACE_SCOPES)
    # calendar は readonly（freebusy/list 用）と events（insert 用）の両方を持つ。
    assert "https://www.googleapis.com/auth/calendar.readonly" in WORKSPACE_SCOPES
