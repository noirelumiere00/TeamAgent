"""report_link_token（レポート短縮リンクの署名トークン）の単体テスト。

round-trip・改竄・失効・prefix/bucket allowlist・鍵未設定 fail-closed・has_secret・
typ ドメイン分離・SLACK_BOT_TOKEN へ fallback しないこと（鍵不一致 footgun 排除）を検証。
"""

from __future__ import annotations

import time

import pytest

from teamagent.adapters.report_link_token import (
    decode_report_token,
    encode_report_token,
    has_secret,
)

_BUCKET = "teamagent-dev-raw-files"
_KEY = "vseo-reports/abc123.html"


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "test-secret-xyz")
    monkeypatch.setenv("VSEO_REPORT_BUCKET", _BUCKET)


def test_round_trip() -> None:
    token = encode_report_token(_BUCKET, _KEY)
    assert "?" not in token and "/" not in token  # 短くクエリ/スラッシュ無し（openclaw耐性）
    assert decode_report_token(token) == (_BUCKET, _KEY)


def test_proposals_prefix_ok() -> None:
    assert decode_report_token(encode_report_token(_BUCKET, "vseo-proposals/d.pdf")) == (
        _BUCKET,
        "vseo-proposals/d.pdf",
    )


def test_tamper_rejected() -> None:
    token = encode_report_token(_BUCKET, _KEY)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert decode_report_token(tampered) is None


def test_expired_rejected() -> None:
    token = encode_report_token(_BUCKET, _KEY, now=int(time.time()) - 100, ttl_s=10)
    assert decode_report_token(token) is None


def test_foreign_prefix_rejected() -> None:
    assert decode_report_token(encode_report_token(_BUCKET, "secrets/leak.txt")) is None


def test_foreign_bucket_rejected() -> None:
    assert decode_report_token(encode_report_token("other-bucket", _KEY)) is None


def test_no_key_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encode_report_token(_BUCKET, _KEY)  # 鍵ありで発行
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET", raising=False)
    assert not has_secret()
    assert decode_report_token(token) is None  # 鍵が無ければ何も信用しない


def test_has_secret_reflects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert has_secret() is True
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "   ")  # 空白のみは未設定扱い
    assert has_secret() is False


def test_does_not_fall_back_to_slack_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAIL_ACTION_HMAC_SECRET が無く SLACK_BOT_TOKEN だけある時、署名鍵に流用しない。

    流用すると connect-web(SLACK_BOT_TOKEN 非保持) と鍵不一致→全件404 になるため。
    """
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-be-used")
    assert has_secret() is False
    assert decode_report_token(encode_report_token(_BUCKET, _KEY)) is None


def test_type_tag_blocks_cross_token_transfer() -> None:
    """他用途トークン(draft: typ 無し・owner付き)は同一鍵でも report として受理しない。"""
    from teamagent.skills.morning_digest.draft_token import encode_draft_token

    draft = encode_draft_token("thread-123", "someone@vectorinc.co.jp")
    assert decode_report_token(draft) is None  # typ 不一致 or フィールド不足で拒否
