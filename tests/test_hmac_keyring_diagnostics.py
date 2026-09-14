"""Non-secret diagnostics for the confirmation-button signing key.

The production defect these tests pin: ``MAIL_ACTION_HMAC_SECRET`` and ``DATABASE_URL`` resolved to
the same Secrets Manager ARN, so the mail-action keyring refused to load and no confirmation button
could be issued. The loader was already correct -- it is supposed to refuse a shared credential --
but it refused silently, so the outage was invisible.
"""

from __future__ import annotations

import pytest

from teamagent.hmac_keyring import (
    PRIMARY_LOOKS_LIKE_CREDENTIAL,
    PRIMARY_MISSING,
    PRIMARY_NOT_STRIPPED,
    PRIMARY_OK,
    PRIMARY_REUSES_OTHER_PURPOSE,
    PRIMARY_REUSES_PROCESS_CREDENTIAL,
    PRIMARY_TOO_SHORT,
    diagnose_mail_action_primary,
    diagnose_report_link_primary,
    load_mail_action_hmac_keyring,
    load_report_link_hmac_keyring,
)

_DEDICATED_MAIL = "a" * 64
_DEDICATED_REPORT = "b" * 64
_DATABASE_URL = "postgresql://teamagent:" + "z" * 40 + "@db.internal.example:5432/teamagent"

_TEST_ENVS = (
    "MAIL_ACTION_HMAC_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
    "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
    "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
    "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET",
    "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION",
    "MAIL_ACTION_TTL_S",
    "REPORT_LINK_HMAC_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
    "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
    "REPORT_LINK_TTL_S",
    "TEAMAGENT_HMAC_STATE_REQUIRED",
    "DATABASE_URL",
    "SLACK_BOT_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_hmac_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _TEST_ENVS:
        monkeypatch.delenv(name, raising=False)


def test_production_shape_is_reported_as_credential_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live mcp/connect-web/morning-digest shape: one ARN feeding both variables."""
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _DATABASE_URL)

    assert load_mail_action_hmac_keyring() is None
    assert diagnose_mail_action_primary() == PRIMARY_REUSES_PROCESS_CREDENTIAL


def test_dedicated_key_alongside_database_url_issues_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix: a distinct value in the same process is accepted with no rotation metadata."""
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _DEDICATED_MAIL)

    assert diagnose_mail_action_primary() == PRIMARY_OK
    keyring = load_mail_action_hmac_keyring()
    assert keyring is not None


def test_dedicated_key_signature_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _DEDICATED_MAIL)

    keyring = load_mail_action_hmac_keyring()
    assert keyring is not None
    payload = b"thread-42"
    signature = keyring.sign(payload, purpose="teamagent.mail-action.draft", digest_bytes=16)
    assert keyring.verify(
        payload, signature, purpose="teamagent.mail-action.draft", digest_bytes=16
    )
    assert not keyring.verify(
        b"thread-43", signature, purpose="teamagent.mail-action.draft", digest_bytes=16
    )
    # Purpose framing must still separate the button types under the dedicated key.
    assert not keyring.verify(
        payload, signature, purpose="teamagent.mail-action.ack", digest_bytes=16
    )


def test_report_link_is_unaffected_by_the_mail_action_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report-link already had its own secret; adding a mail-action key must not disturb it."""
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _DEDICATED_REPORT)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _DEDICATED_MAIL)

    assert diagnose_report_link_primary() == PRIMARY_OK
    assert load_report_link_hmac_keyring() is not None
    assert diagnose_mail_action_primary() == PRIMARY_OK
    assert load_mail_action_hmac_keyring() is not None


def test_same_value_for_both_purposes_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Purpose separation survives the new diagnostic: one value may not serve both domains."""
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _DEDICATED_MAIL)
    monkeypatch.setenv("REPORT_LINK_HMAC_SECRET", _DEDICATED_MAIL)

    assert load_mail_action_hmac_keyring() is None
    # Any other variable holding the same value trips the broader process-reuse predicate first;
    # both codes name the same corrective action, so accept either.
    assert diagnose_mail_action_primary() in {
        PRIMARY_REUSES_OTHER_PURPOSE,
        PRIMARY_REUSES_PROCESS_CREDENTIAL,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, PRIMARY_MISSING, id="unset"),
        pytest.param("", PRIMARY_MISSING, id="empty"),
        pytest.param("short", PRIMARY_TOO_SHORT, id="under-32-bytes"),
        pytest.param(_DEDICATED_MAIL + "\n", PRIMARY_NOT_STRIPPED, id="trailing-newline"),
        pytest.param("x" * 5000, "too_long", id="over-4096-bytes"),
        pytest.param("redis://cache.internal:6379/" + "y" * 40, PRIMARY_LOOKS_LIKE_CREDENTIAL),
    ],
)
def test_reason_codes_cover_the_operator_mistakes(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str
) -> None:
    if value is not None:
        monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", value)

    assert diagnose_mail_action_primary() == expected
    # A diagnostic that disagrees with the loader would be worse than none at all.
    assert load_mail_action_hmac_keyring() is None


def test_ok_is_reported_only_when_the_keyring_actually_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-drift: for a primary-only configuration, ``ok`` and a loaded keyring must agree.

    Production runs exactly this shape -- no previous key, no rotation T0, no durable state -- so
    the two must not be allowed to diverge as the loader evolves.
    """
    candidates = (
        None,
        "",
        "short",
        _DEDICATED_MAIL,
        _DEDICATED_MAIL + "\n",
        _DATABASE_URL,
        "mongodb://user:" + "p" * 40 + "@mongo.internal:27017/app",
        "x" * 5000,
    )
    for candidate in candidates:
        monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET", raising=False)
        if candidate is not None:
            monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", candidate)
        diagnosed_ok = diagnose_mail_action_primary() == PRIMARY_OK
        loaded = load_mail_action_hmac_keyring() is not None
        assert diagnosed_ok == loaded, f"diagnostic disagreed with loader for {candidate!r}"


def test_diagnostic_never_emits_key_material(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reason codes are printed to logs and tickets; they must not carry values or names."""
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _DATABASE_URL)

    reason = diagnose_mail_action_primary()
    assert _DATABASE_URL not in reason
    assert "DATABASE_URL" not in reason
    assert reason.replace("_", "").isalpha()
