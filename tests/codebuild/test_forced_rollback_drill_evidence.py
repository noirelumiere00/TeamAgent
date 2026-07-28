from __future__ import annotations

import copy
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODEBUILD = ROOT / "infra" / "codebuild"
MODULE_PATH = CODEBUILD / "forced_rollback_drill_evidence.py"

if str(CODEBUILD) not in sys.path:
    sys.path.insert(0, str(CODEBUILD))


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "forced_rollback_drill_evidence_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVIDENCE = _load_module()
RELEASE_APPROVAL = sys.modules["teamagent_release_approval"]

DRILL_ID = "10000000-0000-4000-8000-000000000001"
APPROVAL_IDS = (
    "20000000-0000-4000-8000-000000000001",
    "20000000-0000-4000-8000-000000000002",
)
LINEAGE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
KMS_KEY_ARN = "arn:aws:kms:ap-northeast-1:718959508629:key/11111111-1111-4111-8111-111111111111"
EVIDENCE_BUCKET = "teamagent-dev-forced-rollback-evidence"

INITIATING_PRINCIPAL = {
    "arn": "arn:aws:iam::718959508629:user/drill-operator",
    "user_id": "AIDAEXAMPLEDRILLOPERATOR",
    "source_identity": "forced-rollback-drill",
}
AUTOMATION_PRINCIPAL = {
    "arn": ("arn:aws:sts::718959508629:assumed-role/teamagent-dev-drill-controller/build-1"),
    "user_id": "AROAAUTOMATION:build-1",
    "source_identity": "forced-rollback-drill",
}
APPROVING_PRINCIPAL = {
    "arn": "arn:aws:iam::718959508629:role/teamagent-dev-drill-approver",
    "user_id": "AROAAPPROVER",
    "source_identity": "forced-rollback-review",
}


def _locator(name: str, index: int) -> dict[str, Any]:
    key = f"forced-rollback/{name}-{index}.json"
    sha256 = f"{index:064x}"
    size = 1000 + index
    version_id = f"payload-version-{index}"
    return {
        "bucket": EVIDENCE_BUCKET,
        "key": key,
        "version_id": version_id,
        "sha256": sha256,
        "size": size,
        "content_type": "application/json",
        "object_lock_mode": "COMPLIANCE",
        "retain_until": "2036-07-24T12:00:00Z",
        "encryption_kms_key_arn": KMS_KEY_ARN,
        "signature": {
            "key": f"{key}.sig",
            "version_id": f"signature-version-{index}",
            "sha256": f"{10000 + index:064x}",
            "verified": True,
        },
        "signer": {
            "kms_key_arn": KMS_KEY_ARN,
            "algorithm": "RSASSA_PSS_SHA_256",
        },
        "exact_version_redownload": {
            "requested_version_id": version_id,
            "returned_version_id": version_id,
            "sha256": sha256,
            "size": size,
            "bytes_match": True,
        },
    }


def _scope() -> dict[str, Any]:
    return {
        "pipelines": ["mcp"],
        "subjects": [
            {
                "pipeline": "mcp",
                "name": "core",
                "release_repository": "teamagent-mcp",
                "previous_digest": f"sha256:{'1' * 64}",
                "initial_new_digest": f"sha256:{'3' * 64}",
            },
            {
                "pipeline": "mcp",
                "name": "media",
                "release_repository": "teamagent-media-worker",
                "previous_digest": f"sha256:{'2' * 64}",
                "initial_new_digest": f"sha256:{'4' * 64}",
            },
        ],
        "resources": [
            {
                "consumer_id": "connect_web",
                "terraform_address": "aws_ecs_task_definition.connect_web[0]",
                "pipeline": "mcp",
                "subject": "core",
                "previous_task_definition_arn": (
                    "arn:aws:ecs:ap-northeast-1:718959508629:"
                    "task-definition/teamagent-dev-connect-web:10"
                ),
                "previous_task_revision": 10,
                "initial_new_task_definition_arn": (
                    "arn:aws:ecs:ap-northeast-1:718959508629:"
                    "task-definition/teamagent-dev-connect-web:11"
                ),
                "initial_new_task_revision": 11,
            },
            {
                "consumer_id": "mcp",
                "terraform_address": "aws_ecs_task_definition.mcp",
                "pipeline": "mcp",
                "subject": "core",
                "previous_task_definition_arn": (
                    "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:20"
                ),
                "previous_task_revision": 20,
                "initial_new_task_definition_arn": (
                    "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:21"
                ),
                "initial_new_task_revision": 21,
            },
            {
                "consumer_id": "tiktok_acquire",
                "terraform_address": "aws_ecs_task_definition.tiktok_acquire[0]",
                "pipeline": "mcp",
                "subject": "media",
                "previous_task_definition_arn": (
                    "arn:aws:ecs:ap-northeast-1:718959508629:"
                    "task-definition/teamagent-dev-tiktok-acquire:30"
                ),
                "previous_task_revision": 30,
                "initial_new_task_definition_arn": (
                    "arn:aws:ecs:ap-northeast-1:718959508629:"
                    "task-definition/teamagent-dev-tiktok-acquire:31"
                ),
                "initial_new_task_revision": 31,
            },
        ],
    }


def _snapshot(scope: dict[str, Any], endpoint: str) -> dict[str, Any]:
    digest_key = "initial_new_digest" if endpoint == "initial_new" else "previous_digest"
    arn_key = (
        "initial_new_task_definition_arn"
        if endpoint == "initial_new"
        else "previous_task_definition_arn"
    )
    revision_key = (
        "initial_new_task_revision" if endpoint == "initial_new" else "previous_task_revision"
    )
    digests = {
        (subject["pipeline"], subject["name"]): subject[digest_key] for subject in scope["subjects"]
    }
    return {
        "subjects": [
            {
                "pipeline": subject["pipeline"],
                "name": subject["name"],
                "release_repository": subject["release_repository"],
                "digest": subject[digest_key],
            }
            for subject in scope["subjects"]
        ],
        "resources": [
            {
                "consumer_id": resource["consumer_id"],
                "terraform_address": resource["terraform_address"],
                "pipeline": resource["pipeline"],
                "subject": resource["subject"],
                "task_definition_arn": resource[arn_key],
                "task_revision": resource[revision_key],
                "digest": digests[(resource["pipeline"], resource["subject"])],
            }
            for resource in scope["resources"]
        ],
    }


def _complete_passed_aggregate() -> dict[str, Any]:
    scope = _scope()
    initial_new = _snapshot(scope, "initial_new")
    previous_old = _snapshot(scope, "previous_old")
    referenced_locators: list[dict[str, Any]] = []
    locator_index = 1

    def evidence_locator(name: str) -> dict[str, Any]:
        nonlocal locator_index
        locator = _locator(name, locator_index)
        locator_index += 1
        referenced_locators.append(locator)
        return locator

    initial_release_apply = evidence_locator("initial-release-apply")
    baseline_locator = evidence_locator("baseline-live-snapshot")

    leg_times = (
        {
            "started": "2026-07-24T12:02:00Z",
            "authorization": "2026-07-24T12:03:00Z",
            "plan": "2026-07-24T12:04:00Z",
            "approval": "2026-07-24T12:05:00Z",
            "apply_started": "2026-07-24T12:06:00Z",
            "apply_completed": "2026-07-24T12:07:00Z",
            "ecs": "2026-07-24T12:08:00Z",
            "run_task": "2026-07-24T12:09:00Z",
            "dm_qa": "2026-07-24T12:10:00Z",
            "completed": "2026-07-24T12:11:00Z",
        },
        {
            "started": "2026-07-24T12:12:00Z",
            "authorization": "2026-07-24T12:13:00Z",
            "plan": "2026-07-24T12:14:00Z",
            "approval": "2026-07-24T12:15:00Z",
            "apply_started": "2026-07-24T12:16:00Z",
            "apply_completed": "2026-07-24T12:17:00Z",
            "ecs": "2026-07-24T12:18:00Z",
            "run_task": "2026-07-24T12:19:00Z",
            "dm_qa": "2026-07-24T12:20:00Z",
            "completed": "2026-07-24T12:21:00Z",
        },
    )

    def leg(
        *,
        index: int,
        source: dict[str, Any],
        target: dict[str, Any],
        channel: str,
        name: str,
        serial_before: int,
        serial_after: int,
    ) -> dict[str, Any]:
        ordinal = index + 1
        times = leg_times[index]
        authorization_locator = evidence_locator(f"leg-{ordinal}-release-authorization")
        plan_locator = evidence_locator(f"leg-{ordinal}-plan")
        approval_locator = evidence_locator(f"leg-{ordinal}-approval")
        apply_locator = evidence_locator(f"leg-{ordinal}-apply")
        ecs_locator = evidence_locator(f"leg-{ordinal}-ecs")
        run_task_locator = evidence_locator(f"leg-{ordinal}-run-task")
        dm_qa_locator = evidence_locator(f"leg-{ordinal}-dm-qa")
        return {
            "ordinal": ordinal,
            "name": name,
            "channel": channel,
            "from": copy.deepcopy(source),
            "to": copy.deepcopy(target),
            "release_authorizations": [
                {
                    "authorization_id": (f"30000000-0000-4000-8000-{ordinal:012d}"),
                    "deployment_intent_id": (f"40000000-0000-4000-8000-{ordinal:012d}"),
                    "drill_id": DRILL_ID,
                    "pipeline": "mcp",
                    "channel": channel,
                    "subjects": copy.deepcopy(target["subjects"]),
                    "issued_at_utc": times["authorization"],
                    "locator": authorization_locator,
                }
            ],
            "plan": {
                "sha256": plan_locator["sha256"],
                "created_at_utc": times["plan"],
                "terraform_lineage": LINEAGE,
                "terraform_serial": serial_before,
                "from": copy.deepcopy(source),
                "to": copy.deepcopy(target),
                "changed_resources": [
                    resource["terraform_address"] for resource in scope["resources"]
                ],
                "locator": plan_locator,
            },
            "approval": {
                "approval_id": APPROVAL_IDS[index],
                "drill_id": DRILL_ID,
                "plan_sha256": plan_locator["sha256"],
                "decision": "APPROVED",
                "approved_at_utc": times["approval"],
                "approved_by": copy.deepcopy(APPROVING_PRINCIPAL),
                "locator": approval_locator,
            },
            "apply": {
                "apply_attempt_id": (f"50000000-0000-4000-8000-{ordinal:012d}"),
                "plan_sha256": plan_locator["sha256"],
                "started_at_utc": times["apply_started"],
                "completed_at_utc": times["apply_completed"],
                "result": "PASSED",
                "terraform_lineage": LINEAGE,
                "terraform_serial_before": serial_before,
                "terraform_serial_after": serial_after,
                "state": copy.deepcopy(target),
                "locator": apply_locator,
            },
            "ecs": {
                "result": "PASSED",
                "steady": True,
                "verified_at_utc": times["ecs"],
                "live_snapshot": copy.deepcopy(target),
                "locator": ecs_locator,
            },
            "run_task_health": {
                "result": "PASSED",
                "verified_at_utc": times["run_task"],
                "tasks": copy.deepcopy(target["resources"]),
                "locator": run_task_locator,
            },
            "dm_qa": {
                "result": "PASSED",
                "verified_at_utc": times["dm_qa"],
                "subject_digests": copy.deepcopy(target["subjects"]),
                "locator": dm_qa_locator,
            },
            "started_at_utc": times["started"],
            "completed_at_utc": times["completed"],
            "result": "PASSED",
            "recovery": {
                "attempted": False,
                "result": "NOT_REQUIRED",
                "completed_at_utc": None,
                "last_exact_confirmed_digests": copy.deepcopy(target["subjects"]),
                "locator": None,
            },
        }

    legs = [
        leg(
            index=0,
            source=initial_new,
            target=previous_old,
            channel="rollback",
            name="rollback_to_previous",
            serial_before=40,
            serial_after=41,
        ),
        leg(
            index=1,
            source=previous_old,
            target=initial_new,
            channel="active",
            name="restore_active",
            serial_before=41,
            serial_after=42,
        ),
    ]
    terminal_locator = evidence_locator("safe-terminal-live-snapshot")
    aggregate_locator = _locator("aggregate", 900)
    immutable_object = {
        key: copy.deepcopy(value)
        for key, value in aggregate_locator.items()
        if key not in {"signature", "signer"}
    }
    aggregate = {
        "schema_version": 1,
        "kind": "teamagent.forced-rollback-drill",
        "drill_id": DRILL_ID,
        "status": "PASSED",
        "environment": {
            "account_id": "718959508629",
            "region": "ap-northeast-1",
            "name": "dev",
        },
        "control": {
            "git_commit": "a" * 40,
            "drill_contract_sha256": "b" * 64,
            "initial_release_apply": initial_release_apply,
            "initial_release_verified_at_utc": "2026-07-24T12:00:00Z",
            "started_at_utc": "2026-07-24T12:01:00Z",
            "completed_at_utc": "2026-07-24T12:23:00Z",
            "max_start_delay_seconds": 1800,
            "max_old_dwell_seconds": 1200,
        },
        "actors": {
            "initiating_principal": copy.deepcopy(INITIATING_PRINCIPAL),
            "automation_principals": [copy.deepcopy(AUTOMATION_PRINCIPAL)],
            "approvals": [
                {
                    "approval_id": APPROVAL_IDS[0],
                    "principal": copy.deepcopy(APPROVING_PRINCIPAL),
                },
                {
                    "approval_id": APPROVAL_IDS[1],
                    "principal": copy.deepcopy(APPROVING_PRINCIPAL),
                },
            ],
        },
        "scope": scope,
        "baseline": {
            "terraform_lineage": LINEAGE,
            "terraform_serial": 40,
            "live_snapshot": {
                "snapshot": copy.deepcopy(initial_new),
                "locator": baseline_locator,
            },
            "initial_new_verified": True,
        },
        "legs": legs,
        "safe_terminal_state": {
            "classification": "INITIAL_NEW",
            "steady": True,
            "verified_at_utc": "2026-07-24T12:22:00Z",
            "live_snapshot": {
                "snapshot": copy.deepcopy(initial_new),
                "locator": terminal_locator,
            },
        },
        "artifact_manifest": [
            copy.deepcopy(locator)
            for locator in sorted(
                referenced_locators,
                key=lambda item: (
                    item["bucket"],
                    item["key"],
                    item["version_id"],
                ),
            )
        ],
        "integrity": {
            "canonical_sha256": "0" * 64,
            "kms_key_arn": aggregate_locator["signer"]["kms_key_arn"],
            "signing_algorithm": aggregate_locator["signer"]["algorithm"],
            "signature": copy.deepcopy(aggregate_locator["signature"]),
            "immutable_object": immutable_object,
        },
    }
    canonical_body = EVIDENCE.canonical_drill_body_bytes(aggregate)
    canonical_sha256 = EVIDENCE.canonical_drill_sha256(aggregate)
    aggregate["integrity"]["canonical_sha256"] = canonical_sha256
    aggregate["integrity"]["immutable_object"]["sha256"] = canonical_sha256
    aggregate["integrity"]["immutable_object"]["size"] = len(canonical_body)
    redownload = aggregate["integrity"]["immutable_object"]["exact_version_redownload"]
    redownload["sha256"] = canonical_sha256
    redownload["size"] = len(canonical_body)
    return aggregate


def _rehash(payload: dict[str, Any]) -> None:
    canonical_body = EVIDENCE.canonical_drill_body_bytes(payload)
    canonical_sha256 = EVIDENCE.canonical_drill_sha256(payload)
    payload["integrity"]["canonical_sha256"] = canonical_sha256
    payload["integrity"]["immutable_object"]["sha256"] = canonical_sha256
    payload["integrity"]["immutable_object"]["size"] = len(canonical_body)
    redownload = payload["integrity"]["immutable_object"]["exact_version_redownload"]
    redownload["sha256"] = canonical_sha256
    redownload["size"] = len(canonical_body)


def _expected_bindings() -> dict[str, Any]:
    return {
        "git_commit": "a" * 40,
        "drill_contract_sha256": "b" * 64,
        "initial_release_apply": _locator("initial-release-apply", 1),
        "initial_release_verified_at_utc": "2026-07-24T12:00:00Z",
        "scope": _scope(),
    }


def _validate(
    payload: dict[str, Any],
    *,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bindings = _expected_bindings() if expected is None else expected
    return EVIDENCE.validate_drill_evidence(payload, bindings)


def _get_path(value: Any, path: tuple[str | int, ...]) -> Any:
    current = value
    for item in path:
        current = current[item]
    return current


def _locator_identity(locator: dict[str, Any]) -> tuple[str, str, str]:
    return locator["bucket"], locator["key"], locator["version_id"]


def _mutate_locator_and_manifest(
    payload: dict[str, Any],
    path: tuple[str | int, ...],
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    locator = _get_path(payload, path)
    identity = _locator_identity(locator)
    manifest_matches = [
        item for item in payload["artifact_manifest"] if _locator_identity(item) == identity
    ]
    assert len(manifest_matches) == 1
    mutation(locator)
    mutation(manifest_matches[0])
    _rehash(payload)


def _reverse_object_key_order(value: Any) -> Any:
    if type(value) is dict:
        return {key: _reverse_object_key_order(item) for key, item in reversed(list(value.items()))}
    if type(value) is list:
        return [_reverse_object_key_order(item) for item in value]
    return value


def _replace_text(value: Any, before: str, after: str) -> Any:
    if type(value) is dict:
        return {key: _replace_text(item, before, after) for key, item in value.items()}
    if type(value) is list:
        return [_replace_text(item, before, after) for item in value]
    return after if value == before else value


def _set_leg2_times(payload: dict[str, Any], *, start: str) -> None:
    leg2 = payload["legs"][1]
    if start == "boundary":
        values = {
            "started": "2026-07-24T12:31:00Z",
            "authorization": "2026-07-24T12:31:01Z",
            "plan": "2026-07-24T12:31:02Z",
            "approval": "2026-07-24T12:31:03Z",
            "apply_started": "2026-07-24T12:31:04Z",
            "apply_completed": "2026-07-24T12:31:05Z",
            "ecs": "2026-07-24T12:31:06Z",
            "run_task": "2026-07-24T12:31:07Z",
            "dm_qa": "2026-07-24T12:31:08Z",
            "completed": "2026-07-24T12:31:09Z",
            "safe": "2026-07-24T12:31:10Z",
            "control": "2026-07-24T12:31:11Z",
        }
    else:
        values = {
            "started": "2026-07-24T12:31:01Z",
            "authorization": "2026-07-24T12:31:02Z",
            "plan": "2026-07-24T12:31:03Z",
            "approval": "2026-07-24T12:31:04Z",
            "apply_started": "2026-07-24T12:31:05Z",
            "apply_completed": "2026-07-24T12:31:06Z",
            "ecs": "2026-07-24T12:31:07Z",
            "run_task": "2026-07-24T12:31:08Z",
            "dm_qa": "2026-07-24T12:31:09Z",
            "completed": "2026-07-24T12:31:10Z",
            "safe": "2026-07-24T12:31:11Z",
            "control": "2026-07-24T12:31:12Z",
        }
    leg2["started_at_utc"] = values["started"]
    leg2["release_authorizations"][0]["issued_at_utc"] = values["authorization"]
    leg2["plan"]["created_at_utc"] = values["plan"]
    leg2["approval"]["approved_at_utc"] = values["approval"]
    leg2["apply"]["started_at_utc"] = values["apply_started"]
    leg2["apply"]["completed_at_utc"] = values["apply_completed"]
    leg2["ecs"]["verified_at_utc"] = values["ecs"]
    leg2["run_task_health"]["verified_at_utc"] = values["run_task"]
    leg2["dm_qa"]["verified_at_utc"] = values["dm_qa"]
    leg2["completed_at_utc"] = values["completed"]
    payload["safe_terminal_state"]["verified_at_utc"] = values["safe"]
    payload["control"]["completed_at_utc"] = values["control"]


def _make_failure_evidence(
    payload: dict[str, Any],
    *,
    status: str,
) -> None:
    payload["status"] = status
    payload["legs"][0]["dm_qa"]["result"] = "FAILED"
    payload["legs"][0]["result"] = "FAILED"
    payload["legs"][0]["recovery"]["result"] = "NOT_ATTEMPTED"
    _rehash(payload)


def test_complete_passed_aggregate_is_accepted_and_detached() -> None:
    payload = _complete_passed_aggregate()
    original = copy.deepcopy(payload)

    normalized = EVIDENCE.validate_forced_rollback_drill_evidence(
        payload,
        _expected_bindings(),
    )

    assert normalized == original
    assert normalized is not payload
    payload["status"] = "FAILED"
    assert normalized == original


@pytest.mark.parametrize(
    "mutation",
    [
        lambda legs: legs.pop(),
        lambda legs: legs.append(copy.deepcopy(legs[-1])),
        lambda legs: legs.reverse(),
    ],
    ids=["one-leg", "three-legs", "reversed-order"],
)
def test_passed_requires_exactly_two_legs_in_fixed_order(
    mutation: Callable[[list[dict[str, Any]]], None],
) -> None:
    payload = _complete_passed_aggregate()
    mutation(payload["legs"])
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError):
        _validate(payload)


@pytest.mark.parametrize(
    ("leg_index", "channel"),
    [(0, "active"), (1, "rollback")],
    ids=["rollback-leg-uses-active", "restore-leg-uses-rollback"],
)
def test_each_leg_channel_is_fixed(leg_index: int, channel: str) -> None:
    payload = _complete_passed_aggregate()
    payload["legs"][leg_index]["channel"] = channel
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError):
        _validate(payload)


@pytest.mark.parametrize("ordinal", [True, 1.0], ids=["boolean", "float"])
def test_leg_ordinal_requires_the_exact_integer_type(ordinal: Any) -> None:
    payload = _complete_passed_aggregate()
    payload["legs"][0]["ordinal"] = ordinal
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="ordinal"):
        _validate(payload)


def test_leg1_to_must_equal_leg2_from() -> None:
    payload = _complete_passed_aggregate()
    payload["legs"][0]["to"] = copy.deepcopy(payload["baseline"]["live_snapshot"]["snapshot"])
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError):
        _validate(payload)


def test_leg2_to_must_equal_baseline_initial_new() -> None:
    payload = _complete_passed_aggregate()
    payload["legs"][1]["to"] = copy.deepcopy(payload["legs"][0]["to"])
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError):
        _validate(payload)


def test_start_delay_over_thirty_minutes_is_rejected() -> None:
    payload = _complete_passed_aggregate()
    verified_at = "2026-07-24T11:35:59Z"
    payload["control"]["initial_release_verified_at_utc"] = verified_at
    expected = _expected_bindings()
    expected["initial_release_verified_at_utc"] = verified_at
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="max_start_delay"):
        _validate(payload, expected=expected)


def test_start_delay_anchor_must_match_the_initial_receipt() -> None:
    payload = _complete_passed_aggregate()
    payload["control"]["initial_release_verified_at_utc"] = "2026-07-24T11:36:00Z"
    expected = _expected_bindings()
    expected["initial_release_verified_at_utc"] = "2026-07-24T11:35:59Z"
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="trusted binding"):
        _validate(payload, expected=expected)


def test_previous_old_dwell_over_twenty_minutes_is_rejected() -> None:
    payload = _complete_passed_aggregate()
    _set_leg2_times(payload, start="over")
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="dwell"):
        _validate(payload)


def test_timestamp_regression_is_rejected() -> None:
    payload = _complete_passed_aggregate()
    payload["legs"][0]["completed_at_utc"] = "2026-07-24T12:05:30Z"
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="timestamp"):
        _validate(payload)


def test_exact_time_limits_are_accepted() -> None:
    payload = _complete_passed_aggregate()
    verified_at = "2026-07-24T11:36:00Z"
    payload["control"]["initial_release_verified_at_utc"] = verified_at
    expected = _expected_bindings()
    expected["initial_release_verified_at_utc"] = verified_at
    _set_leg2_times(payload, start="boundary")
    _rehash(payload)

    assert _validate(payload, expected=expected)["status"] == "PASSED"


def test_approval_id_cannot_be_reused_between_legs() -> None:
    payload = _complete_passed_aggregate()
    duplicate_id = payload["legs"][0]["approval"]["approval_id"]
    payload["legs"][1]["approval"]["approval_id"] = duplicate_id
    payload["actors"]["approvals"][1]["approval_id"] = duplicate_id
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="distinct"):
        _validate(payload)


def test_approval_must_bind_its_own_plan_sha() -> None:
    payload = _complete_passed_aggregate()
    payload["legs"][1]["approval"]["plan_sha256"] = payload["legs"][0]["plan"]["sha256"]
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="plan"):
        _validate(payload)


def test_approval_must_bind_the_drill_id() -> None:
    payload = _complete_passed_aggregate()
    payload["legs"][1]["approval"]["drill_id"] = "10000000-0000-4000-8000-000000000099"
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="drill_id"):
        _validate(payload)


LOCATOR_PATHS = [
    ("control", "initial_release_apply"),
    ("baseline", "live_snapshot", "locator"),
    ("legs", 0, "release_authorizations", 0, "locator"),
    ("legs", 0, "plan", "locator"),
    ("legs", 0, "approval", "locator"),
    ("legs", 0, "apply", "locator"),
    ("legs", 0, "ecs", "locator"),
    ("legs", 0, "run_task_health", "locator"),
    ("legs", 0, "dm_qa", "locator"),
    ("safe_terminal_state", "live_snapshot", "locator"),
]


@pytest.mark.parametrize("path", LOCATOR_PATHS)
def test_every_artifact_host_requires_compliance_object_lock(
    path: tuple[str | int, ...],
) -> None:
    payload = _complete_passed_aggregate()

    def use_governance(locator: dict[str, Any]) -> None:
        locator["object_lock_mode"] = "GOVERNANCE"

    _mutate_locator_and_manifest(payload, path, use_governance)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="COMPLIANCE"):
        _validate(payload)


def test_locator_requires_exact_version_id() -> None:
    payload = _complete_passed_aggregate()

    def delete_version(locator: dict[str, Any]) -> None:
        del locator["version_id"]

    _mutate_locator_and_manifest(
        payload,
        ("control", "initial_release_apply"),
        delete_version,
    )

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="missing"):
        _validate(payload)


def test_locator_rejects_unknown_key() -> None:
    payload = _complete_passed_aggregate()

    def add_unknown(locator: dict[str, Any]) -> None:
        locator["latest_version_fallback"] = True

    _mutate_locator_and_manifest(
        payload,
        ("control", "initial_release_apply"),
        add_unknown,
    )

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="unknown"):
        _validate(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda locator: locator["signature"].__setitem__("verified", False),
        lambda locator: locator["signer"].__setitem__("algorithm", "RSASSA_PKCS1_V1_5_SHA_256"),
        lambda locator: locator["exact_version_redownload"].__setitem__(
            "returned_version_id",
            "different-version",
        ),
        lambda locator: locator["exact_version_redownload"].__setitem__(
            "bytes_match",
            False,
        ),
    ],
    ids=[
        "signature-not-verified",
        "wrong-signing-algorithm",
        "different-returned-version",
        "redownload-bytes-differ",
    ],
)
def test_locator_requires_verified_signature_and_exact_redownload(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    payload = _complete_passed_aggregate()
    _mutate_locator_and_manifest(
        payload,
        ("legs", 0, "apply", "locator"),
        mutation,
    )

    with pytest.raises(EVIDENCE.DrillEvidenceError):
        _validate(payload)


def test_manifest_locator_is_validated_as_the_common_exact_schema() -> None:
    payload = _complete_passed_aggregate()
    payload["artifact_manifest"][0]["signature"]["verified"] = False
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="verified"):
        _validate(payload)


def test_integrity_immutable_object_uses_compliance_lock() -> None:
    payload = _complete_passed_aggregate()
    payload["integrity"]["immutable_object"]["object_lock_mode"] = "GOVERNANCE"

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="COMPLIANCE"):
        _validate(payload)


def test_integrity_immutable_object_is_the_canonical_body() -> None:
    payload = _complete_passed_aggregate()
    unrelated_sha = "e" * 64
    payload["integrity"]["immutable_object"]["sha256"] = unrelated_sha
    payload["integrity"]["immutable_object"]["exact_version_redownload"]["sha256"] = unrelated_sha

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="canonical body"):
        _validate(payload)


def test_attempted_recovery_artifact_uses_the_common_locator_schema() -> None:
    payload = _complete_passed_aggregate()
    _make_failure_evidence(payload, status="FAILED")
    recovery_locator = _locator("leg-1-recovery", 800)
    recovery = payload["legs"][0]["recovery"]
    recovery.update(
        {
            "attempted": True,
            "result": "FAILED",
            "completed_at_utc": "2026-07-24T12:10:30Z",
            "locator": recovery_locator,
        }
    )
    payload["artifact_manifest"].append(copy.deepcopy(recovery_locator))
    payload["artifact_manifest"].sort(key=_locator_identity)

    def use_governance(locator: dict[str, Any]) -> None:
        locator["object_lock_mode"] = "GOVERNANCE"

    _mutate_locator_and_manifest(
        payload,
        ("legs", 0, "recovery", "locator"),
        use_governance,
    )

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="COMPLIANCE"):
        _validate(payload)


def test_unapproved_third_digest_is_rejected() -> None:
    payload = _complete_passed_aggregate()
    snapshot = payload["legs"][0]["ecs"]["live_snapshot"]
    third_digest = f"sha256:{'9' * 64}"
    snapshot["subjects"][0]["digest"] = third_digest
    for resource in snapshot["resources"]:
        if resource["pipeline"] == "mcp" and resource["subject"] == "core":
            resource["digest"] = third_digest
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="target"):
        _validate(payload)


def test_scope_cannot_relabel_a_third_digest_as_initial_new() -> None:
    payload = _complete_passed_aggregate()
    declared_initial_new = payload["scope"]["subjects"][0]["initial_new_digest"]
    third_digest = f"sha256:{'8' * 64}"
    payload = _replace_text(payload, declared_initial_new, third_digest)
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="trusted registry"):
        _validate(payload)


def test_unapproved_third_task_revision_is_rejected() -> None:
    payload = _complete_passed_aggregate()
    task = payload["legs"][0]["run_task_health"]["tasks"][0]
    task["task_definition_arn"] = (
        "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-connect-web:99"
    )
    task["task_revision"] = 99
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="target"):
        _validate(payload)


def test_unapproved_third_resource_is_rejected() -> None:
    payload = _complete_passed_aggregate()
    third_resource = copy.deepcopy(payload["legs"][0]["apply"]["state"]["resources"][0])
    third_resource["consumer_id"] = "unapproved_consumer"
    third_resource["terraform_address"] = "aws_ecs_task_definition.unapproved_consumer"
    payload["legs"][0]["apply"]["state"]["resources"].append(third_resource)
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="resource"):
        _validate(payload)


def test_failed_leg_can_never_be_promoted_back_to_passed() -> None:
    payload = _complete_passed_aggregate()
    payload["legs"][0]["dm_qa"]["result"] = "FAILED"
    payload["legs"][0]["result"] = "FAILED"
    payload["legs"][0]["recovery"]["result"] = "NOT_ATTEMPTED"
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="never be PASSED"):
        _validate(payload)


@pytest.mark.parametrize("status", ["FAILED", "RECONCILE_REQUIRED"])
def test_failure_aggregates_retain_terminal_and_recovery_evidence(
    status: str,
) -> None:
    payload = _complete_passed_aggregate()
    _make_failure_evidence(payload, status=status)

    normalized = _validate(payload)

    assert normalized["status"] == status
    assert normalized["safe_terminal_state"]["classification"] == "INITIAL_NEW"
    assert normalized["legs"][0]["recovery"] == {
        "attempted": False,
        "result": "NOT_ATTEMPTED",
        "completed_at_utc": None,
        "last_exact_confirmed_digests": payload["legs"][0]["to"]["subjects"],
        "locator": None,
    }


def test_failure_aggregate_requires_safe_terminal_state() -> None:
    payload = _complete_passed_aggregate()
    _make_failure_evidence(payload, status="FAILED")
    del payload["safe_terminal_state"]
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="missing"):
        _validate(payload)


@pytest.mark.parametrize(
    "field",
    ["attempted", "result", "last_exact_confirmed_digests"],
)
def test_failure_recovery_requires_each_mandatory_fact(field: str) -> None:
    payload = _complete_passed_aggregate()
    _make_failure_evidence(payload, status="FAILED")
    del payload["legs"][0]["recovery"][field]
    _rehash(payload)

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="missing"):
        _validate(payload)


def test_canonical_sha256_is_deterministic_across_object_key_order() -> None:
    payload = _complete_passed_aggregate()
    reordered = _reverse_object_key_order(payload)

    assert EVIDENCE.canonical_drill_body_bytes(payload).endswith(b"\n")
    assert not EVIDENCE.canonical_drill_body_bytes(payload).endswith(b"\n\n")
    assert EVIDENCE.canonical_json_bytes is RELEASE_APPROVAL.canonical_json_bytes
    assert EVIDENCE.canonical_drill_body_bytes(reordered) == EVIDENCE.canonical_drill_body_bytes(
        payload
    )
    assert EVIDENCE.canonical_drill_sha256(reordered) == payload["integrity"]["canonical_sha256"]
    assert _validate(reordered) == payload


def test_canonical_sha256_must_bind_the_body() -> None:
    payload = _complete_passed_aggregate()
    payload["integrity"]["canonical_sha256"] = "f" * 64

    with pytest.raises(EVIDENCE.DrillEvidenceError, match="canonical_sha256"):
        _validate(payload)
