from __future__ import annotations

import copy
import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "deploy" / "deployment_apply_finalizer.py"
INTENT = "11111111-1111-4111-8111-111111111111"
ATTEMPT = "22222222-2222-4222-8222-222222222222"
PLAN_SHA256 = "a" * 64
BASELINE_SHA256 = "b" * 64
PLANNED_SHA256 = "c" * 64
ROTATION_EPOCH = "2026-07-final"
VERIFIED_AT = 1_784_500_000


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "deployment_apply_finalizer_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FINALIZER = _load_module()


def _key_token(
    table_name: str,
    key: Mapping[str, Mapping[str, str]],
) -> tuple[str, str]:
    return (
        table_name,
        json.dumps(key, separators=(",", ":"), sort_keys=True),
    )


class FakeLedger:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transactions: list[list[dict[str, Any]]] = []
        self.failure = ""

    def seed(
        self,
        *,
        table_name: str,
        key: Mapping[str, Mapping[str, str]],
        item: Mapping[str, Any],
    ) -> None:
        self.items[_key_token(table_name, key)] = copy.deepcopy(dict(item))

    def get_item(
        self,
        *,
        table_name: str,
        key: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Any] | None:
        item = self.items.get(_key_token(table_name, key))
        return copy.deepcopy(item) if item is not None else None

    def transact_write(
        self,
        *,
        items: Sequence[Mapping[str, Any]],
        client_request_token: str,
    ) -> None:
        assert client_request_token == FINALIZER._client_request_token(ATTEMPT)
        assert len(client_request_token) == 36
        if self.failure == "before":
            raise RuntimeError("injected pre-commit failure")
        staged = copy.deepcopy(self.items)
        transaction = [copy.deepcopy(dict(item)) for item in items]
        for operation in transaction:
            if "Put" in operation:
                request = operation["Put"]
                key = FINALIZER._image_key(request["Item"]["record_id"]["S"])
                token = _key_token(request["TableName"], key)
                if token in staged:
                    raise RuntimeError("conditional put failed")
                staged[token] = copy.deepcopy(request["Item"])
                continue
            if "Delete" in operation:
                request = operation["Delete"]
                token = _key_token(request["TableName"], request["Key"])
                if token not in staged:
                    raise RuntimeError("conditional delete failed")
                del staged[token]
                continue
            request = operation["Update"]
            token = _key_token(request["TableName"], request["Key"])
            current = staged.get(token)
            if current is None:
                raise RuntimeError("conditional update failed")
            values = request["ExpressionAttributeValues"]
            if current.get("record_type", {}).get("S") == FINALIZER._EVENTBRIDGE_ACTIVE_RECORD_TYPE:
                assert current["stage"]["S"] == "applying"
                current["stage"] = copy.deepcopy(values[":complete"])
                current["finished_at"] = copy.deepcopy(values[":finished"])
                current["revision"] = {"N": str(int(current["revision"]["N"]) + 1)}
            elif current["record_id"]["S"] == f"intent#{INTENT}":
                assert current["state"]["S"] == "CONSUMED"
                current["state"] = copy.deepcopy(values[":applied"])
                current["outcome_recorded_at"] = copy.deepcopy(values[":recorded"])
            else:
                assert current["stage"]["S"] == "APPLYING"
                current["stage"] = copy.deepcopy(values[":applied"])
        self.items = staged
        self.transactions.append(transaction)
        if self.failure == "after":
            raise RuntimeError("injected ambiguous post-commit failure")


def _ecs_attempt() -> dict[str, Any]:
    return FINALIZER._ddb_item(
        {
            "record_id": f"ecs-service-apply#{ATTEMPT}",
            "record_type": "teamagent.ecs-service-apply-saga",
            "schema_version": 1,
            "stage": "APPLYING",
            "plan_sha256": PLAN_SHA256,
            "apply_attempt_id": ATTEMPT,
            "baseline_json": '{"mcp":{},"connect_web":{}}',
            "baseline_sha256": BASELINE_SHA256,
            "planned_json": '{"services":{}}',
            "planned_sha256": PLANNED_SHA256,
        }
    )


def _ecs_active() -> dict[str, Any]:
    return FINALIZER._ddb_item(
        {
            "record_id": FINALIZER._ECS_ACTIVE_RECORD_ID,
            "record_type": FINALIZER._ECS_ACTIVE_RECORD_TYPE,
            "schema_version": 1,
            "scope_id": FINALIZER._ECS_ACTIVE_SCOPE_ID,
            "stage": "APPLYING",
            "apply_attempt_id": ATTEMPT,
            "attempt_record_id": f"ecs-service-apply#{ATTEMPT}",
            "plan_sha256": PLAN_SHA256,
            "baseline_sha256": BASELINE_SHA256,
            "planned_sha256": PLANNED_SHA256,
        }
    )


def _eventbridge_active() -> dict[str, Any]:
    return FINALIZER._ddb_item(
        {
            "record_id": (f"{FINALIZER._EVENTBRIDGE_RECORD_PREFIX}{ROTATION_EPOCH}"),
            "record_type": FINALIZER._EVENTBRIDGE_ACTIVE_RECORD_TYPE,
            "schema_version": 2,
            "stage": "applying",
            "revision": 4,
            "rotation_epoch": ROTATION_EPOCH,
            "plan_sha256": PLAN_SHA256,
            "apply_attempt_id": ATTEMPT,
            "baseline_json": '{"schema_version":2,"rules":{}}',
            "baseline_sha256": BASELINE_SHA256,
            "planned_json": '{"schema_version":2,"rules":{}}',
            "planned_sha256": PLANNED_SHA256,
            "started_at": VERIFIED_AT - 100,
        }
    )


def _ecs_verification(attempt: Mapping[str, Any]) -> dict[str, Any]:
    receipt = {
        "kind": "teamagent-ecs-service-apply-saga-receipt",
        "schema_version": 1,
        "record_id": f"ecs-service-apply#{ATTEMPT}",
        "stage": "VERIFIED_APPLIED",
        "plan_sha256": PLAN_SHA256,
        "apply_attempt_id": ATTEMPT,
        "baseline_sha256": BASELINE_SHA256,
        "planned_sha256": PLANNED_SHA256,
        "ledger_item_sha256": FINALIZER._digest(attempt),
    }
    receipt["receipt_sha256"] = FINALIZER._digest(receipt)
    return receipt


def _eventbridge_verification(active: Mapping[str, Any]) -> dict[str, Any]:
    receipt = {
        "kind": "teamagent-eventbridge-apply-saga-receipt",
        "schema_version": 2,
        "record_id": f"{FINALIZER._EVENTBRIDGE_RECORD_PREFIX}{ROTATION_EPOCH}",
        "rotation_epoch": ROTATION_EPOCH,
        "stage": "verified_applied",
        "plan_sha256": PLAN_SHA256,
        "apply_attempt_id": ATTEMPT,
        "baseline_sha256": BASELINE_SHA256,
        "planned_sha256": PLANNED_SHA256,
        "ledger_item_sha256": FINALIZER._digest(active),
        "verified_at": VERIFIED_AT,
    }
    receipt["receipt_sha256"] = FINALIZER._digest(receipt)
    return receipt


def _draft() -> dict[str, Any]:
    return {
        "kind": "terraform-runtime-apply-receipt-draft",
        "schema_version": 7,
        "guard_version": "24",
        "account_id": "718959508629",
        "region": "ap-northeast-1",
        "git_commit": "d" * 40,
        "status": "verified_pending_finalization",
        "migration_kind": "runtime",
        "migration_id": "test-migration",
        "required_migration_id": "",
        "provenance_outcome": "pending",
        "image_deployment_intent_id": INTENT,
        "apply_attempt_id": ATTEMPT,
        "source_receipt_sha256": "1" * 64,
        "migration_contract_sha256": "2" * 64,
        "reviewed_plan_sha256": "3" * 64,
        "plan_sha256": PLAN_SHA256,
        "openclaw_rollout_result_sha256": "4" * 64,
        "post_apply_service_probe_sha256": "5" * 64,
        "post_state_contract_sha256": "6" * 64,
        "post_live_fingerprint_sha256": "7" * 64,
        "post_runtime_inventory_sha256": "8" * 64,
        "shared_deployment_lock_record_id": FINALIZER._LOCK_RECORD_ID,
        "shared_deployment_lock_receipt_sha256": "9" * 64,
    }


def _ledger() -> tuple[FakeLedger, dict[str, Any], dict[str, Any]]:
    ledger = FakeLedger()
    attempt = _ecs_attempt()
    active = _eventbridge_active()
    ledger.seed(
        table_name=FINALIZER._IMAGE_LEDGER_TABLE,
        key=FINALIZER._image_key(f"ecs-service-apply#{ATTEMPT}"),
        item=attempt,
    )
    ledger.seed(
        table_name=FINALIZER._IMAGE_LEDGER_TABLE,
        key=FINALIZER._image_key(FINALIZER._ECS_ACTIVE_RECORD_ID),
        item=_ecs_active(),
    )
    ledger.seed(
        table_name=FINALIZER._IMAGE_LEDGER_TABLE,
        key=FINALIZER._image_key(f"{FINALIZER._EVENTBRIDGE_RECORD_PREFIX}{ROTATION_EPOCH}"),
        item=active,
    )
    ledger.seed(
        table_name=FINALIZER._IMAGE_LEDGER_TABLE,
        key=FINALIZER._image_key(f"intent#{INTENT}"),
        item=FINALIZER._ddb_item(
            {
                "record_id": f"intent#{INTENT}",
                "record_type": "teamagent.image-deployment-intent",
                "schema_version": 1,
                "intent_id": INTENT,
                "state": "CONSUMED",
                "plan_sha256": PLAN_SHA256,
                "apply_attempt_id": ATTEMPT,
                "audit_expires_at": VERIFIED_AT + 7_776_000,
            }
        ),
    )
    ledger.seed(
        table_name=FINALIZER._IMAGE_LEDGER_TABLE,
        key=FINALIZER._image_key(FINALIZER._LOCK_RECORD_ID),
        item=FINALIZER._ddb_item(
            {
                "record_id": FINALIZER._LOCK_RECORD_ID,
                "record_type": "teamagent.image-release-apply-lock",
                "schema_version": 1,
                "state": "LOCKED",
                "intent_id": INTENT,
                "plan_sha256": PLAN_SHA256,
                "apply_attempt_id": ATTEMPT,
                "lease_expires_at": VERIFIED_AT + 300,
            }
        ),
    )
    return ledger, _ecs_verification(attempt), _eventbridge_verification(active)


def _finalizer(ledger: FakeLedger) -> Any:
    return FINALIZER.ApplyFinalizer(
        client=ledger,
        intent_id=INTENT,
        plan_sha256=PLAN_SHA256,
        apply_attempt_id=ATTEMPT,
    )


def test_commit_atomically_terminalizes_every_ledger_and_persists_receipt(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    ledger, ecs, eventbridge = _ledger()
    output = tmp_path / "apply.json"

    result = _finalizer(ledger).commit(
        draft_raw=_draft(),
        eventbridge_raw=eventbridge,
        ecs_raw=ecs,
        output=output,
    )

    assert result["state"] == "COMMITTED"
    assert len(ledger.transactions) == 1
    assert len(ledger.transactions[0]) >= 7
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["status"] == "applied"
    assert receipt["schema_version"] == 7
    assert receipt["ecs_service_saga_receipt"]["stage"] == "APPLIED"
    assert receipt["deployment_finalization_receipt"]["state"] == "APPLIED"
    assert (
        ledger.get_item(
            table_name=FINALIZER._IMAGE_LEDGER_TABLE,
            key=FINALIZER._image_key(FINALIZER._LOCK_RECORD_ID),
        )
        is None
    )


def test_ambiguous_success_is_confirmed_and_materialized(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    ledger, ecs, eventbridge = _ledger()
    ledger.failure = "after"
    output = tmp_path / "apply.json"

    result = _finalizer(ledger).commit(
        draft_raw=_draft(),
        eventbridge_raw=eventbridge,
        ecs_raw=ecs,
        output=output,
    )

    assert result["state"] == "RECOVERED_AFTER_AMBIGUOUS_COMMIT"
    assert output.is_file()


def test_committed_receipt_can_be_recovered_after_local_crash(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    ledger, ecs, eventbridge = _ledger()
    first = tmp_path / "first.json"
    _finalizer(ledger).commit(
        draft_raw=_draft(),
        eventbridge_raw=eventbridge,
        ecs_raw=ecs,
        output=first,
    )
    recovered = tmp_path / "recovered.json"

    result = _finalizer(ledger).recover(output=recovered)

    assert result["state"] == "RECOVERED"
    assert recovered.read_bytes() == first.read_bytes()


def test_failure_before_transaction_leaves_sagas_recoverable(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    ledger, ecs, eventbridge = _ledger()
    ledger.failure = "before"
    output = tmp_path / "apply.json"

    with pytest.raises(FINALIZER.FinalizationError):
        _finalizer(ledger).commit(
            draft_raw=_draft(),
            eventbridge_raw=eventbridge,
            ecs_raw=ecs,
            output=output,
        )

    assert not output.exists()
    assert (
        ledger.get_item(
            table_name=FINALIZER._IMAGE_LEDGER_TABLE,
            key=FINALIZER._image_key(f"ecs-service-apply#{ATTEMPT}"),
        )["stage"]["S"]
        == "APPLYING"
    )


def test_tampered_verification_is_rejected_before_transaction(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    ledger, ecs, eventbridge = _ledger()
    eventbridge["verified_at"] += 1

    with pytest.raises(FINALIZER.FinalizationError):
        _finalizer(ledger).commit(
            draft_raw=_draft(),
            eventbridge_raw=eventbridge,
            ecs_raw=ecs,
            output=tmp_path / "apply.json",
        )

    assert ledger.transactions == []


def test_replay_with_different_draft_is_rejected(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    ledger, ecs, eventbridge = _ledger()
    _finalizer(ledger).commit(
        draft_raw=_draft(),
        eventbridge_raw=eventbridge,
        ecs_raw=ecs,
        output=tmp_path / "first.json",
    )
    changed = _draft()
    changed["migration_id"] = "different"

    with pytest.raises(FINALIZER.FinalizationError):
        _finalizer(ledger).commit(
            draft_raw=changed,
            eventbridge_raw=eventbridge,
            ecs_raw=ecs,
            output=tmp_path / "second.json",
        )


def test_recovery_rejects_manifest_extension_or_missing_chunk(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    ledger, ecs, eventbridge = _ledger()
    _finalizer(ledger).commit(
        draft_raw=_draft(),
        eventbridge_raw=eventbridge,
        ecs_raw=ecs,
        output=tmp_path / "first.json",
    )
    manifest_key = _key_token(
        FINALIZER._IMAGE_LEDGER_TABLE,
        FINALIZER._image_key(FINALIZER._finalization_record_id(INTENT)),
    )
    ledger.items[manifest_key]["unexpected"] = {"S": "tampered"}
    with pytest.raises(FINALIZER.FinalizationError):
        _finalizer(ledger).recover(output=tmp_path / "tampered.json")
    del ledger.items[manifest_key]["unexpected"]

    chunk_key = _key_token(
        FINALIZER._IMAGE_LEDGER_TABLE,
        FINALIZER._image_key(FINALIZER._chunk_record_id(INTENT, 0)),
    )
    del ledger.items[chunk_key]
    with pytest.raises(FINALIZER.FinalizationError):
        _finalizer(ledger).recover(output=tmp_path / "missing.json")
