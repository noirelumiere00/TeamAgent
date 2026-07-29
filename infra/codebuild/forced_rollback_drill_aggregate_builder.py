#!/usr/bin/env python3
"""Build forced-rollback aggregate inputs from controller-bound receipts only."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

ACCOUNT_ID = "718959508629"
REGION = "ap-northeast-1"
TRUSTED_AUTOMATION_ARN = (
    f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/"
    "teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class AggregateBuildError(ValueError):
    """A controller artifact is absent or differs from its recorded binding."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _load(path: Path, *, label: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AggregateBuildError(f"{label} is not readable JSON") from exc
    if type(value) is not dict:
        raise AggregateBuildError(f"{label} must be an object")
    return value


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _bound_path(value: Any, expected_sha256: Any, *, label: str) -> Path:
    if type(value) is not str or not value:
        raise AggregateBuildError(f"{label} path is missing")
    if type(expected_sha256) is not str or not _SHA256_RE.fullmatch(expected_sha256):
        raise AggregateBuildError(f"{label} SHA-256 is invalid")
    path = Path(value)
    if not path.is_file() or path.is_symlink():
        raise AggregateBuildError(f"{label} must be a regular non-symlink file")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AggregateBuildError(f"{label} cannot be read") from exc
    if not payload or _sha256_bytes(payload) != expected_sha256:
        raise AggregateBuildError(f"{label} differs from its controller binding")
    return path


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)


def _utc(epoch: Any, *, label: str) -> str:
    if type(epoch) is not int or epoch < 0:
        raise AggregateBuildError(f"{label} must be an epoch second")
    return (
        dt.datetime.fromtimestamp(epoch, tz=dt.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _leg_state(state: dict[str, Any], key: str) -> dict[str, Any]:
    legs = state.get("legs")
    if type(legs) is not dict or type(legs.get(key)) is not dict:
        raise AggregateBuildError(f"state leg {key} is missing")
    return legs[key]


def _validate_complete_failure_history(state: dict[str, Any]) -> None:
    failures = state.get("failures")
    if type(failures) is not list:
        raise AggregateBuildError("controller failure history is malformed")
    if not failures:
        return
    restore = _leg_state(state, "restore_active")
    apply_state = restore.get("apply")
    if (
        state.get("state") != "RECOVERY_REQUIRED"
        or type(apply_state) is not dict
        or type(apply_state.get("completed_at_epoch")) is not int
    ):
        raise AggregateBuildError(
            "complete drill failure history is not reachable from the controller state machine"
        )
    completed_at = apply_state["completed_at_epoch"]
    for failure in failures:
        if (
            type(failure) is not dict
            or failure.get("leg") != "restore-active"
            or failure.get("phase") != "finalize"
            or type(failure.get("at_epoch")) is not int
            or failure["at_epoch"] < completed_at
        ):
            raise AggregateBuildError(
                "complete drill failure history is not a source-backed finalize failure"
            )


def _artifact_entries(
    *,
    state: dict[str, Any],
    contract: dict[str, Any],
    initial_receipt_path: Path,
    terminal_snapshot_path: Path,
    artifact_directory: Path,
    trusted_scope: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Path]]:
    _validate_complete_failure_history(state)
    control = contract.get("control")
    if type(control) is not dict:
        raise AggregateBuildError("contract.control is missing")
    initial_locator = control.get("initial_release_apply_locator")
    if (
        type(initial_locator) is not dict
        or type(initial_locator.get("sha256")) is not str
        or not _SHA256_RE.fullmatch(initial_locator["sha256"])
    ):
        raise AggregateBuildError("initial release locator SHA-256 is missing")
    initial_path = _bound_path(
        str(initial_receipt_path),
        initial_locator["sha256"],
        label="initial release apply receipt",
    )
    if not terminal_snapshot_path.is_file() or terminal_snapshot_path.is_symlink():
        raise AggregateBuildError("terminal snapshot is missing")
    terminal_payload = terminal_snapshot_path.read_bytes()
    if not terminal_payload:
        raise AggregateBuildError("terminal snapshot is empty")

    artifact_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    entries: list[dict[str, str]] = []
    paths: dict[str, Path] = {}
    evidence_base = f"forced-rollback-drills/{state['drill_id']}"

    def add(name: str, path: Path, key: str) -> None:
        entries.append(
            {
                "name": name,
                "path": str(path),
                "key": f"{evidence_base}/{key}",
                "content_type": "application/json",
            }
        )
        paths[name] = path

    add("baseline", initial_path, "baseline/initial-release.apply.json")
    for key, ordinal in (
        ("rollback_to_previous", 1),
        ("restore_active", 2),
    ):
        leg = _leg_state(state, key)
        authorization_state = leg.get("authorization")
        plan_state = leg.get("plan")
        approval_state = leg.get("approval")
        apply_state = leg.get("apply")
        if not all(
            type(value) is dict
            for value in (
                authorization_state,
                plan_state,
                approval_state,
                apply_state,
            )
        ):
            raise AggregateBuildError(f"state leg {key} is incomplete")
        authorization_path = _bound_path(
            authorization_state.get("path"),
            authorization_state.get("sha256"),
            label=f"{key} authorization receipt",
        )
        authorization = _load(
            authorization_path,
            label=f"{key} authorization receipt",
        )
        plan_receipt_path = _bound_path(
            plan_state.get("receipt_path"),
            plan_state.get("receipt_sha256"),
            label=f"{key} plan receipt",
        )
        apply_receipt_path = _bound_path(
            apply_state.get("path"),
            apply_state.get("sha256"),
            label=f"{key} apply receipt",
        )
        automation_identity_path = _bound_path(
            apply_state.get("automation_identity_path"),
            apply_state.get("automation_identity_sha256"),
            label=f"{key} automation identity receipt",
        )
        apply_receipt = _load(
            apply_receipt_path,
            label=f"{key} apply receipt",
        )
        target_snapshot = _projected_snapshot(
            apply_receipt.get("post_live_contract"),
            apply_receipt.get("post_state_contract"),
            trusted_scope,
        )
        release_approval = authorization.get("release_approval")
        if type(release_approval) is not dict:
            raise AggregateBuildError(f"{key} authorization has no verified release approval")
        approval_payload = _canonical_bytes(
            {
                "plan_confirmation": approval_state,
                "release_approval": release_approval,
            }
        )
        approval_path = artifact_directory / f"leg-{ordinal}-approval.json"
        _write_exclusive(approval_path, approval_payload)
        authorization_payload = _canonical_bytes(
            {
                "authorization_id": authorization["authorization_id"],
                "channel": authorization["channel"],
                "deployment_intent_id": plan_state["intent_id"],
                "drill_id": authorization["drill_id"],
                "gate_var_sha256": authorization["gate_var_sha256"],
                "issued_at_epoch": authorization["issued_at_epoch"],
                "pipeline": authorization["pipeline"],
                "receipt_key": authorization["receipt_key"],
                "receipt_signature_key": authorization["receipt_signature_key"],
                "receipt_signature_version_id": authorization["receipt_signature_version_id"],
                "receipt_version_id": authorization["receipt_version_id"],
                "release_approval": release_approval,
                "subjects": target_snapshot["subjects"],
            }
        )
        authorization_evidence_path = artifact_directory / f"leg-{ordinal}-authorization.json"
        _write_exclusive(authorization_evidence_path, authorization_payload)
        prefix = f"legs/{ordinal}"
        add(
            f"leg{ordinal}_authorization",
            authorization_evidence_path,
            f"{prefix}/release-authorization.json",
        )
        add(
            f"leg{ordinal}_plan",
            plan_receipt_path,
            f"{prefix}/plan.runtime-guard.json",
        )
        add(
            f"leg{ordinal}_approval",
            approval_path,
            f"{prefix}/approval.json",
        )
        add(
            f"leg{ordinal}_apply",
            apply_receipt_path,
            f"{prefix}/apply.runtime-guard.json",
        )
        add(
            f"leg{ordinal}_automation_identity",
            automation_identity_path,
            f"{prefix}/automation-identity.json",
        )
    add(
        "terminal",
        terminal_snapshot_path,
        "safe-terminal/final-live.snapshot.json",
    )
    return entries, paths


def prepare(args: argparse.Namespace) -> None:
    state = _load(args.state, label="controller state")
    contract = _load(args.contract, label="drill contract")
    trusted_scope = _load(args.trusted_scope, label="trusted scope")
    entries, _ = _artifact_entries(
        state=state,
        contract=contract,
        initial_receipt_path=args.initial_receipt,
        terminal_snapshot_path=args.terminal_snapshot,
        artifact_directory=args.artifact_directory,
        trusted_scope=trusted_scope,
    )
    drill_id = state.get("drill_id")
    if type(drill_id) is not str:
        raise AggregateBuildError("state drill_id is missing")
    _write_exclusive(
        args.out,
        _canonical_bytes({"drill_id": drill_id, "artifacts": entries}),
    )


def _projected_snapshot(
    live_contract: Any,
    state_contract: Any,
    trusted_scope: dict[str, Any],
) -> dict[str, Any]:
    if type(live_contract) is not dict or type(state_contract) is not dict:
        raise AggregateBuildError("runtime receipt snapshot contracts are missing")
    raw_resources = live_contract.get("resources")
    revisions = state_contract.get("task_revisions")
    if type(raw_resources) is not list or type(revisions) is not dict:
        raise AggregateBuildError("runtime receipt resource contracts are missing")
    live_by_id = {
        resource.get("consumer_id"): resource
        for resource in raw_resources
        if type(resource) is dict and type(resource.get("consumer_id")) is str
    }
    resources: list[dict[str, Any]] = []
    for scoped in trusted_scope["resources"]:
        observed = live_by_id.get(scoped["consumer_id"])
        if type(observed) is not dict:
            raise AggregateBuildError(f"runtime receipt lacks consumer {scoped['consumer_id']}")
        image = observed.get("image")
        if type(image) is not str or "@" not in image:
            raise AggregateBuildError("runtime receipt image is not digest pinned")
        resources.append(
            {
                "consumer_id": scoped["consumer_id"],
                "terraform_address": scoped["terraform_address"],
                "pipeline": scoped["pipeline"],
                "subject": scoped["subject"],
                "task_definition_arn": observed.get("task_definition_arn"),
                "task_revision": revisions.get(scoped["consumer_id"]),
                "digest": image.rsplit("@", 1)[1],
            }
        )
    subjects: list[dict[str, Any]] = []
    for scoped in trusted_scope["subjects"]:
        matching = {
            resource["digest"]
            for resource in resources
            if resource["pipeline"] == scoped["pipeline"] and resource["subject"] == scoped["name"]
        }
        if len(matching) != 1:
            raise AggregateBuildError("runtime subject digest is not unique")
        subjects.append(
            {
                "pipeline": scoped["pipeline"],
                "name": scoped["name"],
                "release_repository": scoped["release_repository"],
                "digest": matching.pop(),
            }
        )
    return {"subjects": subjects, "resources": resources}


def _automation_principal(identity: dict[str, Any]) -> dict[str, Any]:
    if (
        identity.get("Account") != ACCOUNT_ID
        or type(identity.get("Arn")) is not str
        or identity.get("Arn") != TRUSTED_AUTOMATION_ARN
        or type(identity.get("UserId")) is not str
        or not identity["UserId"]
    ):
        raise AggregateBuildError("guard automation identity is incomplete")
    return {
        "account_id": identity["Account"],
        "arn": identity["Arn"],
        "user_id": identity["UserId"],
    }


def _unique_locators(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    for locator in values:
        if type(locator) is not dict:
            raise AggregateBuildError("artifact locator is missing")
        identity = (
            locator.get("bucket"),
            locator.get("key"),
            locator.get("version_id"),
        )
        if not all(type(value) is str and value for value in identity):
            raise AggregateBuildError("artifact locator identity is incomplete")
        previous = by_identity.get(identity)
        if previous is not None and previous != locator:
            raise AggregateBuildError("artifact locator identity is conflicting")
        by_identity[identity] = locator
    return [by_identity[key] for key in sorted(by_identity)]


def build(args: argparse.Namespace) -> None:
    state = _load(args.state, label="controller state")
    _validate_complete_failure_history(state)
    contract = _load(args.contract, label="drill contract")
    initial_receipt = _load(args.initial_receipt, label="initial apply receipt")
    trusted_scope = _load(args.trusted_scope, label="trusted scope")
    terminal_observation = _load(args.terminal_snapshot, label="terminal snapshot")
    locators = _load(args.locators, label="artifact locators")
    expected_locator_names = {
        "baseline",
        "leg1_apply",
        "leg1_automation_identity",
        "leg1_approval",
        "leg1_authorization",
        "leg1_plan",
        "leg2_apply",
        "leg2_automation_identity",
        "leg2_approval",
        "leg2_authorization",
        "leg2_plan",
        "terminal",
    }
    if set(locators) != expected_locator_names:
        raise AggregateBuildError("artifact locator set is incomplete or unknown")
    if _sha256_bytes(args.terminal_snapshot.read_bytes()) != locators["terminal"]["sha256"]:
        raise AggregateBuildError("persisted terminal observation bytes differ from fresh snapshot")

    initial_locator = contract["control"]["initial_release_apply_locator"]
    initial_path = _bound_path(
        str(args.initial_receipt),
        initial_locator.get("sha256"),
        label="initial apply receipt",
    )
    if _sha256_bytes(initial_path.read_bytes()) != locators["baseline"]["sha256"]:
        raise AggregateBuildError("persisted baseline bytes differ from initial receipt")

    baseline_snapshot = _projected_snapshot(
        initial_receipt.get("post_live_contract"),
        initial_receipt.get("post_state_contract"),
        trusted_scope,
    )

    def build_leg(
        state_key: str,
        ordinal: int,
        name: str,
        channel: str,
    ) -> dict[str, Any]:
        leg = _leg_state(state, state_key)
        authorization_state = leg["authorization"]
        plan_state = leg["plan"]
        approval_state = leg["approval"]
        apply_state = leg["apply"]
        authorization_path = _bound_path(
            authorization_state["path"],
            authorization_state["sha256"],
            label=f"{state_key} authorization receipt",
        )
        authorization_receipt = _load(
            authorization_path,
            label=f"{state_key} authorization receipt",
        )
        plan_receipt_path = _bound_path(
            plan_state["receipt_path"],
            plan_state["receipt_sha256"],
            label=f"{state_key} plan receipt",
        )
        apply_receipt_path = _bound_path(
            apply_state["path"],
            apply_state["sha256"],
            label=f"{state_key} apply receipt",
        )
        automation_path = _bound_path(
            apply_state["automation_identity_path"],
            apply_state["automation_identity_sha256"],
            label=f"{state_key} automation identity receipt",
        )
        apply_receipt = _load(
            apply_receipt_path,
            label=f"{state_key} apply receipt",
        )
        automation_identity = _load(
            automation_path,
            label=f"{state_key} automation identity receipt",
        )
        if (
            _sha256_bytes(plan_receipt_path.read_bytes())
            != locators[f"leg{ordinal}_plan"]["sha256"]
        ):
            raise AggregateBuildError("persisted plan receipt bytes differ")
        if (
            _sha256_bytes(apply_receipt_path.read_bytes())
            != locators[f"leg{ordinal}_apply"]["sha256"]
        ):
            raise AggregateBuildError("persisted apply receipt bytes differ")
        source = _projected_snapshot(
            apply_receipt.get("pre_live_contract"),
            apply_receipt.get("pre_state_contract"),
            trusted_scope,
        )
        target = _projected_snapshot(
            apply_receipt.get("post_live_contract"),
            apply_receipt.get("post_state_contract"),
            trusted_scope,
        )
        release_approval = authorization_receipt.get("release_approval")
        if type(release_approval) is not dict:
            raise AggregateBuildError("verified release approval is missing")
        expected_authorization_payload = _canonical_bytes(
            {
                "authorization_id": authorization_receipt["authorization_id"],
                "channel": authorization_receipt["channel"],
                "deployment_intent_id": plan_state["intent_id"],
                "drill_id": authorization_receipt["drill_id"],
                "gate_var_sha256": authorization_receipt["gate_var_sha256"],
                "issued_at_epoch": authorization_receipt["issued_at_epoch"],
                "pipeline": authorization_receipt["pipeline"],
                "receipt_key": authorization_receipt["receipt_key"],
                "receipt_signature_key": authorization_receipt["receipt_signature_key"],
                "receipt_signature_version_id": authorization_receipt[
                    "receipt_signature_version_id"
                ],
                "receipt_version_id": authorization_receipt["receipt_version_id"],
                "release_approval": release_approval,
                "subjects": target["subjects"],
            }
        )
        if (
            _sha256_bytes(expected_authorization_payload)
            != locators[f"leg{ordinal}_authorization"]["sha256"]
        ):
            raise AggregateBuildError("persisted authorization evidence differs from its sources")
        expected_approval_payload = _canonical_bytes(
            {
                "plan_confirmation": approval_state,
                "release_approval": release_approval,
            }
        )
        if _sha256_bytes(expected_approval_payload) != locators[f"leg{ordinal}_approval"]["sha256"]:
            raise AggregateBuildError("persisted approval evidence differs from its sources")
        if (
            _sha256_bytes(automation_path.read_bytes())
            != locators[f"leg{ordinal}_automation_identity"]["sha256"]
        ):
            raise AggregateBuildError("persisted automation identity differs from guard receipt")
        post_state = apply_receipt.get("post_state_contract", {}).get("state")
        pre_state = apply_receipt.get("pre_state_contract", {}).get("state")
        if type(post_state) is not dict or type(pre_state) is not dict:
            raise AggregateBuildError("apply state contracts are missing")
        apply_locator = locators[f"leg{ordinal}_apply"]
        service_probe = apply_receipt.get("post_apply_service_probe")
        dm_qa_receipt = apply_receipt.get("openclaw_rollout_result", {}).get("dmQa")
        ecs_receipt = apply_receipt.get("ecs_service_saga_verification_receipt")
        if not all(type(value) is dict for value in (service_probe, dm_qa_receipt, ecs_receipt)):
            raise AggregateBuildError("apply proof receipts are incomplete")
        finalization_receipt = apply_receipt.get("deployment_finalization_receipt")
        service_task = service_probe.get("task")
        service_result = service_probe.get("result")
        service_checks = service_result.get("checks") if type(service_result) is dict else None
        if (
            apply_receipt.get("status") != "applied"
            or type(finalization_receipt) is not dict
            or finalization_receipt.get("state") != "APPLIED"
            or ecs_receipt.get("stage") != "VERIFIED_APPLIED"
            or type(service_task) is not dict
            or service_task.get("exit_code") != 0
            or type(service_checks) is not dict
            or not service_checks
            or not all(value is True for value in service_checks.values())
            or dm_qa_receipt.get("result") != "PASSED"
        ):
            raise AggregateBuildError(
                "registered apply receipt does not prove a completed successful leg"
            )
        # A terminal observation may fail after both guarded applies completed.
        # Preserve the receipt facts here; terminal drift belongs to the drill
        # status/recovery record and must not rewrite successful proof results.
        result = "PASSED"
        approval = {
            "confirmation_id": approval_state["approval_id"],
            "drill_id": approval_state["drill_id"],
            "action": approval_state["action"],
            "plan_sha256": approval_state["plan_sha256"],
            "approval_text_sha256": approval_state["approval_text_sha256"],
            "consumed_at_utc": _utc(
                approval_state["consumed_at_epoch"],
                label=f"{state_key} confirmation time",
            ),
            "release_approval": copy.deepcopy(release_approval),
            "receipt_sha256": locators[f"leg{ordinal}_approval"]["sha256"],
            "locator": locators[f"leg{ordinal}_approval"],
        }
        return {
            "ordinal": ordinal,
            "name": name,
            "channel": channel,
            "from": source,
            "to": target,
            "release_authorizations": [
                {
                    "authorization_id": authorization_receipt["authorization_id"],
                    "deployment_intent_id": plan_state["intent_id"],
                    "drill_id": authorization_receipt["drill_id"],
                    "pipeline": authorization_receipt["pipeline"],
                    "channel": authorization_receipt["channel"],
                    "subjects": copy.deepcopy(target["subjects"]),
                    "issued_at_utc": _utc(
                        authorization_receipt["issued_at_epoch"],
                        label=f"{state_key} authorization time",
                    ),
                    "release_approval_id": release_approval["approval_id"],
                    "release_approved_by_arn": release_approval["approved_by"],
                    "receipt_sha256": locators[f"leg{ordinal}_authorization"]["sha256"],
                    "locator": locators[f"leg{ordinal}_authorization"],
                }
            ],
            "plan": {
                "sha256": plan_state["sha256"],
                "receipt_sha256": plan_state["receipt_sha256"],
                "created_at_utc": _utc(
                    plan_state["created_at_epoch"],
                    label=f"{state_key} plan time",
                ),
                "terraform_lineage": plan_state["terraform_lineage"],
                "terraform_serial": plan_state["state_serial"],
                "from": copy.deepcopy(source),
                "to": copy.deepcopy(target),
                "changed_resources": [
                    resource["terraform_address"] for resource in trusted_scope["resources"]
                ],
                "locator": locators[f"leg{ordinal}_plan"],
            },
            "approval": approval,
            "apply": {
                "apply_attempt_id": apply_state["apply_attempt_id"],
                "plan_sha256": apply_state["plan_sha256"],
                "receipt_sha256": apply_state["sha256"],
                "started_at_utc": _utc(
                    apply_state["started_at_epoch"],
                    label=f"{state_key} apply start",
                ),
                "completed_at_utc": _utc(
                    apply_state["completed_at_epoch"],
                    label=f"{state_key} apply completion",
                ),
                "result": result,
                "terraform_lineage": apply_state["terraform_lineage"],
                "terraform_serial_before": pre_state["serial"],
                "terraform_serial_after": post_state["serial"],
                "state": copy.deepcopy(target),
                "automation_principal": _automation_principal(automation_identity),
                "automation_identity_sha256": locators[f"leg{ordinal}_automation_identity"][
                    "sha256"
                ],
                "automation_identity_locator": locators[f"leg{ordinal}_automation_identity"],
                "locator": apply_locator,
            },
            "ecs": {
                "result": result,
                "steady": ecs_receipt.get("stage") == "VERIFIED_APPLIED",
                "verified_at_utc": _utc(
                    apply_state["completed_at_epoch"],
                    label=f"{state_key} ECS receipt admission",
                ),
                "live_snapshot": copy.deepcopy(target),
                "locator": apply_locator,
            },
            "run_task_health": {
                "result": result,
                "verified_at_utc": service_probe["verified_at_utc"],
                "apply_attempt_id": service_probe["apply_attempt_id"],
                "task_definition_arn": service_probe["task_definition"],
                "image": service_probe["image"],
                "log_stream_name": service_probe["log_stream_name"],
                "task": service_probe["task"],
                "checks": service_probe["result"]["checks"],
                "locator": apply_locator,
            },
            "dm_qa": {
                "result": dm_qa_receipt["result"],
                "verified_at_utc": dm_qa_receipt["verified_at_utc"],
                "apply_attempt_id": dm_qa_receipt["applyAttemptId"],
                "mcp_task_definition_arn": dm_qa_receipt["mcpTaskDefinitionArn"],
                "openclaw_task_definition_arn": dm_qa_receipt["openclawTaskDefinitionArn"],
                "locator": dm_qa_receipt["locator"],
            },
            "started_at_utc": _utc(
                authorization_receipt["issued_at_epoch"],
                label=f"{state_key} leg start",
            ),
            "completed_at_utc": _utc(
                apply_state["completed_at_epoch"],
                label=f"{state_key} leg completion",
            ),
            "result": result,
            "recovery": {
                "attempted": False,
                "result": "NOT_REQUIRED",
                "completed_at_utc": None,
                "last_exact_confirmed_digests": copy.deepcopy(target["subjects"]),
                "locator": None,
            },
        }

    legs = [
        build_leg(
            "rollback_to_previous",
            1,
            "rollback_to_previous",
            "rollback",
        ),
        build_leg("restore_active", 2, "restore_active", "active"),
    ]
    observed_at_epoch = terminal_observation.get("observed_at_epoch")
    if type(observed_at_epoch) is not int:
        raise AggregateBuildError("terminal observation time is missing")
    if (
        state["legs"]["restore_active"]["apply"]["post_target_sha256"]
        != state["target_sha256"]["new"]
    ):
        raise AggregateBuildError("terminal restore target is not initial new")
    terminal_resources = terminal_observation.get("resources")
    if type(terminal_resources) is not list:
        raise AggregateBuildError("terminal scoped resources are missing")
    terminal_revisions: dict[str, int] = {}
    for resource in terminal_resources:
        if type(resource) is not dict:
            raise AggregateBuildError("terminal scoped resource is malformed")
        task_definition_arn = resource.get("task_definition_arn")
        if type(task_definition_arn) is not str or ":" not in task_definition_arn:
            raise AggregateBuildError("terminal task definition ARN is missing")
        try:
            revision = int(task_definition_arn.rsplit(":", 1)[1])
        except ValueError as exc:
            raise AggregateBuildError("terminal task definition revision is invalid") from exc
        terminal_revisions[resource["consumer_id"]] = revision
    terminal_snapshot = _projected_snapshot(
        {"resources": terminal_resources},
        {"task_revisions": terminal_revisions},
        trusted_scope,
    )
    classification = terminal_observation.get("classification")
    if classification not in {
        "INITIAL_NEW",
        "PREVIOUS_OLD",
        "UNKNOWN",
    }:
        raise AggregateBuildError("terminal observation classification is missing or unsupported")
    expected_terminal = {
        "INITIAL_NEW": baseline_snapshot,
        "PREVIOUS_OLD": legs[0]["to"],
    }.get(classification)
    if expected_terminal is not None and terminal_snapshot != expected_terminal:
        raise AggregateBuildError("fresh terminal snapshot differs from its exact classification")
    if args.status == "PASSED" and classification != "INITIAL_NEW":
        raise AggregateBuildError("PASSED aggregate terminal observation is not initial new")
    if args.status != "PASSED":
        failures = state.get("failures")
        if type(failures) is not list or not failures:
            raise AggregateBuildError("non-passed aggregate has no recorded controller failure")
        affected_legs = {
            failure.get("leg")
            for failure in failures
            if type(failure) is dict
            and failure.get("leg")
            in {
                "rollback-to-previous",
                "restore-active",
            }
        }
        if not affected_legs:
            raise AggregateBuildError("non-passed aggregate failure does not identify a drill leg")
        for index, failure_leg in enumerate(("rollback-to-previous", "restore-active")):
            if failure_leg not in affected_legs:
                continue
            legs[index]["recovery"] = {
                "attempted": False,
                "result": "NOT_ATTEMPTED",
                "completed_at_utc": None,
                "last_exact_confirmed_digests": copy.deepcopy(terminal_snapshot["subjects"]),
                "locator": None,
            }
    automation_principals = sorted(
        {
            (
                item["apply"]["automation_principal"]["account_id"],
                item["apply"]["automation_principal"]["arn"],
                item["apply"]["automation_principal"]["user_id"],
            ): item["apply"]["automation_principal"]
            for item in legs
        }.values(),
        key=lambda item: (
            item["account_id"],
            item["arn"],
            item["user_id"],
        ),
    )
    actor_approvals = [
        {
            "approval_id": leg["approval"]["release_approval"]["approval_id"],
            "approved_by_arn": leg["approval"]["release_approval"]["approved_by"],
        }
        for leg in legs
    ]
    initiating_principal = copy.deepcopy(contract["actors"]["initiating_principal"])
    if initiating_principal.get("source_identity") == "":
        initiating_principal["source_identity"] = None
    referenced_locators = [
        initial_locator,
        *locators.values(),
        *(leg["dm_qa"]["locator"] for leg in legs),
    ]
    aggregate = {
        "schema_version": 1,
        "kind": "teamagent.forced-rollback-drill",
        "drill_id": state["drill_id"],
        "status": args.status,
        "environment": {
            "account_id": ACCOUNT_ID,
            "region": REGION,
            "name": "dev",
        },
        "control": {
            "git_commit": state["git_commit"],
            "drill_contract_sha256": state["contract_sha256"],
            "initial_release_apply": initial_locator,
            "initial_release_verified_at_utc": _utc(
                state["initial_release_verified_at_epoch"],
                label="initial release verification",
            ),
            "started_at_utc": legs[0]["started_at_utc"],
            "completed_at_utc": _utc(
                observed_at_epoch,
                label="terminal observation",
            ),
            "max_start_delay_seconds": 1800,
            "max_old_dwell_seconds": 1200,
        },
        "actors": {
            "initiating_principal": initiating_principal,
            "automation_principals": automation_principals,
            "approvals": actor_approvals,
        },
        "scope": trusted_scope,
        "baseline": {
            "terraform_lineage": state["initial_state"]["lineage"],
            "terraform_serial": state["initial_state"]["serial"],
            "live_snapshot": {
                "snapshot": baseline_snapshot,
                "locator": locators["baseline"],
            },
            "initial_new_verified": True,
        },
        "legs": legs,
        "safe_terminal_state": {
            "classification": classification,
            "steady": args.status == "PASSED",
            "verified_at_utc": _utc(
                observed_at_epoch,
                label="terminal observation",
            ),
            "live_snapshot": {
                "snapshot": terminal_snapshot,
                "locator": locators["terminal"],
            },
        },
        "artifact_manifest": _unique_locators(referenced_locators),
        "integrity": {
            "canonical_sha256": "",
            "kms_key_arn": "",
            "signing_algorithm": "RSASSA_PSS_SHA_256",
            "signature": {},
            "immutable_object": {},
        },
    }
    _write_exclusive(args.out, _canonical_bytes(aggregate))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--state", type=Path, required=True)
    prepare_parser.add_argument("--contract", type=Path, required=True)
    prepare_parser.add_argument("--initial-receipt", type=Path, required=True)
    prepare_parser.add_argument("--trusted-scope", type=Path, required=True)
    prepare_parser.add_argument("--terminal-snapshot", type=Path, required=True)
    prepare_parser.add_argument("--artifact-directory", type=Path, required=True)
    prepare_parser.add_argument("--out", type=Path, required=True)
    prepare_parser.set_defaults(handler=prepare)

    build_parser = commands.add_parser("build")
    build_parser.add_argument(
        "--status",
        choices=("PASSED", "FAILED", "RECONCILE_REQUIRED"),
        required=True,
    )
    build_parser.add_argument("--state", type=Path, required=True)
    build_parser.add_argument("--contract", type=Path, required=True)
    build_parser.add_argument("--initial-receipt", type=Path, required=True)
    build_parser.add_argument("--trusted-scope", type=Path, required=True)
    build_parser.add_argument("--terminal-snapshot", type=Path, required=True)
    build_parser.add_argument("--locators", type=Path, required=True)
    build_parser.add_argument("--out", type=Path, required=True)
    build_parser.set_defaults(handler=build)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        args.handler(args)
    except (AggregateBuildError, KeyError, OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
