"""Report-link token issuance, verification, TTL, and rotation tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from teamagent.adapters.report_link_token import (
    _default_ttl_s,
    decode_report_token,
    encode_report_token,
    has_secret,
    is_allowed_key,
)
from teamagent.hmac_keyring import (
    HMAC_MAX_ROLLOUT_OVERLAP_S,
    HMAC_PURPOSE_REPORT_LINK,
    HmacKeyring,
)

_BUCKET = "teamagent-dev-raw-files"
_KEY = "vseo-reports/abc123.html"
_REPORT_PRIMARY = "report-primary-" + "r" * 32
_MAIL_PRIMARY = "mail-primary-" + "m" * 32
_LEGACY_DATABASE_URL = (
    "postgresql://teamagent:legacy-db-password@db.internal:5432/teamagent?sslmode=require"
)
_ROTATION_NOW = 2_000_000_000
_REPORT_TTL_S = 60 * 60 * 24 * 7

_HMAC_ENVS = (
    "REPORT_LINK_HMAC_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
    "REPORT_LINK_HMAC_PRIMARY_GENERATION",
    "REPORT_LINK_HMAC_PREVIOUS_GENERATION",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "REPORT_LINK_TTL_S",
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
    "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "MAIL_ACTION_TTL_S",
    "DATABASE_URL",
    "SLACK_BOT_TOKEN",
)


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HMAC_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _REPORT_PRIMARY)
    monkeypatch.setenv("VSEO_REPORT_BUCKET", _BUCKET)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _signed_report_payload(secret: str, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).digest()[:16]
    return f"{_b64(raw)}.{_b64(sig)}"


def _legacy_report_token(
    secret: str,
    *,
    now: int = _ROTATION_NOW - 10,
    ttl_s: int = _REPORT_TTL_S,
    expires: object | None = None,
    key: str = _KEY,
) -> str:
    payload = {
        "typ": "r",
        "b": _BUCKET,
        "k": key,
        "r": "",
        "e": now + ttl_s if expires is None else expires,
    }
    return _signed_report_payload(secret, payload)


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


def test_round_trip() -> None:
    token = _require_token(encode_report_token(_BUCKET, _KEY))
    assert "?" not in token and "/" not in token
    assert decode_report_token(token) == (_BUCKET, _KEY, "")


def test_region_round_trip() -> None:
    token = _require_token(encode_report_token(_BUCKET, _KEY, region="us-west-2"))
    assert decode_report_token(token) == (_BUCKET, _KEY, "us-west-2")


def test_proposals_prefix_ok() -> None:
    token = _require_token(encode_report_token(_BUCKET, "vseo-proposals/d.pdf"))
    assert decode_report_token(token) == (_BUCKET, "vseo-proposals/d.pdf", "")


def test_is_allowed_key() -> None:
    assert is_allowed_key("vseo-reports/a.html")
    assert is_allowed_key("vseo-proposals/d.pdf")
    assert not is_allowed_key("custom-prefix/a.html")
    assert not is_allowed_key("")


def test_tamper_rejected() -> None:
    token = _require_token(encode_report_token(_BUCKET, _KEY))
    body, sig = token.split(".", 1)
    flip = "A" if body[5] != "A" else "B"
    assert decode_report_token(body[:5] + flip + body[6:] + "." + sig) is None


def test_token_expiry_is_exclusive_at_exact_boundary() -> None:
    token = _require_token(encode_report_token(_BUCKET, _KEY, now=1000, ttl_s=60))
    assert decode_report_token(token, now=1059) == (_BUCKET, _KEY, "")
    assert decode_report_token(token, now=1060) is None


def test_foreign_prefix_rejected() -> None:
    token = _require_token(encode_report_token(_BUCKET, "secrets/leak.txt"))
    assert decode_report_token(token) is None


def test_foreign_bucket_rejected() -> None:
    token = _require_token(encode_report_token("other-bucket", _KEY))
    assert decode_report_token(token) is None


def test_no_key_fail_closed_for_issue_and_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _require_token(encode_report_token(_BUCKET, _KEY))
    monkeypatch.delenv("REPORT_LINK_HMAC_SECRET")
    assert not has_secret()
    assert decode_report_token(token) is None
    assert encode_report_token(_BUCKET, _KEY) is None


@pytest.mark.parametrize("invalid", ["", "   ", "too-short"])
def test_empty_or_short_primary_fail_closed(monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", invalid)
    assert has_secret() is False
    assert encode_report_token(_BUCKET, _KEY) is None


def test_does_not_fall_back_to_mail_slack_or_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_LINK_HMAC_SECRET")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-" + "s" * 40)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    assert has_secret() is False
    assert (
        decode_report_token(_legacy_report_token(_LEGACY_DATABASE_URL), now=_ROTATION_NOW) is None
    )
    assert encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW) is None


def test_legacy_database_key_is_bounded_previous_verification_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_report_token(_LEGACY_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)

    assert decode_report_token(legacy, now=_ROTATION_NOW) is None
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    assert decode_report_token(legacy, now=_ROTATION_NOW) is None
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    assert decode_report_token(legacy, now=_ROTATION_NOW) is None
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY", "1")
    assert decode_report_token(legacy, now=_ROTATION_NOW) == (_BUCKET, _KEY, "")

    issued = _require_token(encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW))
    raw, signature = _signature(issued)
    primary_sig = HmacKeyring(_REPORT_PRIMARY.encode(), (_REPORT_PRIMARY.encode(),)).sign(
        raw,
        purpose=HMAC_PURPOSE_REPORT_LINK,
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
    expires = last_old_issue + _REPORT_TTL_S
    legacy = _legacy_report_token(
        _LEGACY_DATABASE_URL,
        expires=expires,
    )
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY", "1")

    assert decode_report_token(legacy, now=last_old_issue) == (_BUCKET, _KEY, "")
    assert decode_report_token(legacy, now=expires - 1) == (_BUCKET, _KEY, "")
    assert decode_report_token(legacy, now=expires) is None


def test_previous_key_deadline_is_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    deadline = _ROTATION_NOW + HMAC_MAX_ROLLOUT_OVERLAP_S + _REPORT_TTL_S
    legacy = _legacy_report_token(_LEGACY_DATABASE_URL, expires=deadline + 60)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY", "1")
    assert decode_report_token(legacy, now=deadline - 1) == (_BUCKET, _KEY, "")
    assert decode_report_token(legacy, now=deadline) is None


def test_invalid_previous_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _require_token(encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW))
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _REPORT_PRIMARY)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    assert has_secret() is False
    assert decode_report_token(token, now=_ROTATION_NOW) is None
    assert encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW) is None


def test_database_primary_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    assert has_secret() is False
    assert encode_report_token(_BUCKET, _KEY) is None


def test_report_and_mail_primary_must_be_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _REPORT_PRIMARY)
    assert has_secret() is False
    assert encode_report_token(_BUCKET, _KEY) is None


def test_type_tag_blocks_cross_token_transfer() -> None:
    draft_shaped = _signed_report_payload(
        _REPORT_PRIMARY,
        {"t": "thread-123", "o": "owner-hash", "e": _ROTATION_NOW + 60},
    )
    assert decode_report_token(draft_shaped, now=_ROTATION_NOW) is None


@pytest.mark.parametrize(
    "malformed_expiry",
    ["bad", " 2000000001", "+2000000001", "２００００００００１", True, 1.5, 10**100],
)
def test_signed_malformed_expiry_fails_closed(malformed_expiry: object) -> None:
    token = _legacy_report_token(_REPORT_PRIMARY, expires=malformed_expiry)
    assert decode_report_token(token, now=_ROTATION_NOW) is None


def test_default_ttl_is_seven_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_LINK_TTL_S", raising=False)
    assert _default_ttl_s() == _REPORT_TTL_S


def test_ttl_env_override_is_used_for_issuance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LINK_TTL_S", "3600")
    token = _require_token(encode_report_token(_BUCKET, _KEY, now=1000))
    assert _default_ttl_s() == 3600
    assert _payload(token)["e"] == 4600


@pytest.mark.parametrize("ttl_s", [1, _REPORT_TTL_S])
def test_explicit_ttl_accepts_inclusive_bounds(ttl_s: int) -> None:
    token = _require_token(encode_report_token(_BUCKET, _KEY, now=1000, ttl_s=ttl_s))
    assert _payload(token)["e"] == 1000 + ttl_s


@pytest.mark.parametrize("ttl_s", [0, -1, _REPORT_TTL_S + 1, True, 1.0, "60", 10**100])
def test_explicit_ttl_outside_strict_integer_bounds_returns_none(ttl_s: object) -> None:
    # type: ignore[arg-type] -- adversarial runtime inputs intentionally violate the annotation.
    assert encode_report_token(_BUCKET, _KEY, now=1000, ttl_s=ttl_s) is None


@pytest.mark.parametrize(
    "invalid", ["", " 60", "60 ", "+60", "0", "604801", "１２", "١٢", "9" * 10_000]
)
def test_present_invalid_ttl_returns_none_instead_of_defaulting(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    monkeypatch.setenv("REPORT_LINK_TTL_S", invalid)
    assert _default_ttl_s() is None
    assert has_secret() is False
    assert encode_report_token(_BUCKET, _KEY) is None
    assert encode_report_token(_BUCKET, _KEY, ttl_s=60) is None
