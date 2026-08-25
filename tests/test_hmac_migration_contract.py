"""canonical 化移行が満たすべき署名/検証契約を固定する。

2026-08-26 の裁定で、live の legacy secret から canonical secret へ
`primary = canonical / previous = legacy(VersionId pin)` の形で移行する方針が採られた。
その前提は **「sign は primary だけ・verify は primary + previous」** が成立していること。
成立しなければ移行中に既存署名が検証できなくなるため、ここで機械的に固定する。

さらに、この repo には purpose ごとに 2 系統の verify がある:

- ``verify()``               … v2（purpose-framed）。primary + 非 legacy previous
- ``verify_legacy_previous()`` … v1（unframed）。previous のみ・primary を意図的に除外

``MAIL_ACTION`` の legacy previous は **DB 認証情報そのもの**であるため、
``*_PREVIOUS_IS_LEGACY=1`` を立てて v1 専用に閉じ込める設計になっている
（`hmac_keyring.py` の「must never become a general v2 verification key」）。

``REPORT_LINK`` も同じく 2 経路を持ち、**トークン形式**で振り分ける
（v2 は ``verify()``・v1 は ``verify_legacy_previous()``）。
したがって移行時に legacy フラグを立てるか否かは
**既存リンクがどちらの形式か**で決まる。取り違えると本番で黙って壊れる。
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from teamagent.hmac_keyring import (
    HmacKeyring,
    load_mail_action_hmac_keyring,
    load_report_link_hmac_keyring,
)

_NOW = 2_000_000_000
_PRIMARY = "canonical-primary-" + "c" * 32
_LEGACY = "legacy-previous-" + "l" * 32
_TEST_PURPOSE = "teamagent.test"

_ENVS = (
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
    "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
    "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET",
    "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION",
    "REPORT_LINK_HMAC_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
    "REPORT_LINK_HMAC_PRIMARY_GENERATION",
    "REPORT_LINK_HMAC_PREVIOUS_GENERATION",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)


def _v2_signature(secret: str, payload: bytes, *, purpose: str) -> bytes:
    """v2（purpose-framed）署名を、実装と同じ枠付けで作る。"""
    return HmacKeyring(secret.encode(), (secret.encode(),)).sign(
        payload, purpose=purpose, digest_bytes=16
    )


def _v1_signature(secret: str, payload: bytes) -> bytes:
    """v1（unframed）署名。purpose 枠が無い旧形式。"""
    return hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:16]


# ── 中核契約: sign は primary だけ / verify は primary + previous ────────────


def test_sign_uses_only_the_primary_key() -> None:
    """previous を持っていても、発行は primary でしか行われない。"""
    primary = b"p" * 32
    previous = b"v" * 32
    payload = b"payload"
    keyring = HmacKeyring(primary, (primary, previous))

    produced = keyring.sign(payload, purpose=_TEST_PURPOSE, digest_bytes=16)
    primary_only = HmacKeyring(primary, (primary,)).sign(
        payload, purpose=_TEST_PURPOSE, digest_bytes=16
    )
    previous_only = HmacKeyring(previous, (previous,)).sign(
        payload, purpose=_TEST_PURPOSE, digest_bytes=16
    )

    assert produced == primary_only
    assert produced != previous_only


def test_verify_accepts_both_primary_and_previous_signatures() -> None:
    """移行中の互換性の本体。previous で署名された既存トークンも検証できる。"""
    primary = b"p" * 32
    previous = b"v" * 32
    payload = b"payload"
    keyring = HmacKeyring(primary, (primary, previous))

    for key in (primary, previous):
        signature = HmacKeyring(key, (key,)).sign(payload, purpose=_TEST_PURPOSE, digest_bytes=16)
        assert keyring.verify(payload, signature, purpose=_TEST_PURPOSE, digest_bytes=16)

    unrelated = b"x" * 32
    stranger = HmacKeyring(unrelated, (unrelated,)).sign(
        payload, purpose=_TEST_PURPOSE, digest_bytes=16
    )
    assert not keyring.verify(payload, stranger, purpose=_TEST_PURPOSE, digest_bytes=16)


# ── report_link: legacy フラグは既存トークン形式と一致させる ────────────────


def _load_report_keyring(monkeypatch: pytest.MonkeyPatch, *, legacy: bool) -> object:
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _PRIMARY)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", _LEGACY)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    if legacy:
        monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY", "1")
    return load_report_link_hmac_keyring(now=_NOW)


def test_report_link_non_legacy_previous_keeps_existing_links_verifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """legacy フラグ **無し** なら、旧鍵で署名された v2 リンクが検証できる。"""
    keyring = _load_report_keyring(monkeypatch, legacy=False)
    assert keyring is not None

    payload = b"report-link-payload"
    old = _v2_signature(_LEGACY, payload, purpose="teamagent.report_link")

    assert keyring.verify(  # type: ignore[attr-defined]
        payload, old, purpose="teamagent.report_link", digest_bytes=16
    )


def test_report_link_legacy_flag_would_break_existing_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """legacy フラグを立てると v2 検証鍵から previous が外れる。

    既存リンクが v2 形式なら、この構成は本番でリンク切れになる。
    フラグの選択が形式と一致していなければならないことの negative fixation。
    """
    keyring = _load_report_keyring(monkeypatch, legacy=True)
    assert keyring is not None

    payload = b"report-link-payload"
    old = _v2_signature(_LEGACY, payload, purpose="teamagent.report_link")

    assert not keyring.verify(  # type: ignore[attr-defined]
        payload, old, purpose="teamagent.report_link", digest_bytes=16
    )


# ── mail_action: DB 認証情報は v2 検証鍵にしない ────────────────────────────


def test_mail_action_legacy_previous_never_becomes_a_v2_verification_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """legacy previous（＝DB 認証情報）で purpose-framed トークンを作れないこと。

    これが緩むと、DB 認証情報を持つ者が有効な v2 トークンを発行できてしまう。
    """
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _LEGACY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    keyring = load_mail_action_hmac_keyring(now=_NOW)
    assert keyring is not None

    payload = b"mail-action-payload"
    framed_with_legacy = _v2_signature(_LEGACY, payload, purpose="teamagent.mail_draft")

    assert not keyring.verify(  # type: ignore[attr-defined]
        payload, framed_with_legacy, purpose="teamagent.mail_draft", digest_bytes=16
    )
    # v1（unframed）だけは移行窓の間だけ通る
    assert keyring.verify_legacy_previous(  # type: ignore[attr-defined]
        payload, _v1_signature(_LEGACY, payload), digest_bytes=16
    )


def test_report_link_dispatches_verification_by_token_format() -> None:
    """report_link は **トークン形式**で検証経路を振り分ける。

    - v2（`v` と `typ` を持つ purpose-framed）→ `verify()`（primary + 非 legacy previous）
    - v1（`v` 無し・legacy fields）→ `verify_legacy_previous()`（legacy フラグ付き previous のみ）

    したがって「report_link は legacy verifier を使わない」は**誤り**である。
    移行時に legacy フラグを立てるか否かは、**既存リンクがどちらの形式か**で決まる:

    - 既存リンクが v2 なら legacy フラグは **立ててはいけない**
      （立てると previous が v2 検証鍵から外れてリンクが落ちる）
    - 既存リンクが v1 なら legacy フラグが **必要**

    この分岐が消えると片方の形式が検証不能になるため、両方の存在をここで固定する。
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src/teamagent/adapters/report_link_token.py"
    ).read_text(encoding="utf-8")

    assert "keyring.sign(" in source
    # v2 経路と v1 経路の両方が実在する
    assert "keyring.verify(" in source
    assert "keyring.verify_legacy_previous(" in source
    # 形式で分岐している（v2 判定が先・legacy は else 側）
    v2_index = source.index("keyring.verify(")
    v1_index = source.index("keyring.verify_legacy_previous(")
    assert v2_index < v1_index


def test_legacy_flag_choice_follows_the_existing_token_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """legacy フラグの選択が既存トークン形式と一致しないと検証が落ちることを固定する。

    移行設計の要。フラグを取り違えると本番でリンク/ボタンが黙って壊れる。
    """
    payload = b"report-link-payload"
    v2_sig = _v2_signature(_LEGACY, payload, purpose="teamagent.report_link")
    v1_sig = _v1_signature(_LEGACY, payload)

    non_legacy = _load_report_keyring(monkeypatch, legacy=False)
    assert non_legacy is not None
    # 非 legacy: v2 は通り、v1 は通らない
    assert non_legacy.verify(  # type: ignore[attr-defined]
        payload, v2_sig, purpose="teamagent.report_link", digest_bytes=16
    )
    assert not non_legacy.verify_legacy_previous(  # type: ignore[attr-defined]
        payload, v1_sig, digest_bytes=16
    )

    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)

    legacy = _load_report_keyring(monkeypatch, legacy=True)
    assert legacy is not None
    # legacy: v1 は通り、v2 は通らない
    assert not legacy.verify(  # type: ignore[attr-defined]
        payload, v2_sig, purpose="teamagent.report_link", digest_bytes=16
    )
    assert legacy.verify_legacy_previous(  # type: ignore[attr-defined]
        payload, v1_sig, digest_bytes=16
    )
