from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).parents[2]
    / "infra"
    / "deploy"
    / "media_cutover_apply_authorizer.py"
)
_SPEC = importlib.util.spec_from_file_location("_media_cutover_authorizer", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = module
_SPEC.loader.exec_module(module)

_INTENT = "12345678-1234-4abc-8def-123456789abc"
_ATTEMPT = "87654321-4321-4cba-8fed-cba987654321"
_PLAN_SHA = "a" * 64
_CONTEXT_SHA = "b" * 64
_CLAIMS_SHA = "c" * 64
_GATE_SHA = "d" * 64
_SHARED_SHA = "e" * 64
_MIGRATION_SHA = "f" * 64
_REVIEWED_SHA = "1" * 64
_SIGNATURE_SHA = "2" * 64
_KEY_ARN = (
    "arn:aws:kms:ap-northeast-1:718959508629:key/"
    "11111111-2222-4333-8444-555555555555"
)
_IMAGE = (
    "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
    f"teamagent-media-worker@sha256:{'3' * 64}"
)
_COMMIT = "4" * 40


def _metadata() -> dict[str, str]:
    return {
        "intent_id": _INTENT,
        "plan_sha256": _PLAN_SHA,
        "deployment_context_sha256": _CONTEXT_SHA,
        "receipt_claims_sha256": _CLAIMS_SHA,
        "gate_query_sha256": _GATE_SHA,
        "shared_ledger_sha256": _SHARED_SHA,
    }


def _state() -> dict[str, dict[str, str | int]]:
    return {
        f"intent#{_INTENT}": {
            "record_id": f"intent#{_INTENT}",
            "state": "PREPARED",
            "terraform_context_sha256": _CONTEXT_SHA,
        },
        f"media-cutover#{_INTENT}": {
            "record_id": f"media-cutover#{_INTENT}",
            "status": "READY",
            "image_deployment_intent_id": _INTENT,
            "desired_image": _IMAGE,
            "claims_sha256": _CLAIMS_SHA,
            "kms_key_arn": _KEY_ARN,
            "signature_base64": "c2lnbmF0dXJl",
            "audit_expires_at": 9_999,
        },
    }


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, dict[str, str | int]],
    *,
    fail_transaction: bool = False,
) -> list[list[dict[str, Any]]]:
    transactions: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        module.release,
        "deployment_plan_metadata",
        lambda _path: _metadata(),
    )
    monkeypatch.setattr(
        module.runtime,
        "verify_media_cutover",
        lambda *_args, **_kwargs: {
            "kms_verified_at_epoch": 2_000,
            "claims_sha256": _CLAIMS_SHA,
            "signature_sha256": _SIGNATURE_SHA,
            "kms_key_arn": _KEY_ARN,
        },
    )
    monkeypatch.setattr(
        module.release,
        "_validate_prepared_intent",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        module.release,
        "_validate_deployment_lock",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        module.release,
        "_validate_applying_intent",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        module.release,
        "_dynamodb_get",
        lambda record_id: copy.deepcopy(state.get(record_id)),
    )

    def transact(*args: str, **_kwargs: Any) -> str:
        assert args[:2] == ("dynamodb", "transact-write-items")
        payload = json.loads(args[args.index("--transact-items") + 1])
        transactions.append(payload)
        if fail_transaction:
            raise module.release.EvidenceError("conditional transaction failed")
        assert len(payload) == 3
        assert state[f"intent#{_INTENT}"]["state"] == "PREPARED"
        assert state[f"media-cutover#{_INTENT}"]["status"] == "READY"
        lock = module.release._decode_ddb_item(payload[0]["Put"]["Item"])
        state[module.release.DEPLOYMENT_LOCK_RECORD_ID] = lock
        state[f"intent#{_INTENT}"].update(
            {
                "state": "APPLYING",
                "apply_attempt_id": _ATTEMPT,
                "apply_started_at": "1970-01-01T00:33:20Z",
            }
        )
        state[f"media-cutover#{_INTENT}"].update(
            {
                "status": "CONSUMED",
                "apply_attempt_id": _ATTEMPT,
                "plan_sha256": _PLAN_SHA,
                "consumed_at_epoch": 2_000,
            }
        )
        return "{}"

    monkeypatch.setattr(module.release, "_aws", transact)
    return transactions


def _authorize(state: dict[str, dict[str, str | int]]) -> dict[str, Any]:
    return module.authorize_media_apply(
        object(),
        plan_path=Path("/private/plan.tfplan"),
        media_receipt={
            "claims": {"expires_at_epoch": 3_000},
            "signature_base64": "c2lnbmF0dXJl",
        },
        desired_image=_IMAGE,
        image_deployment_intent_id=_INTENT,
        migration_contract_sha256=_MIGRATION_SHA,
        reviewed_plan_sha256=_REVIEWED_SHA,
        apply_attempt_id=_ATTEMPT,
        control_commit=_COMMIT,
    )


def test_authorization_consumes_media_intent_and_lock_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    transactions = _install_fakes(monkeypatch, state)

    receipt = _authorize(state)

    assert receipt["state"] == "AUTHORIZED"
    assert receipt["record_id"] == f"media-cutover#{_INTENT}"
    assert receipt["plan_sha256"] == _PLAN_SHA
    assert receipt["apply_attempt_id"] == _ATTEMPT
    assert len(transactions) == 1
    transaction = transactions[0]
    assert set(transaction[0]) == {"Put"}
    assert set(transaction[1]) == {"Update"}
    assert set(transaction[2]) == {"Update"}
    media_update = transaction[2]["Update"]
    assert ":ready" in media_update["ExpressionAttributeValues"]
    assert ":consumed" in media_update["ExpressionAttributeValues"]
    assert "attribute_not_exists(apply_attempt_id)" in media_update[
        "ConditionExpression"
    ]
    assert state[f"media-cutover#{_INTENT}"]["status"] == "CONSUMED"


def test_transaction_failure_has_no_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    before = copy.deepcopy(state)
    _install_fakes(monkeypatch, state, fail_transaction=True)

    with pytest.raises(module.AuthorizationError, match="atomically"):
        _authorize(state)
    assert state == before


def test_consumed_media_cannot_authorize_a_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    _install_fakes(monkeypatch, state)
    _authorize(state)

    with pytest.raises(module.AuthorizationError, match="READY"):
        _authorize(state)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "CONSUMED"),
        ("image_deployment_intent_id", "00000000-0000-4000-8000-000000000000"),
        ("desired_image", _IMAGE.replace("3", "5")),
        ("claims_sha256", "0" * 64),
        ("kms_key_arn", _KEY_ARN.replace("1", "9")),
        ("signature_base64", "b3RoZXI="),
    ],
)
def test_authorization_rejects_any_ready_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    state = _state()
    state[f"media-cutover#{_INTENT}"][field] = value
    transactions = _install_fakes(monkeypatch, state)

    with pytest.raises(module.AuthorizationError):
        _authorize(state)
    assert transactions == []


def test_other_intent_is_rejected_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    transactions = _install_fakes(monkeypatch, state)

    with pytest.raises(module.AuthorizationError, match="does not bind"):
        module.authorize_media_apply(
            object(),
            plan_path=Path("/private/plan.tfplan"),
            media_receipt={"claims": {"expires_at_epoch": 3_000}},
            desired_image=_IMAGE,
            image_deployment_intent_id=(
                "00000000-0000-4000-8000-000000000000"
            ),
            migration_contract_sha256=_MIGRATION_SHA,
            reviewed_plan_sha256=_REVIEWED_SHA,
            apply_attempt_id=_ATTEMPT,
            control_commit=_COMMIT,
        )
    assert transactions == []


def test_release_aws_calls_can_be_pinned_to_the_reviewed_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    aws_bin = tmp_path / "aws"
    aws_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    aws_bin.chmod(0o500)
    monkeypatch.setattr(module.release, "AWS_EXECUTABLE", "aws")

    module.release.configure_aws_executable(aws_bin)

    assert module.release.AWS_EXECUTABLE == str(aws_bin.resolve())
