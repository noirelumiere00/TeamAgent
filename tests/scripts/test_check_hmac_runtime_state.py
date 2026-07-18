from __future__ import annotations

import json

import pytest

from scripts.check_hmac_runtime_state import main
from teamagent.hmac_durable_state import (
    HmacRuntimeDecision,
    HmacRuntimeExpectation,
    _set_state_store_for_testing,
)

_MAIL_GENERATION = "arn:aws:secretsmanager:region:123456789012:secret:mail@" + "m" * 32
_REPORT_GENERATION = "arn:aws:secretsmanager:region:123456789012:secret:report@" + "r" * 32
_PROVENANCE = "a" * 64


class _ReadinessStore:
    def __init__(self, *, attest: bool = True) -> None:
        self.attest = attest
        self.attested_domains: tuple[str, ...] = ()

    def evaluate(self, expectation: HmacRuntimeExpectation) -> HmacRuntimeDecision | None:
        return HmacRuntimeDecision(
            effective_now=2_000_000_000,
            previous_eligible=False,
            issuance_allowed=False,
            expectation=expectation,
        )

    def attest_worker(self, expectations: tuple[HmacRuntimeExpectation, ...]) -> bool:
        self.attested_domains = tuple(expectation.domain for expectation in expectations)
        return self.attest


@pytest.fixture(autouse=True)
def _runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "TEAMAGENT_HMAC_STATE_REQUIRED": "1",
        "TEAMAGENT_HMAC_STATE_TABLE": "teamagent-dev-hmac-state",
        "TEAMAGENT_HMAC_STATE_SCOPE": "teamagent/dev",
        "TEAMAGENT_HMAC_ROTATION_EPOCH": "hmac-2026-07-18",
        "TEAMAGENT_HMAC_PROVENANCE": _PROVENANCE,
        "TEAMAGENT_HMAC_WORKER_ID": "i-0123456789abcdef0",
        "MAIL_ACTION_HMAC_PRIMARY_GENERATION": _MAIL_GENERATION,
        "REPORT_LINK_HMAC_PRIMARY_GENERATION": _REPORT_GENERATION,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    _set_state_store_for_testing(None)
    yield
    _set_state_store_for_testing(None)


def test_worker_readiness_attests_both_domains_without_metadata_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _ReadinessStore()
    _set_state_store_for_testing(store)

    assert main(["--domains", "MAIL_ACTION,REPORT_LINK", "--worker-attestation"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"ok": True}
    assert store.attested_domains == ("mail_action", "report_link")
    assert _MAIL_GENERATION not in output
    assert _REPORT_GENERATION not in output
    assert _PROVENANCE not in output


def test_worker_readiness_failure_is_boolean_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_state_store_for_testing(_ReadinessStore(attest=False))
    assert main(["--domains", "MAIL_ACTION,REPORT_LINK", "--worker-attestation"]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False}
