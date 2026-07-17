"""report_link_token（レポート短縮リンク署名）の単体・鍵ローテーションテスト。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from teamagent.adapters.report_link_token import (
    decode_report_token,
    encode_report_token,
    has_secret,
    is_allowed_key,
)
from teamagent.hmac_keyring import HmacKeyConfigurationError

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
    "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
    "DATABASE_URL",
    "SLACK_BOT_TOKEN",
)


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HMAC_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _REPORT_PRIMARY)
    monkeypatch.setenv("VSEO_REPORT_BUCKET", _BUCKET)


def _legacy_report_token(
    secret: str,
    *,
    now: int = _ROTATION_NOW - 10,
    ttl_s: int = _REPORT_TTL_S,
    key: str = _KEY,
) -> str:
    """分離前の wire format を旧鍵で署名する（新 encode は DB DSN を主鍵にできないため）。"""
    payload = {"typ": "r", "b": _BUCKET, "k": key, "r": "", "e": now + ttl_s}
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


def test_round_trip() -> None:
    token = encode_report_token(_BUCKET, _KEY)
    assert "?" not in token and "/" not in token
    assert decode_report_token(token) == (_BUCKET, _KEY, "")


def test_region_round_trip() -> None:
    token = encode_report_token(_BUCKET, _KEY, region="us-west-2")
    assert decode_report_token(token) == (_BUCKET, _KEY, "us-west-2")


def test_proposals_prefix_ok() -> None:
    assert decode_report_token(encode_report_token(_BUCKET, "vseo-proposals/d.pdf")) == (
        _BUCKET,
        "vseo-proposals/d.pdf",
        "",
    )


def test_is_allowed_key() -> None:
    assert is_allowed_key("vseo-reports/a.html")
    assert is_allowed_key("vseo-proposals/d.pdf")
    assert not is_allowed_key("custom-prefix/a.html")
    assert not is_allowed_key("")


def test_tamper_rejected() -> None:
    token = encode_report_token(_BUCKET, _KEY)
    body, sig = token.split(".", 1)
    flip = "A" if body[5] != "A" else "B"
    assert decode_report_token(body[:5] + flip + body[6:] + "." + sig) is None


def test_expired_rejected() -> None:
    token = encode_report_token(_BUCKET, _KEY, now=int(time.time()) - 100, ttl_s=10)
    assert decode_report_token(token) is None


def test_foreign_prefix_rejected() -> None:
    assert decode_report_token(encode_report_token(_BUCKET, "secrets/leak.txt")) is None


def test_foreign_bucket_rejected() -> None:
    assert decode_report_token(encode_report_token("other-bucket", _KEY)) is None


def test_no_key_fail_closed_for_issue_and_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encode_report_token(_BUCKET, _KEY)
    monkeypatch.delenv("REPORT_LINK_HMAC_SECRET")
    assert not has_secret()
    assert decode_report_token(token) is None
    with pytest.raises(HmacKeyConfigurationError):
        encode_report_token(_BUCKET, _KEY)


@pytest.mark.parametrize("invalid", ["", "   ", "too-short"])
def test_empty_or_short_primary_fail_closed(monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", invalid)
    assert has_secret() is False
    with pytest.raises(HmacKeyConfigurationError):
        encode_report_token(_BUCKET, _KEY)


def test_does_not_fall_back_to_mail_slack_or_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_LINK_HMAC_SECRET")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-" + "s" * 40)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    assert has_secret() is False
    assert (
        decode_report_token(_legacy_report_token(_LEGACY_DATABASE_URL), now=_ROTATION_NOW) is None
    )
    with pytest.raises(HmacKeyConfigurationError):
        encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW)


def test_legacy_database_key_is_explicit_previous_verification_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_report_token(_LEGACY_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)

    assert decode_report_token(legacy, now=_ROTATION_NOW) is None
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    assert decode_report_token(legacy, now=_ROTATION_NOW) is None  # 期限なし keyring は不正
    monkeypatch.setenv(
        "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(_ROTATION_NOW + _REPORT_TTL_S)
    )
    assert decode_report_token(legacy, now=_ROTATION_NOW) == (_BUCKET, _KEY, "")

    issued = encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW)
    raw, signature = _signature(issued)
    primary_sig = hmac.new(_REPORT_PRIMARY.encode(), raw, hashlib.sha256).digest()[:16]
    previous_sig = hmac.new(_LEGACY_DATABASE_URL.encode(), raw, hashlib.sha256).digest()[:16]
    assert hmac.compare_digest(signature, primary_sig)
    assert not hmac.compare_digest(signature, previous_sig)


def test_previous_stops_verifying_after_explicit_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_report_token(_LEGACY_DATABASE_URL)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(_ROTATION_NOW + 30))
    assert decode_report_token(legacy, now=_ROTATION_NOW) is not None
    assert decode_report_token(legacy, now=_ROTATION_NOW + 31) is None


def test_invalid_previous_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _REPORT_PRIMARY)
    monkeypatch.setenv(
        "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(_ROTATION_NOW + _REPORT_TTL_S)
    )
    assert has_secret() is False
    assert decode_report_token(token, now=_ROTATION_NOW) is None
    with pytest.raises(HmacKeyConfigurationError):
        encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW)


def test_previous_deadline_cannot_be_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv(
        "REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
        str(_ROTATION_NOW + _REPORT_TTL_S + 301),
    )
    assert has_secret() is False
    with pytest.raises(HmacKeyConfigurationError):
        encode_report_token(_BUCKET, _KEY, now=_ROTATION_NOW)


def test_database_primary_rejected_without_leaking_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _LEGACY_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", _LEGACY_DATABASE_URL)
    with pytest.raises(HmacKeyConfigurationError) as caught:
        encode_report_token(_BUCKET, _KEY)
    assert _LEGACY_DATABASE_URL not in str(caught.value)
    assert "legacy-db-password" not in repr(caught.value)


def test_report_and_mail_primary_must_be_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _REPORT_PRIMARY)
    assert has_secret() is False
    with pytest.raises(HmacKeyConfigurationError):
        encode_report_token(_BUCKET, _KEY)


def test_type_tag_blocks_cross_token_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.skills.morning_digest.draft_token import encode_draft_token

    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    draft = encode_draft_token("thread-123", "someone@vectorinc.co.jp")
    assert decode_report_token(draft) is None


def test_default_ttl_is_seven_days(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.report_link_token import _default_ttl_s

    monkeypatch.delenv("REPORT_LINK_TTL_S", raising=False)
    assert _default_ttl_s() == _REPORT_TTL_S


def test_ttl_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.report_link_token import _default_ttl_s

    monkeypatch.setenv("REPORT_LINK_TTL_S", "3600")
    assert _default_ttl_s() == 3600
    monkeypatch.setenv("REPORT_LINK_TTL_S", "not-a-number")
    assert _default_ttl_s() == _REPORT_TTL_S
    monkeypatch.setenv("REPORT_LINK_TTL_S", "0")
    assert _default_ttl_s() == _REPORT_TTL_S
