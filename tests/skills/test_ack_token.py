"""Morning-digest acknowledgement token integrity and fail-closed tests."""

from __future__ import annotations

import base64
import json

import pytest

from teamagent.hmac_keyring import (
    HMAC_PURPOSE_CALENDAR_EVENT,
    HMAC_PURPOSE_DIGEST_ACK,
    HMAC_PURPOSE_MAIL_DRAFT,
    HMAC_PURPOSE_REPORT_LINK,
    HmacKeyring,
)
from teamagent.skills.morning_digest.ack_token import (
    AckItem,
    AckTokenPayload,
    ack_hmac_configured,
    decode_ack_token,
    encode_ack_all_token,
    encode_ack_token,
    encode_unack_token,
)
from teamagent.skills.morning_digest.draft_token import (
    _SIG_LEN,
    _owner_hash,
    decode_draft_token,
    encode_draft_token,
)

ME = "s-komata@vectorinc.co.jp"
_MAIL_PRIMARY = "mail-primary-" + "m" * 32
_NOW = 1_000
_ITEM_MAIL = AckItem("m", "0123456789abcdef", 42)
_ITEM_SLACK = AckItem("s", "fedcba9876543210", 7)

_HMAC_ENVS = (
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
    "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
    "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET",
    "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION",
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


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _require_token(token: str | None) -> str:
    assert token is not None
    return token


def _signature(token: str) -> tuple[bytes, bytes]:
    body_b64, sig_b64 = token.split(".", 1)
    return _decode_b64(body_b64), _decode_b64(sig_b64)


def _payload(token: str) -> dict[str, object]:
    raw, _signature_bytes = _signature(token)
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def _valid_payload(
    *,
    kind: str = "ack",
    items: list[list[object]] | None = None,
    expires: object = _NOW + 60,
) -> dict[str, object]:
    return {
        "v": 2,
        "typ": kind,
        "n": items if items is not None else [["m", "0123456789abcdef", 42]],
        "o": _owner_hash(ME),
        "e": expires,
    }


def _signed_payload(
    payload: dict[str, object],
    *,
    purpose: str = HMAC_PURPOSE_DIGEST_ACK,
) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    secret = _MAIL_PRIMARY.encode("utf-8")
    signature = HmacKeyring(secret, (secret,)).sign(
        raw,
        purpose=purpose,
        digest_bytes=_SIG_LEN,
    )
    return f"{_b64(raw)}.{_b64(signature)}"


def test_roundtrip_for_all_token_kinds() -> None:
    ack = _require_token(encode_ack_token([_ITEM_MAIL], ME, now=_NOW))
    ack_all = _require_token(encode_ack_all_token([_ITEM_MAIL, _ITEM_SLACK], ME, now=_NOW))
    unack_items = (AckItem("m", _ITEM_MAIL.item_key, 0), AckItem("s", _ITEM_SLACK.item_key, 0))
    unack = _require_token(encode_unack_token(unack_items, ME, now=_NOW))

    assert decode_ack_token(ack, ME, now=_NOW) == AckTokenPayload("ack", (_ITEM_MAIL,))
    assert decode_ack_token(ack_all, ME, now=_NOW) == AckTokenPayload(
        "ackall", (_ITEM_MAIL, _ITEM_SLACK)
    )
    assert decode_ack_token(unack, ME, now=_NOW) == AckTokenPayload("unack", unack_items)
    assert _payload(unack)["n"] == [
        ["m", _ITEM_MAIL.item_key],
        ["s", _ITEM_SLACK.item_key],
    ]


def test_ack_and_draft_tokens_cannot_cross_verify() -> None:
    ack = _require_token(encode_ack_token([_ITEM_MAIL], ME, now=_NOW))
    draft = _require_token(encode_draft_token("thread-123", ME, now=_NOW))

    assert decode_ack_token(draft, ME, now=_NOW) is None
    assert decode_draft_token(ack, ME, now=_NOW) is None
    assert _payload(ack)["typ"] == "ack"
    assert _payload(ack)["v"] == 2


@pytest.mark.parametrize(
    "purpose",
    [HMAC_PURPOSE_MAIL_DRAFT, HMAC_PURPOSE_CALENDAR_EVENT, HMAC_PURPOSE_REPORT_LINK],
)
def test_other_hmac_purposes_cannot_sign_ack_payload(purpose: str) -> None:
    token = _signed_payload(_valid_payload(), purpose=purpose)
    assert decode_ack_token(token, ME, now=_NOW) is None


def test_owner_mismatch_rejected() -> None:
    token = _require_token(encode_ack_token([_ITEM_MAIL], ME, now=_NOW))
    assert decode_ack_token(token, "attacker@evil.example", now=_NOW) is None


def test_expired_token_rejected_at_exact_boundary() -> None:
    token = _require_token(encode_ack_token([_ITEM_MAIL], ME, now=_NOW, ttl_s=60))
    assert decode_ack_token(token, ME, now=_NOW + 59) == AckTokenPayload("ack", (_ITEM_MAIL,))
    assert decode_ack_token(token, ME, now=_NOW + 60) is None


def test_one_signature_byte_tamper_is_rejected() -> None:
    token = _require_token(encode_ack_token([_ITEM_MAIL], ME, now=_NOW))
    raw, signature = _signature(token)
    tampered_signature = bytes([signature[0] ^ 1]) + signature[1:]

    assert decode_ack_token(f"{_b64(raw)}.{_b64(tampered_signature)}", ME, now=_NOW) is None


def test_item_key_payload_tamper_is_rejected() -> None:
    token = _require_token(encode_ack_token([_ITEM_MAIL], ME, now=_NOW))
    payload = _payload(token)
    nodes = payload["n"]
    assert isinstance(nodes, list) and isinstance(nodes[0], list)
    nodes[0][1] = "aaaaaaaaaaaaaaaa"
    forged_raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _original_raw, signature = _signature(token)

    assert decode_ack_token(f"{_b64(forged_raw)}.{_b64(signature)}", ME, now=_NOW) is None


def test_signed_single_ack_with_two_items_is_rejected() -> None:
    token = _signed_payload(
        _valid_payload(
            items=[
                ["m", "0123456789abcdef", 42],
                ["s", "fedcba9876543210", 7],
            ]
        )
    )
    assert decode_ack_token(token, ME, now=_NOW) is None


@pytest.mark.parametrize(
    "item",
    [
        ["x", "0123456789abcdef", 42],
        ["m", "not-16-hex", 42],
        ["m", "0123456789abcdeF", 42],
        ["m", "0123456789abcdef", -1],
        ["m", "0123456789abcdef", True],
    ],
)
def test_signed_invalid_item_is_rejected(item: list[object]) -> None:
    token = _signed_payload(_valid_payload(items=[item]))
    assert decode_ack_token(token, ME, now=_NOW) is None


def test_no_secret_fails_closed_for_issue_and_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _require_token(encode_ack_token([_ITEM_MAIL], ME, now=_NOW))
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET")

    assert ack_hmac_configured() is False
    assert encode_ack_token([_ITEM_MAIL], ME, now=_NOW) is None
    assert decode_ack_token(token, ME, now=_NOW) is None


def test_does_not_fall_back_to_slack_or_database(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _require_token(encode_ack_token([_ITEM_MAIL], ME, now=_NOW))
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-" + "s" * 40)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://teamagent:password@db.internal:5432/teamagent",
    )

    assert ack_hmac_configured() is False
    assert encode_ack_token([_ITEM_MAIL], ME, now=_NOW) is None
    assert decode_ack_token(token, ME, now=_NOW) is None


def test_ack_all_size_guard_rejects_oversized_button_value() -> None:
    items = tuple(AckItem("m", f"{index:016x}", index) for index in range(100))
    assert encode_ack_all_token(items, ME, now=_NOW) is None


def test_unack_token_has_explicit_one_hour_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_TTL_S", "60")
    item = AckItem("m", _ITEM_MAIL.item_key, 0)
    token = _require_token(encode_unack_token([item], ME, now=_NOW))

    assert _payload(token)["e"] == _NOW + 3600
    assert decode_ack_token(token, ME, now=_NOW + 3599) == AckTokenPayload("unack", (item,))
    assert decode_ack_token(token, ME, now=_NOW + 3600) is None


def test_encode_cardinality_and_item_validation_fail_closed() -> None:
    assert encode_ack_token([], ME, now=_NOW) is None
    assert encode_ack_token([_ITEM_MAIL, _ITEM_SLACK], ME, now=_NOW) is None
    assert encode_ack_all_token([], ME, now=_NOW) is None
    assert encode_unack_token([], ME, now=_NOW) is None
    assert encode_ack_token([AckItem("m", "RAW_THREAD_SECRET", 1)], ME, now=_NOW) is None
