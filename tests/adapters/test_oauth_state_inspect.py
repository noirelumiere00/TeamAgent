"""google_oauth_flow.inspect_state（state 失敗理由の出し分け）と verify_state 不変のテスト。

4 分岐: ok / bad_signature / expired / malformed。connect-web の callback はこれで
CONNECT-S01（署名不一致）と CONNECT-S02（期限切れ）を区別する。
"""

from __future__ import annotations

import base64

import pytest

from teamagent.adapters.google_oauth_flow import inspect_state, make_state, verify_state

_SECRET = b"unit-test-state-secret"
_EMAIL = "taro@vectorinc.co.jp"


def test_ok_returns_email() -> None:
    state = make_state(_EMAIL, secret=_SECRET, now=1_000_000, nonce="n1")
    assert inspect_state(state, secret=_SECRET, now=1_000_100) == ("ok", _EMAIL)


def test_bad_signature_when_one_character_is_retyped() -> None:
    """LLM の再タイプ事故（1 文字違い）は bad_signature。email は署名未検証なので返さない。"""
    state = make_state(_EMAIL, secret=_SECRET, now=1_000_000, nonce="n1")
    raw = base64.urlsafe_b64decode(state.encode("ascii")).decode("utf-8")
    body, sig = raw.rsplit("|", 1)
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")  # 末尾 1 文字だけ違う署名
    tampered = base64.urlsafe_b64encode(f"{body}|{flipped}".encode()).decode("ascii")
    assert inspect_state(tampered, secret=_SECRET, now=1_000_100) == ("bad_signature", None)


def test_bad_signature_when_signed_with_another_secret() -> None:
    state = make_state(_EMAIL, secret=b"other-secret", now=1_000_000, nonce="n1")
    assert inspect_state(state, secret=_SECRET, now=1_000_100) == ("bad_signature", None)


def test_expired_returns_signed_email() -> None:
    """署名は正しいので期限切れページに本人（マスク）を出せる。"""
    state = make_state(_EMAIL, secret=_SECRET, now=1_000_000, nonce="n1")
    assert inspect_state(state, secret=_SECRET, now=1_000_000 + 1801) == ("expired", _EMAIL)
    # 境界: ちょうど 1800s は有効
    assert inspect_state(state, secret=_SECRET, now=1_000_000 + 1800)[0] == "ok"


def test_future_issued_state_is_expired() -> None:
    state = make_state(_EMAIL, secret=_SECRET, now=1_000_000, nonce="n1")
    assert inspect_state(state, secret=_SECRET, now=1_000_000 - 61)[0] == "expired"


@pytest.mark.parametrize(
    "state",
    [
        "garbage",
        "",
        base64.urlsafe_b64encode(b"no-separators").decode("ascii"),
        base64.urlsafe_b64encode(b"a|b|c").decode("ascii"),  # sig が無い形（4 要素でない）
        base64.urlsafe_b64encode(b"a|notint|c|sig").decode("ascii"),
        "あい",  # 非 ASCII
    ],
)
def test_malformed(state: str) -> None:
    assert inspect_state(state, secret=_SECRET, now=1_000_100) == ("malformed", None)


def test_verify_state_behaviour_is_unchanged() -> None:
    """verify_state は ok のときだけ email、それ以外は None（従来契約）。"""
    state = make_state(_EMAIL, secret=_SECRET, now=1_000_000, nonce="n1")
    assert verify_state(state, secret=_SECRET, now=1_000_100) == _EMAIL
    assert verify_state(state, secret=_SECRET, now=1_000_000 + 1801) is None
    assert verify_state("garbage", secret=_SECRET, now=1_000_100) is None
    assert verify_state(state, secret=b"other", now=1_000_100) is None
