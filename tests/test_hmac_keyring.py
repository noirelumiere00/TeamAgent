"""Shared HMAC keyring rotation, parsing, credential, and timing tests."""

from __future__ import annotations

import hashlib
import hmac
import pickle
import threading
from collections.abc import Callable
from dataclasses import asdict

import pytest

import teamagent.hmac_keyring as hmac_keyring_module
from teamagent.hmac_keyring import (
    HMAC_MAX_FUTURE_T0_SKEW_S,
    HMAC_MAX_ROLLOUT_OVERLAP_S,
    HMAC_PURPOSE_MAIL_DRAFT,
    MAIL_ACTION_MAX_TOKEN_TTL_S,
    REPORT_LINK_MAX_TOKEN_TTL_S,
    HmacKeyring,
    hmac_previous_key_deadline,
    load_mail_action_hmac_keyring,
    load_mail_action_token_ttl_s,
    load_report_link_hmac_keyring,
    load_report_link_token_ttl_s,
    validate_hmac_rotation_transition,
)

_NOW = 2_000_000_000
_MAIL_PRIMARY = "mail-primary-" + "m" * 32
_MAIL_PREVIOUS = "mail-previous-" + "p" * 32
_REPORT_PRIMARY = "report-primary-" + "r" * 32
_REPORT_PREVIOUS = "report-previous-" + "q" * 32
_LEGACY_GENERATION = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:database-url@legacy-v1"
)
_MAIL_GENERATION = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:mail-action@primary-v2"
)
_MAIL_NEXT_GENERATION = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:mail-action@primary-v3"
)
_WRONG_GENERATION = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:wrong@unrelated-v1"
_TEST_PURPOSE = "teamagent.test"

_TEST_ENVS = (
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
    "PAYMENTS_API_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "REDIS_URL",
    "SENTRY_DSN",
)


@pytest.fixture(autouse=True)
def _clean_hmac_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _TEST_ENVS:
        monkeypatch.delenv(name, raising=False)


def _signature(secret: str, payload: bytes) -> bytes:
    return hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:16]


def test_repr_never_contains_keys() -> None:
    primary = b"primary-secret-that-must-never-appear"
    previous = b"previous-secret-that-must-never-appear"
    keyring = HmacKeyring(primary, (primary, previous))
    rendered = repr(keyring)
    assert rendered == "HmacKeyring(<redacted>)"
    assert str(keyring) == "HmacKeyring(<redacted>)"
    assert primary.decode() not in rendered
    assert previous.decode() not in rendered


def test_keyring_uses_identity_equality_and_hash_without_dataclass_leakage() -> None:
    primary = b"primary-secret-that-must-never-appear"
    previous = b"previous-secret-that-must-never-appear"
    first = HmacKeyring(primary, (primary, previous))
    second = HmacKeyring(primary, (primary, previous))

    assert first == first
    assert first != second
    assert hash(first) == object.__hash__(first)
    assert asdict(first) == {}
    assert not hasattr(first, "__dict__")

    with pytest.raises(TypeError, match="serialization is disabled") as exc_info:
        pickle.dumps(first)
    assert primary.decode() not in str(exc_info.value)
    assert previous.decode() not in str(exc_info.value)


@pytest.mark.parametrize("matching_index", [0, 1])
def test_verify_compares_every_key_without_early_exit(
    monkeypatch: pytest.MonkeyPatch, matching_index: int
) -> None:
    primary = b"p" * 32
    previous = b"v" * 32
    keys = (primary, previous)
    payload = b"signed-payload"
    signature = HmacKeyring(keys[matching_index], (keys[matching_index],)).sign(
        payload, purpose=_TEST_PURPOSE, digest_bytes=16
    )
    keyring = HmacKeyring(primary, keys)

    original: Callable[[bytes, bytes], bool] = hmac.compare_digest
    calls: list[tuple[bytes, bytes]] = []

    def _record(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr("teamagent.hmac_keyring.hmac.compare_digest", _record)
    assert keyring.verify(payload, signature, purpose=_TEST_PURPOSE, digest_bytes=16) is True
    assert len(calls) == 2


def test_verify_wrong_length_still_compares_every_eligible_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = HmacKeyring(b"p" * 32, (b"p" * 32, b"v" * 32))
    original: Callable[[bytes, bytes], bool] = hmac.compare_digest
    calls = 0

    def _record(left: bytes, right: bytes) -> bool:
        nonlocal calls
        calls += 1
        return original(left, right)

    monkeypatch.setattr("teamagent.hmac_keyring.hmac.compare_digest", _record)
    assert (
        keyring.verify(
            b"payload",
            b"short",
            purpose=_TEST_PURPOSE,
            digest_bytes=16,
        )
        is False
    )
    assert calls == 2


def test_sign_uses_primary_only() -> None:
    primary = b"p" * 32
    previous = b"v" * 32
    payload = b"signed-payload"
    keyring = HmacKeyring(primary, (primary, previous))
    signature = keyring.sign(payload, purpose=_TEST_PURPOSE, digest_bytes=16)
    primary_signature = HmacKeyring(primary, (primary,)).sign(
        payload,
        purpose=_TEST_PURPOSE,
        digest_bytes=16,
    )
    previous_signature = HmacKeyring(previous, (previous,)).sign(
        payload,
        purpose=_TEST_PURPOSE,
        digest_bytes=16,
    )
    assert hmac.compare_digest(signature, primary_signature)
    assert not hmac.compare_digest(
        signature,
        previous_signature,
    )


def test_hmac_purposes_are_cryptographically_separated() -> None:
    keyring = HmacKeyring(b"p" * 32, (b"p" * 32,))
    payload = b"same-payload"
    signature = keyring.sign(payload, purpose=HMAC_PURPOSE_MAIL_DRAFT, digest_bytes=16)

    assert keyring.verify(
        payload,
        signature,
        purpose=HMAC_PURPOSE_MAIL_DRAFT,
        digest_bytes=16,
    )
    assert not keyring.verify(
        payload,
        signature,
        purpose="teamagent.mail-action.event",
        digest_bytes=16,
    )


def test_dedicated_previous_never_enables_unframed_legacy_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    payload = b"same-payload"

    keyring = load_mail_action_hmac_keyring(now=_NOW)
    assert keyring is not None
    framed_signature = HmacKeyring(
        _MAIL_PREVIOUS.encode(),
        (_MAIL_PREVIOUS.encode(),),
    ).sign(payload, purpose=HMAC_PURPOSE_MAIL_DRAFT, digest_bytes=16)
    assert keyring.verify(
        payload,
        framed_signature,
        purpose=HMAC_PURPOSE_MAIL_DRAFT,
        digest_bytes=16,
    )
    assert not keyring.verify_legacy_previous(
        payload,
        _signature(_MAIL_PREVIOUS, payload),
        digest_bytes=16,
    )


@pytest.mark.parametrize("marker", ["", "0", "true", "01", " 1", "1 "])
def test_legacy_marker_is_strict_and_requires_the_previous_pair(
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", marker)
    assert load_mail_action_hmac_keyring(now=_NOW) is None

    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    assert load_mail_action_hmac_keyring(now=_NOW) is None


@pytest.mark.parametrize(
    (
        "primary_env",
        "previous_env",
        "started_env",
        "legacy_env",
        "primary",
        "previous",
        "loader",
        "max_ttl",
    ),
    [
        (
            "MAIL_ACTION_HMAC_SECRET",
            "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
            "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
            "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
            _MAIL_PRIMARY,
            _MAIL_PREVIOUS,
            load_mail_action_hmac_keyring,
            MAIL_ACTION_MAX_TOKEN_TTL_S,
        ),
        (
            "REPORT_LINK_HMAC_SECRET",
            "REPORT_LINK_HMAC_PREVIOUS_SECRET",
            "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
            "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
            _REPORT_PRIMARY,
            _REPORT_PREVIOUS,
            load_report_link_hmac_keyring,
            REPORT_LINK_MAX_TOKEN_TTL_S,
        ),
    ],
)
def test_verifier_first_timeline_is_restart_stable_and_deadline_exclusive(
    monkeypatch: pytest.MonkeyPatch,
    primary_env: str,
    previous_env: str,
    started_env: str,
    legacy_env: str,
    primary: str,
    previous: str,
    loader: Callable[..., HmacKeyring | None],
    max_ttl: int,
) -> None:
    """T0 verifier deploy, issuer cutover minutes later, and deterministic prior-key removal."""
    monkeypatch.setenv(primary_env, primary)
    monkeypatch.setenv(previous_env, previous)
    monkeypatch.setenv(started_env, str(_NOW))
    monkeypatch.setenv(legacy_env, "1")
    payload = b"last-token-from-old-issuer"
    old_signature = _signature(previous, payload)
    deadline = _NOW + HMAC_MAX_ROLLOUT_OVERLAP_S + max_ttl

    # Each call constructs a fresh keyring, modelling restarts throughout the rollout.
    for current in (_NOW, _NOW + 5 * 60, _NOW + HMAC_MAX_ROLLOUT_OVERLAP_S, deadline - 1):
        keyring = loader(now=current)
        assert keyring is not None
        assert keyring.verify_legacy_previous(payload, old_signature, digest_bytes=16)

    at_deadline = loader(now=deadline)
    after_restart = loader(now=deadline + 10_000)
    assert at_deadline is not None and after_restart is not None
    assert not at_deadline.verify_legacy_previous(payload, old_signature, digest_bytes=16)
    assert not after_restart.verify_legacy_previous(payload, old_signature, digest_bytes=16)


def test_future_rotation_start_accepts_only_the_bounded_clock_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    monkeypatch.setenv(
        "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        str(_NOW + HMAC_MAX_FUTURE_T0_SKEW_S),
    )
    keyring = load_mail_action_hmac_keyring(now=_NOW)
    assert keyring is not None
    assert keyring.verify_legacy_previous(
        b"payload",
        _signature(_MAIL_PREVIOUS, b"payload"),
        digest_bytes=16,
    )


def test_future_rotation_start_beyond_bounded_skew_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    monkeypatch.setenv(
        "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        str(_NOW + HMAC_MAX_FUTURE_T0_SKEW_S + 1),
    )
    assert load_mail_action_hmac_keyring(now=_NOW) is None


def test_process_high_water_prevents_previous_key_reactivation_after_clock_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    payload = b"old-token"
    old_signature = _signature(_MAIL_PREVIOUS, payload)
    deadline = _NOW + HMAC_MAX_ROLLOUT_OVERLAP_S + MAIL_ACTION_MAX_TOKEN_TTL_S

    expired = load_mail_action_hmac_keyring(now=deadline)
    assert expired is not None
    assert not expired.verify_legacy_previous(payload, old_signature, digest_bytes=16)

    after_rollback = load_mail_action_hmac_keyring(now=_NOW)
    assert after_rollback is not None
    assert not after_rollback.verify_legacy_previous(payload, old_signature, digest_bytes=16)


def test_process_high_water_applies_before_first_previous_key_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    deadline = _NOW + HMAC_MAX_ROLLOUT_OVERLAP_S + MAIL_ACTION_MAX_TOKEN_TTL_S
    assert load_mail_action_hmac_keyring(now=deadline) is not None

    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    payload = b"old-token"
    after_rollback = load_mail_action_hmac_keyring(now=_NOW)
    assert after_rollback is not None
    assert not after_rollback.verify_legacy_previous(
        payload,
        _signature(_MAIL_PREVIOUS, payload),
        digest_bytes=16,
    )


def test_high_water_survives_atomic_pair_removal_and_stale_reintroduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    payload = b"old-token"
    old_signature = _signature(_MAIL_PREVIOUS, payload)
    deadline = _NOW + HMAC_MAX_ROLLOUT_OVERLAP_S + MAIL_ACTION_MAX_TOKEN_TTL_S
    assert load_mail_action_hmac_keyring(now=_NOW) is not None

    monkeypatch.delenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET")
    monkeypatch.delenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT")
    monkeypatch.delenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY")
    assert load_mail_action_hmac_keyring(now=deadline) is not None

    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    after_rollback = load_mail_action_hmac_keyring(now=_NOW)
    assert after_rollback is not None
    assert not after_rollback.verify_legacy_previous(payload, old_signature, digest_bytes=16)


def test_deadline_observation_and_state_creation_are_atomic_across_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix the old A(stale advance) → B(deadline advance) → A(state create) interleaving."""
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    deadline = _NOW + HMAC_MAX_ROLLOUT_OVERLAP_S + MAIL_ACTION_MAX_TOKEN_TTL_S
    payload = b"old-token"
    old_signature = _signature(_MAIL_PREVIOUS, payload)

    a_advanced = threading.Barrier(2)
    b_advanced = threading.Barrier(2)
    a_loaded = threading.Barrier(2)
    original_advance = hmac_keyring_module._advance_rotation_high_water

    def interleaved_advance(*, purpose: str, current: int) -> int:
        if current == deadline - 1:
            effective = original_advance(purpose=purpose, current=current)
            a_advanced.wait(timeout=5)
            b_advanced.wait(timeout=5)
            return effective
        if current == deadline:
            a_advanced.wait(timeout=5)
            effective = original_advance(purpose=purpose, current=current)
            b_advanced.wait(timeout=5)
            a_loaded.wait(timeout=5)
            return effective
        return original_advance(purpose=purpose, current=current)

    monkeypatch.setattr(
        hmac_keyring_module,
        "_advance_rotation_high_water",
        interleaved_advance,
    )
    results: dict[str, HmacKeyring | None] = {}
    errors: list[BaseException] = []

    def load_stale() -> None:
        try:
            results["stale"] = load_mail_action_hmac_keyring(now=deadline - 1)
        except BaseException as exc:  # pragma: no cover - assertion reports worker failures
            errors.append(exc)
        finally:
            a_loaded.wait(timeout=5)

    def load_deadline() -> None:
        try:
            results["deadline"] = load_mail_action_hmac_keyring(now=deadline)
        except BaseException as exc:  # pragma: no cover - assertion reports worker failures
            errors.append(exc)

    stale_thread = threading.Thread(target=load_stale)
    deadline_thread = threading.Thread(target=load_deadline)
    stale_thread.start()
    deadline_thread.start()
    stale_thread.join(timeout=10)
    deadline_thread.join(timeout=10)

    assert not stale_thread.is_alive()
    assert not deadline_thread.is_alive()
    assert errors == []
    assert results["stale"] is not None
    assert results["deadline"] is not None
    assert not results["stale"].verify_legacy_previous(
        payload,
        old_signature,
        digest_bytes=16,
    )
    assert not results["deadline"].verify_legacy_previous(
        payload,
        old_signature,
        digest_bytes=16,
    )


@pytest.mark.parametrize("changed_t0", [_NOW - 1, _NOW + 1])
def test_process_rejects_t0_change_for_the_same_previous_key(
    monkeypatch: pytest.MonkeyPatch,
    changed_t0: int,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    assert load_mail_action_hmac_keyring(now=_NOW) is not None

    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(changed_t0))
    assert load_mail_action_hmac_keyring(now=_NOW + 1) is None


def test_iac_contract_enforces_immutable_t0_and_atomic_deadline_removal() -> None:
    deadline = hmac_previous_key_deadline(_NOW, MAIL_ACTION_MAX_TOKEN_TTL_S)
    assert deadline is not None

    introduced = validate_hmac_rotation_transition(
        deployed_primary_generation=_LEGACY_GENERATION,
        deployed_previous_generation=None,
        deployed_rotation_started_at=None,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=_LEGACY_GENERATION,
        proposed_rotation_started_at=_NOW,
        now=_NOW,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert introduced == {
        "ok": True,
        "code": "ok",
        "previous_deadline": deadline,
    }

    changed = validate_hmac_rotation_transition(
        deployed_primary_generation=_MAIL_GENERATION,
        deployed_previous_generation=_LEGACY_GENERATION,
        deployed_rotation_started_at=_NOW,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=_LEGACY_GENERATION,
        proposed_rotation_started_at=_NOW + 1,
        now=_NOW + 1,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert changed["code"] == "t0_changed"

    early_removal = validate_hmac_rotation_transition(
        deployed_primary_generation=_MAIL_GENERATION,
        deployed_previous_generation=_LEGACY_GENERATION,
        deployed_rotation_started_at=_NOW,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=None,
        proposed_rotation_started_at=None,
        now=deadline - 1,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert early_removal["code"] == "removal_before_deadline"

    at_deadline = validate_hmac_rotation_transition(
        deployed_primary_generation=_MAIL_GENERATION,
        deployed_previous_generation=_LEGACY_GENERATION,
        deployed_rotation_started_at=_NOW,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=None,
        proposed_rotation_started_at=None,
        now=deadline,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert at_deadline == {
        "ok": True,
        "code": "ok",
        "previous_deadline": deadline,
    }


def test_iac_contract_rejects_pair_mismatch_future_skew_and_stale_presence() -> None:
    pair_mismatch = validate_hmac_rotation_transition(
        deployed_primary_generation=_LEGACY_GENERATION,
        deployed_previous_generation=None,
        deployed_rotation_started_at=None,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=_LEGACY_GENERATION,
        proposed_rotation_started_at=None,
        now=_NOW,
        max_token_ttl_s=REPORT_LINK_MAX_TOKEN_TTL_S,
    )
    assert pair_mismatch["code"] == "proposed_pair_mismatch"

    future = validate_hmac_rotation_transition(
        deployed_primary_generation=_LEGACY_GENERATION,
        deployed_previous_generation=None,
        deployed_rotation_started_at=None,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=_LEGACY_GENERATION,
        proposed_rotation_started_at=_NOW + HMAC_MAX_FUTURE_T0_SKEW_S + 1,
        now=_NOW,
        max_token_ttl_s=REPORT_LINK_MAX_TOKEN_TTL_S,
    )
    assert future["code"] == "future_t0"

    old_t0 = _NOW - HMAC_MAX_ROLLOUT_OVERLAP_S - REPORT_LINK_MAX_TOKEN_TTL_S
    stale = validate_hmac_rotation_transition(
        deployed_primary_generation=_MAIL_GENERATION,
        deployed_previous_generation=_LEGACY_GENERATION,
        deployed_rotation_started_at=old_t0,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=_LEGACY_GENERATION,
        proposed_rotation_started_at=old_t0,
        now=_NOW,
        max_token_ttl_s=REPORT_LINK_MAX_TOKEN_TTL_S,
    )
    assert stale["code"] == "expired_previous_not_removed"


def test_iac_contract_rejects_direct_cutover_and_wrong_previous_generation() -> None:
    direct = validate_hmac_rotation_transition(
        deployed_primary_generation=_LEGACY_GENERATION,
        deployed_previous_generation=None,
        deployed_rotation_started_at=None,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=None,
        proposed_rotation_started_at=None,
        now=_NOW,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert direct["code"] == "primary_changed_without_previous"

    wrong_previous = validate_hmac_rotation_transition(
        deployed_primary_generation=_LEGACY_GENERATION,
        deployed_previous_generation=None,
        deployed_rotation_started_at=None,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=_WRONG_GENERATION,
        proposed_rotation_started_at=_NOW,
        now=_NOW,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert wrong_previous["code"] == "previous_generation_mismatch"


def test_iac_contract_rejects_mid_window_generation_swaps() -> None:
    swapped_previous = validate_hmac_rotation_transition(
        deployed_primary_generation=_MAIL_GENERATION,
        deployed_previous_generation=_LEGACY_GENERATION,
        deployed_rotation_started_at=_NOW,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=_WRONG_GENERATION,
        proposed_rotation_started_at=_NOW,
        now=_NOW + 1,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert swapped_previous["code"] == "previous_generation_changed"

    swapped_primary = validate_hmac_rotation_transition(
        deployed_primary_generation=_MAIL_GENERATION,
        deployed_previous_generation=_LEGACY_GENERATION,
        deployed_rotation_started_at=_NOW,
        proposed_primary_generation=_MAIL_NEXT_GENERATION,
        proposed_previous_generation=_LEGACY_GENERATION,
        proposed_rotation_started_at=_NOW,
        now=_NOW + 1,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert swapped_primary["code"] == "primary_generation_changed"


@pytest.mark.parametrize(
    "invalid_generation",
    ["", " leading", "trailing ", "contains whitespace", "世代", "x" * 2049],
)
def test_iac_contract_rejects_malformed_generation_identifiers(
    invalid_generation: str,
) -> None:
    result = validate_hmac_rotation_transition(
        deployed_primary_generation=invalid_generation,
        deployed_previous_generation=None,
        deployed_rotation_started_at=None,
        proposed_primary_generation=_MAIL_GENERATION,
        proposed_previous_generation=None,
        proposed_rotation_started_at=None,
        now=_NOW,
        max_token_ttl_s=MAIL_ACTION_MAX_TOKEN_TTL_S,
    )
    assert result["code"] == "invalid_deployed_primary_generation"


def test_old_valid_until_does_not_substitute_for_fixed_rotation_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv(
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
        str(_NOW + MAIL_ACTION_MAX_TOKEN_TTL_S),
    )
    assert load_mail_action_hmac_keyring(now=_NOW) is None


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        " 1",
        "1 ",
        "+1",
        "-1",
        "１２３",
        "١٢٣",
        "9" * 10_000,
        "18446744073709551616",
    ],
)
def test_rotation_timestamp_parser_never_raises_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, malformed: str
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_PREVIOUS)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", malformed)
    assert load_mail_action_hmac_keyring(now=_NOW) is None


@pytest.mark.parametrize("malformed_now", [True, 1.5, "2000000000", 10**100])
def test_load_helpers_return_none_for_malformed_now(
    monkeypatch: pytest.MonkeyPatch, malformed_now: object
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    os_now = malformed_now  # keep the intentionally invalid runtime value visible in the test
    # type: ignore[arg-type] -- adversarial runtime inputs intentionally violate the annotation.
    assert load_mail_action_hmac_keyring(now=os_now) is None


def test_clock_exception_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)

    def _boom() -> float:
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr("teamagent.hmac_keyring.time.time", _boom)
    assert load_mail_action_hmac_keyring() is None


@pytest.mark.parametrize(
    ("env_name", "loader", "maximum"),
    [
        ("MAIL_ACTION_TTL_S", load_mail_action_token_ttl_s, MAIL_ACTION_MAX_TOKEN_TTL_S),
        ("REPORT_LINK_TTL_S", load_report_link_token_ttl_s, REPORT_LINK_MAX_TOKEN_TTL_S),
    ],
)
def test_ttl_loader_defaults_and_accepts_exact_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    loader: Callable[..., int | None],
    maximum: int,
) -> None:
    assert loader() == maximum
    monkeypatch.setenv(env_name, "1")
    assert loader() == 1
    monkeypatch.setenv(env_name, str(maximum))
    assert loader() == maximum
    assert loader(explicit_ttl_s=1) == 1
    assert loader(explicit_ttl_s=maximum) == maximum


@pytest.mark.parametrize("malformed", ["", " 1", "1 ", "+1", "-1", "0", "１２", "١٢", "9" * 10_000])
@pytest.mark.parametrize(
    ("env_name", "loader"),
    [
        ("MAIL_ACTION_TTL_S", load_mail_action_token_ttl_s),
        ("REPORT_LINK_TTL_S", load_report_link_token_ttl_s),
    ],
)
def test_present_invalid_ttl_never_silently_defaults(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    loader: Callable[..., int | None],
    malformed: str,
) -> None:
    monkeypatch.setenv(env_name, malformed)
    assert loader() is None
    assert loader(explicit_ttl_s=1) is None


@pytest.mark.parametrize("invalid", [0, -1, True, 1.0, "1", 10**100])
@pytest.mark.parametrize(
    ("loader", "maximum"),
    [
        (load_mail_action_token_ttl_s, MAIL_ACTION_MAX_TOKEN_TTL_S),
        (load_report_link_token_ttl_s, REPORT_LINK_MAX_TOKEN_TTL_S),
    ],
)
def test_explicit_ttl_rejects_non_integer_or_out_of_range(
    loader: Callable[..., int | None], maximum: int, invalid: object
) -> None:
    assert loader(explicit_ttl_s=invalid) is None
    assert loader(explicit_ttl_s=maximum + 1) is None


@pytest.mark.parametrize(
    "credential",
    [
        "jdbc:postgresql://user:password@db.internal:5432/teamagent",
        "jdbc:not-a-recognized-datastore-but-still-forbidden",
        "JDBC:any-value-is-a-credential-reference",
        "mysql+pymysql://user:password@db.internal/teamagent",
        "mongodb+srv://user:password@cluster.example/teamagent",
        "redis://:a-very-long-password-value@cache.internal:6379/0",
        "rediss://user:a-very-long-password@cache.internal:6380/0",
        "xoxb-" + "b" * 40,
        "xoxp-" + "p" * 40,
        "xoxs-" + "s" * 40,
        "xoxa-" + "a" * 40,
        "xoxr-" + "r" * 40,
        "xoxe-" + "e" * 40,
        "xapp-" + "x" * 40,
    ],
)
def test_primary_rejects_common_credential_shapes(
    monkeypatch: pytest.MonkeyPatch, credential: str
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", credential)
    assert load_mail_action_hmac_keyring(now=_NOW) is None


@pytest.mark.parametrize(
    "credential_env",
    [
        "DATABASE_URL",
        "PAYMENTS_API_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "SENTRY_DSN",
        "PGPASSWORD",
        "MYSQL_PWD",
        "REDISCLI_AUTH",
        "BASIC_AUTH",
        "SPRING_JDBC",
        "SPRING_DATASOURCE",
        "MESSAGE_BROKER",
        "SESSION_CACHE",
        "ALERT_WEBHOOK",
    ],
)
def test_primary_rejects_equality_with_any_visible_credential_env(
    monkeypatch: pytest.MonkeyPatch, credential_env: str
) -> None:
    random_hmac_value = "otherwise-valid-random-hmac-value-" + "z" * 32
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", random_hmac_value)
    monkeypatch.setenv(credential_env, random_hmac_value)
    assert load_mail_action_hmac_keyring(now=_NOW) is None


def test_primary_rejects_equality_with_long_unknown_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    random_hmac_value = "otherwise-valid-random-hmac-value-" + "u" * 32
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", random_hmac_value)
    monkeypatch.setenv("UNCLASSIFIED_RUNTIME_SETTING", random_hmac_value)
    assert load_mail_action_hmac_keyring(now=_NOW) is None


@pytest.mark.parametrize(
    "legacy_previous",
    [
        "old",
        "legacy-value-with-exact-trailing-newline\n",
        "postgresql://user:legacy-password@db.internal:5432/teamagent?sslmode=require",
        "xoxb-" + "b" * 40,
        "xoxp-" + "p" * 40,
        "xoxs-" + "s" * 40,
        "xoxa-" + "a" * 40,
        "xoxr-" + "r" * 40,
        "xoxe-" + "e" * 40,
        "xapp-" + "x" * 40,
    ],
)
def test_legacy_previous_validator_preserves_exact_bytes_and_never_issues(
    monkeypatch: pytest.MonkeyPatch,
    legacy_previous: str,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", legacy_previous)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    payload = b"legacy-token-payload"

    keyring = load_mail_action_hmac_keyring(now=_NOW)
    assert keyring is not None
    assert keyring.verify_legacy_previous(
        payload,
        _signature(legacy_previous, payload),
        digest_bytes=16,
    )

    issued = keyring.sign(payload, purpose=_TEST_PURPOSE, digest_bytes=16)
    primary_signature = HmacKeyring(_MAIL_PRIMARY.encode(), (_MAIL_PRIMARY.encode(),)).sign(
        payload,
        purpose=_TEST_PURPOSE,
        digest_bytes=16,
    )
    assert hmac.compare_digest(issued, primary_signature)
    assert not hmac.compare_digest(issued, _signature(_MAIL_PRIMARY, payload))
    assert not hmac.compare_digest(issued, _signature(legacy_previous, payload))
    framed_legacy_signature = HmacKeyring(
        legacy_previous.encode(),
        (legacy_previous.encode(),),
    ).sign(payload, purpose=_TEST_PURPOSE, digest_bytes=16)
    assert not keyring.verify(
        payload,
        framed_legacy_signature,
        purpose=_TEST_PURPOSE,
        digest_bytes=16,
    )


def test_legacy_previous_trailing_newline_is_not_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_legacy = "legacy-value-with-exact-trailing-newline\n"
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", exact_legacy)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    payload = b"legacy-token-payload"

    keyring = load_mail_action_hmac_keyring(now=_NOW)
    assert keyring is not None
    assert keyring.verify_legacy_previous(
        payload,
        _signature(exact_legacy, payload),
        digest_bytes=16,
    )
    assert not keyring.verify_legacy_previous(
        payload,
        _signature(exact_legacy.rstrip("\n"), payload),
        digest_bytes=16,
    )


@pytest.mark.parametrize(
    "legacy_only_value",
    [
        "old",
        "legacy-value-with-exact-trailing-newline\n",
        "postgresql://user:legacy-password@db.internal:5432/teamagent?sslmode=require",
        "xoxb-" + "b" * 40,
    ],
)
def test_migration_only_values_are_never_valid_primary_keys(
    monkeypatch: pytest.MonkeyPatch,
    legacy_only_value: str,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", legacy_only_value)
    assert load_mail_action_hmac_keyring(now=_NOW) is None


def test_both_domains_can_share_only_a_bounded_legacy_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_db = "postgresql://user:legacy-password@db.internal:5432/teamagent?sslmode=require"
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _REPORT_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", legacy_db)
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_SECRET", legacy_db)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY", "1")
    monkeypatch.setenv("REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY", "1")
    monkeypatch.setenv("DATABASE_URL", legacy_db)

    assert load_mail_action_hmac_keyring(now=_NOW) is not None
    assert load_report_link_hmac_keyring(now=_NOW) is not None


def test_primary_keys_remain_purpose_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _MAIL_PRIMARY)
    assert load_mail_action_hmac_keyring(now=_NOW) is None
    assert load_report_link_hmac_keyring(now=_NOW) is None


def test_previous_cannot_equal_other_purpose_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_PRIMARY)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _REPORT_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _REPORT_PRIMARY)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_NOW))
    assert load_mail_action_hmac_keyring(now=_NOW) is None
