"""下書きボタン用 HMAC 署名トークンのセキュリティテスト。

偽造・改竄・期限切れ・所有者不一致・鍵未設定 を fail-closed で拒否することを固定する。
"""

from __future__ import annotations

import pytest

from teamagent.skills.morning_digest.draft_token import (
    decode_draft_token,
    encode_draft_token,
)

ME = "s-komata@vectorinc.co.jp"
SECRET = "test-secret-key"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", SECRET)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)


def test_roundtrip_returns_thread_id() -> None:
    tok = encode_draft_token("thread-123", ME, now=1000)
    assert decode_draft_token(tok, ME, now=1000) == "thread-123"


def test_expired_token_rejected() -> None:
    tok = encode_draft_token("t1", ME, now=1000, ttl_s=60)
    assert decode_draft_token(tok, ME, now=1000 + 61) is None  # 61秒後＝失効


def test_owner_mismatch_rejected() -> None:
    tok = encode_draft_token("t1", ME, now=1000)
    # 別人が押しても所有者ハッシュ不一致で拒否（DM 越しの取り違え/転送対策）。
    assert decode_draft_token(tok, "attacker@evil.com", now=1000) is None


def test_tampered_payload_rejected() -> None:
    tok = encode_draft_token("t1", ME, now=1000)
    _body, sig = tok.split(".", 1)
    # body を別スレッドに差し替え（署名はそのまま）→ HMAC 不一致で拒否。
    forged = encode_draft_token("t999", ME, now=1000).split(".", 1)[0] + "." + sig
    assert decode_draft_token(forged, ME, now=1000) is None


def test_wrong_secret_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    tok = encode_draft_token("t1", ME, now=1000)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "different-secret")
    assert decode_draft_token(tok, ME, now=1000) is None  # 鍵が違えば偽造扱い


def test_no_secret_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    tok = encode_draft_token("t1", ME, now=1000)
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert decode_draft_token(tok, ME, now=1000) is None  # 鍵無し＝何も信用しない


def test_garbage_token_rejected() -> None:
    assert decode_draft_token("not-a-token", ME, now=1000) is None
    assert decode_draft_token("", ME, now=1000) is None
    assert decode_draft_token("a.b.c", ME, now=1000) is None


def test_falls_back_to_slack_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # MAIL_ACTION_HMAC_SECRET 未設定なら SLACK_BOT_TOKEN を鍵に使う（両プロセスが保持）。
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-shared")
    tok = encode_draft_token("t1", ME, now=1000)
    assert decode_draft_token(tok, ME, now=1000) == "t1"


def test_raw_thread_id_not_in_token() -> None:
    # 生 thread_id を value にそのまま出さない（base64 署名で隠す＝G3）。
    tok = encode_draft_token("RAW_THREAD_SECRET_ID", ME, now=1000)
    assert "RAW_THREAD_SECRET_ID" not in tok
