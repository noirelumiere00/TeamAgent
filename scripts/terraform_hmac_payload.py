#!/usr/bin/env python3
"""Extract exact ECS registration artifacts from one immutable Terraform saved plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from scripts.hmac_rollout_gate import RolloutGateError

TASK_ADDRESSES = {
    "mcp": "aws_ecs_task_definition.mcp",
    "connect_web": "aws_ecs_task_definition.connect_web[0]",
    "morning_digest": "aws_ecs_task_definition.morning_digest[0]",
}
WORKER_DEPLOY_ADDRESS = "terraform_data.hmac_worker_deploy[0]"
PRODUCTION_GATE_ADDRESS = "terraform_data.production_image_release_gate"
MORNING_PROMOTION_ADDRESS = "terraform_data.hmac_morning_digest_target_transaction[0]"
SERVICE_ADDRESSES = {
    "mcp": "aws_ecs_service.mcp[0]",
    "connect_web": "aws_ecs_service.connect_web[0]",
}
SERVICE_PROMOTION_ADDRESSES = {
    "mcp": (
        "terraform_data.hmac_mcp_pre_update[0]",
        "terraform_data.hmac_mcp_post_update[0]",
    ),
    "connect_web": (
        "terraform_data.hmac_connect_web_pre_update[0]",
        "terraform_data.hmac_connect_web_post_update[0]",
    ),
}
WORKER_ARTIFACT_BINDING_KEYS = frozenset(
    {
        "base_environment",
        "candidate_artifact",
        "candidate_env",
        "candidate_receipt",
        "candidate_signature",
        "deploy_overrides",
        "deploy_script",
        "provenance_verifier",
        "reviewed_manifest",
        "rollback_artifact",
        "rollback_env",
        "rollback_receipt",
        "rollback_signature",
        "rollout_control",
    }
)
_TASK_TERRAFORM_KEYS = frozenset(
    {
        "arn",
        "arn_without_revision",
        "container_definitions",
        "cpu",
        "enable_fault_injection",
        "ephemeral_storage",
        "execution_role_arn",
        "family",
        "id",
        "inference_accelerator",
        "ipc_mode",
        "memory",
        "network_mode",
        "pid_mode",
        "placement_constraints",
        "proxy_configuration",
        "requires_compatibilities",
        "revision",
        "runtime_platform",
        "skip_destroy",
        "tags",
        "tags_all",
        "task_role_arn",
        "track_latest",
        "volume",
    }
)
_TASK_TERRAFORM_REQUIRED_KEYS = frozenset(
    {
        "container_definitions",
        "cpu",
        "execution_role_arn",
        "family",
        "memory",
        "network_mode",
        "requires_compatibilities",
        "runtime_platform",
        "task_role_arn",
        "volume",
    }
)
_REGISTER_ACTIONS = frozenset(
    {
        ("create",),
        ("update",),
        ("delete", "create"),
        ("create", "delete"),
    }
)


def saved_plan_sha256(path: Path) -> str:
    """Hash the complete opaque saved plan without loading it all into memory."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RolloutGateError("terraform_plan_unreadable") from exc
    return digest.hexdigest()


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _terraform_value(
    value: object,
    *,
    preserve_mapping_fields: frozenset[str] = frozenset(),
) -> object:
    if type(value) is dict:
        converted: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise RolloutGateError("terraform_plan_task_invalid")
            if key in preserve_mapping_fields:
                if type(item) is not dict or any(
                    type(map_key) is not str or type(map_value) is not str
                    for map_key, map_value in item.items()
                ):
                    raise RolloutGateError("terraform_plan_task_invalid")
                converted[_camel(key)] = dict(item)
            else:
                converted[_camel(key)] = _terraform_value(
                    item,
                    preserve_mapping_fields=preserve_mapping_fields,
                )
        return converted
    if type(value) is list:
        return [
            _terraform_value(
                item,
                preserve_mapping_fields=preserve_mapping_fields,
            )
            for item in value
        ]
    return value


def _one_block(value: object, *, label: str) -> dict[str, object] | None:
    if value in (None, []):
        return None
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise RolloutGateError("terraform_plan_task_invalid", scope=label)
    return dict(value[0])


def _present(mapping: dict[str, Any], name: str) -> bool:
    return name in mapping and mapping[name] is not None


def _volume_payload(value: object, *, task: str) -> list[dict[str, object]]:
    if value in (None, []):
        return []
    if type(value) is not list:
        raise RolloutGateError("terraform_plan_task_invalid", scope=task)
    volumes: list[dict[str, object]] = []
    for raw_volume in value:
        if type(raw_volume) is not dict or type(raw_volume.get("name")) is not str:
            raise RolloutGateError("terraform_plan_task_invalid", scope=task)
        volume: dict[str, object] = {"name": raw_volume["name"]}
        configured_at_launch = raw_volume.get("configure_at_launch")
        if configured_at_launch is not None:
            if type(configured_at_launch) is not bool:
                raise RolloutGateError("terraform_plan_task_invalid", scope=task)
            volume["configuredAtLaunch"] = configured_at_launch
        host_path = raw_volume.get("host_path")
        if host_path is not None:
            if type(host_path) is not str:
                raise RolloutGateError("terraform_plan_task_invalid", scope=task)
            volume["host"] = {"sourcePath": host_path}

        block_names = {
            "docker_volume_configuration": "dockerVolumeConfiguration",
            "efs_volume_configuration": "efsVolumeConfiguration",
            "fsx_windows_file_server_volume_configuration": (
                "fsxWindowsFileServerVolumeConfiguration"
            ),
        }
        present_blocks = 0
        for terraform_name, api_name in block_names.items():
            block = _one_block(raw_volume.get(terraform_name), label=task)
            if block is None:
                continue
            present_blocks += 1
            converted = _terraform_value(
                block,
                preserve_mapping_fields=(
                    frozenset({"driver_opts", "labels"})
                    if terraform_name == "docker_volume_configuration"
                    else frozenset()
                ),
            )
            if type(converted) is not dict:
                raise RolloutGateError("terraform_plan_task_invalid", scope=task)
            if terraform_name in {
                "efs_volume_configuration",
                "fsx_windows_file_server_volume_configuration",
            }:
                authorization = _one_block(block.get("authorization_config"), label=task)
                converted.pop("authorizationConfig", None)
                if authorization is not None:
                    converted["authorizationConfig"] = _terraform_value(authorization)
            volume[api_name] = converted
        if (host_path is not None and present_blocks) or present_blocks > 1:
            raise RolloutGateError("terraform_plan_task_invalid", scope=task)
        volumes.append(volume)
    return volumes


def task_from_change(after: dict[str, Any], *, task: str) -> dict[str, object]:
    """Convert one real aws_ecs_task_definition after-value to the ECS API payload."""

    if frozenset(after) - _TASK_TERRAFORM_KEYS or not _TASK_TERRAFORM_REQUIRED_KEYS.issubset(after):
        raise RolloutGateError("terraform_plan_task_invalid", scope=task)
    try:
        containers = json.loads(after["container_definitions"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RolloutGateError("terraform_plan_task_invalid", scope=task) from exc
    if type(containers) is not list or not containers:
        raise RolloutGateError("terraform_plan_task_invalid", scope=task)
    payload: dict[str, object] = {
        "family": after.get("family"),
        "taskRoleArn": after.get("task_role_arn"),
        "executionRoleArn": after.get("execution_role_arn"),
        "networkMode": after.get("network_mode"),
        "containerDefinitions": containers,
        "volumes": _volume_payload(after.get("volume", []), task=task),
        "requiresCompatibilities": after.get("requires_compatibilities"),
        "cpu": str(after["cpu"]) if after.get("cpu") is not None else None,
        "memory": str(after["memory"]) if after.get("memory") is not None else None,
    }
    optional_scalars = {
        "ipc_mode": "ipcMode",
        "pid_mode": "pidMode",
        "enable_fault_injection": "enableFaultInjection",
    }
    for terraform_name, api_name in optional_scalars.items():
        if _present(after, terraform_name):
            payload[api_name] = after[terraform_name]
    optional_lists = {
        "inference_accelerator": "inferenceAccelerators",
        "placement_constraints": "placementConstraints",
    }
    for terraform_name, api_name in optional_lists.items():
        value = after.get(terraform_name)
        if value not in (None, []):
            payload[api_name] = _terraform_value(value)
    optional_blocks = {
        "ephemeral_storage": "ephemeralStorage",
        "proxy_configuration": "proxyConfiguration",
        "runtime_platform": "runtimePlatform",
    }
    for terraform_name, api_name in optional_blocks.items():
        block = _one_block(after.get(terraform_name), label=task)
        if block is not None:
            payload[api_name] = _terraform_value(
                block,
                preserve_mapping_fields=(
                    frozenset({"properties"})
                    if terraform_name == "proxy_configuration"
                    else frozenset()
                ),
            )
    tags = after.get("tags_all", after.get("tags", {}))
    if type(tags) is not dict:
        raise RolloutGateError("terraform_plan_task_invalid", scope=task)
    if tags:
        payload["tags"] = [
            {"key": str(key), "value": str(value)} for key, value in sorted(tags.items())
        ]
    if any(value is None for value in payload.values()):
        raise RolloutGateError("terraform_plan_task_invalid", scope=task)
    return payload


def show_saved_plan(plan_path: Path) -> dict[str, object]:
    if not plan_path.is_file():
        raise RolloutGateError("terraform_plan_unreadable")
    try:
        completed = subprocess.run(
            ["terraform", "show", "-json", str(plan_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        plan = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RolloutGateError("terraform_plan_unreadable") from exc
    if type(plan) is not dict:
        raise RolloutGateError("terraform_plan_unreadable")
    return plan


def candidates_from_plan(
    plan: dict[str, object],
    *,
    tasks: frozenset[str] = frozenset(TASK_ADDRESSES),
) -> dict[str, dict[str, object]]:
    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    candidates: dict[str, dict[str, object]] = {}
    for task in tasks:
        after, actions = _task_change(changes, task=task)
        if actions not in _REGISTER_ACTIONS:
            raise RolloutGateError("terraform_plan_task_invalid", scope=task)
        candidates[task] = task_from_change(after, task=task)
    return candidates


def _task_change(
    changes: list[object],
    *,
    task: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    expected_address = TASK_ADDRESSES.get(task)
    if expected_address is None:
        raise RolloutGateError("terraform_plan_task_invalid", scope=task)
    matching = [
        change
        for change in changes
        if type(change) is dict and change.get("address") == expected_address
    ]
    if len(matching) != 1:
        raise RolloutGateError("terraform_plan_task_invalid", scope=task)
    change = matching[0].get("change")
    after = change.get("after") if type(change) is dict else None
    actions = change.get("actions") if type(change) is dict else None
    if (
        type(after) is not dict
        or type(actions) is not list
        or not actions
        or any(type(action) is not str for action in actions)
        or tuple(actions) not in _REGISTER_ACTIONS | {("no-op",)}
    ):
        raise RolloutGateError("terraform_plan_task_invalid", scope=task)
    return after, tuple(str(action) for action in actions)


def _resource_change(
    changes: list[object],
    *,
    address: str,
    scope: str,
    code: str = "terraform_plan_worker_invalid",
) -> dict[str, Any]:
    matching = [
        change for change in changes if type(change) is dict and change.get("address") == address
    ]
    if len(matching) != 1:
        raise RolloutGateError(code, scope=scope)
    change = matching[0].get("change")
    after = change.get("after") if type(change) is dict else None
    actions = change.get("actions") if type(change) is dict else None
    if (
        type(after) is not dict
        or type(actions) is not list
        or not actions
        or any(type(action) is not str for action in actions)
        or tuple(actions) not in _REGISTER_ACTIONS
    ):
        raise RolloutGateError(code, scope=scope)
    return after


def hmac_release_bindings_from_plan(plan: dict[str, object]) -> dict[str, object]:
    """Extract the HMAC snapshot/control binding from the one-use production gate."""

    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    production_after = _resource_change(
        changes,
        address=PRODUCTION_GATE_ADDRESS,
        scope="hmac",
        code="terraform_plan_hmac_invalid",
    )
    production_input = production_after.get("input")
    release = (
        production_input.get("hmac_release_bindings") if type(production_input) is dict else None
    )
    if (
        type(release) is not dict
        or frozenset(release)
        != frozenset(
            {
                "rotation_epoch",
                "gate_mode",
                "cleanup_domain",
                "manifest_sha256",
                "rollout_control_sha256",
                "worker_enabled",
                "worker_mode",
                "worker_artifacts",
                "worker_provenance_key_arn",
            }
        )
        or type(release.get("rotation_epoch")) is not str
        or not release.get("rotation_epoch")
        or release.get("gate_mode") not in {"candidate", "cleanup", "rollback"}
        or type(release.get("cleanup_domain")) is not str
        or _hex_digest(release.get("manifest_sha256")) is None
        or _hex_digest(release.get("rollout_control_sha256")) is None
        or type(release.get("worker_enabled")) is not bool
        or release.get("worker_mode") not in {"candidate", "cleanup", "rollback"}
        or type(release.get("worker_artifacts")) is not dict
        or type(release.get("worker_provenance_key_arn")) is not str
        or (
            release.get("worker_enabled") is True
            and (
                frozenset(release["worker_artifacts"]) != WORKER_ARTIFACT_BINDING_KEYS
                or any(
                    _hex_digest(digest) is None for digest in release["worker_artifacts"].values()
                )
                or not release.get("worker_provenance_key_arn")
            )
        )
        or (
            release.get("worker_enabled") is False
            and (
                release.get("worker_artifacts") != {}
                or release.get("worker_provenance_key_arn") != ""
            )
        )
    ):
        raise RolloutGateError("terraform_plan_hmac_invalid", scope="hmac")
    return dict(release)


def validate_saved_plan_hmac_files(
    plan: dict[str, object],
    *,
    manifest_path: Path,
    control_path: Path,
    mode: str,
    cleanup_domain: str,
) -> dict[str, object]:
    """Require current snapshots to match the hashes embedded in the complete plan."""

    release = hmac_release_bindings_from_plan(plan)
    if (
        release["gate_mode"] != mode
        or release["cleanup_domain"] != cleanup_domain
        or release["manifest_sha256"] != saved_plan_sha256(manifest_path)
        or release["rollout_control_sha256"] != saved_plan_sha256(control_path)
        or (bool(release["worker_enabled"]) and release["worker_mode"] != mode)
    ):
        raise RolloutGateError("terraform_plan_hmac_invalid", scope="hmac")
    return release


def cleanup_worker_bindings_from_plan(
    plan: dict[str, object],
    *,
    domain: str,
) -> dict[str, str]:
    """Extract the exact worker files bound into the same one-use cleanup plan."""

    return worker_bindings_from_plan(
        plan,
        mode="cleanup",
        cleanup_domain=domain,
        advance_stage=False,
    )


def worker_bindings_from_plan(
    plan: dict[str, object],
    *,
    mode: str,
    cleanup_domain: str,
    advance_stage: bool,
) -> dict[str, str]:
    """Extract exact worker files from the production-gated saved-plan resource."""

    if (
        mode not in {"candidate", "cleanup", "rollback"}
        or type(advance_stage) is not bool
        or (mode == "cleanup" and cleanup_domain not in {"mail_action", "report_link"})
        or (mode != "cleanup" and cleanup_domain)
        or (mode in {"cleanup", "rollback"} and advance_stage)
    ):
        raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    worker_after = _resource_change(
        changes,
        address=WORKER_DEPLOY_ADDRESS,
        scope="worker",
    )
    worker_input = worker_after.get("input")
    release = hmac_release_bindings_from_plan(plan)
    artifacts = worker_input.get("complete_artifacts") if type(worker_input) is dict else None
    release_artifacts = release.get("worker_artifacts") if type(release) is dict else None
    if (
        type(worker_input) is not dict
        or frozenset(worker_input)
        != frozenset(
            {
                "rotation_epoch",
                "mode",
                "cleanup_domain",
                "advance_stage",
                "provenance_key_arn",
                "complete_artifacts",
            }
        )
        or worker_input.get("mode") != mode
        or worker_input.get("cleanup_domain") != cleanup_domain
        or worker_input.get("advance_stage") is not advance_stage
        or type(worker_input.get("rotation_epoch")) is not str
        or not worker_input.get("rotation_epoch")
        or type(worker_input.get("provenance_key_arn")) is not str
        or not worker_input.get("provenance_key_arn")
        or type(artifacts) is not dict
        or frozenset(artifacts) != WORKER_ARTIFACT_BINDING_KEYS
        or any(
            type(digest) is not str or len(digest) != 64 or _hex_digest(digest) is None
            for digest in artifacts.values()
        )
        or type(release) is not dict
        or release.get("rotation_epoch") != worker_input.get("rotation_epoch")
        or release.get("gate_mode") != mode
        or release.get("cleanup_domain") != cleanup_domain
        or release.get("worker_enabled") is not True
        or release.get("worker_mode") != mode
        or release.get("worker_provenance_key_arn") != worker_input.get("provenance_key_arn")
        or release_artifacts != artifacts
        or release.get("manifest_sha256") != artifacts.get("reviewed_manifest")
        or release.get("rollout_control_sha256") != artifacts.get("rollout_control")
    ):
        raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
    return {str(name): str(digest) for name, digest in artifacts.items()}


def validate_saved_plan_event_target(
    plan: dict[str, object],
    *,
    target: dict[str, object],
    task_definition: str,
    mode: str,
    cleanup_domain: str,
    manifest_path: Path,
    control_path: Path,
) -> None:
    """Bind every known EventBridge target field to its saved-plan transaction."""

    validate_saved_plan_hmac_files(
        plan,
        manifest_path=manifest_path,
        control_path=control_path,
        mode=mode,
        cleanup_domain=cleanup_domain,
    )
    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    after = _resource_change(
        changes,
        address=MORNING_PROMOTION_ADDRESS,
        scope="morning_digest",
        code="terraform_plan_event_invalid",
    )
    transaction = after.get("input")
    if type(transaction) is not dict or frozenset(transaction) != frozenset(
        {
            "mode",
            "expected_rule_state",
            "target",
            "task_definition_arn",
            "manifest_sha256",
            "rollout_control_sha256",
        }
    ):
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    planned_target = transaction.get("target")
    if type(planned_target) is not dict:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    planned_target = json.loads(json.dumps(planned_target))
    planned_ecs = planned_target.get("EcsParameters")
    actual_ecs = target.get("EcsParameters")
    if type(planned_ecs) is not dict or type(actual_ecs) is not dict:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    planned_task = planned_ecs.get("TaskDefinitionArn")
    planned_input_task = transaction.get("task_definition_arn")
    if (
        transaction.get("mode") != mode
        or transaction.get("expected_rule_state") != "DISABLED"
        or transaction.get("manifest_sha256") != saved_plan_sha256(manifest_path)
        or transaction.get("rollout_control_sha256") != saved_plan_sha256(control_path)
        or (
            planned_task is not None
            and (type(planned_task) is not str or planned_task != task_definition)
        )
        or (
            planned_input_task is not None
            and (type(planned_input_task) is not str or planned_input_task != task_definition)
        )
        or actual_ecs.get("TaskDefinitionArn") != task_definition
    ):
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    planned_ecs["TaskDefinitionArn"] = task_definition
    if planned_target != target:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")


def validate_saved_plan_runtime_mutations(plan: dict[str, object]) -> None:
    """Reject an ECS task-definition mutation that lacks both apply-time gates."""

    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    for task, service_address in SERVICE_ADDRESSES.items():
        matches = [
            item
            for item in changes
            if type(item) is dict and item.get("address") == service_address
        ]
        if not matches:
            continue
        if len(matches) != 1:
            raise RolloutGateError("terraform_plan_runtime_invalid", scope=task)
        raw_change = matches[0].get("change")
        before = raw_change.get("before") if type(raw_change) is dict else None
        after = raw_change.get("after") if type(raw_change) is dict else None
        actions = raw_change.get("actions") if type(raw_change) is dict else None
        after_unknown = raw_change.get("after_unknown") if type(raw_change) is dict else None
        if (
            type(actions) is not list
            or any(type(action) is not str for action in actions)
            or tuple(actions) not in _REGISTER_ACTIONS | {("no-op",)}
        ):
            raise RolloutGateError("terraform_plan_runtime_invalid", scope=task)
        task_definition_unknown = (
            type(after_unknown) is dict and after_unknown.get("task_definition") is True
        )
        before_task = before.get("task_definition") if type(before) is dict else None
        after_task = after.get("task_definition") if type(after) is dict else None
        mutates_task_definition = tuple(actions) != ("no-op",) and (
            before is None or after is None or task_definition_unknown or before_task != after_task
        )
        if not mutates_task_definition:
            continue
        for address in SERVICE_PROMOTION_ADDRESSES[task]:
            _resource_change(
                changes,
                address=address,
                scope=task,
                code="terraform_plan_runtime_invalid",
            )


def _hex_digest(value: object) -> str | None:
    return (
        value
        if type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
        else None
    )


def candidate_change_from_saved_plan(
    *,
    plan_path: Path,
    task: str,
) -> tuple[dict[str, object], tuple[str, ...]]:
    plan = show_saved_plan(plan_path)
    return candidate_change_from_plan(plan, task=task)


def candidate_change_from_plan(
    plan: dict[str, object],
    *,
    task: str,
) -> tuple[dict[str, object], tuple[str, ...]]:
    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    after, actions = _task_change(changes, task=task)
    return task_from_change(after, task=task), actions


def candidate_from_saved_plan(*, plan_path: Path, task: str) -> dict[str, object]:
    candidate, _actions = candidate_change_from_saved_plan(
        plan_path=plan_path,
        task=task,
    )
    return candidate


def all_candidates_from_saved_plan(plan_path: Path) -> dict[str, dict[str, object]]:
    return candidates_from_plan(show_saved_plan(plan_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify-worker-bindings",))
    args = parser.parse_args(argv)
    try:
        if (
            args.action != "verify-worker-bindings"
            or os.environ.get("TEAMAGENT_HMAC_DEPLOY_FROM_TERRAFORM") != "1"
        ):
            raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
        plan_path = Path(os.environ["TEAMAGENT_SAVED_PLAN_PATH"])
        mode = os.environ["HMAC_WORKER_MODE"]
        cleanup_domain = os.environ.get("HMAC_CLEANUP_DOMAIN", "")
        advance_raw = os.environ["HMAC_WORKER_ADVANCE_STAGE"]
        if advance_raw not in {"0", "1"}:
            raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
        expected = json.loads(os.environ["HMAC_WORKER_EXPECTED_HASHES"])
        actual = worker_bindings_from_plan(
            show_saved_plan(plan_path),
            mode=mode,
            cleanup_domain=cleanup_domain,
            advance_stage=advance_raw == "1",
        )
        if type(expected) is not dict or expected != actual:
            raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
    except Exception:
        print('{"code":"terraform_plan_worker_invalid","ok":false}')
        return 2
    print('{"code":"ok","ok":true}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
