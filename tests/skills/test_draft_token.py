"""Mail-action HMAC token separation, rotation, TTL, and fail-closed tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from teamagent.hmac_keyring import (
    HMAC_MAX_ROLLOUT_OVERLAP_S,
    HMAC_PURPOSE_MAIL_DRAFT,
    HmacKeyring,
)
from teamagent.skills.morning_digest.draft_token import (
    _owner_hash,
    decode_draft_token,
    encode_draft_token,
    has_secret,
)
from teamagent.skills.morning_digest.event_token import (
    decode_event_token,
    encode_event_token,
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
    "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
    "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "MAIL_ACTION_TTL_S",
    "REPORT_LINK_HMAC_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
    "REPORT_LINK_HMAC_PRIMARY_GENERATION",
    "REPORT_LINK_HMAC_PREVIOUS_GENERATION",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "REPORT_LINK_TTL_S",
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


def _require_token(token: str | None) -> str:
    assert token is not None
    return token


def _signature(token: str) -> tuple[bytes, bytes]:
    body_b64, sig_b64 = token.split(".", 1)

    def decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    return decode(body_b64), decode(sig_b64)


def _payload(token: str) -> dict[str, object]:
    raw, _signature_bytes = _signature(token)
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def test_roundtrip_returns_thread_id() -> None:
    token = _require_token(encode_draft_token("thread-123", ME, now=1000))
    assert decode_draft_token(token, ME, now=1000) == "thread-123"


def test_draft_and_event_tokens_cannot_cross_verify() -> None:
    draft = _require_token(encode_draft_token("thread-123", ME, now=1000))
    event = _require_token(
        encode_event_token(
            start_iso="2026-07-15T14:00:00+09:00",
            end_iso="2026-07-15T15:00:00+09:00",
            title="meeting",
            owner_email=ME,
            now=1000,
        )
    )

    assert decode_event_token(draft, ME, now=1000) is None
    assert decode_draft_token(event, ME, now=1000) is None
    assert _payload(draft)["typ"] == "draft"
    assert _payload(event)["typ"] == "event"
    assert _payload(draft)["v"] == 2
    assert _payload(event)["v"] == 2


def test_token_expiry_is_exclusive_at_exact_boundary() -> None:
    token = _require_token(encode_draft_token("t1", ME, now=1000, ttl_s=60))
    assert decode_draft_token(token, ME, now=1059) == "t1"
    assert decode_draft_token(token, ME, now=1060) is None


@pytest.mark.parametrize("ttl_s", [1, _MAIL_TTL_S])
def test_explicit_ttl_accepts_inclusive_bounds(ttl_s: int) -> None:
    token = _require_token(encode_draft_token("t1", ME, now=1000, ttl_s=ttl_s))
    assert _payload(token)["e"] == 1000 + ttl_s


@pytest.mark.parametrize("ttl_s", [0, -1, _MAIL_TTL_S + 1, True, 1.0, "60", 10**100])
def test_explicit_ttl_outside_strict_integer_bounds_returns_none(ttl_s: object) -> None:
    # type: ignore[arg-type] -- adversarial runtime inputs intentionally violate the annotation.
    assert encode_draft_token("t1", ME, now=1000, ttl_s=ttl_s) is None


def test_configured_mail_ttl_is_shared_by_draft_issuance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_TTL_S", "3600")
    token = _require_token(encode_draft_token("t1", ME, now=1000))
    assert _payload(token)["e"] == 4600


@pytest.mark.parametrize(
    "invalid", ["", " 60", "60 ", "+60", "0", "86401", "１２", "١٢", "9" * 10_000]
)
def test_present_invalid_mail_ttl_suppresses_issuance(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    monkeypatch.setenv("MAIL_ACTION_TTL_S", invalid)
    assert has_secret() is False
    assert encode_draft_token("t1", ME, now=1000) is None
    assert encode_draft_token("t1", ME, now=1000, ttl_s=60) is None


def test_owner_mismatch_rejected() -> None:
    token = _require_token(encode_draft_token("t1", ME, now=1000))
    assert decode_draft_token(token, "attacker@evil.com", now=1000) is None


def test_tampered_payload_rejected() -> None:
    token = _require_token(encode_draft_token("t1", ME, now=1000))
    _body, signature = token.split(".", 1)
    forged_body = _require_token(encode_draft_token("t999", ME, now=1000)).split(".", 1)[0]
    assert decode_draft_token(forged_body + "." + signature, ME, now=1000) is None


def test_wrong_secret_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _require_token(encode_draft_token("t1", ME, now=1000))
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_NEXT)
    assert decode_draft_token(token, ME, now=1000) is None


def test_no_secret_fails_closed_for_issue_and_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _require_token(encode_draft_token("t1", ME, now=1000))
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET")
    assert has_secret() is False
    assert decode_draft_token(token, ME, now=1000) is None
    assert encode_draft_token("t2", ME, now=1000) is None


@pytest.mark.parametrize("invalid", ["", "   ", "too-short"])
def test_empty_or_short_primary_fails_closed(monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", invalid)
    assert has_secret() is False
    assert encode_draft_token("t1", ME, now=1000) is None


def test_garbage_token_rejected() -> None:
    assert decode_draft_token("not-a-token", ME, now=1000) is None
    assert decode_draft_token("", ME, now=1000) is None
    assert decode_draft_token("a.b.c", ME, now=1000) is None


@pytest.mark.parametrize(
    "malformed_expiry",
    ["not-an-integer", " 2000000001", "+2000000001", "２００００００００１", True, 1.5, 10**100],
)
def test_signed_malformed_expiry_is_fail_closed(malformed_expiry: object) -> None:
    token = _signed_draft_token(_MAIL_PRIMARY, expires=malformed_expiry)
    assert decode_draft_token(token, ME, now=_ROTATION_NOW) is None


def test_does_not_fall_back_to_slack_or_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-" + "s" * 40)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    assert has_secret() is False
    assert encode_draft_token("t1", ME, now=_ROTATION_NOW) is None


def test_legacy_database_key_is_bounded_previous_verification_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _signed_draft_token(
        _LEGACY_DATABASE_URL, expires=_ROTATION_NOW + _MAIL_TTL_S, thread_id="legacy-thread"
    )
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW) is None

    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW) is None
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW) is None
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    assert decode_draft_token(legacy, ME, now=_ROTATION_NOW) == "legacy-thread"

    issued = _require_token(encode_draft_token("new-thread", ME, now=_ROTATION_NOW))
    raw, signature = _signature(issued)
    primary_sig = HmacKeyring(_MAIL_PRIMARY.encode(), (_MAIL_PRIMARY.encode(),)).sign(
        raw,
        purpose=HMAC_PURPOSE_MAIL_DRAFT,
        digest_bytes=16,
    )
    previous_sig = hmac.new(_LEGACY_DATABASE_URL.encode(), raw, hashlib.sha256).digest()[:16]
    assert hmac.compare_digest(signature, primary_sig)
    assert not hmac.compare_digest(signature, previous_sig)


def test_verifier_first_rollout_covers_last_old_issuer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutover = _ROTATION_NOW + HMAC_MAX_ROLLOUT_OVERLAP_S
    last_old_issue = cutover - 1
    expires = last_old_issue + _MAIL_TTL_S
    legacy = _signed_draft_token(
        _LEGACY_DATABASE_URL,
        expires=expires,
        thread_id="last-old-issuer-token",
    )
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")

    assert decode_draft_token(legacy, ME, now=last_old_issue) == "last-old-issuer-token"
    assert decode_draft_token(legacy, ME, now=expires - 1) == "last-old-issuer-token"
    assert decode_draft_token(legacy, ME, now=expires) is None


def test_previous_key_deadline_is_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    deadline = _ROTATION_NOW + HMAC_MAX_ROLLOUT_OVERLAP_S + _MAIL_TTL_S
    legacy = _signed_draft_token(
        _LEGACY_DATABASE_URL,
        expires=deadline + 60,
        thread_id="deadline-probe",
    )
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    assert decode_draft_token(legacy, ME, now=deadline - 1) == "deadline-probe"
    assert decode_draft_token(legacy, ME, now=deadline) is None


def test_same_primary_and_previous_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _require_token(encode_draft_token("t1", ME, now=_ROTATION_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    assert has_secret() is False
    assert decode_draft_token(token, ME, now=_ROTATION_NOW) is None
    assert encode_draft_token("t2", ME, now=_ROTATION_NOW) is None


def test_database_primary_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    assert has_secret() is False
    assert encode_draft_token("t1", ME) is None


def test_mail_and_report_primary_must_be_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _MAIL_PRIMARY)
    assert has_secret() is False
    assert encode_draft_token("t1", ME) is None


def test_raw_thread_id_not_in_token() -> None:
    token = _require_token(encode_draft_token("RAW_THREAD_SECRET_ID", ME, now=1000))
    assert "RAW_THREAD_SECRET_ID" not in token
