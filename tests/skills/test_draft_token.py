"""メール action 用 HMAC token の分離・ローテーション・fail-closed テスト。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from teamagent.hmac_keyring import HmacKeyConfigurationError
from teamagent.skills.morning_digest.draft_token import (
    _owner_hash,
    decode_draft_token,
    encode_draft_token,
    has_secret,
)

ME = "s-komata@vectorinc.co.jp"
_MAIL_PRIMARY = "mail-primary-" + "m" * 32
_MAIL_NEXT = "mail-next-" + "n" * 32
_LEGACY_DATABASE_URL = (
    "postgresql://teamagent:legacy-db-password@db.internal:5432/teamagent?sslmode=require"
)
_ROTATION_NOW = 2_000_000_000
_MAIL_TTL_S = 60 * 60 * 24

_HMAC_ENVS = (
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "REPORT_LINK_HMAC_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "DATABASE_URL",
    "SLACK_BOT_TOKEN",
)


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HMAC_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)


def _signed_draft_token(secret: str, *, expires: object, thread_id: str = "legacy-thread") -> str:
    payload = {"t": thread_id, "o": _owner_hash(ME), "e": expires}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).digest()[:16]

    def b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return f"{b64(raw)}.{b64(sig)}"


def _signature(token: str) -> tuple[bytes, bytes]:
    body_b64, sig_b64 = token.split(".", 1)

    def decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    return decode(body_b64), decode(sig_b64)


def test_roundtrip_returns_thread_id() -> None:
    token = encode_draft_token("thread-123", ME, now=1000)
    assert decode_draft_token(token, ME, now=1000) == "thread-123"


def test_expired_token_rejected() -> None:
    token = encode_draft_token("t1", ME, now=1000, ttl_s=60)
    assert decode_draft_token(token, ME, now=1061) is None


def test_owner_mismatch_rejected() -> None:
    token = encode_draft_token("t1", ME, now=1000)
    assert decode_draft_token(token, "attacker@evil.com", now=1000) is None


def test_tampered_payload_rejected() -> None:
    token = encode_draft_token("t1", ME, now=1000)
    _body, signature = token.split(".", 1)
    forged = encode_draft_token("t999", ME, now=1000).split(".", 1)[0] + "." + signature
    assert decode_draft_token(forged, ME, now=1000) is None


def test_wrong_secret_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encode_draft_token("t1", ME, now=1000)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_NEXT)
    assert decode_draft_token(token, ME, now=1000) is None


def test_no_secret_fails_closed_for_issue_and_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encode_draft_token("t1", ME, now=1000)
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET")
    assert has_secret() is False
    assert decode_draft_token(token, ME, now=1000) is None
    with pytest.raises(HmacKeyConfigurationError):
        encode_draft_token("t2", ME, now=1000)


@pytest.mark.parametrize("invalid", ["", "   ", "too-short"])
def test_empty_or_short_primary_fails_closed(monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", invalid)
    assert has_secret() is False
    with pytest.raises(HmacKeyConfigurationError):
        encode_draft_token("t1", ME, now=1000)


def test_garbage_token_rejected() -> None:
    assert decode_draft_token("not-a-token", ME, now=1000) is None
    assert decode_draft_token("", ME, now=1000) is None
    assert decode_draft_token("a.b.c", ME, now=1000) is None


def test_signed_malformed_expiry_is_fail_closed() -> None:
    token = _signed_draft_token(_MAIL_PRIMARY, expires="not-an-integer")
    assert decode_draft_token(token, ME, now=_ROTATION_NOW) is None


def test_does_not_fall_back_to_slack_or_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-" + "s" * 40)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    assert has_secret() is False
    with pytest.raises(HmacKeyConfigurationError):
        encode_draft_token("t1", ME, now=_ROTATION_NOW)


def test_legacy_database_key_is_explicit_previous_verification_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _signed_draft_token(
        _LEGACY_DATABASE_URL, expires=_ROTATION_NOW + _MAIL_TTL_S, thread_id="legacy-thread"
    )
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW) is None

    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW) is None  # 期限なしは不正
    monkeypatch.setenv(
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(_ROTATION_NOW + _MAIL_TTL_S)
    )
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW) == "legacy-thread"

    issued = encode_draft_token("new-thread", ME, now=_ROTATION_NOW)
    raw, signature = _signature(issued)
    primary_sig = hmac.new(_MAIL_PRIMARY.encode(), raw, hashlib.sha256).digest()[:16]
    previous_sig = hmac.new(_LEGACY_DATABASE_URL.encode(), raw, hashlib.sha256).digest()[:16]
    assert hmac.compare_digest(signature, primary_sig)
    assert not hmac.compare_digest(signature, previous_sig)


def test_previous_stops_verifying_after_explicit_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _signed_draft_token(
        _LEGACY_DATABASE_URL, expires=_ROTATION_NOW + _MAIL_TTL_S, thread_id="legacy-thread"
    )
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(_ROTATION_NOW + 30))
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW) == "legacy-thread"
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW + 31) is None


def test_same_primary_and_previous_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encode_draft_token("t1", ME, now=_ROTATION_NOW)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv(
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(_ROTATION_NOW + _MAIL_TTL_S)
    )
    assert has_secret() is False
    assert decode_draft_token(token, ME, now=_ROTATION_NOW) is None
    with pytest.raises(HmacKeyConfigurationError):
        encode_draft_token("t2", ME, now=_ROTATION_NOW)


def test_previous_deadline_cannot_be_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv(
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
        str(_ROTATION_NOW + _MAIL_TTL_S + 301),
    )
    with pytest.raises(HmacKeyConfigurationError):
        encode_draft_token("t1", ME, now=_ROTATION_NOW)


def test_database_primary_rejected_without_leaking_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    with pytest.raises(HmacKeyConfigurationError) as caught:
        encode_draft_token("t1", ME)
    assert _LEGACY_DATABASE_URL not in str(caught.value)
    assert "legacy-db-password" not in repr(caught.value)


def test_mail_and_report_primary_must_be_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _MAIL_PRIMARY)
    assert has_secret() is False
    with pytest.raises(HmacKeyConfigurationError):
        encode_draft_token("t1", ME)


def test_raw_thread_id_not_in_token() -> None:
    token = encode_draft_token("RAW_THREAD_SECRET_ID", ME, now=1000)
    assert "RAW_THREAD_SECRET_ID" not in token
