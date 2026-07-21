#!/usr/bin/env python3
"""Extract exact ECS registration artifacts from one immutable Terraform saved plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from scripts.hmac_rollout_gate import (  # noqa: E402
    RolloutGateError,
    _canonical_event_rule,
)

TASK_ADDRESSES = {
    "mcp": "aws_ecs_task_definition.mcp",
    "connect_web": "aws_ecs_task_definition.connect_web[0]",
    "morning_digest": "aws_ecs_task_definition.morning_digest[0]",
}
WORKER_DEPLOY_ADDRESS = "terraform_data.hmac_worker_deploy[0]"
PRODUCTION_GATE_ADDRESS = "terraform_data.production_image_release_gate"
MORNING_TARGET_ADDRESS = "aws_cloudwatch_event_target.morning_digest_run_task[0]"
LIVE_TASK_GATE_ADDRESSES = {
    task: f'terraform_data.hmac_live_task_gate["{task}"]' for task in TASK_ADDRESSES
}
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
MORNING_PROMOTION_ADDRESSES = (
    "terraform_data.hmac_morning_digest_pre_update[0]",
    "terraform_data.hmac_morning_digest_post_update[0]",
)
# Kept as the pre-gate alias for callers that only need the planned target payload.
MORNING_PROMOTION_ADDRESS = MORNING_PROMOTION_ADDRESSES[0]
WORKER_ARTIFACT_BINDING_KEYS = frozenset(
    {
        "atomic_switch",
        "base_environment",
        "base_env_renderer",
        "candidate_artifact",
        "candidate_env",
        "candidate_receipt",
        "candidate_signature",
        "deploy_overrides",
        "deploy_script",
        "provenance_verifier",
        "promotion_attester",
        "release_measurer",
        "reviewed_manifest",
        "runtime_lock",
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
_GATE_MUTATION_ACTIONS = frozenset({("create",), ("create", "delete")})


def saved_plan_sha256(path: Path) -> str:
    """Hash the complete opaque saved plan without loading it all into memory."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RolloutGateError("terraform_plan_unreadable") from exc
    measured = digest.hexdigest()
    expected = os.environ.get("TEAMAGENT_SAVED_PLAN_SHA256")
    if expected is not None and measured != expected:
        raise RolloutGateError("terraform_plan_unreadable")
    return measured


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
    before = saved_plan_sha256(plan_path)
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
    if saved_plan_sha256(plan_path) != before:
        raise RolloutGateError("terraform_plan_unreadable")
    return plan


def candidates_from_plan(
    plan: dict[str, object],
    *,
    tasks: frozenset[str] = frozenset(TASK_ADDRESSES),
    allow_noop: bool = False,
) -> dict[str, dict[str, object]]:
    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    candidates: dict[str, dict[str, object]] = {}
    for task in tasks:
        after, actions = _task_change(changes, task=task)
        if actions not in _REGISTER_ACTIONS and not (allow_noop and actions == ("no-op",)):
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
    allow_noop: bool = False,
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
        or (
            tuple(actions) not in _REGISTER_ACTIONS
            and not (allow_noop and tuple(actions) == ("no-op",))
        )
    ):
        raise RolloutGateError(code, scope=scope)
    return after


def _matching_change(
    changes: list[object],
    *,
    address: str,
    scope: str,
    required: bool = True,
) -> dict[str, Any] | None:
    matching = [
        change for change in changes if type(change) is dict and change.get("address") == address
    ]
    if not matching and not required:
        return None
    if len(matching) != 1:
        raise RolloutGateError("terraform_plan_runtime_invalid", scope=scope)
    raw = matching[0].get("change")
    actions = raw.get("actions") if type(raw) is dict else None
    if (
        type(raw) is not dict
        or type(actions) is not list
        or not actions
        or any(type(action) is not str for action in actions)
    ):
        raise RolloutGateError("terraform_plan_runtime_invalid", scope=scope)
    return raw


def _change_actions(change: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(action) for action in change["actions"])


def _mutates(change: dict[str, Any] | None) -> bool:
    return change is not None and _change_actions(change) != ("no-op",)


def _link_mutates(
    change: dict[str, Any] | None,
    *,
    path: tuple[object, ...],
    scope: str,
) -> bool:
    if change is None or _change_actions(change) == ("no-op",):
        return False
    before = change.get("before")
    after = change.get("after")
    unknown: object = change.get("after_unknown")

    def descend(value: object) -> object:
        current = value
        for item in path:
            if type(item) is int:
                if type(current) is not list or item >= len(current):
                    return None
                current = current[item]
            else:
                if type(current) is not dict:
                    return None
                current = current.get(item)
        return current

    actions = _change_actions(change)
    if actions not in _REGISTER_ACTIONS:
        raise RolloutGateError("terraform_plan_runtime_invalid", scope=scope)
    return (
        before is None
        or after is None
        or descend(unknown) is True
        or descend(before) != descend(after)
    )


def _promotion_tasks_from_plan(plan: dict[str, object]) -> frozenset[str]:
    variables = plan.get("variables")
    raw = variables.get("hmac_runtime_promotion_tasks") if type(variables) is dict else None
    value = raw.get("value") if type(raw) is dict else None
    if (
        type(value) is not list
        or any(type(task) is not str for task in value)
        or len(value) != len(set(value))
        or not set(value).issubset(TASK_ADDRESSES)
    ):
        raise RolloutGateError("terraform_plan_runtime_invalid", scope="hmac")
    return frozenset(str(task) for task in value)


def _gate_input(
    change: dict[str, Any],
    *,
    scope: str,
) -> dict[str, Any]:
    if _change_actions(change) not in _GATE_MUTATION_ACTIONS:
        raise RolloutGateError("terraform_plan_runtime_invalid", scope=scope)
    after = change.get("after")
    gate_input = after.get("input") if type(after) is dict else None
    if type(gate_input) is not dict:
        raise RolloutGateError("terraform_plan_runtime_invalid", scope=scope)
    return gate_input


def _common_gate_input(
    *,
    release: dict[str, object],
    action: str,
    workload: str,
) -> dict[str, object]:
    return {
        "action": action,
        "workload": workload,
        "mode": release["gate_mode"],
        "rotation_epoch": release["rotation_epoch"],
        "cleanup_domain": release["cleanup_domain"],
        "manifest_sha256": release["manifest_sha256"],
        "rollout_control_sha256": release["rollout_control_sha256"],
    }


def _canonical_planned_event_target(after: object) -> dict[str, object]:
    if type(after) is not dict:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    ecs = after.get("ecs_target")
    retry = after.get("retry_policy")
    if (
        type(ecs) is not list
        or len(ecs) != 1
        or type(ecs[0]) is not dict
        or type(retry) is not list
        or len(retry) != 1
        or type(retry[0]) is not dict
    ):
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    network = ecs[0].get("network_configuration")
    if type(network) is not list or len(network) != 1 or type(network[0]) is not dict:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    subnets = network[0].get("subnets")
    security_groups = network[0].get("security_groups")
    if type(subnets) is not list or type(security_groups) is not list:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    return {
        "Id": after.get("target_id"),
        "Arn": after.get("arn"),
        "RoleArn": after.get("role_arn"),
        "Input": after.get("input"),
        "EcsParameters": {
            "TaskDefinitionArn": ecs[0].get("task_definition_arn"),
            "TaskCount": ecs[0].get("task_count"),
            "LaunchType": ecs[0].get("launch_type"),
            "PlatformVersion": ecs[0].get("platform_version"),
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": sorted(subnets),
                    "SecurityGroups": sorted(security_groups),
                    "AssignPublicIp": (
                        "ENABLED" if network[0].get("assign_public_ip") is True else "DISABLED"
                    ),
                }
            },
        },
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": retry[0].get("maximum_event_age_in_seconds"),
            "MaximumRetryAttempts": retry[0].get("maximum_retry_attempts"),
        },
    }


def _raw_hmac_release_bindings(plan: dict[str, object]) -> object:
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
    return production_input.get("hmac_release_bindings") if type(production_input) is dict else None


def hmac_release_bindings_from_plan(plan: dict[str, object]) -> dict[str, object]:
    """Extract the active HMAC snapshot/control binding from the one-use production gate."""

    release = _raw_hmac_release_bindings(plan)
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


def active_hmac_release_bindings_from_plan(
    plan: dict[str, object],
) -> dict[str, object] | None:
    """Return the exact release binding, or ``None`` when HMAC rollout is inactive."""

    release = _raw_hmac_release_bindings(plan)
    if release == {} or (type(release) is dict and release.get("rotation_epoch") == ""):
        return None
    return hmac_release_bindings_from_plan(plan)


def morning_target_mutates_from_plan(plan: dict[str, object]) -> bool:
    """Return whether the authoritative morning target changes task definition."""

    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    change = _matching_change(
        changes,
        address=MORNING_TARGET_ADDRESS,
        scope="morning_digest",
        required=False,
    )
    return _link_mutates(
        change,
        path=("ecs_target", 0, "task_definition_arn"),
        scope="morning_digest",
    )


def deployment_intent_id_from_plan(plan: dict[str, object]) -> str:
    """Return the UUIDv4 one-use intent bound to the production gate."""

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
    intent_id = (
        production_input.get("deployment_intent_id") if type(production_input) is dict else None
    )
    if (
        type(intent_id) is not str
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            intent_id,
        )
        is None
    ):
        raise RolloutGateError("terraform_plan_hmac_invalid", scope="hmac")
    return intent_id


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
    allow_noop: bool = False,
) -> dict[str, str]:
    """Extract the exact worker files bound into the same one-use cleanup plan."""

    return worker_bindings_from_plan(
        plan,
        mode="cleanup",
        cleanup_domain=domain,
        advance_stage=False,
        allow_noop=allow_noop,
    )


def worker_bindings_from_plan(
    plan: dict[str, object],
    *,
    mode: str,
    cleanup_domain: str,
    advance_stage: bool,
    allow_noop: bool = False,
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
        allow_noop=allow_noop,
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
    """Bind every known EventBridge target field to both ordered saved-plan gates."""

    release = validate_saved_plan_hmac_files(
        plan,
        manifest_path=manifest_path,
        control_path=control_path,
        mode=mode,
        cleanup_domain=cleanup_domain,
    )
    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    gate_inputs: list[dict[str, Any]] = []
    for action, address in zip(
        ("pre-update", "post-update"),
        MORNING_PROMOTION_ADDRESSES,
        strict=True,
    ):
        change = _matching_change(
            changes,
            address=address,
            scope="morning_digest",
        )
        assert change is not None
        gate_input = _gate_input(change, scope="morning_digest")
        expected_common = _common_gate_input(
            release=release,
            action=action,
            workload="morning_digest",
        )
        if frozenset(gate_input) != frozenset(
            {
                *expected_common,
                "expected_rule",
                "target",
                "task_definition_arn",
            }
        ) or any(gate_input.get(name) != value for name, value in expected_common.items()):
            raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
        gate_inputs.append(gate_input)
    pre_input, post_input = gate_inputs
    if {name: value for name, value in pre_input.items() if name != "action"} != {
        name: value for name, value in post_input.items() if name != "action"
    }:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")

    planned_target = pre_input.get("target")
    if type(planned_target) is not dict:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    planned_target = json.loads(json.dumps(planned_target))
    planned_ecs = planned_target.get("EcsParameters")
    actual_ecs = target.get("EcsParameters")
    if type(planned_ecs) is not dict or type(actual_ecs) is not dict:
        raise RolloutGateError("terraform_plan_event_invalid", scope="morning_digest")
    planned_task = planned_ecs.get("TaskDefinitionArn")
    planned_input_task = pre_input.get("task_definition_arn")
    try:
        control_value = json.loads(control_path.read_text(encoding="utf-8"))
        control_rule = control_value["morning_digest"]["expected_rule"]
        planned_rule = _canonical_event_rule(pre_input.get("expected_rule"))
        expected_rule = _canonical_event_rule(control_rule)
    except (OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RolloutGateError(
            "terraform_plan_event_invalid",
            scope="morning_digest",
        ) from exc
    if (
        planned_rule != expected_rule
        or expected_rule["State"] != "DISABLED"
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
    """Require exact HMAC gate inputs and complete workload mutation coverage."""

    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise RolloutGateError("terraform_plan_unreadable")
    selected = _promotion_tasks_from_plan(plan)
    release = active_hmac_release_bindings_from_plan(plan)
    morning_change = _matching_change(
        changes,
        address=MORNING_TARGET_ADDRESS,
        scope="morning_digest",
        required=False,
    )
    morning_mutates = _link_mutates(
        morning_change,
        path=("ecs_target", 0, "task_definition_arn"),
        scope="morning_digest",
    )
    if release is None:
        gate_addresses = {
            *LIVE_TASK_GATE_ADDRESSES.values(),
            *(address for pair in SERVICE_PROMOTION_ADDRESSES.values() for address in pair),
            *MORNING_PROMOTION_ADDRESSES,
            "terraform_data.hmac_morning_digest_target_transaction[0]",
        }
        if (
            selected
            or morning_mutates
            or any(
                type(item) is dict
                and item.get("address") in gate_addresses
                and _mutates(item.get("change") if type(item.get("change")) is dict else None)
                for item in changes
            )
        ):
            raise RolloutGateError("terraform_plan_runtime_invalid", scope="hmac")
        return
    required: set[str] = set()

    for task, address in TASK_ADDRESSES.items():
        task_change = _matching_change(
            changes,
            address=address,
            scope=task,
            required=False,
        )
        if _mutates(task_change):
            required.add(task)

    for task, service_address in SERVICE_ADDRESSES.items():
        service_change = _matching_change(
            changes,
            address=service_address,
            scope=task,
            required=False,
        )
        if _link_mutates(
            service_change,
            path=("task_definition",),
            scope=task,
        ):
            required.add(task)

    if morning_mutates:
        required.add("morning_digest")

    if selected != frozenset(required):
        raise RolloutGateError("terraform_plan_runtime_invalid", scope="hmac")

    legacy_address = "terraform_data.hmac_morning_digest_target_transaction[0]"
    for item in changes:
        if (
            type(item) is dict
            and item.get("address") == legacy_address
            and _mutates(item.get("change") if type(item.get("change")) is dict else None)
        ):
            raise RolloutGateError(
                "terraform_plan_runtime_invalid",
                scope="morning_digest",
            )

    for task in TASK_ADDRESSES:
        live_change = _matching_change(
            changes,
            address=LIVE_TASK_GATE_ADDRESSES[task],
            scope=task,
            required=task in selected,
        )
        if task not in selected:
            if _mutates(live_change):
                raise RolloutGateError("terraform_plan_runtime_invalid", scope=task)
            continue
        assert live_change is not None
        live_input = _gate_input(live_change, scope=task)
        expected = {
            **_common_gate_input(
                release=release,
                action="pre-register",
                workload=task,
            ),
            "task_address": TASK_ADDRESSES[task],
        }
        if live_input != expected:
            raise RolloutGateError("terraform_plan_runtime_invalid", scope=task)

    for task, addresses in SERVICE_PROMOTION_ADDRESSES.items():
        if task not in selected:
            for address in addresses:
                change = _matching_change(
                    changes,
                    address=address,
                    scope=task,
                    required=False,
                )
                if _mutates(change):
                    raise RolloutGateError("terraform_plan_runtime_invalid", scope=task)
            continue
        inputs: list[dict[str, Any]] = []
        for action, address in zip(("pre-update", "post-update"), addresses, strict=True):
            change = _matching_change(
                changes,
                address=address,
                scope=task,
            )
            assert change is not None
            gate_input = _gate_input(change, scope=task)
            common = _common_gate_input(
                release=release,
                action=action,
                workload=task,
            )
            if frozenset(gate_input) != frozenset({*common, "task_definition_arn"}) or any(
                gate_input.get(name) != value for name, value in common.items()
            ):
                raise RolloutGateError("terraform_plan_runtime_invalid", scope=task)
            inputs.append(gate_input)
        if inputs[0].get("task_definition_arn") != inputs[1].get("task_definition_arn"):
            raise RolloutGateError("terraform_plan_runtime_invalid", scope=task)

    if "morning_digest" in selected:
        inputs = []
        for action, address in zip(
            ("pre-update", "post-update"),
            MORNING_PROMOTION_ADDRESSES,
            strict=True,
        ):
            change = _matching_change(
                changes,
                address=address,
                scope="morning_digest",
            )
            assert change is not None
            gate_input = _gate_input(change, scope="morning_digest")
            common = _common_gate_input(
                release=release,
                action=action,
                workload="morning_digest",
            )
            if frozenset(gate_input) != frozenset(
                {
                    *common,
                    "expected_rule",
                    "target",
                    "task_definition_arn",
                }
            ) or any(gate_input.get(name) != value for name, value in common.items()):
                raise RolloutGateError(
                    "terraform_plan_runtime_invalid",
                    scope="morning_digest",
                )
            inputs.append(gate_input)
        pre_input, post_input = inputs
        if {name: value for name, value in pre_input.items() if name != "action"} != {
            name: value for name, value in post_input.items() if name != "action"
        }:
            raise RolloutGateError(
                "terraform_plan_runtime_invalid",
                scope="morning_digest",
            )
        if morning_change is None:
            raise RolloutGateError(
                "terraform_plan_runtime_invalid",
                scope="morning_digest",
            )
        native_after = morning_change.get("after")
        if _canonical_planned_event_target(native_after) != pre_input.get("target"):
            raise RolloutGateError(
                "terraform_plan_runtime_invalid",
                scope="morning_digest",
            )
    else:
        for address in MORNING_PROMOTION_ADDRESSES:
            change = _matching_change(
                changes,
                address=address,
                scope="morning_digest",
                required=False,
            )
            if _mutates(change):
                raise RolloutGateError(
                    "terraform_plan_runtime_invalid",
                    scope="morning_digest",
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
    parser.add_argument(
        "action",
        choices=("verify-worker-bindings", "verify-runtime-mutations"),
    )
    parser.add_argument("--plan-json", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "verify-runtime-mutations":
            if args.plan_json is None:
                raise RolloutGateError("terraform_plan_runtime_invalid", scope="hmac")
            try:
                plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RolloutGateError(
                    "terraform_plan_runtime_invalid",
                    scope="hmac",
                ) from exc
            if type(plan) is not dict:
                raise RolloutGateError("terraform_plan_runtime_invalid", scope="hmac")
            validate_saved_plan_runtime_mutations(plan)
            print('{"code":"ok","ok":true}')
            return 0
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
        code = (
            "terraform_plan_runtime_invalid"
            if args.action == "verify-runtime-mutations"
            else "terraform_plan_worker_invalid"
        )
        print(json.dumps({"code": code, "ok": False}, separators=(",", ":"), sort_keys=True))
        return 2
    print('{"code":"ok","ok":true}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
