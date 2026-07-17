"""共有 HMAC keyring の秘密非露出・constant-time 検証テスト。"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable

import pytest

from teamagent.hmac_keyring import (
    HmacKeyring,
    load_mail_action_hmac_keyring,
    load_report_link_hmac_keyring,
)


def test_repr_never_contains_keys() -> None:
    primary = b"primary-secret-that-must-never-appear"
    previous = b"previous-secret-that-must-never-appear"
    keyring = HmacKeyring(primary, (primary, previous))
    rendered = repr(keyring)
    assert rendered == "HmacKeyring(<redacted>)"
    assert primary.decode() not in rendered
    assert previous.decode() not in rendered


@pytest.mark.parametrize("matching_index", [0, 1])
def test_verify_compares_every_key_without_early_exit(
    monkeypatch: pytest.MonkeyPatch, matching_index: int
) -> None:
    primary = b"p" * 32
    previous = b"v" * 32
    keys = (primary, previous)
    payload = b"signed-payload"
    signature = hmac.new(keys[matching_index], payload, hashlib.sha256).digest()[:16]
    keyring = HmacKeyring(primary, keys)

    original: Callable[[bytes, bytes], bool] = hmac.compare_digest
    calls: list[tuple[bytes, bytes]] = []

    def _record(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr("teamagent.hmac_keyring.hmac.compare_digest", _record)
    assert keyring.verify(payload, signature, digest_bytes=16) is True
    assert len(calls) == 2  # primary 一致でも previous まで必ず比較する


def test_sign_uses_primary_only() -> None:
    primary = b"p" * 32
    previous = b"v" * 32
    payload = b"signed-payload"
    keyring = HmacKeyring(primary, (primary, previous))
    signature = keyring.sign(payload, digest_bytes=16)
    assert hmac.compare_digest(signature, hmac.new(primary, payload, hashlib.sha256).digest()[:16])
    assert not hmac.compare_digest(
        signature, hmac.new(previous, payload, hashlib.sha256).digest()[:16]
    )


def test_both_domains_can_temporarily_share_only_the_legacy_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """現本番の旧DB鍵は両用途の previous に置けるが、新主鍵同士は必ず分離する。"""
    now = 2_000_000_000
    legacy_db = "postgresql://user:legacy-password@db.internal:5432/teamagent?sslmode=require"
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "mail-primary-" + "m" * 32)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", "report-primary-" + "r" * 32)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", legacy_db)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", legacy_db)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(now + 60 * 60 * 24))
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET_VALID_UNTIL", str(now + 60 * 60 * 24 * 7))
    monkeypatch.setenv("DATABASE_URL", legacy_db)

    assert load_mail_action_hmac_keyring(now=now) is not None
    assert load_report_link_hmac_keyring(now=now) is not None
