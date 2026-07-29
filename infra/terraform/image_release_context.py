#!/usr/bin/env python3
"""Capture a fail-closed Terraform context for one image-release saved plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CODEBUILD_DIR = Path(__file__).resolve().parents[1] / "codebuild"
if str(CODEBUILD_DIR) not in sys.path:
    sys.path.insert(0, str(CODEBUILD_DIR))

from image_deployment_consumers import (  # noqa: E402
    ConsumerRegistryError,
    consumer_registry_sha256,
    load_consumer_registry,
)

CONTEXT_KIND = "teamagent.image-release-terraform-context"
CONTEXT_SCHEMA = 3
CONSUMER_MANIFEST_SCHEMA = 1
CONSUMER_MANIFEST_VARIABLE = "image_deployment_consumer_manifest"
RECEIPT_CATALOG_VARIABLE = "image_release_receipt_catalog"
CONSUMER_RECEIPT_BINDINGS_VARIABLE = "image_release_consumer_receipt_bindings"
RELEASE_MODE_RECEIPT_REQUIRED = "receipt-required"
RELEASE_MODE_NO_IMAGE_TRANSITION = "no-image-transition"
ABSENT_CONSUMER_SNAPSHOT = {"absent": True}
ECR_REGISTRY = "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com"
EXPECTED_BACKEND = {
    "type": "s3",
    "bucket": "teamagent-tfstate-718959508629",
    "key": "teamagent/terraform.tfstate",
    "region": "ap-northeast-1",
    "dynamodb_table": "teamagent-tflock",
    "encrypt": True,
}
EXPECTED_WORKSPACE = "default"
ALLOWED_EXISTING_LOG_IMPORTS = {
    "aws_cloudwatch_log_group.codebuild_aiia_image_builder": (
        "/aws/codebuild/teamagent-dev-aiia-image-builder"
    ),
    "aws_cloudwatch_log_group.codebuild_image": (
        "/aws/codebuild/teamagent-dev-image-builder"
    ),
    "aws_cloudwatch_log_group.ecs_containerinsights_teamagent": (
        "/aws/ecs/containerinsights/teamagent-dev/performance"
    ),
    "aws_cloudwatch_log_group.ecs_containerinsights_tiktok": (
        "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance"
    ),
    "aws_cloudwatch_log_group.reminder_notify": (
        "/aws/lambda/teamagent-dev-reminders-notify"
    ),
    "aws_cloudwatch_log_group.tiktok_dispatch": (
        "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch"
    ),
    "aws_cloudwatch_log_group.x_dispatch": (
        "/aws/lambda/teamagent-dev-x-buzz-dispatch"
    ),
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
LEGACY_TIKTOK_IMAGE_RE = re.compile(
    r"^718959508629\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/"
    r"teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$"
)
INSTANCE_SELECTOR_RE = re.compile(r'\[(?:[0-9]+|"(?:[^"\\]|\\.)*")\]')
TASK_DEFINITION_ARN_RE = re.compile(
    r"arn:aws:ecs:ap-northeast-1:718959508629:"
    r"task-definition/([a-z0-9][a-z0-9_-]*):([1-9][0-9]*)"
)
CONTAINER_DEFINITION_COMPARE_FIELDS = {
    "name",
    "image",
    "command",
    "entryPoint",
    "environment",
    "secrets",
    "user",
    "privileged",
    "readonlyRootFilesystem",
    "linuxParameters",
    "mountPoints",
    "logConfiguration",
}
TASK_DEFINITION_COMPARE_FIELDS = {
    "container_definitions",
    "task_role_arn",
    "execution_role_arn",
    "network_mode",
    "cpu",
    "memory",
    "volumes",
}
RELEASE_GATE_ADDRESS = "terraform_data.production_image_release_gate"
RUNTIME_IMAGE_PATTERNS = {
    "mcp_image": re.compile(
        r"^718959508629\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/"
        r"teamagent-mcp@sha256:[0-9a-f]{64}$"
    ),
    "openclaw_image": re.compile(
        r"^718959508629\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/"
        r"teamagent-openclaw@sha256:[0-9a-f]{64}$"
    ),
    "x_buzz_image": re.compile(
        r"^718959508629\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/"
        r"teamagent-mcp@sha256:[0-9a-f]{64}$"
    ),
    "media_worker_image": re.compile(
        r"^718959508629\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/"
        r"teamagent-media-worker@sha256:[0-9a-f]{64}$"
    ),
    "tiktok_acquire_image": re.compile(
        r"^718959508629\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/"
        r"teamagent-media-worker@sha256:[0-9a-f]{64}$"
    ),
}


class ContextError(ValueError):
    """The Terraform plan, backend, workspace, or state is outside the contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContextError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads(value: str, *, label: str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ContextError(f"{label} is not valid JSON") from exc


def _load(path: Path, *, label: str) -> Any:
    try:
        return _loads(path.read_text(encoding="utf-8"), label=label)
    except (OSError, UnicodeDecodeError) as exc:
        raise ContextError(f"cannot read {label}: {path}") from exc


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ContextError(f"{label} must be an object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContextError(f"{label} must be a lowercase SHA-256")
    return value


def _exact_keys(
    value: Any,
    expected: set[str],
    *,
    label: str,
) -> Mapping[str, Any]:
    item = _mapping(value, label=label)
    if set(item) != expected:
        raise ContextError(f"{label} schema mismatch")
    return item


def consumer_snapshot_is_absent(value: Any) -> bool:
    """Return whether a normalized consumer snapshot is the exact absent sentinel."""

    return isinstance(value, dict) and value == ABSENT_CONSUMER_SNAPSHOT


def _task_definition_arn(
    value: Any,
    *,
    family: str,
    label: str,
    planned_address: str | None = None,
) -> str:
    if planned_address is not None and value == planned_address:
        return planned_address
    if not isinstance(value, str):
        raise ContextError(f"{label} must be a task definition ARN")
    match = TASK_DEFINITION_ARN_RE.fullmatch(value)
    if match is None or match.group(1) != family:
        raise ContextError(f"{label} does not match the registry family")
    return value


def _normalized_json_value(value: Any, *, label: str) -> Any:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ContextError(f"{label} contains a non-string JSON key")
        return {
            key: _normalized_json_value(value[key], label=f"{label}.{key}")
            for key in sorted(value)
        }
    if isinstance(value, list):
        return [
            _normalized_json_value(item, label=f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ContextError(f"{label} contains a non-JSON value")


def _normalized_container_definitions(value: Any, *, label: str) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = _loads(value, label=label)
    if not isinstance(value, list):
        raise ContextError(f"{label} must be an array")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw_container in enumerate(value):
        container_label = f"{label}[{index}]"
        container = _mapping(raw_container, label=container_label)
        name = container.get("name")
        image = container.get("image")
        if not isinstance(name, str) or not name:
            raise ContextError(f"{container_label}.name is unknown")
        if name in names:
            raise ContextError(f"{label} contains a duplicate container name")
        names.add(name)
        if not isinstance(image, str) or not image:
            raise ContextError(f"{container_label}.image is unknown")
        normalized_container = _normalized_json_value(
            dict(container),
            label=container_label,
        )
        for field in sorted(CONTAINER_DEFINITION_COMPARE_FIELDS):
            normalized_container.setdefault(field, None)
        environment = normalized_container["environment"]
        if environment is not None:
            if not isinstance(environment, list):
                raise ContextError(f"{container_label}.environment must be an array")
            environment_names: set[str] = set()
            for environment_index, raw_environment in enumerate(environment):
                environment_entry = _mapping(
                    raw_environment,
                    label=(
                        f"{container_label}.environment"
                        f"[{environment_index}]"
                    ),
                )
                if not isinstance(environment_entry.get("name"), str):
                    raise ContextError(
                        f"{container_label}.environment"
                        f"[{environment_index}].name is unknown"
                    )
                if environment_entry["name"] in environment_names:
                    raise ContextError(
                        f"{container_label}.environment contains a duplicate name"
                    )
                environment_names.add(environment_entry["name"])
            normalized_container["environment"] = sorted(
                environment,
                key=lambda entry: (
                    entry["name"],
                    _canonical_bytes(entry),
                ),
            )
        normalized.append(normalized_container)
    return normalized


def _normalized_task_definition(value: Any, *, label: str) -> dict[str, Any]:
    task_definition = _exact_keys(
        value,
        TASK_DEFINITION_COMPARE_FIELDS,
        label=label,
    )
    for field in (
        "task_role_arn",
        "execution_role_arn",
        "network_mode",
        "cpu",
        "memory",
    ):
        if task_definition[field] is not None and not isinstance(
            task_definition[field],
            str,
        ):
            raise ContextError(f"{label}.{field} must be a string or null")
    if task_definition["volumes"] is not None and not isinstance(
        task_definition["volumes"],
        list,
    ):
        raise ContextError(f"{label}.volumes must be an array or null")
    normalized = {
        field: _normalized_json_value(
            task_definition[field],
            label=f"{label}.{field}",
        )
        for field in sorted(TASK_DEFINITION_COMPARE_FIELDS - {"container_definitions"})
    }
    normalized["container_definitions"] = _normalized_container_definitions(
        task_definition["container_definitions"],
        label=f"{label}.container_definitions",
    )
    return {
        "container_definitions": normalized.pop("container_definitions"),
        **normalized,
    }


def _contains_unknown(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, dict):
        return any(_contains_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_unknown(item) for item in value)
    return False


def _reject_unknown_task_definition(
    value: Any,
    *,
    label: str,
) -> None:
    if value in (None, False):
        return
    if value is True:
        raise ContextError(f"{label} is unknown")
    unknown = _mapping(value, label=label)
    for field in TASK_DEFINITION_COMPARE_FIELDS:
        if _contains_unknown(unknown.get(field)):
            raise ContextError(f"{label}.{field} is unknown")


def _consumer_snapshot(
    value: Any,
    *,
    consumer: Mapping[str, Any],
    label: str,
    allow_planned_pointer: bool = False,
    allow_pre_media_cutover_legacy: bool = False,
) -> dict[str, Any]:
    if consumer_snapshot_is_absent(value):
        return dict(ABSENT_CONSUMER_SNAPSHOT)
    snapshot = _exact_keys(
        value,
        {"image", "task_definition_arn", "task_definition", "activation"},
        label=label,
    )
    repository = consumer["release_repository"]
    image = snapshot["image"]
    registry_image = isinstance(image, str) and (
        re.fullmatch(
            rf"{re.escape(ECR_REGISTRY)}/{re.escape(repository)}"
            r"@sha256:[0-9a-f]{64}",
            image,
        )
        is not None
    )
    pre_media_cutover_legacy = (
        allow_pre_media_cutover_legacy
        and consumer["consumer_id"] == "tiktok_acquire"
        and isinstance(image, str)
        and LEGACY_TIKTOK_IMAGE_RE.fullmatch(image) is not None
    )
    if not registry_image and not pre_media_cutover_legacy:
        raise ContextError(
            f"{label}.image is not the registry repository digest "
            "or anchored pre-cutover TikTok legacy digest"
        )
    task_definition_arn = _task_definition_arn(
        snapshot["task_definition_arn"],
        family=consumer["ecs_family"],
        label=f"{label}.task_definition_arn",
        planned_address=(
            consumer["terraform_task_definition_address"]
            if allow_planned_pointer
            else None
        ),
    )
    activator = consumer["activator"]
    activator_type = activator["type"]
    activation = _mapping(snapshot["activation"], label=f"{label}.activation")
    if activator_type == "ecs_service":
        _exact_keys(
            activation,
            {"desired_count", "task_definition_arn"},
            label=f"{label}.activation",
        )
        desired_count = activation["desired_count"]
        if (
            not isinstance(desired_count, int)
            or isinstance(desired_count, bool)
            or desired_count < 0
        ):
            raise ContextError(f"{label}.activation.desired_count is invalid")
        normalized_activation: dict[str, Any] = {
            "desired_count": desired_count,
            "task_definition_arn": _task_definition_arn(
                activation["task_definition_arn"],
                family=consumer["ecs_family"],
                label=f"{label}.activation.task_definition_arn",
                planned_address=(
                    consumer["terraform_task_definition_address"]
                    if allow_planned_pointer
                    else None
                ),
            ),
        }
    elif activator_type == "eventbridge_rule_ecs_target":
        _exact_keys(
            activation,
            {"state", "task_definition_arn"},
            label=f"{label}.activation",
        )
        state = activation["state"]
        if state not in {
            "DISABLED",
            "ENABLED",
            "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS",
        }:
            raise ContextError(f"{label}.activation.state is invalid")
        normalized_activation = {
            "state": state,
            "task_definition_arn": _task_definition_arn(
                activation["task_definition_arn"],
                family=consumer["ecs_family"],
                label=f"{label}.activation.task_definition_arn",
                planned_address=(
                    consumer["terraform_task_definition_address"]
                    if allow_planned_pointer
                    else None
                ),
            ),
        }
    elif activator_type == "lambda_taskdef_arn_environment":
        _exact_keys(
            activation,
            {"event_source_mapping_enabled", "task_definition_arn"},
            label=f"{label}.activation",
        )
        mapping_enabled = activation["event_source_mapping_enabled"]
        if not isinstance(mapping_enabled, bool):
            raise ContextError(
                f"{label}.activation.event_source_mapping_enabled is invalid"
            )
        normalized_activation = {
            "event_source_mapping_enabled": mapping_enabled,
            "task_definition_arn": _task_definition_arn(
                activation["task_definition_arn"],
                family=consumer["ecs_family"],
                label=f"{label}.activation.task_definition_arn",
                planned_address=(
                    consumer["terraform_task_definition_address"]
                    if allow_planned_pointer
                    else None
                ),
            ),
        }
    else:
        raise ContextError(f"{label}.activation type is unsupported")
    if normalized_activation["task_definition_arn"] != task_definition_arn:
        raise ContextError(f"{label} activation edge points at another task definition")
    task_definition = _normalized_task_definition(
        snapshot["task_definition"],
        label=f"{label}.task_definition",
    )
    named_containers = [
        container
        for container in task_definition["container_definitions"]
        if container["name"] == consumer["container_name"]
    ]
    if len(named_containers) != 1 or named_containers[0]["image"] != image:
        raise ContextError(
            f"{label} task definition does not bind the registry container image"
        )
    return {
        "image": image,
        "task_definition_arn": task_definition_arn,
        "task_definition": task_definition,
        "activation": normalized_activation,
    }


def _activation_execution_state(
    snapshot: Mapping[str, Any],
    *,
    activator_type: str,
) -> Any:
    activation = _mapping(snapshot["activation"], label="consumer activation")
    if activator_type == "ecs_service":
        return activation["desired_count"]
    if activator_type == "eventbridge_rule_ecs_target":
        return activation["state"]
    if activator_type == "lambda_taskdef_arn_environment":
        return activation["event_source_mapping_enabled"]
    raise ContextError("consumer activation type is unsupported")


def derive_consumer_manifest_mode(value: Any) -> str:
    """Derive the release mode solely from a validated eight-consumer comparison."""

    manifest = _mapping(value, label="image deployment consumer manifest")
    consumers = manifest.get("consumers")
    if not isinstance(consumers, list) or len(consumers) != 8:
        raise ContextError("consumer manifest must contain exactly eight consumers")
    no_image_transition = True
    for index, raw_consumer in enumerate(consumers):
        consumer = _mapping(
            raw_consumer,
            label=f"consumer manifest consumers[{index}]",
        )
        live = _mapping(consumer.get("live"), label="consumer live snapshot")
        before = _mapping(consumer.get("before"), label="consumer before snapshot")
        after = _mapping(consumer.get("after"), label="consumer after snapshot")
        live_absent = consumer_snapshot_is_absent(live)
        before_absent = consumer_snapshot_is_absent(before)
        after_absent = consumer_snapshot_is_absent(after)
        if live_absent != before_absent:
            raise ContextError(
                "consumer live/before presence differs in the complete plan"
            )
        if after_absent:
            if not live_absent:
                raise ContextError(
                    "consumer decommission requires a separately reviewed "
                    "destructive workflow"
                )
            continue
        if before_absent:
            no_image_transition = False
            continue
        activator = _mapping(consumer.get("activator"), label="consumer activator")
        if not (
            live.get("image") == before.get("image") == after.get("image")
            and live.get("task_definition_arn")
            == before.get("task_definition_arn")
            and live.get("task_definition")
            == before.get("task_definition")
            == after.get("task_definition")
            and _activation_execution_state(
                live,
                activator_type=str(activator.get("type")),
            )
            == _activation_execution_state(
                before,
                activator_type=str(activator.get("type")),
            )
            == _activation_execution_state(
                after,
                activator_type=str(activator.get("type")),
            )
        ):
            no_image_transition = False
    return (
        RELEASE_MODE_NO_IMAGE_TRANSITION
        if no_image_transition
        else RELEASE_MODE_RECEIPT_REQUIRED
    )


def validate_consumer_manifest(value: Any) -> dict[str, Any]:
    """Validate the complete registry-owned consumer comparison and derive its mode."""

    manifest = _exact_keys(
        value,
        {"schema_version", "registry_sha256", "mode", "consumers"},
        label="image deployment consumer manifest",
    )
    if manifest["schema_version"] != CONSUMER_MANIFEST_SCHEMA:
        raise ContextError("consumer manifest schema version is unsupported")
    try:
        registry = load_consumer_registry()
        expected_registry_sha256 = consumer_registry_sha256()
    except ConsumerRegistryError as exc:
        raise ContextError("code-owned consumer registry is invalid") from exc
    if manifest["registry_sha256"] != expected_registry_sha256:
        raise ContextError("consumer manifest registry hash does not match code")
    consumers = manifest["consumers"]
    registry_consumers = registry["consumers"]
    if (
        not isinstance(consumers, list)
        or len(consumers) != len(registry_consumers)
        or len(consumers) != 8
    ):
        raise ContextError("consumer manifest must contain exactly eight consumers")
    normalized_consumers: list[dict[str, Any]] = []
    identity_keys = {
        "consumer_id",
        "terraform_task_definition_address",
        "ecs_family",
        "container_name",
        "activator",
        "release_repository",
        "receipt",
    }
    for index, (raw_consumer, registry_consumer) in enumerate(
        zip(consumers, registry_consumers, strict=True)
    ):
        label = f"consumer manifest consumers[{index}]"
        consumer = _exact_keys(
            raw_consumer,
            identity_keys | {"live", "before", "after"},
            label=label,
        )
        if {key: consumer[key] for key in identity_keys} != {
            key: registry_consumer[key] for key in identity_keys
        }:
            raise ContextError(f"{label} identity does not match the code-owned registry")
        # The canonical manifest live snapshot is the same source used by the
        # media cutover gate.  Permit legacy only for an unchanged TikTok row in
        # no-image-transition mode; no other consumer or transition can opt in.
        pre_media_cutover_legacy = (
            manifest["mode"] == RELEASE_MODE_NO_IMAGE_TRANSITION
            and registry_consumer["consumer_id"] == "tiktok_acquire"
            and isinstance(consumer["live"], dict)
            and consumer["live"] == consumer["before"] == consumer["after"]
            and isinstance(consumer["live"].get("image"), str)
            and (
                LEGACY_TIKTOK_IMAGE_RE.fullmatch(consumer["live"]["image"])
                is not None
            )
        )
        normalized = {
            key: json.loads(json.dumps(registry_consumer[key]))
            for key in identity_keys
        }
        normalized["live"] = _consumer_snapshot(
            consumer["live"],
            consumer=registry_consumer,
            label=f"{label}.live",
            allow_pre_media_cutover_legacy=pre_media_cutover_legacy,
        )
        normalized["before"] = _consumer_snapshot(
            consumer["before"],
            consumer=registry_consumer,
            label=f"{label}.before",
            allow_pre_media_cutover_legacy=pre_media_cutover_legacy,
        )
        normalized["after"] = _consumer_snapshot(
            consumer["after"],
            consumer=registry_consumer,
            label=f"{label}.after",
            allow_planned_pointer=True,
            allow_pre_media_cutover_legacy=pre_media_cutover_legacy,
        )
        normalized_consumers.append(normalized)
    normalized_manifest = {
        "schema_version": CONSUMER_MANIFEST_SCHEMA,
        "registry_sha256": expected_registry_sha256,
        "mode": manifest["mode"],
        "consumers": normalized_consumers,
    }
    derived_mode = derive_consumer_manifest_mode(normalized_manifest)
    if manifest["mode"] != derived_mode:
        raise ContextError("consumer manifest mode does not match the derived comparison")
    normalized_manifest["mode"] = derived_mode
    return normalized_manifest


def _configuration_addresses(module: Mapping[str, Any], prefix: str = "") -> set[str]:
    result: set[str] = set()
    resources = module.get("resources", [])
    if not isinstance(resources, list):
        raise ContextError("Terraform configuration resources are malformed")
    for raw_resource in resources:
        resource = _mapping(raw_resource, label="Terraform configuration resource")
        address = resource.get("address")
        if not isinstance(address, str) or not address:
            raise ContextError("Terraform configuration resource address is invalid")
        result.add(f"{prefix}{address}")
    module_calls = module.get("module_calls", {})
    if not isinstance(module_calls, dict):
        raise ContextError("Terraform configuration module calls are malformed")
    for name, raw_call in module_calls.items():
        if not isinstance(name, str) or not name:
            raise ContextError("Terraform module call name is invalid")
        call = _mapping(raw_call, label=f"Terraform module call {name}")
        child = _mapping(call.get("module"), label=f"Terraform module call {name}.module")
        result.update(_configuration_addresses(child, f"{prefix}module.{name}."))
    return result


def _configuration_resources(
    module: Mapping[str, Any],
    prefix: str = "",
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    resources = module.get("resources", [])
    if not isinstance(resources, list):
        raise ContextError("Terraform configuration resources are malformed")
    for raw_resource in resources:
        resource = _mapping(raw_resource, label="Terraform configuration resource")
        address = resource.get("address")
        if not isinstance(address, str) or not address:
            raise ContextError("Terraform configuration resource address is invalid")
        full_address = f"{prefix}{address}"
        if full_address in result:
            raise ContextError("Terraform configuration resource address is duplicated")
        result[full_address] = resource
    module_calls = module.get("module_calls", {})
    if not isinstance(module_calls, dict):
        raise ContextError("Terraform configuration module calls are malformed")
    for name, raw_call in module_calls.items():
        if not isinstance(name, str) or not name:
            raise ContextError("Terraform module call name is invalid")
        call = _mapping(raw_call, label=f"Terraform module call {name}")
        child = _mapping(call.get("module"), label=f"Terraform module call {name}.module")
        result.update(_configuration_resources(child, f"{prefix}module.{name}."))
    return result


def _expression_references(value: Any) -> list[str]:
    references: list[str] = []
    if isinstance(value, dict):
        raw_references = value.get("references")
        if isinstance(raw_references, list):
            references.extend(
                reference
                for reference in raw_references
                if isinstance(reference, str)
            )
        for nested in value.values():
            references.extend(_expression_references(nested))
    elif isinstance(value, list):
        for nested in value:
            references.extend(_expression_references(nested))
    return references


def _require_planned_pointer_reference(
    *,
    configuration_resources: Mapping[str, Mapping[str, Any]],
    pointer_resource_address: str,
    task_definition_address: str,
    activator_type: str,
    label: str,
) -> None:
    configuration_address = _configuration_address(pointer_resource_address)
    configuration = configuration_resources.get(configuration_address)
    if configuration is None:
        raise ContextError(f"{label} pointer configuration is absent")
    expressions = _mapping(
        configuration.get("expressions"),
        label=f"{label} pointer expressions",
    )
    expression_name = {
        "ecs_service": "task_definition",
        "eventbridge_rule_ecs_target": "ecs_target",
        "lambda_taskdef_arn_environment": "environment",
    }[activator_type]
    pointer_expression = expressions.get(expression_name)
    if pointer_expression is None:
        raise ContextError(f"{label} planned pointer expression is absent")
    references = _expression_references(pointer_expression)
    task_references = [
        match.group(0)
        for reference in references
        for match in re.finditer(
            r"aws_ecs_task_definition\.[a-zA-Z0-9_-]+(?:\[[0-9]+\])?",
            reference,
        )
    ]
    base_task_definition_address = _configuration_address(task_definition_address)
    if not task_references or any(
        (
            reference != task_definition_address
            if "[" in reference
            else reference != base_task_definition_address
        )
        for reference in task_references
    ):
        raise ContextError(
            f"{label} planned pointer does not reference only its consumer task definition"
        )


def _configuration_address(address: str) -> str:
    return INSTANCE_SELECTOR_RE.sub("", address)


def _plan_variable(plan: Mapping[str, Any], name: str) -> Any:
    variables = _mapping(plan.get("variables"), label="Terraform plan variables")
    variable = _mapping(
        variables.get(name),
        label=f"Terraform plan variable {name}",
    )
    if set(variable) != {"value"}:
        raise ContextError(f"Terraform plan variable schema is invalid: {name}")
    return variable["value"]


def _runtime_image_binding(plan: Mapping[str, Any]) -> dict[str, Any]:
    images: dict[str, str] = {}
    for variable_name in (
        "mcp_image",
        "openclaw_image",
        "x_buzz_image",
        "media_worker_image",
        "tiktok_acquire_image",
    ):
        value = _plan_variable(plan, variable_name)
        if not isinstance(value, str):
            raise ContextError(
                f"Terraform plan runtime image variable is invalid: {variable_name}"
            )
        if variable_name in {"mcp_image", "openclaw_image"} and not (
            value and RUNTIME_IMAGE_PATTERNS[variable_name].fullmatch(value)
        ):
            raise ContextError(
                f"Terraform saved plan requires a nonempty release digest: {variable_name}"
            )
        if (
            variable_name not in {"mcp_image", "openclaw_image"}
            and value
            and not RUNTIME_IMAGE_PATTERNS[variable_name].fullmatch(value)
        ):
            raise ContextError(
                f"Terraform saved plan runtime image digest is invalid: {variable_name}"
            )
        images[variable_name] = value
    if (
        images["media_worker_image"]
        and images["tiktok_acquire_image"]
        and images["media_worker_image"] != images["tiktok_acquire_image"]
    ):
        raise ContextError("Terraform saved plan media image aliases disagree")

    enable_flags: dict[str, bool] = {}
    for variable_name in (
        "enable_connect_web",
        "enable_canary_health",
        "enable_ingest_schedule",
        "enable_morning_digest",
        "enable_x_research",
        "enable_media_worker",
        "enable_tiktok_acquire",
    ):
        value = _plan_variable(plan, variable_name)
        if not isinstance(value, bool):
            raise ContextError(
                f"Terraform plan consumer enable variable is invalid: {variable_name}"
            )
        enable_flags[variable_name] = value
    return {
        "enable_flags": enable_flags,
        "images": images,
        "effective_media_worker_image": (
            images["media_worker_image"] or images["tiktok_acquire_image"]
        ),
    }


def _transition_binding(ownership: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    deletes: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    for item in ownership:
        if item["address"] == RELEASE_GATE_ADDRESS:
            continue
        actions = item["actions"]
        transition = {
            "address": item["address"],
            "actions": actions,
        }
        if "delete" in actions and "create" not in actions:
            deletes.append(transition)
        elif "delete" in actions and "create" in actions:
            replacements.append(transition)
    value = {
        "delete": deletes,
        "replace": replacements,
    }
    return {
        "delete_change_count": len(deletes),
        "replace_change_count": len(replacements),
        "transition_sha256": hashlib.sha256(_canonical_bytes(value)).hexdigest(),
    }


def _raw_state_addresses(state: Mapping[str, Any]) -> list[str]:
    raw_resources = state.get("resources")
    if not isinstance(raw_resources, list):
        raise ContextError("Terraform state resources are malformed")
    addresses: set[str] = set()
    for index, raw_resource in enumerate(raw_resources):
        resource = _mapping(
            raw_resource,
            label=f"Terraform state resource[{index}]",
        )
        mode = resource.get("mode", "managed")
        if mode == "data":
            continue
        if mode != "managed":
            raise ContextError("Terraform state resource mode is unsupported")
        resource_type = resource.get("type")
        name = resource.get("name")
        module = resource.get("module")
        if (
            not isinstance(resource_type, str)
            or not resource_type
            or not isinstance(name, str)
            or not name
            or (module is not None and (not isinstance(module, str) or not module))
        ):
            raise ContextError("Terraform state resource identity is invalid")
        base = f"{resource_type}.{name}"
        if module is not None:
            base = f"{module}.{base}"
        instances = resource.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ContextError(f"Terraform state resource has no instances: {base}")
        for raw_instance in instances:
            instance = _mapping(
                raw_instance,
                label=f"Terraform state resource instance {base}",
            )
            index_key = instance.get("index_key")
            if index_key is None:
                address = base
            elif isinstance(index_key, str):
                address = (
                    f"{base}[{json.dumps(index_key, ensure_ascii=False, separators=(',', ':'))}]"
                )
            elif isinstance(index_key, int) and not isinstance(index_key, bool):
                if index_key < 0:
                    raise ContextError("Terraform state instance index is negative")
                address = f"{base}[{index_key}]"
            else:
                raise ContextError("Terraform state instance index is invalid")
            addresses.add(address)
    if not addresses:
        raise ContextError("Terraform state has no managed resource ownership")
    return sorted(addresses)


def _state_resource_instances(
    state: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    raw_resources = state.get("resources")
    if not isinstance(raw_resources, list):
        raise ContextError("Terraform state resources are malformed")
    result: dict[str, dict[str, Any]] = {}
    for index, raw_resource in enumerate(raw_resources):
        resource = _mapping(
            raw_resource,
            label=f"Terraform state resource[{index}]",
        )
        if resource.get("mode", "managed") != "managed":
            continue
        resource_type = resource.get("type")
        name = resource.get("name")
        module = resource.get("module")
        if (
            not isinstance(resource_type, str)
            or not resource_type
            or not isinstance(name, str)
            or not name
            or (module is not None and (not isinstance(module, str) or not module))
        ):
            raise ContextError("Terraform state resource identity is invalid")
        base = f"{resource_type}.{name}"
        if module is not None:
            base = f"{module}.{base}"
        instances = resource.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ContextError(f"Terraform state resource has no instances: {base}")
        for raw_instance in instances:
            instance = _mapping(
                raw_instance,
                label=f"Terraform state resource instance {base}",
            )
            index_key = instance.get("index_key")
            if index_key is None:
                address = base
            elif isinstance(index_key, str):
                address = (
                    f"{base}["
                    f"{json.dumps(index_key, ensure_ascii=False, separators=(',', ':'))}"
                    "]"
                )
            elif isinstance(index_key, int) and not isinstance(index_key, bool):
                if index_key < 0:
                    raise ContextError("Terraform state instance index is negative")
                address = f"{base}[{index_key}]"
            else:
                raise ContextError("Terraform state instance index is invalid")
            if address in result:
                raise ContextError(f"Terraform state address is duplicated: {address}")
            result[address] = {
                "type": resource_type,
                "attributes": _mapping(
                    instance.get("attributes"),
                    label=f"Terraform state resource attributes {address}",
                ),
            }
    return result


def _container_binding(
    attributes: Mapping[str, Any],
    *,
    family: str,
    container_name: str,
    label: str,
    planned_address: str | None = None,
    unknown_attributes: Any = None,
) -> dict[str, Any]:
    if attributes.get("family") != family:
        raise ContextError(f"{label} family does not match the consumer registry")
    _reject_unknown_task_definition(
        unknown_attributes,
        label=label,
    )
    task_definition = _normalized_task_definition(
        {
            "container_definitions": attributes.get("container_definitions"),
            "task_role_arn": attributes.get("task_role_arn"),
            "execution_role_arn": attributes.get("execution_role_arn"),
            "network_mode": attributes.get("network_mode"),
            "cpu": attributes.get("cpu"),
            "memory": attributes.get("memory"),
            "volumes": attributes.get("volumes"),
        },
        label=f"{label} task definition",
    )
    containers = task_definition["container_definitions"]
    matches = [
        container
        for container in containers
        if isinstance(container, dict) and container.get("name") == container_name
    ]
    if len(matches) != 1:
        raise ContextError(f"{label} named container is missing or ambiguous")
    image = matches[0].get("image")
    if not isinstance(image, str):
        raise ContextError(f"{label} named container image is unknown")
    arn = attributes.get("arn")
    if not isinstance(arn, str):
        arn = attributes.get("id")
    if not isinstance(arn, str) and planned_address is not None:
        arn = planned_address
    return {
        "image": image,
        "task_definition_arn": _task_definition_arn(
            arn,
            family=family,
            label=f"{label} ARN",
            planned_address=planned_address,
        ),
        "task_definition": task_definition,
    }


def _managed_plan_changes(
    plan: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    raw_changes = plan.get("resource_changes")
    if not isinstance(raw_changes, list):
        raise ContextError("Terraform saved plan lacks resource changes")
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw_change in enumerate(raw_changes):
        change = _mapping(raw_change, label=f"Terraform resource change[{index}]")
        if change.get("mode", "managed") != "managed":
            continue
        address = change.get("address")
        if not isinstance(address, str) or not address:
            raise ContextError("Terraform managed resource change address is invalid")
        if address in result:
            raise ContextError("Terraform saved plan has duplicate managed addresses")
        result[address] = _mapping(
            change.get("change"),
            label=f"Terraform resource change {address}",
        )
    return result


def _matches_resource_type(address: str, resource_type: str) -> bool:
    return re.search(
        rf"(?:^|\.){re.escape(resource_type)}\.[a-zA-Z0-9_-]+(?:\[|$)",
        address,
    ) is not None


def _identity_matches(value: Any, identity: str) -> bool:
    return isinstance(value, str) and (
        value == identity
        or value.endswith(f":{identity}")
        or value.endswith(f"/{identity}")
    )


def _find_optional_state_resource(
    resources: Mapping[str, Mapping[str, Any]],
    *,
    resource_type: str,
    identity_field: str,
    identity: str,
    label: str,
) -> tuple[str, Mapping[str, Any]] | None:
    matches = [
        (address, _mapping(record["attributes"], label=f"{label} attributes"))
        for address, record in resources.items()
        if record["type"] == resource_type
        and _identity_matches(
            _mapping(record["attributes"], label=f"{label} attributes").get(
                identity_field
            ),
            identity,
        )
    ]
    if len(matches) > 1:
        raise ContextError(f"{label} is ambiguous in Terraform state")
    return matches[0] if matches else None


def _find_state_resource(
    resources: Mapping[str, Mapping[str, Any]],
    *,
    resource_type: str,
    identity_field: str,
    identity: str,
    label: str,
) -> tuple[str, Mapping[str, Any]]:
    match = _find_optional_state_resource(
        resources,
        resource_type=resource_type,
        identity_field=identity_field,
        identity=identity,
        label=label,
    )
    if match is None:
        raise ContextError(f"{label} is missing in Terraform state")
    return match


def _find_optional_plan_change(
    changes: Mapping[str, Mapping[str, Any]],
    *,
    resource_type: str,
    identity_field: str,
    identity: str,
    label: str,
    expected_address: str | None = None,
) -> tuple[str, Mapping[str, Any]] | None:
    matches: list[tuple[str, Mapping[str, Any]]] = []
    for address, details in changes.items():
        if not _matches_resource_type(address, resource_type):
            continue
        identities: list[Any] = []
        for phase in ("before", "after"):
            phase_value = details.get(phase)
            if isinstance(phase_value, dict):
                identities.append(phase_value.get(identity_field))
        if any(_identity_matches(value, identity) for value in identities):
            matches.append((address, details))
    if len(matches) > 1:
        raise ContextError(f"{label} is ambiguous in the complete plan")
    if not matches:
        return None
    if expected_address is not None and matches[0][0] != expected_address:
        raise ContextError(f"{label} is remapped in the complete plan")
    return matches[0]


def _find_plan_change(
    changes: Mapping[str, Mapping[str, Any]],
    *,
    resource_type: str,
    identity_field: str,
    identity: str,
    state_address: str,
    label: str,
) -> Mapping[str, Any]:
    match = _find_optional_plan_change(
        changes,
        resource_type=resource_type,
        identity_field=identity_field,
        identity=identity,
        label=label,
        expected_address=state_address,
    )
    if match is None:
        raise ContextError(f"{label} is missing in the complete plan")
    return match[1]


def _task_definition_from_environment(
    value: Any,
    *,
    label: str,
    planned_address: str | None = None,
) -> str:
    if planned_address is not None and value in (None, []):
        return planned_address
    if not isinstance(value, list) or len(value) != 1:
        raise ContextError(f"{label} environment is malformed")
    environment = _mapping(value[0], label=f"{label} environment block")
    variables = _mapping(
        environment.get("variables"),
        label=f"{label} environment variables",
    )
    task_definition_arn = variables.get("TASKDEF_ARN")
    if task_definition_arn is None and planned_address is not None:
        return planned_address
    if not isinstance(task_definition_arn, str):
        raise ContextError(f"{label} TASKDEF_ARN is unknown")
    return task_definition_arn


def _event_target_task_definition(
    value: Any,
    *,
    label: str,
    planned_address: str | None = None,
) -> str:
    if planned_address is not None and value in (None, []):
        return planned_address
    if not isinstance(value, list) or len(value) != 1:
        raise ContextError(f"{label} ECS target is malformed")
    target = _mapping(value[0], label=f"{label} ECS target")
    task_definition_arn = target.get("task_definition_arn")
    if task_definition_arn is None and planned_address is not None:
        return planned_address
    if not isinstance(task_definition_arn, str):
        raise ContextError(f"{label} task definition ARN is unknown")
    return task_definition_arn


def _activation_phase(
    *,
    activator_type: str,
    family: str,
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any] | None,
    label: str,
    planned_address: str | None = None,
) -> dict[str, Any]:
    if activator_type == "ecs_service":
        desired_count = primary.get("desired_count")
        task_definition_arn = primary.get("task_definition")
        if task_definition_arn is None and planned_address is not None:
            task_definition_arn = planned_address
        if (
            not isinstance(desired_count, int)
            or isinstance(desired_count, bool)
            or desired_count < 0
        ):
            raise ContextError(f"{label} desired count is invalid")
        return {
            "desired_count": desired_count,
            "task_definition_arn": _task_definition_arn(
                task_definition_arn,
                family=family,
                label=f"{label} task definition ARN",
                planned_address=planned_address,
            ),
        }
    if activator_type == "eventbridge_rule_ecs_target":
        if secondary is None:
            raise ContextError(f"{label} EventBridge target is absent")
        state = primary.get("state")
        if state not in {
            "DISABLED",
            "ENABLED",
            "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS",
        }:
            raise ContextError(f"{label} rule state is invalid")
        return {
            "state": state,
            "task_definition_arn": _task_definition_arn(
                _event_target_task_definition(
                    secondary.get("ecs_target"),
                    label=label,
                    planned_address=planned_address,
                ),
                family=family,
                label=f"{label} task definition ARN",
                planned_address=planned_address,
            ),
        }
    if secondary is None:
        raise ContextError(f"{label} event source mapping is absent")
    mapping_enabled = secondary.get("enabled")
    if not isinstance(mapping_enabled, bool):
        raise ContextError(f"{label} event source mapping state is invalid")
    return {
        "event_source_mapping_enabled": mapping_enabled,
        "task_definition_arn": _task_definition_arn(
            _task_definition_from_environment(
                primary.get("environment"),
                label=label,
                planned_address=planned_address,
            ),
            family=family,
            label=f"{label} task definition ARN",
            planned_address=planned_address,
        ),
    }


def _consumer_activation_binding(
    *,
    consumer: Mapping[str, Any],
    state_resources: Mapping[str, Mapping[str, Any]],
    plan_changes: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    consumer_id = str(consumer["consumer_id"])
    family = str(consumer["ecs_family"])
    activator = _mapping(consumer["activator"], label=f"{consumer_id} activator")
    activator_type = str(activator["type"])
    identity = str(activator["identity"])
    if activator_type == "ecs_service":
        primary_type = "aws_ecs_service"
        primary_field = "name"
        secondary_type = None
        secondary_field = None
    elif activator_type == "eventbridge_rule_ecs_target":
        primary_type = "aws_cloudwatch_event_rule"
        primary_field = "name"
        secondary_type = "aws_cloudwatch_event_target"
        secondary_field = "rule"
    else:
        primary_type = "aws_lambda_function"
        primary_field = "function_name"
        secondary_type = "aws_lambda_event_source_mapping"
        secondary_field = "function_name"
    present = {
        phase: not consumer_snapshot_is_absent(consumer[phase])
        for phase in ("live", "before", "after")
    }

    def bind_resource(
        *,
        resource_type: str,
        identity_field: str,
        label: str,
    ) -> tuple[
        Mapping[str, Any] | None,
        Mapping[str, Any] | None,
        Mapping[str, Any] | None,
        str | None,
    ]:
        state_match = _find_optional_state_resource(
            state_resources,
            resource_type=resource_type,
            identity_field=identity_field,
            identity=identity,
            label=label,
        )
        if (state_match is not None) != present["live"]:
            raise ContextError(
                f"{label} presence differs from the consumer manifest live snapshot"
            )
        state_address = None if state_match is None else state_match[0]
        live_value = None if state_match is None else state_match[1]
        plan_match = _find_optional_plan_change(
            plan_changes,
            resource_type=resource_type,
            identity_field=identity_field,
            identity=identity,
            label=label,
            expected_address=state_address,
        )
        if not present["before"] and not present["after"]:
            if plan_match is not None:
                raise ContextError(
                    f"{label} must have no planned resource while the consumer is absent"
                )
            return live_value, None, None, state_address
        if plan_match is None:
            raise ContextError(f"{label} is missing in the complete plan")
        plan_address, change = plan_match
        actions = change.get("actions")
        if not isinstance(actions, list):
            raise ContextError(f"{label} actions are invalid")
        if not present["before"] and present["after"] and actions != ["create"]:
            raise ContextError(
                f"{label} absent-to-present transition must be a create"
            )
        if present["before"] and not present["after"] and actions != ["delete"]:
            raise ContextError(
                f"{label} present-to-absent transition must be a delete"
            )
        phase_values: dict[str, Mapping[str, Any] | None] = {}
        for phase in ("before", "after"):
            raw_phase = change.get(phase)
            if not present[phase]:
                if raw_phase is not None:
                    raise ContextError(
                        f"{label} plan {phase} must prove the resource is absent"
                    )
                phase_values[phase] = None
            else:
                phase_values[phase] = _mapping(
                    raw_phase,
                    label=f"{label} plan {phase}",
                )
        return (
            live_value,
            phase_values["before"],
            phase_values["after"],
            plan_address,
        )

    primary_live, primary_before, primary_after, primary_address = bind_resource(
        resource_type=primary_type,
        identity_field=primary_field,
        label=f"{consumer_id} activation resource",
    )
    secondary_live: Mapping[str, Any] | None = None
    secondary_before: Mapping[str, Any] | None = None
    secondary_after: Mapping[str, Any] | None = None
    secondary_address: str | None = None
    if secondary_type is not None and secondary_field is not None:
        (
            secondary_live,
            secondary_before,
            secondary_after,
            secondary_address,
        ) = bind_resource(
            resource_type=secondary_type,
            identity_field=secondary_field,
            label=f"{consumer_id} activation edge",
        )
    phases: dict[str, dict[str, Any]] = {}
    for phase, primary_phase, secondary_phase in (
        ("live", primary_live, secondary_live),
        ("before", primary_before, secondary_before),
        ("after", primary_after, secondary_after),
    ):
        if not present[phase]:
            phases[phase] = dict(ABSENT_CONSUMER_SNAPSHOT)
            continue
        if primary_phase is None:
            raise ContextError(f"{consumer_id} {phase} activation is absent")
        phases[phase] = _activation_phase(
            activator_type=activator_type,
            family=family,
            primary=primary_phase,
            secondary=secondary_phase,
            label=f"{consumer_id} {phase} activation",
            planned_address=(
                str(consumer["terraform_task_definition_address"])
                if phase == "after"
                else None
            ),
        )
    pointer_resource_address = (
        primary_address
        if activator_type in {"ecs_service", "lambda_taskdef_arn_environment"}
        else secondary_address
    )
    if present["after"] and pointer_resource_address is None:
        raise ContextError(f"{consumer_id} activation pointer resource is absent")
    return phases, pointer_resource_address


def _consumer_state_activation(
    *,
    consumer: Mapping[str, Any],
    state_resources: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    consumer_id = str(consumer["consumer_id"])
    activator = _mapping(consumer["activator"], label=f"{consumer_id} activator")
    activator_type = str(activator["type"])
    identity = str(activator["identity"])
    if activator_type == "ecs_service":
        primary_type = "aws_ecs_service"
        primary_field = "name"
        secondary_type = None
        secondary_field = None
    elif activator_type == "eventbridge_rule_ecs_target":
        primary_type = "aws_cloudwatch_event_rule"
        primary_field = "name"
        secondary_type = "aws_cloudwatch_event_target"
        secondary_field = "rule"
    else:
        primary_type = "aws_lambda_function"
        primary_field = "function_name"
        secondary_type = "aws_lambda_event_source_mapping"
        secondary_field = "function_name"
    _, primary = _find_state_resource(
        state_resources,
        resource_type=primary_type,
        identity_field=primary_field,
        identity=identity,
        label=f"{consumer_id} activation resource",
    )
    secondary: Mapping[str, Any] | None = None
    if secondary_type is not None and secondary_field is not None:
        _, secondary = _find_state_resource(
            state_resources,
            resource_type=secondary_type,
            identity_field=secondary_field,
            identity=identity,
            label=f"{consumer_id} activation edge",
        )
    return _activation_phase(
        activator_type=activator_type,
        family=str(consumer["ecs_family"]),
        primary=primary,
        secondary=secondary,
        label=f"{consumer_id} state activation",
    )


def _consumer_state_activation_is_absent(
    *,
    consumer: Mapping[str, Any],
    state_resources: Mapping[str, Mapping[str, Any]],
) -> bool:
    consumer_id = str(consumer["consumer_id"])
    activator = _mapping(consumer["activator"], label=f"{consumer_id} activator")
    activator_type = str(activator["type"])
    identity = str(activator["identity"])
    if activator_type == "ecs_service":
        resource_contracts = (("aws_ecs_service", "name"),)
    elif activator_type == "eventbridge_rule_ecs_target":
        resource_contracts = (
            ("aws_cloudwatch_event_rule", "name"),
            ("aws_cloudwatch_event_target", "rule"),
        )
    else:
        resource_contracts = (
            ("aws_lambda_function", "function_name"),
            ("aws_lambda_event_source_mapping", "function_name"),
        )
    return all(
        _find_optional_state_resource(
            state_resources,
            resource_type=resource_type,
            identity_field=identity_field,
            identity=identity,
            label=f"{consumer_id} activation resource",
        )
        is None
        for resource_type, identity_field in resource_contracts
    )


def validate_consumer_activation_state(
    manifest: Any,
    state: Mapping[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    """Match all eight live state activation edges to a validated manifest phase."""

    if phase not in {"live", "after"}:
        raise ContextError("consumer activation state phase must be live or after")
    validated_manifest = validate_consumer_manifest(manifest)
    _state_binding(state)
    state_resources = _state_resource_instances(state)
    edges: list[dict[str, Any]] = []
    for consumer in validated_manifest["consumers"]:
        consumer_id = str(consumer["consumer_id"])
        address = str(consumer["terraform_task_definition_address"])
        state_resource = state_resources.get(address)
        expected = _mapping(
            consumer[phase],
            label=f"{consumer_id} {phase} manifest snapshot",
        )
        if consumer_snapshot_is_absent(expected):
            if state_resource is not None or not _consumer_state_activation_is_absent(
                consumer=consumer,
                state_resources=state_resources,
            ):
                raise ContextError(
                    f"{consumer_id} state resources must be absent for manifest {phase}"
                )
            edges.append(
                {
                    "consumer_id": consumer_id,
                    **ABSENT_CONSUMER_SNAPSHOT,
                }
            )
            continue
        if state_resource is None or state_resource["type"] != "aws_ecs_task_definition":
            raise ContextError(
                f"consumer task definition is absent from state: {consumer_id}"
            )
        task_definition = _container_binding(
            _mapping(
                state_resource["attributes"],
                label=f"{consumer_id} state task definition",
            ),
            family=str(consumer["ecs_family"]),
            container_name=str(consumer["container_name"]),
            label=f"{consumer_id} state task definition",
        )
        planned_pointer = expected["task_definition_arn"] == address
        expected_task_definition_arn = (
            task_definition["task_definition_arn"]
            if planned_pointer
            else expected["task_definition_arn"]
        )
        if task_definition != {
            "image": expected["image"],
            "task_definition_arn": expected_task_definition_arn,
            "task_definition": expected["task_definition"],
        }:
            raise ContextError(
                f"{consumer_id} state task definition differs from manifest {phase}"
            )
        expected_activation = dict(
            _mapping(
                expected["activation"],
                label=f"{consumer_id} {phase} manifest activation",
            )
        )
        if planned_pointer:
            expected_activation["task_definition_arn"] = task_definition[
                "task_definition_arn"
            ]
        activation = _consumer_state_activation(
            consumer=consumer,
            state_resources=state_resources,
        )
        if activation != expected_activation:
            raise ContextError(
                f"{consumer_id} state activation edge differs from manifest {phase}"
            )
        edges.append(
            {
                "consumer_id": consumer_id,
                "image": task_definition["image"],
                "task_definition_arn": task_definition["task_definition_arn"],
                "task_definition": task_definition["task_definition"],
                "activation": activation,
            }
        )
    return {
        "phase": phase,
        "registry_sha256": validated_manifest["registry_sha256"],
        "consumer_count": len(edges),
        "activation_edges_sha256": hashlib.sha256(
            _canonical_bytes(edges)
        ).hexdigest(),
    }


def _manifest_plan_binding(
    *,
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        registry_consumers = load_consumer_registry()["consumers"]
    except ConsumerRegistryError as exc:
        raise ContextError("code-owned consumer registry is invalid") from exc
    raw_consumers = manifest.get("consumers")
    if not isinstance(raw_consumers, list):
        raise ContextError("consumer manifest consumers must be an array")
    expected_consumers = [
        (
            consumer["consumer_id"],
            consumer["terraform_task_definition_address"],
        )
        for consumer in registry_consumers
    ]
    actual_consumers = [
        (
            consumer.get("consumer_id"),
            consumer.get("terraform_task_definition_address"),
        )
        for consumer in (
            _mapping(
                raw_consumer,
                label=f"consumer manifest consumers[{index}]",
            )
            for index, raw_consumer in enumerate(raw_consumers)
        )
    ]
    if actual_consumers != expected_consumers:
        raise ContextError(
            "consumer manifest consumer set does not match the code-owned registry"
        )
    state_resources = _state_resource_instances(state)
    plan_changes = _managed_plan_changes(plan)
    configuration = _mapping(
        plan.get("configuration"),
        label="Terraform plan configuration",
    )
    configuration_root = _mapping(
        configuration.get("root_module"),
        label="Terraform plan configuration root",
    )
    configuration_resources = _configuration_resources(configuration_root)
    comparison: list[dict[str, Any]] = []
    for raw_consumer in manifest["consumers"]:
        consumer = _mapping(raw_consumer, label="consumer manifest entry")
        consumer_id = str(consumer["consumer_id"])
        address = str(consumer["terraform_task_definition_address"])
        live_manifest = _mapping(
            consumer["live"],
            label=f"{consumer_id} live manifest snapshot",
        )
        before_manifest = _mapping(
            consumer["before"],
            label=f"{consumer_id} before manifest snapshot",
        )
        after_manifest = _mapping(
            consumer["after"],
            label=f"{consumer_id} after manifest snapshot",
        )
        live_absent = consumer_snapshot_is_absent(live_manifest)
        before_absent = consumer_snapshot_is_absent(before_manifest)
        after_absent = consumer_snapshot_is_absent(after_manifest)
        state_resource = state_resources.get(address)
        if live_absent:
            if state_resource is not None:
                raise ContextError(
                    f"{consumer_id} live task definition must be absent from state"
                )
            live = dict(ABSENT_CONSUMER_SNAPSHOT)
        else:
            if (
                state_resource is None
                or state_resource["type"] != "aws_ecs_task_definition"
            ):
                raise ContextError(
                    f"consumer live task definition is absent from state: {consumer_id}"
                )
            live = _container_binding(
                _mapping(
                    state_resource["attributes"],
                    label=f"{consumer_id} live task definition",
                ),
                family=str(consumer["ecs_family"]),
                container_name=str(consumer["container_name"]),
                label=f"{consumer_id} live task definition",
            )
            if live != {
                "image": live_manifest["image"],
                "task_definition_arn": live_manifest["task_definition_arn"],
                "task_definition": live_manifest["task_definition"],
            }:
                raise ContextError(
                    f"{consumer_id} live task definition differs from the manifest"
                )

        task_change = plan_changes.get(address)
        if before_absent and after_absent:
            if task_change is not None:
                raise ContextError(
                    f"{consumer_id} absent task definition must have no planned resource"
                )
            actions: list[str] = []
            before = dict(ABSENT_CONSUMER_SNAPSHOT)
            after = dict(ABSENT_CONSUMER_SNAPSHOT)
        else:
            if task_change is None:
                raise ContextError(
                    "consumer task definition is absent from the complete plan: "
                    f"{consumer_id}"
                )
            actions_value = task_change.get("actions")
            if not isinstance(actions_value, list):
                raise ContextError(
                    f"{consumer_id} task definition actions are invalid"
                )
            actions = list(actions_value)
            if before_absent and not after_absent and actions != ["create"]:
                raise ContextError(
                    f"{consumer_id} absent-to-present task definition must be a create"
                )
            if not before_absent and after_absent and actions != ["delete"]:
                raise ContextError(
                    f"{consumer_id} present-to-absent task definition must be a delete"
                )
            if before_absent:
                if task_change.get("before") is not None:
                    raise ContextError(
                        f"{consumer_id} plan before must prove the task definition is absent"
                    )
                before = dict(ABSENT_CONSUMER_SNAPSHOT)
            else:
                before = _container_binding(
                    _mapping(
                        task_change.get("before"),
                        label=f"{consumer_id} plan before task definition",
                    ),
                    family=str(consumer["ecs_family"]),
                    container_name=str(consumer["container_name"]),
                    label=f"{consumer_id} plan before task definition",
                )
                if before != {
                    "image": before_manifest["image"],
                    "task_definition_arn": before_manifest["task_definition_arn"],
                    "task_definition": before_manifest["task_definition"],
                }:
                    raise ContextError(
                        f"{consumer_id} plan before task definition differs "
                        "from the manifest"
                    )
            if after_absent:
                if task_change.get("after") is not None or _contains_unknown(
                    task_change.get("after_unknown")
                ):
                    raise ContextError(
                        f"{consumer_id} plan after must prove the task definition is absent"
                    )
                after = dict(ABSENT_CONSUMER_SNAPSHOT)
            else:
                after = _container_binding(
                    _mapping(
                        task_change.get("after"),
                        label=f"{consumer_id} plan after task definition",
                    ),
                    family=str(consumer["ecs_family"]),
                    container_name=str(consumer["container_name"]),
                    label=f"{consumer_id} plan after task definition",
                    planned_address=(
                        address
                        if after_manifest["task_definition_arn"] == address
                        else None
                    ),
                    unknown_attributes=task_change.get("after_unknown"),
                )
                if after != {
                    "image": after_manifest["image"],
                    "task_definition_arn": after_manifest["task_definition_arn"],
                    "task_definition": after_manifest["task_definition"],
                }:
                    raise ContextError(
                        f"{consumer_id} plan after task definition differs "
                        "from the manifest"
                    )

        pointer_changed = (
            not after_absent
            and (
                before_absent
                or after_manifest["task_definition_arn"]
                != before_manifest["task_definition_arn"]
            )
        )
        if pointer_changed and not before_absent and (
            "create" not in actions
            or any(action not in {"create", "delete"} for action in actions)
        ):
            raise ContextError(
                f"{consumer_id} after pointer is not its scheduled task definition"
            )
        activation, pointer_resource_address = _consumer_activation_binding(
            consumer=consumer,
            state_resources=state_resources,
            plan_changes=plan_changes,
        )
        for phase in ("live", "before", "after"):
            expected_activation = (
                dict(ABSENT_CONSUMER_SNAPSHOT)
                if consumer_snapshot_is_absent(consumer[phase])
                else consumer[phase]["activation"]
            )
            if activation[phase] != expected_activation:
                raise ContextError(
                    f"{consumer_id} {phase} activation edge differs from the manifest"
                )
        if pointer_changed:
            if pointer_resource_address is None:
                raise ContextError(
                    f"{consumer_id} activation pointer resource is absent"
                )
            if (
                manifest["mode"] == RELEASE_MODE_NO_IMAGE_TRANSITION
                and not before_absent
                and after_manifest["image"] != before_manifest["image"]
            ):
                raise ContextError(
                    f"{consumer_id} no-image planned task definition changes its image"
                )
            _require_planned_pointer_reference(
                configuration_resources=configuration_resources,
                pointer_resource_address=pointer_resource_address,
                task_definition_address=address,
                activator_type=str(consumer["activator"]["type"]),
                label=consumer_id,
            )
        comparison.append(
            {
                "consumer_id": consumer_id,
                "task_definition_address": address,
                "actions": actions,
                "live": live,
                "before": before,
                "after": after,
                "activation": activation,
            }
        )
    return {
        "consumer_count": len(comparison),
        "consumer_comparison_sha256": hashlib.sha256(
            _canonical_bytes(comparison)
        ).hexdigest(),
    }


def _release_evidence_plan_binding(
    *,
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, str]:
    receipt_catalog = dict(
        _mapping(
            _plan_variable(plan, RECEIPT_CATALOG_VARIABLE),
            label="Terraform receipt catalog variable",
        )
    )
    consumer_bindings = dict(
        _mapping(
            _plan_variable(plan, CONSUMER_RECEIPT_BINDINGS_VARIABLE),
            label="Terraform consumer receipt bindings variable",
        )
    )
    gate_change = _managed_plan_changes(plan).get(RELEASE_GATE_ADDRESS)
    if gate_change is None:
        raise ContextError("Terraform saved plan lacks the release gate change")
    gate_after = _mapping(
        gate_change.get("after"),
        label="Terraform release gate plan after",
    )
    gate_input = _mapping(
        gate_after.get("input"),
        label="Terraform release gate plan input",
    )
    release_channels = dict(
        _mapping(
            gate_input.get("release_channels"),
            label="Terraform release gate channels",
        )
    )
    if manifest["mode"] == RELEASE_MODE_NO_IMAGE_TRANSITION and (
        receipt_catalog or consumer_bindings or release_channels
    ):
        raise ContextError(
            "no-image-transition requires empty receipt catalog, bindings, and channels"
        )
    value = {
        "receipt_catalog": receipt_catalog,
        "consumer_receipt_bindings": consumer_bindings,
        "release_channels": release_channels,
    }
    return {
        "release_evidence_binding_sha256": hashlib.sha256(
            _canonical_bytes(value)
        ).hexdigest()
    }


def _state_binding(state: Mapping[str, Any]) -> dict[str, Any]:
    if state.get("version") != 4:
        raise ContextError("Terraform state version is not supported")
    lineage = state.get("lineage")
    try:
        if not isinstance(lineage, str) or str(uuid.UUID(lineage)) != lineage:
            raise ValueError
    except ValueError as exc:
        raise ContextError("Terraform state lineage is not a canonical UUID") from exc
    serial = state.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        raise ContextError("Terraform state serial is invalid")
    addresses = _raw_state_addresses(state)
    return {
        "lineage": lineage,
        "serial": serial,
        "managed_address_count": len(addresses),
        "managed_addresses_sha256": hashlib.sha256(_canonical_bytes(addresses)).hexdigest(),
        "_addresses": addresses,
    }


def _backend_binding(metadata: Mapping[str, Any]) -> dict[str, Any]:
    backend = _mapping(metadata.get("backend"), label="Terraform backend metadata")
    backend_type = backend.get("type")
    config = _mapping(backend.get("config"), label="Terraform backend config")
    actual = {
        "type": backend_type,
        "bucket": config.get("bucket"),
        "key": config.get("key"),
        "region": config.get("region"),
        "dynamodb_table": config.get("dynamodb_table"),
        "encrypt": config.get("encrypt"),
    }
    if actual != EXPECTED_BACKEND:
        raise ContextError("Terraform backend identity does not match the fixed state")
    return actual


def build_context(
    *,
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    backend_metadata: Mapping[str, Any],
    workspace: str,
) -> dict[str, Any]:
    if workspace != EXPECTED_WORKSPACE:
        raise ContextError("Terraform workspace must be exactly default")
    if plan.get("complete") is not True:
        raise ContextError("Terraform saved plan is incomplete")
    if plan.get("errored") is not False:
        raise ContextError("Terraform saved plan is errored or lacks an errored=false marker")
    if plan.get("applyable") is not True:
        raise ContextError("Terraform saved plan is not applyable")

    runtime_images = _runtime_image_binding(plan)
    consumer_manifest = validate_consumer_manifest(
        _plan_variable(plan, CONSUMER_MANIFEST_VARIABLE)
    )
    state_binding = _state_binding(state)
    state_addresses = set(state_binding.pop("_addresses"))
    configuration = _mapping(
        plan.get("configuration"),
        label="Terraform plan configuration",
    )
    configuration_root = _mapping(
        configuration.get("root_module"),
        label="Terraform plan configuration root",
    )
    configuration_addresses = _configuration_addresses(configuration_root)
    raw_changes = plan.get("resource_changes")
    if not isinstance(raw_changes, list):
        raise ContextError("Terraform saved plan lacks resource changes")
    ownership: list[dict[str, Any]] = []
    for index, raw_change in enumerate(raw_changes):
        change = _mapping(raw_change, label=f"Terraform resource change[{index}]")
        if change.get("mode", "managed") != "managed":
            continue
        address = change.get("address")
        if not isinstance(address, str) or not address:
            raise ContextError("Terraform managed resource change address is invalid")
        details = _mapping(
            change.get("change"),
            label=f"Terraform resource change {address}",
        )
        importing = details.get("importing")
        import_id = ""
        if importing is not None and importing is not False:
            import_contract = _mapping(
                importing,
                label=f"Terraform import operation {address}",
            )
            expected_import_id = ALLOWED_EXISTING_LOG_IMPORTS.get(address)
            if (
                set(import_contract) != {"id"}
                or import_contract.get("id") != expected_import_id
            ):
                raise ContextError(
                    "Terraform import is outside the exact existing-log allowlist"
                )
            import_id = str(expected_import_id)
        actions = details.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
            or any(
                action not in {"no-op", "create", "read", "update", "delete"} for action in actions
            )
        ):
            raise ContextError(f"Terraform actions are invalid for {address}")
        if import_id and actions not in (["no-op"], ["update"]):
            raise ContextError(
                f"Terraform existing-log import action is destructive: {address}"
            )
        in_state = address in state_addresses
        in_configuration = (
            address in configuration_addresses
            or _configuration_address(address) in configuration_addresses
        )
        if import_id and in_state:
            raise ContextError(
                f"Terraform import address is already owned by the bound state: {address}"
            )
        if not in_state and "create" not in actions and not import_id:
            raise ContextError(
                f"Terraform change is not owned by the bound state: {address}"
            )
        if ("create" in actions or import_id) and not in_configuration:
            raise ContextError(
                f"Terraform create/import is not owned by the reviewed configuration: {address}"
            )
        ownership.append(
            {
                "address": address,
                "actions": actions,
                "state_owned": in_state,
                "configuration_owned": in_configuration,
                "import_id": import_id,
            }
        )
    ownership.sort(key=lambda item: item["address"])
    if not ownership:
        raise ContextError("Terraform saved plan has no managed resource ownership")
    if len({item["address"] for item in ownership}) != len(ownership):
        raise ContextError("Terraform saved plan has duplicate managed addresses")
    gate_changes = [
        item
        for item in ownership
        if item["address"] == RELEASE_GATE_ADDRESS
    ]
    if len(gate_changes) != 1 or "create" not in gate_changes[0]["actions"]:
        raise ContextError("Terraform saved plan will not run the production release gate")

    consumer_binding = _manifest_plan_binding(
        manifest=consumer_manifest,
        plan=plan,
        state=state,
    )
    release_evidence_binding = _release_evidence_plan_binding(
        manifest=consumer_manifest,
        plan=plan,
    )
    transition = _transition_binding(ownership)
    return {
        "schema_version": CONTEXT_SCHEMA,
        "kind": CONTEXT_KIND,
        "backend": _backend_binding(backend_metadata),
        "workspace": workspace,
        "state": state_binding,
        "consumer_manifest": consumer_manifest,
        "plan": {
            "complete": True,
            "applyable": True,
            "errored": False,
            "managed_change_count": len(ownership),
            "address_ownership_sha256": hashlib.sha256(
                _canonical_bytes(ownership)
            ).hexdigest(),
            "runtime_images_sha256": hashlib.sha256(
                _canonical_bytes(runtime_images)
            ).hexdigest(),
            "consumer_manifest_sha256": hashlib.sha256(
                _canonical_bytes(consumer_manifest)
            ).hexdigest(),
            **consumer_binding,
            **release_evidence_binding,
            **transition,
        },
    }


def validate_context(value: Any) -> dict[str, Any]:
    context = _mapping(value, label="Terraform image release context")
    expected_keys = {
        "schema_version",
        "kind",
        "backend",
        "workspace",
        "state",
        "consumer_manifest",
        "plan",
    }
    if set(context) != expected_keys:
        raise ContextError("Terraform image release context schema mismatch")
    if (
        context["schema_version"] != CONTEXT_SCHEMA
        or context["kind"] != CONTEXT_KIND
        or context["workspace"] != EXPECTED_WORKSPACE
    ):
        raise ContextError("Terraform image release context identity mismatch")
    if dict(_mapping(context["backend"], label="Terraform context backend")) != EXPECTED_BACKEND:
        raise ContextError("Terraform context backend identity mismatch")
    state = _mapping(context["state"], label="Terraform context state")
    if set(state) != {
        "lineage",
        "serial",
        "managed_address_count",
        "managed_addresses_sha256",
    }:
        raise ContextError("Terraform context state schema mismatch")
    try:
        if str(uuid.UUID(str(state["lineage"]))) != state["lineage"]:
            raise ValueError
    except ValueError as exc:
        raise ContextError("Terraform context lineage is invalid") from exc
    if (
        not isinstance(state["serial"], int)
        or isinstance(state["serial"], bool)
        or state["serial"] < 0
        or not isinstance(state["managed_address_count"], int)
        or state["managed_address_count"] <= 0
    ):
        raise ContextError("Terraform context state counters are invalid")
    _sha256(
        state["managed_addresses_sha256"],
        label="Terraform context managed address hash",
    )
    plan = _mapping(context["plan"], label="Terraform context plan")
    if set(plan) != {
        "complete",
        "applyable",
        "errored",
        "managed_change_count",
        "address_ownership_sha256",
        "runtime_images_sha256",
        "consumer_manifest_sha256",
        "consumer_count",
        "consumer_comparison_sha256",
        "release_evidence_binding_sha256",
        "delete_change_count",
        "replace_change_count",
        "transition_sha256",
    }:
        raise ContextError("Terraform context plan schema mismatch")
    if (
        plan["complete"] is not True
        or plan["applyable"] is not True
        or plan["errored"] is not False
        or not isinstance(plan["managed_change_count"], int)
        or plan["managed_change_count"] <= 0
        or not isinstance(plan["delete_change_count"], int)
        or isinstance(plan["delete_change_count"], bool)
        or plan["delete_change_count"] < 0
        or not isinstance(plan["replace_change_count"], int)
        or isinstance(plan["replace_change_count"], bool)
        or plan["replace_change_count"] < 0
        or plan["delete_change_count"] + plan["replace_change_count"]
        > plan["managed_change_count"]
        or plan["consumer_count"] != 8
    ):
        raise ContextError("Terraform context plan markers are invalid")
    consumer_manifest = validate_consumer_manifest(context["consumer_manifest"])
    if hashlib.sha256(_canonical_bytes(consumer_manifest)).hexdigest() != plan[
        "consumer_manifest_sha256"
    ]:
        raise ContextError("Terraform context consumer manifest hash mismatch")
    _sha256(
        plan["address_ownership_sha256"],
        label="Terraform context address ownership hash",
    )
    _sha256(
        plan["runtime_images_sha256"],
        label="Terraform context runtime image hash",
    )
    _sha256(
        plan["consumer_manifest_sha256"],
        label="Terraform context consumer manifest hash",
    )
    _sha256(
        plan["consumer_comparison_sha256"],
        label="Terraform context consumer comparison hash",
    )
    _sha256(
        plan["release_evidence_binding_sha256"],
        label="Terraform context release evidence binding hash",
    )
    _sha256(
        plan["transition_sha256"],
        label="Terraform context transition hash",
    )
    return dict(context)


def context_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(validate_context(value))).hexdigest()


def _run(terraform_dir: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["terraform", *arguments],
            cwd=terraform_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContextError(f"Terraform command failed: {' '.join(arguments)}") from exc
    return completed.stdout


def capture(terraform_dir: Path, plan_path: Path) -> dict[str, Any]:
    metadata_path = terraform_dir / ".terraform" / "terraform.tfstate"
    backend_metadata = _mapping(
        _load(metadata_path, label="Terraform backend metadata"),
        label="Terraform backend metadata",
    )
    plan = _mapping(
        _loads(
            _run(terraform_dir, "show", "-json", str(plan_path)),
            label="Terraform saved plan",
        ),
        label="Terraform saved plan",
    )
    state = _mapping(
        _loads(_run(terraform_dir, "state", "pull"), label="Terraform live state"),
        label="Terraform live state",
    )
    workspace = _run(terraform_dir, "workspace", "show").strip()
    return build_context(
        plan=plan,
        state=state,
        backend_metadata=backend_metadata,
        workspace=workspace,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    capture_command = commands.add_parser("capture")
    capture_command.add_argument("--terraform-dir", type=Path, required=True)
    capture_command.add_argument("--plan", type=Path, required=True)
    capture_command.add_argument("--output", type=Path, required=True)
    validate_command = commands.add_parser("validate")
    validate_command.add_argument("--context", type=Path, required=True)
    manifest_command = commands.add_parser("validate-consumer-manifest")
    manifest_command.add_argument("--manifest", type=Path, required=True)
    activation_command = commands.add_parser("validate-activation-state")
    activation_command.add_argument("--manifest", type=Path, required=True)
    activation_command.add_argument("--state", type=Path, required=True)
    activation_command.add_argument(
        "--phase",
        choices=("live", "after"),
        required=True,
    )
    hash_command = commands.add_parser("sha256")
    hash_command.add_argument("--context", type=Path, required=True)
    commands.add_parser("registry-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "capture":
            value = capture(args.terraform_dir.resolve(), args.plan.resolve())
            args.output.write_bytes(_canonical_bytes(value))
        elif args.command == "validate":
            validate_context(_load(args.context, label="Terraform context"))
        elif args.command == "validate-consumer-manifest":
            result = validate_consumer_manifest(
                _load(args.manifest, label="consumer manifest")
            )
            sys.stdout.buffer.write(_canonical_bytes(result))
        elif args.command == "validate-activation-state":
            result = validate_consumer_activation_state(
                _load(args.manifest, label="consumer manifest"),
                _mapping(
                    _load(args.state, label="Terraform state"),
                    label="Terraform state",
                ),
                phase=args.phase,
            )
            sys.stdout.buffer.write(_canonical_bytes(result))
        elif args.command == "sha256":
            print(context_sha256(_load(args.context, label="Terraform context")))
        elif args.command == "registry-sha256":
            try:
                registry_sha256 = consumer_registry_sha256()
            except ConsumerRegistryError as exc:
                raise ContextError("code-owned consumer registry is invalid") from exc
            sys.stdout.buffer.write(
                _canonical_bytes({"sha256": registry_sha256})
            )
        else:
            raise ContextError("Terraform context command is unsupported")
    except ContextError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
