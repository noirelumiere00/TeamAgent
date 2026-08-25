#!/usr/bin/env python3
"""Durable apply-level rollback saga for every code-owned image consumer."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(_REPOSITORY_ROOT / "infra" / "codebuild"))

from image_deployment_consumers import (  # noqa: E402
    ConsumerRegistryError,
    load_consumer_registry,
    validate_consumer_registry,
)
from teamagent_release_approval import canonical_json_bytes  # noqa: E402

from scripts.hmac_rollout_gate import (  # noqa: E402
    RolloutGateError,
    _task_artifact_digest,
)
from scripts.terraform_hmac_payload import task_from_change  # noqa: E402

_REGION = "ap-northeast-1"
_ACCOUNT_ID = "718959508629"
_CLUSTER_NAME = "teamagent-dev"
_CLUSTER_ARN = f"arn:aws:ecs:{_REGION}:{_ACCOUNT_ID}:cluster/{_CLUSTER_NAME}"
_TABLE_NAME = "teamagent-dev-image-deployment-intents"
_RECORD_PREFIX = "ecs-service-apply#"
_RECORD_TYPE = "teamagent.ecs-service-apply-saga"
_ACTIVE_RECORD_ID = f"{_RECORD_PREFIX}active#teamagent-dev-mcp-connect-web"
_ACTIVE_RECORD_TYPE = "teamagent.ecs-service-apply-saga-active"
_SCOPE_ID = "teamagent-dev-mcp-connect-web"
_SCHEMA_VERSION = 1
_MAX_PLAN_BYTES = 4 * 1024 * 1024 * 1024
_MAX_RECORD_BYTES = 350_000
_MAX_INVENTORY_SERVICES = 10_000
_MAX_INVENTORY_PAGES = 1_000
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ECS_ADDRESS_RE = re.compile(r"(?:^|\.)aws_ecs_(?:service|task_definition)\.")
_TASK_DEFINITION_RE = re.compile(
    rf"^arn:aws:ecs:{re.escape(_REGION)}:{_ACCOUNT_ID}:"
    r"task-definition/(?P<family>[a-zA-Z0-9_-]{1,255}):(?P<revision>[1-9][0-9]*)$"
)
_ENDPOINTS = {
    "dynamodb": f"https://dynamodb.{_REGION}.amazonaws.com",
    "ecs": f"https://ecs.{_REGION}.amazonaws.com",
    "events": f"https://events.{_REGION}.amazonaws.com",
    "lambda": f"https://lambda.{_REGION}.amazonaws.com",
}
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LEGACY_TIKTOK_IMAGE_RE = re.compile(
    rf"^{_ACCOUNT_ID}\.dkr\.ecr\.{re.escape(_REGION)}\.amazonaws\.com/"
    r"teamagent-dev-tiktok-acquire@sha256:[0-9a-f]{64}$"
)
_RULE_STATES = frozenset({"DISABLED", "ENABLED"})
_HYBRID_ACTIVATOR_TYPE = "eventbridge_rule_lambda_taskdef_arn_environment"
_RULE_ACTIVATOR_TYPES = frozenset(
    {"eventbridge_rule_ecs_target", _HYBRID_ACTIVATOR_TYPE}
)
_LAMBDA_POINTER_ACTIVATOR_TYPES = frozenset(
    {"lambda_taskdef_arn_environment", _HYBRID_ACTIVATOR_TYPE}
)
_DEPLOYMENT_CONFIGURATION_FIELDS = frozenset(
    {
        "alarms",
        "bakeTimeInMinutes",
        "canaryConfiguration",
        "deploymentCircuitBreaker",
        "lifecycleHooks",
        "linearConfiguration",
        "maximumPercent",
        "minimumHealthyPercent",
        "strategy",
    }
)


class SagaError(RuntimeError):
    """The ECS apply transaction cannot be proven safe or reconciled exactly."""


@dataclass(frozen=True)
class ConsumerSpec:
    key: str
    task_address: str
    task_family: str
    container_name: str
    release_repository: str
    activator_type: str
    activator_identity: str
    activator_address: str
    activator_edge_address: str | None
    task_pointer_address: str | None
    task_pointer_identity: str | None

    @property
    def service_arn(self) -> str:
        if self.activator_type != "ecs_service":
            raise SagaError("non-service consumer has no ECS service ARN")
        return (
            f"arn:aws:ecs:{_REGION}:{_ACCOUNT_ID}:service/{_CLUSTER_NAME}/{self.activator_identity}"
        )

    @property
    def rule_arn(self) -> str:
        if self.activator_type not in _RULE_ACTIVATOR_TYPES:
            raise SagaError("non-EventBridge consumer has no rule ARN")
        return f"arn:aws:events:{_REGION}:{_ACCOUNT_ID}:rule/{self.activator_identity}"

    @property
    def function_name(self) -> str:
        if self.activator_type == "lambda_taskdef_arn_environment":
            return self.activator_identity
        if (
            self.activator_type == _HYBRID_ACTIVATOR_TYPE
            and type(self.task_pointer_identity) is str
        ):
            return self.task_pointer_identity
        raise SagaError("non-Lambda consumer has no function name")

    @property
    def function_arn(self) -> str:
        if self.activator_type not in _LAMBDA_POINTER_ACTIVATOR_TYPES:
            raise SagaError("non-Lambda consumer has no function ARN")
        return f"arn:aws:lambda:{_REGION}:{_ACCOUNT_ID}:function:{self.function_name}"

    @property
    def lambda_pointer_address(self) -> str:
        if self.activator_type == "lambda_taskdef_arn_environment":
            return self.activator_address
        if (
            self.activator_type == _HYBRID_ACTIVATOR_TYPE
            and type(self.task_pointer_address) is str
        ):
            return self.task_pointer_address
        raise SagaError("non-Lambda consumer has no Lambda pointer address")


_ACTIVATOR_RESOURCE_ADDRESSES = {
    "mcp": ("aws_ecs_service.mcp[0]", None),
    "connect_web": ("aws_ecs_service.connect_web[0]", None),
    "openclaw": ("aws_ecs_service.openclaw[0]", None),
    "canary": (
        "aws_cloudwatch_event_rule.canary_hourly[0]",
        "aws_cloudwatch_event_target.canary_run_task[0]",
    ),
    "ingest": (
        "aws_cloudwatch_event_rule.ingest_weekly[0]",
        None,
    ),
    "morning_digest": (
        "aws_cloudwatch_event_rule.morning_digest_weekday[0]",
        "aws_cloudwatch_event_target.morning_digest_run_task[0]",
    ),
    "x_buzz_worker": ("aws_lambda_function.x_dispatch[0]", None),
    "tiktok_acquire": ("aws_lambda_function.tiktok_dispatch[0]", None),
}
_HYBRID_POINTER_RESOURCES = {
    "ingest": (
        "aws_lambda_function.ingest_dispatch[0]",
        "teamagent-dev-ingest-dispatch",
    ),
}
_SAGA_CONSUMER_IDS = frozenset(_ACTIVATOR_RESOURCE_ADDRESSES)
_EXPECTED_ACTIVATOR_COUNTS = {
    "ecs_service": 3,
    "eventbridge_rule_ecs_target": 2,
    _HYBRID_ACTIVATOR_TYPE: 1,
    "lambda_taskdef_arn_environment": 2,
}


def _load_consumer_specs(
    registry: object | None = None,
) -> tuple[dict[str, ConsumerSpec], str]:
    try:
        validated = (
            load_consumer_registry() if registry is None else validate_consumer_registry(registry)
        )
    except ConsumerRegistryError as exc:
        raise SagaError("code-owned consumer registry is invalid") from exc
    raw_consumers = validated.get("consumers")
    if type(raw_consumers) is not list:
        raise SagaError("code-owned consumer registry is invalid")
    registry_ids = [
        item.get("consumer_id") if type(item) is dict else None for item in raw_consumers
    ]
    if (
        any(type(consumer_id) is not str for consumer_id in registry_ids)
        or frozenset(registry_ids) != _SAGA_CONSUMER_IDS
        or len(registry_ids) != len(_SAGA_CONSUMER_IDS)
    ):
        raise SagaError("consumer registry and saga scope differ")

    specs: dict[str, ConsumerSpec] = {}
    activator_counts = {name: 0 for name in _EXPECTED_ACTIVATOR_COUNTS}
    seen_families: set[str] = set()
    seen_activators: set[tuple[str, str]] = set()
    for raw in raw_consumers:
        if type(raw) is not dict:
            raise SagaError("code-owned consumer registry is invalid")
        key = raw.get("consumer_id")
        activator = raw.get("activator")
        if type(key) is not str or type(activator) is not dict:
            raise SagaError("code-owned consumer registry is invalid")
        activator_type = activator.get("type")
        activator_identity = activator.get("identity")
        if (
            type(activator_type) is not str
            or activator_type not in _EXPECTED_ACTIVATOR_COUNTS
            or type(activator_identity) is not str
            or raw.get("provisional") is not False
        ):
            raise SagaError("consumer registry has an unsupported saga activator")
        task_address = raw.get("terraform_task_definition_address")
        task_family = raw.get("ecs_family")
        container_name = raw.get("container_name")
        release_repository = raw.get("release_repository")
        if any(
            type(value) is not str
            for value in (
                task_address,
                task_family,
                container_name,
                release_repository,
            )
        ):
            raise SagaError("code-owned consumer registry is invalid")
        assert type(task_address) is str
        assert type(task_family) is str
        assert type(container_name) is str
        assert type(release_repository) is str
        if (
            task_family in seen_families
            or (
                activator_type,
                activator_identity,
            )
            in seen_activators
        ):
            raise SagaError("consumer registry saga identities are not unique")
        activator_address, activator_edge_address = _ACTIVATOR_RESOURCE_ADDRESSES[key]
        pointer_resource = _HYBRID_POINTER_RESOURCES.get(key)
        task_pointer_address = pointer_resource[0] if pointer_resource is not None else None
        task_pointer_identity = pointer_resource[1] if pointer_resource is not None else None
        if (activator_type == "eventbridge_rule_ecs_target") != (
            activator_edge_address is not None
        ):
            raise SagaError("consumer registry activator address contract differs")
        if (activator_type == _HYBRID_ACTIVATOR_TYPE) != (pointer_resource is not None):
            raise SagaError("consumer registry task pointer address contract differs")
        specs[key] = ConsumerSpec(
            key=key,
            task_address=task_address,
            task_family=task_family,
            container_name=container_name,
            release_repository=release_repository,
            activator_type=activator_type,
            activator_identity=activator_identity,
            activator_address=activator_address,
            activator_edge_address=activator_edge_address,
            task_pointer_address=task_pointer_address,
            task_pointer_identity=task_pointer_identity,
        )
        seen_families.add(task_family)
        seen_activators.add((activator_type, activator_identity))
        activator_counts[activator_type] += 1
    if activator_counts != _EXPECTED_ACTIVATOR_COUNTS:
        raise SagaError("consumer registry activator partition differs")
    registry_sha256 = hashlib.sha256(canonical_json_bytes(validated)).hexdigest()
    return specs, registry_sha256


_CONSUMER_SPECS, _CONSUMER_REGISTRY_SHA256 = _load_consumer_specs()
_SERVICE_SPECS = {
    key: spec for key, spec in _CONSUMER_SPECS.items() if spec.activator_type == "ecs_service"
}
_ALLOWED_ECS_ADDRESSES = frozenset(
    {
        *(spec.task_address for spec in _CONSUMER_SPECS.values()),
        *(spec.activator_address for spec in _SERVICE_SPECS.values()),
    }
)


class AwsCli(Protocol):
    """Small AWS CLI surface used by the saga and replaced by unit-test fakes."""

    def json(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        """Run one AWS operation and decode its object response."""

    def run(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = 120,
    ) -> None:
        """Run one AWS operation whose response body is not needed."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _json_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SagaError(f"{label} returned invalid JSON") from exc
    if type(value) is not dict:
        raise SagaError(f"{label} did not return a JSON object")
    return value


def _canonical_json_value(value: object) -> object:
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise SagaError("non-string JSON object key is invalid")
        return {str(key): _canonical_json_value(item) for key, item in sorted(value.items())}
    if type(value) is list:
        return [_canonical_json_value(item) for item in value]
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise SagaError("non-JSON value is invalid")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_json_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _task_artifact_sha256(value: object) -> str:
    """Hash a registerable task payload while normalizing AWS empty defaults."""

    normalized = _canonical_json_value(value)
    if type(normalized) is not dict:
        raise SagaError("ECS task-definition artifact is invalid")
    definition = normalized.get("taskDefinition") if "taskDefinition" in normalized else normalized
    if type(definition) is not dict:
        raise SagaError("ECS task-definition artifact is invalid")
    for name in ("inferenceAccelerators", "placementConstraints"):
        if definition.get(name) == []:
            definition.pop(name)
    if definition.get("enableFaultInjection") is False:
        definition.pop("enableFaultInjection")
    if definition.get("tags") == []:
        definition.pop("tags")
    if normalized is not definition and normalized.get("tags") == []:
        normalized.pop("tags")
    try:
        return _task_artifact_digest(normalized)
    except RolloutGateError as exc:
        raise SagaError("ECS task-definition artifact is invalid") from exc


def _aws_environment() -> dict[str, str]:
    """Remove ambient endpoint, profile, proxy, and trust-store authority."""

    environment = os.environ.copy()
    rejected = {
        "ALL_PROXY",
        "AWS_CA_BUNDLE",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_DATA_PATH",
        "AWS_DEFAULT_PROFILE",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "BOTO_CONFIG",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    for name in tuple(environment):
        if name.startswith("AWS_ENDPOINT_URL") or name in rejected:
            environment.pop(name, None)
    environment.update(
        {
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_DEFAULT_REGION": _REGION,
            "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
            "AWS_PAGER": "",
            "AWS_REGION": _REGION,
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "LC_ALL": "C",
        }
    )
    return environment


def _terraform_environment() -> dict[str, str]:
    """Make ``terraform show`` independent of inherited Terraform controls."""

    environment = os.environ.copy()
    exact = {
        "TERRAFORM_CONFIG",
        "TF_CLI_CONFIG_FILE",
        "TF_DATA_DIR",
        "TF_INPUT",
        "TF_LOG_PATH",
        "TF_PLUGIN_CACHE_DIR",
        "TF_REATTACH_PROVIDERS",
        "TF_WORKSPACE",
    }
    for name in tuple(environment):
        if (
            name in exact
            or name.startswith("TF_CLI_ARGS")
            or name.startswith("TF_LOG")
            or name.startswith("TF_VAR_")
        ):
            environment.pop(name, None)
    environment.update(
        {
            "CHECKPOINT_DISABLE": "1",
            "LC_ALL": "C",
            "TF_CLI_CONFIG_FILE": "/dev/null",
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
        }
    )
    return environment


class _SubprocessAwsCli:
    def __init__(self, aws_bin: Path) -> None:
        self.aws_bin = _trusted_executable(aws_bin, label="AWS CLI")

    def _command(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str],
    ) -> list[str]:
        endpoint = _ENDPOINTS.get(service)
        if endpoint is None:
            raise SagaError("AWS service is outside the saga endpoint allowlist")
        return [
            str(self.aws_bin),
            "--region",
            _REGION,
            "--endpoint-url",
            endpoint,
            "--no-cli-pager",
            "--no-paginate",
            service,
            operation,
            *arguments,
            "--output",
            "json",
        ]

    def _execute(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> str:
        if timeout_seconds <= 0:
            raise SagaError("AWS CLI timeout is invalid")
        try:
            completed = subprocess.run(
                self._command(service, operation, arguments),
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=_aws_environment(),
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise SagaError("AWS saga operation failed without exposing details") from exc
        return completed.stdout

    def json(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        return _json_object(
            self._execute(
                service,
                operation,
                arguments,
                timeout_seconds=timeout_seconds,
            ),
            label=f"AWS {service} {operation}",
        )

    def run(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = 120,
    ) -> None:
        self._execute(
            service,
            operation,
            arguments,
            timeout_seconds=timeout_seconds,
        )


def _descriptor_digest(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _descriptor_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _trusted_executable(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise SagaError(f"{label} executable is unavailable") from exc
    if (
        not resolved.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or not metadata.st_mode & stat.S_IXUSR
    ):
        raise SagaError(f"{label} executable is not a trusted regular file")
    return resolved


def _terraform_show_descriptor(
    descriptor: int,
    *,
    terraform_bin: Path,
) -> dict[str, Any]:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        descriptor_root = Path("/dev/fd")
    descriptor_path = descriptor_root / str(descriptor)
    try:
        completed = subprocess.run(
            [str(terraform_bin), "show", "-json", str(descriptor_path)],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            cwd=_REPOSITORY_ROOT / "infra" / "terraform",
            env=_terraform_environment(),
            pass_fds=(descriptor,),
            timeout=120,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SagaError("saved plan is unreadable") from exc
    return _json_object(completed.stdout, label="terraform show")


def _load_saved_plan(
    path: Path,
    *,
    expected_sha256: str,
    terraform_bin: Path,
) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > _MAX_PLAN_BYTES
            or mode & 0o077
            or mode & 0o111
            or not mode & stat.S_IRUSR
        ):
            raise SagaError("saved plan is not one bounded private regular file")
        if not hmac.compare_digest(_descriptor_digest(descriptor), expected_sha256):
            raise SagaError("saved plan digest differs")
        plan = _terraform_show_descriptor(
            descriptor,
            terraform_bin=terraform_bin,
        )
        after = os.fstat(descriptor)
        if _descriptor_identity(after) != _descriptor_identity(before) or not hmac.compare_digest(
            _descriptor_digest(descriptor), expected_sha256
        ):
            raise SagaError("saved plan changed while it was inspected")
        return plan
    except OSError as exc:
        raise SagaError("saved plan is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _actions(item: Mapping[str, Any], *, label: str) -> tuple[str, ...]:
    change = item.get("change")
    actions = change.get("actions") if type(change) is dict else None
    if (
        type(actions) is not list
        or not actions
        or any(type(action) is not str for action in actions)
    ):
        raise SagaError(f"{label} actions are invalid")
    return tuple(actions)


def _resource_after(
    item: Mapping[str, Any],
    *,
    label: str,
    allowed_actions: frozenset[tuple[str, ...]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    actions = _actions(item, label=label)
    change = item.get("change")
    assert type(change) is dict
    if actions not in allowed_actions or change.get("importing") not in (None, {}):
        raise SagaError(f"{label} mutation is not allowed")
    after = change.get("after")
    after_unknown = change.get("after_unknown", {})
    if type(after) is not dict or type(after_unknown) is not dict:
        raise SagaError(f"{label} planned values are invalid")
    return after, after_unknown


def _validate_resource_identity(
    item: Mapping[str, Any],
    *,
    address: str,
    expected_type: str,
) -> None:
    expected_name = address.split(".", maxsplit=1)[1].split("[", maxsplit=1)[0]
    expected_index: int | None = 0 if address.endswith("[0]") else None
    if (
        item.get("address") != address
        or item.get("mode") != "managed"
        or item.get("type") != expected_type
        or item.get("name") != expected_name
        or item.get("index") != expected_index
        or item.get("deposed") is not None
        or item.get("previous_address") is not None
    ):
        raise SagaError("saved plan saga resource identity is not exact")


def _resource_before(item: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    change = item.get("change")
    before = change.get("before") if type(change) is dict else None
    if type(before) is not dict:
        raise SagaError(f"{label} prior values are invalid")
    return before


def _nested_value(value: object, path: Sequence[object]) -> object:
    current = value
    for part in path:
        if type(part) is str:
            if type(current) is not dict:
                return None
            current = current.get(part)
        else:
            if type(part) is not int or type(current) is not list or part >= len(current):
                return None
            current = current[part]
    return current


def _planned_task_pointer(
    value: object,
    unknown: object,
    *,
    spec: ConsumerSpec,
) -> dict[str, str]:
    if type(value) is str:
        if unknown not in (None, False):
            raise SagaError("saved plan task-definition value is ambiguously known")
        return {
            "kind": "arn",
            "taskDefinition": _validate_task_definition(
                value,
                expected_family=spec.task_family,
            ),
        }
    if value is not None or unknown is not True:
        raise SagaError("saved plan does not determine the desired task definition")
    return {
        "kind": "artifact",
        "family": spec.task_family,
    }


def _task_pointer_changed(before: str, after: Mapping[str, Any]) -> bool:
    return after.get("kind") != "arn" or after.get("taskDefinition") != before


def _container_image(
    value: object,
    *,
    spec: ConsumerSpec,
    label: str,
    pre_media_cutover_sync_image: str = "",
) -> tuple[str, str]:
    if type(value) is not list:
        raise SagaError(f"{label} container definitions are invalid")
    matches = [
        container
        for container in value
        if type(container) is dict and container.get("name") == spec.container_name
    ]
    if len(matches) != 1:
        raise SagaError(f"{label} registry container is not exact")
    image = matches[0].get("image")
    if (
        spec.key == "tiktok_acquire"
        and pre_media_cutover_sync_image
        and _LEGACY_TIKTOK_IMAGE_RE.fullmatch(pre_media_cutover_sync_image) is not None
    ):
        if type(image) is not str or not hmac.compare_digest(
            image,
            pre_media_cutover_sync_image,
        ):
            raise SagaError(f"{label} pre-cutover TikTok image is not exact")
        image_digest = image.rsplit("@", maxsplit=1)[1]
    else:
        prefix = f"{_ACCOUNT_ID}.dkr.ecr.{_REGION}.amazonaws.com/{spec.release_repository}@"
        if type(image) is not str or not image.startswith(prefix):
            raise SagaError(f"{label} registry image is not exact")
        image_digest = image.removeprefix(prefix)
    if _IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
        raise SagaError(f"{label} registry image digest is invalid")
    assert type(image) is str
    return image, image_digest


def _planned_task(
    item: Mapping[str, Any],
    *,
    spec: ConsumerSpec,
    pre_media_cutover_sync_image: str = "",
) -> tuple[str, str]:
    task_after, _task_unknown = _resource_after(
        item,
        label=spec.task_address,
        allowed_actions=frozenset(
            {
                ("no-op",),
                ("read",),
                ("create",),
                ("update",),
                ("delete", "create"),
                ("create", "delete"),
            }
        ),
    )
    if task_after.get("family") != spec.task_family:
        raise SagaError("saved plan ECS task-definition identity differs")
    try:
        payload = task_from_change(task_after, task=spec.key)
    except RolloutGateError as exc:
        raise SagaError("saved plan task-definition payload is invalid") from exc
    artifact_sha256 = _task_artifact_sha256(payload)
    image, image_digest = _container_image(
        payload.get("containerDefinitions"),
        spec=spec,
        label="saved plan task definition",
        pre_media_cutover_sync_image=pre_media_cutover_sync_image,
    )
    del image
    return artifact_sha256, image_digest


def _service_plan(
    item: Mapping[str, Any],
    *,
    spec: ConsumerSpec,
) -> tuple[dict[str, Any], bool]:
    after, unknown = _resource_after(
        item,
        label=spec.activator_address,
        allowed_actions=frozenset({("no-op",), ("read",), ("update",)}),
    )
    before = _resource_before(item, label=spec.activator_address)
    if (
        before.get("name") != spec.activator_identity
        or after.get("name") != spec.activator_identity
        or before.get("cluster") not in {_CLUSTER_NAME, _CLUSTER_ARN}
        or after.get("cluster") not in {_CLUSTER_NAME, _CLUSTER_ARN}
        or before.get("desired_count") != 1
        or after.get("desired_count") != 1
    ):
        raise SagaError("saved plan ECS service identity differs")
    before_task = _validate_task_definition(
        before.get("task_definition"),
        expected_family=spec.task_family,
    )
    pointer = _planned_task_pointer(
        after.get("task_definition"),
        unknown.get("task_definition"),
        spec=spec,
    )
    return (
        {
            "desiredCount": 1,
            "taskDefinition": pointer,
        },
        _task_pointer_changed(before_task, pointer),
    )


def _eventbridge_rule_plan(
    rule_item: Mapping[str, Any],
    *,
    spec: ConsumerSpec,
) -> tuple[str, str]:
    rule_after, rule_unknown = _resource_after(
        rule_item,
        label=spec.activator_address,
        allowed_actions=frozenset({("no-op",), ("read",), ("update",)}),
    )
    rule_before = _resource_before(rule_item, label=spec.activator_address)
    before_state = rule_before.get("state")
    after_state = rule_after.get("state")
    if (
        rule_before.get("name") != spec.activator_identity
        or rule_after.get("name") != spec.activator_identity
        or rule_before.get("event_bus_name") != "default"
        or rule_after.get("event_bus_name") != "default"
        or before_state not in _RULE_STATES
        or after_state not in _RULE_STATES
        or rule_unknown.get("state") not in (None, False)
    ):
        raise SagaError("saved plan EventBridge rule identity is invalid")
    assert type(before_state) is str
    assert type(after_state) is str
    return before_state, after_state


def _eventbridge_plan(
    rule_item: Mapping[str, Any],
    target_item: Mapping[str, Any],
    *,
    spec: ConsumerSpec,
) -> tuple[dict[str, Any], bool]:
    before_state, after_state = _eventbridge_rule_plan(rule_item, spec=spec)

    if spec.activator_edge_address is None:
        raise SagaError("EventBridge activator edge is absent")
    target_after, target_unknown = _resource_after(
        target_item,
        label=spec.activator_edge_address,
        allowed_actions=frozenset({("no-op",), ("read",), ("update",)}),
    )
    target_before = _resource_before(target_item, label=spec.activator_edge_address)
    if (
        target_before.get("rule") != spec.activator_identity
        or target_after.get("rule") != spec.activator_identity
        or target_before.get("event_bus_name") != "default"
        or target_after.get("event_bus_name") != "default"
        or target_before.get("arn") != _CLUSTER_ARN
        or target_after.get("arn") != _CLUSTER_ARN
    ):
        raise SagaError("saved plan EventBridge target identity is invalid")
    before_task = _validate_task_definition(
        _nested_value(target_before, ("ecs_target", 0, "task_definition_arn")),
        expected_family=spec.task_family,
    )
    pointer = _planned_task_pointer(
        _nested_value(target_after, ("ecs_target", 0, "task_definition_arn")),
        _nested_value(target_unknown, ("ecs_target", 0, "task_definition_arn")),
        spec=spec,
    )
    return (
        {
            "state": after_state,
            "taskDefinition": pointer,
        },
        before_state != after_state or _task_pointer_changed(before_task, pointer),
    )


def _lambda_variables(
    value: object,
    *,
    label: str,
    allow_unknown_task: bool = False,
) -> dict[str, Any]:
    variables = _nested_value(value, ("environment", 0, "variables"))
    if type(variables) is not dict or any(
        type(name) is not str
        or (
            type(item) is not str
            and not (allow_unknown_task and name == "TASKDEF_ARN" and item is None)
        )
        for name, item in variables.items()
    ):
        raise SagaError(f"{label} Lambda environment is invalid")
    return dict(variables)


def _lambda_plan(
    item: Mapping[str, Any],
    *,
    spec: ConsumerSpec,
) -> tuple[dict[str, Any], bool]:
    address = spec.lambda_pointer_address
    after, unknown = _resource_after(
        item,
        label=address,
        allowed_actions=frozenset({("no-op",), ("read",), ("update",)}),
    )
    before = _resource_before(item, label=address)
    if (
        before.get("function_name") != spec.function_name
        or after.get("function_name") != spec.function_name
    ):
        raise SagaError("saved plan Lambda identity is invalid")
    before_variables = _lambda_variables(before, label="saved plan prior")
    after_variables = _lambda_variables(
        after,
        label="saved plan",
        allow_unknown_task=True,
    )
    before_task = _validate_task_definition(
        before_variables.get("TASKDEF_ARN"),
        expected_family=spec.task_family,
    )
    pointer = _planned_task_pointer(
        after_variables.get("TASKDEF_ARN"),
        _nested_value(unknown, ("environment", 0, "variables", "TASKDEF_ARN")),
        spec=spec,
    )
    return (
        {"taskDefinition": pointer},
        _task_pointer_changed(before_task, pointer),
    )


def _hybrid_plan(
    rule_item: Mapping[str, Any],
    lambda_item: Mapping[str, Any],
    *,
    spec: ConsumerSpec,
) -> tuple[dict[str, Any], bool]:
    before_state, after_state = _eventbridge_rule_plan(rule_item, spec=spec)
    pointer_activation, pointer_changed = _lambda_plan(lambda_item, spec=spec)
    return (
        {
            "state": after_state,
            "taskDefinition": pointer_activation["taskDefinition"],
        },
        before_state != after_state or pointer_changed,
    )


@dataclass(frozen=True)
class PlanAnalysis:
    binding: dict[str, Any]
    specs: dict[str, ConsumerSpec]
    registry_sha256: str
    pre_media_cutover_sync_image: str


def _pre_media_cutover_sync_image(
    plan: Mapping[str, Any],
    *,
    specs: Mapping[str, ConsumerSpec],
    registry_sha256: str,
) -> str:
    """Return the exact plan-bound legacy image only for the pre-cutover sync state."""

    variables = plan.get("variables")
    if type(variables) is not dict:
        return ""

    def variable_value(name: str) -> object:
        binding = variables.get(name)
        return binding.get("value") if type(binding) is dict else None

    runtime_guard = variable_value("runtime_guard_live")
    manifest = variable_value("image_deployment_consumer_manifest")
    if (
        type(runtime_guard) is not dict
        or runtime_guard.get("mode") != "sync"
        or variable_value("media_worker_image") != ""
        or variable_value("tiktok_acquire_image") != ""
        or type(manifest) is not dict
        or manifest.get("schema_version") != 1
        or manifest.get("registry_sha256") != registry_sha256
        or manifest.get("mode") != "no-image-transition"
    ):
        return ""

    candidate = runtime_guard.get("live_tiktok_image")
    if (
        type(candidate) is not str
        or _LEGACY_TIKTOK_IMAGE_RE.fullmatch(candidate) is None
        or runtime_guard.get("desired_tiktok_image") != candidate
    ):
        return ""

    consumers = manifest.get("consumers")
    if type(consumers) is not list or len(consumers) != len(specs):
        return ""
    consumer_ids = [row.get("consumer_id") if type(row) is dict else None for row in consumers]
    if (
        any(type(consumer_id) is not str for consumer_id in consumer_ids)
        or len(set(consumer_ids)) != len(consumer_ids)
        or frozenset(consumer_ids) != frozenset(specs)
    ):
        return ""
    row = next(
        item
        for item in consumers
        if type(item) is dict and item.get("consumer_id") == "tiktok_acquire"
    )
    spec = specs["tiktok_acquire"]
    activator = row.get("activator")
    live = row.get("live")
    before = row.get("before")
    after = row.get("after")
    if (
        row.get("terraform_task_definition_address") != spec.task_address
        or row.get("ecs_family") != spec.task_family
        or row.get("container_name") != spec.container_name
        or row.get("release_repository") != spec.release_repository
        or type(activator) is not dict
        or activator.get("type") != spec.activator_type
        or activator.get("identity") != spec.activator_identity
        or type(live) is not dict
        or live != before
        or live != after
        or live.get("image") != candidate
    ):
        return ""
    return candidate


def _analyze_plan(plan: Mapping[str, Any]) -> PlanAnalysis:
    specs, registry_sha256 = _load_consumer_specs()
    pre_media_cutover_sync_image = _pre_media_cutover_sync_image(
        plan,
        specs=specs,
        registry_sha256=registry_sha256,
    )
    format_version = plan.get("format_version")
    if (
        type(format_version) is not str
        or re.fullmatch(r"1\.[0-9]+", format_version) is None
        or plan.get("complete") is not True
        or plan.get("errored") is not False
    ):
        raise SagaError("saved plan is incomplete or has an unsupported JSON format")
    raw_changes = plan.get("resource_changes")
    if type(raw_changes) is not list:
        raise SagaError("saved plan resource changes are unavailable")

    expected_resource_types: dict[str, str] = {}
    for spec in specs.values():
        expected_resource_types[spec.task_address] = "aws_ecs_task_definition"
        expected_resource_types[spec.activator_address] = {
            "ecs_service": "aws_ecs_service",
            "eventbridge_rule_ecs_target": "aws_cloudwatch_event_rule",
            _HYBRID_ACTIVATOR_TYPE: "aws_cloudwatch_event_rule",
            "lambda_taskdef_arn_environment": "aws_lambda_function",
        }[spec.activator_type]
        if spec.activator_edge_address is not None:
            expected_resource_types[spec.activator_edge_address] = "aws_cloudwatch_event_target"
        if spec.task_pointer_address is not None:
            expected_resource_types[spec.task_pointer_address] = "aws_lambda_function"

    matches: dict[str, dict[str, Any]] = {}
    for raw_item in raw_changes:
        if type(raw_item) is not dict:
            raise SagaError("saved plan resource change is invalid")
        address = raw_item.get("address")
        resource_type = raw_item.get("type")
        if type(address) is not str:
            raise SagaError("saved plan resource address is invalid")
        is_ecs = (
            resource_type in {"aws_ecs_service", "aws_ecs_task_definition"}
            or _ECS_ADDRESS_RE.search(address) is not None
        )
        if not is_ecs and address not in expected_resource_types:
            continue
        actions = _actions(raw_item, label=address)
        if address not in expected_resource_types:
            if actions not in {("no-op",), ("read",)}:
                if is_ecs:
                    raise SagaError("saved plan mutates an ECS resource outside the saga scope")
            continue
        if address in matches:
            raise SagaError("saved plan repeats an ECS baseline address")
        _validate_resource_identity(
            raw_item,
            address=address,
            expected_type=expected_resource_types[address],
        )
        matches[address] = raw_item

    if frozenset(matches) != frozenset(expected_resource_types):
        raise SagaError("saved plan does not contain every exact consumer baseline address")

    planned_consumers: dict[str, dict[str, Any]] = {}
    for key, spec in specs.items():
        artifact_sha256, image_digest = _planned_task(
            matches[spec.task_address],
            spec=spec,
            pre_media_cutover_sync_image=pre_media_cutover_sync_image,
        )
        if spec.activator_type == "ecs_service":
            activation, activation_changed = _service_plan(
                matches[spec.activator_address],
                spec=spec,
            )
        elif spec.activator_type == "eventbridge_rule_ecs_target":
            if spec.activator_edge_address is None:
                raise SagaError("EventBridge activator edge is absent")
            activation, activation_changed = _eventbridge_plan(
                matches[spec.activator_address],
                matches[spec.activator_edge_address],
                spec=spec,
            )
        elif spec.activator_type == _HYBRID_ACTIVATOR_TYPE:
            if spec.task_pointer_address is None:
                raise SagaError("hybrid activator task pointer is absent")
            activation, activation_changed = _hybrid_plan(
                matches[spec.activator_address],
                matches[spec.task_pointer_address],
                spec=spec,
            )
        elif spec.activator_type == "lambda_taskdef_arn_environment":
            activation, activation_changed = _lambda_plan(
                matches[spec.activator_address],
                spec=spec,
            )
        else:
            raise SagaError("consumer registry activator type is unsupported")
        planned_consumers[key] = {
            "activatorType": spec.activator_type,
            "activation": activation,
            "activationChanged": activation_changed,
            "imageDigest": image_digest,
            "taskDefinitionSha256": artifact_sha256,
        }

    return PlanAnalysis(
        binding={
            "schemaVersion": _SCHEMA_VERSION,
            "consumerRegistrySha256": registry_sha256,
            "preMediaCutoverSyncImage": pre_media_cutover_sync_image,
            "consumers": planned_consumers,
        },
        specs=specs,
        registry_sha256=registry_sha256,
        pre_media_cutover_sync_image=pre_media_cutover_sync_image,
    )


def _validate_task_definition(value: object, *, expected_family: str) -> str:
    if type(value) is not str:
        raise SagaError("ECS task definition is invalid")
    match = _TASK_DEFINITION_RE.fullmatch(value)
    if match is None or match.group("family") != expected_family:
        raise SagaError("ECS task definition is outside the exact service family")
    return value


def _canonical_deployment_configuration(value: object) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) - _DEPLOYMENT_CONFIGURATION_FIELDS:
        raise SagaError("ECS deployment configuration is invalid")
    maximum = value.get("maximumPercent")
    minimum = value.get("minimumHealthyPercent")
    circuit = value.get("deploymentCircuitBreaker")
    if (
        type(maximum) is not int
        or type(minimum) is not int
        or not 0 <= minimum <= maximum <= 100_000
        or type(circuit) is not dict
        or frozenset(circuit) != {"enable", "rollback"}
        or type(circuit.get("enable")) is not bool
        or type(circuit.get("rollback")) is not bool
    ):
        raise SagaError("ECS deployment configuration is incomplete")
    alarms = value.get("alarms")
    if alarms is not None:
        if type(alarms) is not dict or frozenset(alarms) != {
            "alarmNames",
            "enable",
            "rollback",
        }:
            raise SagaError("ECS deployment alarm configuration is invalid")
        names = alarms.get("alarmNames")
        if (
            type(names) is not list
            or any(type(name) is not str or not name for name in names)
            or len(set(names)) != len(names)
            or type(alarms.get("enable")) is not bool
            or type(alarms.get("rollback")) is not bool
        ):
            raise SagaError("ECS deployment alarm configuration is invalid")
        alarms = {
            "alarmNames": sorted(names),
            "enable": alarms["enable"],
            "rollback": alarms["rollback"],
        }
    for name in ("bakeTimeInMinutes",):
        raw_number = value.get(name)
        if raw_number is not None and (type(raw_number) is not int or raw_number < 0):
            raise SagaError("ECS deployment timing is invalid")
    strategy = value.get("strategy")
    if strategy is not None and strategy not in {"BLUE_GREEN", "CANARY", "LINEAR", "ROLLING"}:
        raise SagaError("ECS deployment strategy is invalid")
    hooks = value.get("lifecycleHooks")
    if hooks is not None and type(hooks) is not list:
        raise SagaError("ECS deployment lifecycle hooks are invalid")

    canonical_value = _canonical_json_value(value)
    if type(canonical_value) is not dict:
        raise SagaError("ECS deployment configuration is invalid")
    canonical: dict[str, Any] = canonical_value
    if alarms is not None:
        canonical["alarms"] = alarms
    return canonical


def _canonical_network_configuration(value: object) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != {"awsvpcConfiguration"}:
        raise SagaError("ECS network configuration is invalid")
    awsvpc = value.get("awsvpcConfiguration")
    if type(awsvpc) is not dict or frozenset(awsvpc) != {
        "assignPublicIp",
        "securityGroups",
        "subnets",
    }:
        raise SagaError("ECS awsvpc configuration is invalid")
    subnets = awsvpc.get("subnets")
    security_groups = awsvpc.get("securityGroups")
    public_ip = awsvpc.get("assignPublicIp")
    if (
        type(subnets) is not list
        or not subnets
        or any(type(item) is not str or not item for item in subnets)
        or len(set(subnets)) != len(subnets)
        or type(security_groups) is not list
        or not security_groups
        or any(type(item) is not str or not item for item in security_groups)
        or len(set(security_groups)) != len(security_groups)
        or public_ip not in {"ENABLED", "DISABLED"}
    ):
        raise SagaError("ECS awsvpc configuration is incomplete")
    return {
        "awsvpcConfiguration": {
            "assignPublicIp": public_ip,
            "securityGroups": sorted(security_groups),
            "subnets": sorted(subnets),
        }
    }


def _canonical_service(raw: object, *, spec: ConsumerSpec) -> dict[str, Any]:
    if type(raw) is not dict:
        raise SagaError("ECS service response is invalid")
    if (
        raw.get("serviceArn") != spec.service_arn
        or raw.get("serviceName") != spec.activator_identity
        or raw.get("clusterArn") != _CLUSTER_ARN
        or raw.get("status") != "ACTIVE"
        or raw.get("launchType") != "FARGATE"
        or raw.get("schedulingStrategy") != "REPLICA"
    ):
        raise SagaError("ECS service identity is not exact")
    controller = raw.get("deploymentController")
    if type(controller) is not dict or controller.get("type") != "ECS":
        raise SagaError("ECS deployment controller is not exact")
    task_definition = _validate_task_definition(
        raw.get("taskDefinition"),
        expected_family=spec.task_family,
    )
    desired_count = raw.get("desiredCount")
    if type(desired_count) is not int or desired_count != 1:
        raise SagaError("ECS desired count is invalid")
    return {
        "taskDefinition": task_definition,
        "deploymentConfiguration": _canonical_deployment_configuration(
            raw.get("deploymentConfiguration")
        ),
        "networkConfiguration": _canonical_network_configuration(raw.get("networkConfiguration")),
        "desiredCount": desired_count,
    }


def _list_service_arns(cli: AwsCli) -> set[str]:
    service_arns: set[str] = set()
    token: str | None = None
    seen_tokens: set[str] = set()
    for _page_number in range(_MAX_INVENTORY_PAGES):
        arguments = ["--cluster", _CLUSTER_ARN, "--max-results", "100"]
        if token is not None:
            arguments.extend(["--next-token", token])
        response = cli.json("ecs", "list-services", arguments)
        if frozenset(response) - {"nextToken", "serviceArns"}:
            raise SagaError("ECS service inventory has unknown fields")
        page = response.get("serviceArns")
        if type(page) is not list or any(type(item) is not str for item in page):
            raise SagaError("ECS service inventory is invalid")
        if len(service_arns) + len(page) > _MAX_INVENTORY_SERVICES:
            raise SagaError("ECS service inventory exceeds its durable bound")
        for service_arn in page:
            if service_arn in service_arns:
                raise SagaError("ECS service inventory repeats an identity")
            service_arns.add(service_arn)
        next_token = response.get("nextToken")
        if next_token is None:
            return service_arns
        if type(next_token) is not str or not next_token or next_token in seen_tokens:
            raise SagaError("ECS service inventory pagination is invalid")
        seen_tokens.add(next_token)
        token = next_token
    raise SagaError("ECS service inventory pagination exceeds its bound")


def _read_services(
    cli: AwsCli,
    specs: Mapping[str, ConsumerSpec],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    service_specs = {
        key: spec for key, spec in specs.items() if spec.activator_type == "ecs_service"
    }
    expected_arns = {spec.service_arn for spec in service_specs.values()}
    inventory = _list_service_arns(cli)
    if not expected_arns.issubset(inventory):
        raise SagaError("an exact ECS baseline service is absent from inventory")
    response = cli.json(
        "ecs",
        "describe-services",
        [
            "--cluster",
            _CLUSTER_ARN,
            "--services",
            *(spec.service_arn for spec in service_specs.values()),
        ],
    )
    if frozenset(response) - {"failures", "services"}:
        raise SagaError("ECS service description has unknown fields")
    services = response.get("services")
    failures = response.get("failures")
    if type(services) is not list or failures != [] or len(services) != len(service_specs):
        raise SagaError("ECS service description is incomplete")
    by_arn: dict[str, dict[str, Any]] = {}
    for raw in services:
        if type(raw) is not dict or type(raw.get("serviceArn")) is not str:
            raise SagaError("ECS service description is invalid")
        service_arn = raw["serviceArn"]
        if service_arn in by_arn:
            raise SagaError("ECS service description repeats an identity")
        by_arn[service_arn] = raw
    if frozenset(by_arn) != expected_arns:
        raise SagaError("ECS service description returned the wrong identities")
    baseline: dict[str, dict[str, Any]] = {}
    raw_by_key: dict[str, dict[str, Any]] = {}
    for key, spec in service_specs.items():
        raw = by_arn[spec.service_arn]
        baseline[key] = _canonical_service(raw, spec=spec)
        raw_by_key[key] = raw
    return baseline, raw_by_key


def _list_event_targets(cli: AwsCli, *, spec: ConsumerSpec) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    for _page_number in range(_MAX_INVENTORY_PAGES):
        request: dict[str, object] = {
            "EventBusName": "default",
            "Limit": 100,
            "Rule": spec.activator_identity,
        }
        if token is not None:
            request["NextToken"] = token
        response = cli.json(
            "events",
            "list-targets-by-rule",
            [
                "--cli-input-json",
                json.dumps(request, separators=(",", ":"), sort_keys=True),
            ],
        )
        if frozenset(response) - {"NextToken", "Targets"}:
            raise SagaError("EventBridge target inventory has unknown fields")
        page = response.get("Targets")
        if type(page) is not list or any(type(item) is not dict for item in page):
            raise SagaError("EventBridge target inventory is invalid")
        if len(targets) + len(page) > _MAX_INVENTORY_SERVICES:
            raise SagaError("EventBridge target inventory exceeds its durable bound")
        targets.extend(page)
        next_token = response.get("NextToken")
        if next_token is None:
            return targets
        if type(next_token) is not str or not next_token or next_token in seen_tokens:
            raise SagaError("EventBridge target pagination is invalid")
        seen_tokens.add(next_token)
        token = next_token
    raise SagaError("EventBridge target pagination exceeds its bound")


def _canonical_eventbridge_rule_state(raw: object, *, spec: ConsumerSpec) -> str:
    if (
        type(raw) is not dict
        or raw.get("Name") != spec.activator_identity
        or raw.get("Arn") != spec.rule_arn
        or raw.get("EventBusName") != "default"
        or raw.get("State") not in _RULE_STATES
    ):
        raise SagaError("EventBridge activation identity is not exact")
    state = raw["State"]
    assert type(state) is str
    return state


def _canonical_eventbridge_activation(
    raw: object,
    *,
    spec: ConsumerSpec,
) -> dict[str, Any]:
    if type(raw) is not dict or frozenset(raw) != {"rule", "target"}:
        raise SagaError("EventBridge activation response is invalid")
    rule = raw.get("rule")
    target = raw.get("target")
    state = _canonical_eventbridge_rule_state(rule, spec=spec)
    if type(target) is not dict:
        raise SagaError("EventBridge activation identity is not exact")
    target_id = target.get("Id")
    ecs_parameters = target.get("EcsParameters")
    task_definition = (
        ecs_parameters.get("TaskDefinitionArn") if type(ecs_parameters) is dict else None
    )
    if (
        type(target_id) is not str
        or not target_id
        or target.get("Arn") != _CLUSTER_ARN
        or type(ecs_parameters) is not dict
    ):
        raise SagaError("EventBridge ECS target identity is not exact")
    normalized_target = _canonical_json_value(target)
    if type(normalized_target) is not dict:
        raise SagaError("EventBridge ECS target is invalid")
    return {
        "type": spec.activator_type,
        "identity": spec.activator_identity,
        "ruleArn": spec.rule_arn,
        "state": state,
        "taskDefinition": _validate_task_definition(
            task_definition,
            expected_family=spec.task_family,
        ),
        "target": normalized_target,
    }


def _read_eventbridge_activation(
    cli: AwsCli,
    *,
    spec: ConsumerSpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rule = cli.json(
        "events",
        "describe-rule",
        [
            "--name",
            spec.activator_identity,
            "--event-bus-name",
            "default",
        ],
    )
    targets = _list_event_targets(cli, spec=spec)
    if len(targets) != 1:
        raise SagaError("EventBridge ECS target inventory is not exact")
    raw = {"rule": rule, "target": targets[0]}
    return _canonical_eventbridge_activation(raw, spec=spec), raw


def _canonical_lambda_activation(
    raw: object,
    *,
    spec: ConsumerSpec,
) -> dict[str, Any]:
    if type(raw) is not dict:
        raise SagaError("Lambda activation response is invalid")
    environment = raw.get("Environment")
    variables = environment.get("Variables") if type(environment) is dict else None
    if (
        raw.get("FunctionName") != spec.function_name
        or raw.get("FunctionArn") != spec.function_arn
        or type(variables) is not dict
        or any(type(name) is not str or type(value) is not str for name, value in variables.items())
    ):
        raise SagaError("Lambda activation identity is not exact")
    task_definition = _validate_task_definition(
        variables.get("TASKDEF_ARN"),
        expected_family=spec.task_family,
    )
    return {
        "type": spec.activator_type,
        "identity": spec.function_name,
        "functionArn": spec.function_arn,
        "taskDefinition": task_definition,
        "environmentVariables": {
            str(name): str(value) for name, value in sorted(variables.items())
        },
    }


def _read_lambda_activation(
    cli: AwsCli,
    *,
    spec: ConsumerSpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = cli.json(
        "lambda",
        "get-function-configuration",
        ["--function-name", spec.function_name],
    )
    return _canonical_lambda_activation(raw, spec=spec), raw


def _canonical_hybrid_activation(
    raw: object,
    *,
    spec: ConsumerSpec,
) -> dict[str, Any]:
    if (
        spec.activator_type != _HYBRID_ACTIVATOR_TYPE
        or type(raw) is not dict
        or frozenset(raw) != {"lambda", "rule"}
    ):
        raise SagaError("hybrid activation response is invalid")
    state = _canonical_eventbridge_rule_state(raw.get("rule"), spec=spec)
    lambda_activation = _canonical_lambda_activation(raw.get("lambda"), spec=spec)
    return {
        "type": spec.activator_type,
        "identity": spec.activator_identity,
        "ruleArn": spec.rule_arn,
        "state": state,
        "functionArn": lambda_activation["functionArn"],
        "taskDefinition": lambda_activation["taskDefinition"],
        "environmentVariables": lambda_activation["environmentVariables"],
    }


def _read_hybrid_activation(
    cli: AwsCli,
    *,
    spec: ConsumerSpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rule = cli.json(
        "events",
        "describe-rule",
        [
            "--name",
            spec.activator_identity,
            "--event-bus-name",
            "default",
        ],
    )
    lambda_configuration = cli.json(
        "lambda",
        "get-function-configuration",
        ["--function-name", spec.function_name],
    )
    raw = {"lambda": lambda_configuration, "rule": rule}
    return _canonical_hybrid_activation(raw, spec=spec), raw


def _read_task_definition(
    cli: AwsCli,
    *,
    spec: ConsumerSpec,
    task_definition: str,
    pre_media_cutover_sync_image: str = "",
) -> dict[str, str]:
    response = cli.json(
        "ecs",
        "describe-task-definition",
        [
            "--task-definition",
            task_definition,
            "--include",
            "TAGS",
        ],
    )
    if frozenset(response) - {"tags", "taskDefinition"}:
        raise SagaError("ECS task-definition response has unknown fields")
    definition = response.get("taskDefinition")
    if type(definition) is not dict:
        raise SagaError("ECS task-definition response is invalid")
    revision = definition.get("revision")
    expected_revision = int(task_definition.rsplit(":", maxsplit=1)[1])
    if (
        definition.get("taskDefinitionArn") != task_definition
        or definition.get("family") != spec.task_family
        or definition.get("status") != "ACTIVE"
        or type(revision) is not int
        or revision != expected_revision
    ):
        raise SagaError("ECS task-definition identity differs")
    task_definition_sha256 = _task_artifact_sha256(response)
    image, image_digest = _container_image(
        definition.get("containerDefinitions"),
        spec=spec,
        label="live task definition",
        pre_media_cutover_sync_image=pre_media_cutover_sync_image,
    )
    return {
        "taskDefinition": task_definition,
        "taskDefinitionSha256": task_definition_sha256,
        "image": image,
        "imageDigest": image_digest,
    }


def _read_consumers(
    cli: AwsCli,
    specs: Mapping[str, ConsumerSpec],
    *,
    pre_media_cutover_sync_image: str = "",
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    service_activations, raw_services = _read_services(cli, specs)
    activations: dict[str, dict[str, Any]] = {}
    raw_activations: dict[str, dict[str, Any]] = {}
    for key, service in service_activations.items():
        spec = specs[key]
        activations[key] = {
            "type": spec.activator_type,
            "identity": spec.activator_identity,
            "status": "ACTIVE",
            **service,
        }
        raw_activations[key] = raw_services[key]
    for key, spec in specs.items():
        if spec.activator_type == "eventbridge_rule_ecs_target":
            activation, raw = _read_eventbridge_activation(cli, spec=spec)
            activations[key] = activation
            raw_activations[key] = raw
        elif spec.activator_type == _HYBRID_ACTIVATOR_TYPE:
            activation, raw = _read_hybrid_activation(cli, spec=spec)
            activations[key] = activation
            raw_activations[key] = raw
        elif spec.activator_type == "lambda_taskdef_arn_environment":
            activation, raw = _read_lambda_activation(cli, spec=spec)
            activations[key] = activation
            raw_activations[key] = raw
        elif spec.activator_type != "ecs_service":
            raise SagaError("consumer activator type is unsupported")
    if frozenset(activations) != frozenset(specs):
        raise SagaError("consumer activation inventory is incomplete")

    consumers: dict[str, dict[str, Any]] = {}
    for key, spec in specs.items():
        activation = activations[key]
        task_definition = activation.get("taskDefinition")
        task = _read_task_definition(
            cli,
            spec=spec,
            task_definition=_validate_task_definition(
                task_definition,
                expected_family=spec.task_family,
            ),
            pre_media_cutover_sync_image=pre_media_cutover_sync_image,
        )
        consumers[key] = {
            **task,
            "activation": activation,
        }
    return consumers, raw_activations


def _list_service_tasks(cli: AwsCli, *, spec: ConsumerSpec) -> list[str]:
    task_arns: list[str] = []
    token: str | None = None
    seen_tokens: set[str] = set()
    for _page_number in range(_MAX_INVENTORY_PAGES):
        arguments = [
            "--cluster",
            _CLUSTER_ARN,
            "--service-name",
            spec.activator_identity,
            "--desired-status",
            "RUNNING",
            "--max-results",
            "100",
        ]
        if token is not None:
            arguments.extend(["--next-token", token])
        response = cli.json("ecs", "list-tasks", arguments)
        if frozenset(response) - {"nextToken", "taskArns"}:
            raise SagaError("ECS running task inventory has unknown fields")
        page = response.get("taskArns")
        if type(page) is not list or any(type(item) is not str for item in page):
            raise SagaError("ECS running task inventory is invalid")
        if len(task_arns) + len(page) > _MAX_INVENTORY_SERVICES:
            raise SagaError("ECS running task inventory exceeds its durable bound")
        for task_arn in page:
            if task_arn in task_arns:
                raise SagaError("ECS running task inventory repeats an identity")
            task_arns.append(task_arn)
        next_token = response.get("nextToken")
        if next_token is None:
            return task_arns
        if type(next_token) is not str or not next_token or next_token in seen_tokens:
            raise SagaError("ECS running task pagination is invalid")
        seen_tokens.add(next_token)
        token = next_token
    raise SagaError("ECS running task pagination exceeds its bound")


def _assert_service_tasks(
    cli: AwsCli,
    *,
    spec: ConsumerSpec,
    state: Mapping[str, Any],
) -> None:
    activation = state.get("activation")
    if type(activation) is not dict:
        raise SagaError("ECS stable service task verification is incomplete")
    desired = activation.get("desiredCount")
    task_definition = state.get("taskDefinition")
    image = state.get("image")
    image_digest = state.get("imageDigest")
    task_arns = _list_service_tasks(cli, spec=spec)
    if type(desired) is not int or len(task_arns) != desired:
        raise SagaError(f"ECS service {spec.activator_identity} running tasks are not exact")
    described_by_arn: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(task_arns), 100):
        batch = task_arns[offset : offset + 100]
        response = cli.json(
            "ecs",
            "describe-tasks",
            ["--cluster", _CLUSTER_ARN, "--tasks", *batch],
        )
        if frozenset(response) - {"failures", "tasks"}:
            raise SagaError("ECS running task description has unknown fields")
        tasks = response.get("tasks")
        if response.get("failures") != [] or type(tasks) is not list or len(tasks) != len(batch):
            raise SagaError("ECS running task description is incomplete")
        for raw in tasks:
            if type(raw) is not dict or type(raw.get("taskArn")) is not str:
                raise SagaError("ECS running task description is invalid")
            task_arn = raw["taskArn"]
            containers = raw.get("containers")
            matching_containers = (
                [
                    container
                    for container in containers
                    if type(container) is dict and container.get("name") == spec.container_name
                ]
                if type(containers) is list
                else []
            )
            if (
                task_arn not in batch
                or task_arn in described_by_arn
                or raw.get("clusterArn") != _CLUSTER_ARN
                or raw.get("taskDefinitionArn") != task_definition
                or raw.get("group") != f"service:{spec.activator_identity}"
                or raw.get("desiredStatus") != "RUNNING"
                or raw.get("lastStatus") != "RUNNING"
                or len(matching_containers) != 1
                or matching_containers[0].get("image") != image
                or matching_containers[0].get("imageDigest") != image_digest
            ):
                raise SagaError(f"ECS service {spec.activator_identity} running task differs")
            described_by_arn[task_arn] = raw
    if frozenset(described_by_arn) != frozenset(task_arns):
        raise SagaError("ECS running task descriptions do not cover the service")


def _assert_stable(
    cli: AwsCli,
    raw_activations: Mapping[str, Mapping[str, Any]],
    expected: Mapping[str, Mapping[str, Any]],
    specs: Mapping[str, ConsumerSpec],
) -> None:
    for key, spec in specs.items():
        raw = raw_activations.get(key)
        state = expected.get(key)
        if type(raw) is not dict or type(state) is not dict:
            raise SagaError("consumer steady verification is incomplete")
        if spec.activator_type == "eventbridge_rule_ecs_target":
            activation = state.get("activation")
            if (
                type(activation) is not dict
                or _canonical_eventbridge_activation(raw, spec=spec) != activation
            ):
                raise SagaError(
                    f"EventBridge consumer {spec.activator_identity} is not exactly steady"
                )
            continue
        if spec.activator_type == _HYBRID_ACTIVATOR_TYPE:
            activation = state.get("activation")
            if (
                type(activation) is not dict
                or _canonical_hybrid_activation(raw, spec=spec) != activation
            ):
                raise SagaError(
                    f"hybrid consumer {spec.activator_identity} is not exactly steady"
                )
            continue
        if spec.activator_type == "lambda_taskdef_arn_environment":
            activation = state.get("activation")
            if (
                type(activation) is not dict
                or _canonical_lambda_activation(raw, spec=spec) != activation
            ):
                raise SagaError(f"Lambda consumer {spec.activator_identity} is not exactly steady")
            continue
        if spec.activator_type != "ecs_service":
            raise SagaError("consumer activator type is unsupported")
        activation = state.get("activation")
        if type(activation) is not dict:
            raise SagaError("ECS stable service verification is incomplete")
        running = raw.get("runningCount")
        pending = raw.get("pendingCount")
        deployments = raw.get("deployments")
        desired = activation.get("desiredCount")
        if (
            type(running) is not int
            or type(pending) is not int
            or running != desired
            or pending != 0
            or type(deployments) is not list
            or len(deployments) != 1
            or type(deployments[0]) is not dict
            or deployments[0].get("status") != "PRIMARY"
            or deployments[0].get("taskDefinition") != state.get("taskDefinition")
            or deployments[0].get("rolloutState") != "COMPLETED"
            or deployments[0].get("desiredCount") != desired
            or deployments[0].get("runningCount") != desired
            or deployments[0].get("pendingCount") != 0
        ):
            raise SagaError(f"ECS service {spec.activator_identity} is not exactly stable")
        _assert_service_tasks(cli, spec=spec, state=state)


def _ddb_value(value: object) -> dict[str, str]:
    if type(value) is str:
        return {"S": value}
    if type(value) is int:
        return {"N": str(value)}
    raise SagaError("unsupported DynamoDB saga value")


def _ddb_item(values: Mapping[str, object]) -> dict[str, dict[str, str]]:
    return {name: _ddb_value(value) for name, value in sorted(values.items())}


def _ddb_string(item: Mapping[str, Any], name: str) -> str:
    raw = item.get(name)
    value = raw.get("S") if type(raw) is dict and frozenset(raw) == {"S"} else None
    if type(value) is not str:
        raise SagaError("durable ECS saga record is invalid")
    return value


def _ddb_number(item: Mapping[str, Any], name: str) -> int:
    raw = item.get(name)
    value = raw.get("N") if type(raw) is dict and frozenset(raw) == {"N"} else None
    if type(value) is not str or not value.isdecimal():
        raise SagaError("durable ECS saga record is invalid")
    return int(value)


_RECORD_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "baseline_json",
        "baseline_sha256",
        "plan_sha256",
        "planned_json",
        "planned_sha256",
        "record_id",
        "record_type",
        "schema_version",
        "stage",
    }
)
_ACTIVE_RECORD_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "attempt_record_id",
        "baseline_sha256",
        "plan_sha256",
        "planned_sha256",
        "record_id",
        "record_type",
        "schema_version",
        "scope_id",
        "stage",
    }
)
_LEDGER_STAGES = frozenset({"APPLYING", "APPLIED", "RESTORED"})
_RECEIPT_STAGES = frozenset(
    {
        "APPLYING",
        "VERIFIED_APPLIED",
        "APPLIED",
        "RESTORED",
    }
)


def _validate_consumer_baseline(
    value: object,
    *,
    specs: Mapping[str, ConsumerSpec],
    pre_media_cutover_sync_image: str = "",
) -> dict[str, dict[str, Any]]:
    if type(value) is not dict or frozenset(value) != frozenset(specs):
        raise SagaError("durable ECS rollback baseline is incomplete")
    baseline: dict[str, dict[str, Any]] = {}
    for key, spec in specs.items():
        raw = value.get(key)
        if type(raw) is not dict or frozenset(raw) != {
            "activation",
            "image",
            "imageDigest",
            "taskDefinition",
            "taskDefinitionSha256",
        }:
            raise SagaError("durable ECS rollback baseline is invalid")
        task_definition = _validate_task_definition(
            raw.get("taskDefinition"),
            expected_family=spec.task_family,
        )
        task_definition_sha256 = raw.get("taskDefinitionSha256")
        image = raw.get("image")
        image_digest = raw.get("imageDigest")
        expected_image = (
            pre_media_cutover_sync_image
            if spec.key == "tiktok_acquire" and pre_media_cutover_sync_image
            else (
                f"{_ACCOUNT_ID}.dkr.ecr.{_REGION}.amazonaws.com/"
                f"{spec.release_repository}@{image_digest}"
            )
        )
        if (
            type(task_definition_sha256) is not str
            or _SHA256_RE.fullmatch(task_definition_sha256) is None
            or type(image_digest) is not str
            or _IMAGE_DIGEST_RE.fullmatch(image_digest) is None
            or (
                spec.key == "tiktok_acquire"
                and pre_media_cutover_sync_image
                and _LEGACY_TIKTOK_IMAGE_RE.fullmatch(pre_media_cutover_sync_image) is None
            )
            or image != expected_image
            or (
                spec.key == "tiktok_acquire"
                and pre_media_cutover_sync_image
                and image_digest != pre_media_cutover_sync_image.rsplit("@", maxsplit=1)[1]
            )
        ):
            raise SagaError("durable ECS rollback task binding is invalid")
        activation = raw.get("activation")
        if type(activation) is not dict:
            raise SagaError("durable ECS rollback activation is invalid")
        if spec.activator_type == "ecs_service":
            if frozenset(activation) != {
                "deploymentConfiguration",
                "desiredCount",
                "identity",
                "networkConfiguration",
                "status",
                "taskDefinition",
                "type",
            }:
                raise SagaError("durable ECS service rollback activation is invalid")
            desired = activation.get("desiredCount")
            if (
                activation.get("type") != spec.activator_type
                or activation.get("identity") != spec.activator_identity
                or activation.get("status") != "ACTIVE"
                or activation.get("taskDefinition") != task_definition
                or type(desired) is not int
                or desired != 1
            ):
                raise SagaError("durable ECS service rollback activation is invalid")
            normalized_activation = {
                "type": spec.activator_type,
                "identity": spec.activator_identity,
                "status": "ACTIVE",
                "taskDefinition": task_definition,
                "deploymentConfiguration": _canonical_deployment_configuration(
                    activation.get("deploymentConfiguration")
                ),
                "networkConfiguration": _canonical_network_configuration(
                    activation.get("networkConfiguration")
                ),
                "desiredCount": desired,
            }
        elif spec.activator_type == "eventbridge_rule_ecs_target":
            if frozenset(activation) != {
                "identity",
                "ruleArn",
                "state",
                "target",
                "taskDefinition",
                "type",
            }:
                raise SagaError("durable EventBridge rollback activation is invalid")
            normalized_activation = _canonical_eventbridge_activation(
                {
                    "rule": {
                        "Arn": activation.get("ruleArn"),
                        "EventBusName": "default",
                        "Name": activation.get("identity"),
                        "State": activation.get("state"),
                    },
                    "target": activation.get("target"),
                },
                spec=spec,
            )
            if normalized_activation.get("taskDefinition") != task_definition:
                raise SagaError("durable EventBridge rollback pointer differs")
        elif spec.activator_type == _HYBRID_ACTIVATOR_TYPE:
            if frozenset(activation) != {
                "environmentVariables",
                "functionArn",
                "identity",
                "ruleArn",
                "state",
                "taskDefinition",
                "type",
            }:
                raise SagaError("durable hybrid rollback activation is invalid")
            normalized_activation = _canonical_hybrid_activation(
                {
                    "rule": {
                        "Arn": activation.get("ruleArn"),
                        "EventBusName": "default",
                        "Name": activation.get("identity"),
                        "State": activation.get("state"),
                    },
                    "lambda": {
                        "Environment": {
                            "Variables": activation.get("environmentVariables"),
                        },
                        "FunctionArn": activation.get("functionArn"),
                        "FunctionName": spec.function_name,
                    },
                },
                spec=spec,
            )
            if normalized_activation.get("taskDefinition") != task_definition:
                raise SagaError("durable hybrid rollback pointer differs")
        elif spec.activator_type == "lambda_taskdef_arn_environment":
            if frozenset(activation) != {
                "environmentVariables",
                "functionArn",
                "identity",
                "taskDefinition",
                "type",
            }:
                raise SagaError("durable Lambda rollback activation is invalid")
            normalized_activation = _canonical_lambda_activation(
                {
                    "Environment": {
                        "Variables": activation.get("environmentVariables"),
                    },
                    "FunctionArn": activation.get("functionArn"),
                    "FunctionName": activation.get("identity"),
                },
                spec=spec,
            )
            if normalized_activation.get("taskDefinition") != task_definition:
                raise SagaError("durable Lambda rollback pointer differs")
        else:
            raise SagaError("durable rollback activator type is unsupported")
        baseline[key] = {
            "taskDefinition": task_definition,
            "taskDefinitionSha256": task_definition_sha256,
            "image": image,
            "imageDigest": image_digest,
            "activation": normalized_activation,
        }
    return baseline


class EcsServiceApplySaga:
    def __init__(
        self,
        *,
        plan: PlanAnalysis,
        plan_sha256: str,
        apply_attempt_id: str,
        cli: AwsCli,
    ) -> None:
        if (
            _SHA256_RE.fullmatch(plan_sha256) is None
            or _UUID_RE.fullmatch(apply_attempt_id) is None
            or plan.binding.get("consumerRegistrySha256") != plan.registry_sha256
            or plan.binding.get("preMediaCutoverSyncImage") != plan.pre_media_cutover_sync_image
            or (
                plan.pre_media_cutover_sync_image
                and _LEGACY_TIKTOK_IMAGE_RE.fullmatch(plan.pre_media_cutover_sync_image) is None
            )
            or frozenset(plan.specs) != _SAGA_CONSUMER_IDS
        ):
            raise SagaError("ECS saga identity is invalid")
        self.plan = plan
        self.specs = dict(plan.specs)
        self.registry_sha256 = plan.registry_sha256
        self.plan_sha256 = plan_sha256
        self.apply_attempt_id = apply_attempt_id
        self.cli = cli
        self.record_id = f"{_RECORD_PREFIX}{apply_attempt_id}"
        self._assert_registry_current()

    def _assert_registry_current(self) -> None:
        specs, registry_sha256 = _load_consumer_specs()
        if (
            registry_sha256 != self.registry_sha256
            or specs != self.specs
            or self.plan.binding.get("consumerRegistrySha256") != registry_sha256
        ):
            raise SagaError("consumer registry changed after saved-plan analysis")

    def _read(self, record_id: str | None = None) -> dict[str, Any] | None:
        response = self.cli.json(
            "dynamodb",
            "get-item",
            [
                "--table-name",
                _TABLE_NAME,
                "--key",
                json.dumps(
                    _ddb_item({"record_id": record_id or self.record_id}),
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "--consistent-read",
            ],
        )
        if frozenset(response) - {"Item"}:
            raise SagaError("durable ECS saga read has unknown fields")
        item = response.get("Item")
        if item is None:
            return None
        if type(item) is not dict:
            raise SagaError("durable ECS saga record is invalid")
        return item

    def _validate_item(self, item: Mapping[str, Any]) -> None:
        if (
            frozenset(item) != _RECORD_FIELDS
            or _ddb_string(item, "record_id") != self.record_id
            or _ddb_string(item, "record_type") != _RECORD_TYPE
            or _ddb_number(item, "schema_version") != _SCHEMA_VERSION
            or _ddb_string(item, "plan_sha256") != self.plan_sha256
            or _ddb_string(item, "apply_attempt_id") != self.apply_attempt_id
            or _ddb_string(item, "stage") not in _LEDGER_STAGES
        ):
            raise SagaError("durable ECS saga identity differs")
        planned_json = _canonical_bytes(self.plan.binding).decode("utf-8")
        if not hmac.compare_digest(
            _ddb_string(item, "planned_sha256"),
            _digest(self.plan.binding),
        ) or not hmac.compare_digest(
            _ddb_string(item, "planned_json"),
            planned_json,
        ):
            raise SagaError("durable ECS planned binding differs")

    def _read_required(self) -> dict[str, Any]:
        item = self._read()
        if item is None:
            raise SagaError("durable ECS saga does not exist")
        self._validate_item(item)
        return item

    def _validate_active_item(self, item: Mapping[str, Any], *, stage: str | None = None) -> None:
        expected_stage = stage or _ddb_string(item, "stage")
        if (
            frozenset(item) != _ACTIVE_RECORD_FIELDS
            or _ddb_string(item, "record_id") != _ACTIVE_RECORD_ID
            or _ddb_string(item, "record_type") != _ACTIVE_RECORD_TYPE
            or _ddb_number(item, "schema_version") != _SCHEMA_VERSION
            or _ddb_string(item, "scope_id") != _SCOPE_ID
            or _ddb_string(item, "stage") != expected_stage
            or expected_stage not in _LEDGER_STAGES
            or _ddb_string(item, "attempt_record_id") != self.record_id
            or _ddb_string(item, "plan_sha256") != self.plan_sha256
            or _ddb_string(item, "apply_attempt_id") != self.apply_attempt_id
        ):
            raise SagaError("durable ECS active pointer identity differs")
        attempt = self._read_required()
        for name in ("baseline_sha256", "planned_sha256"):
            if _ddb_string(item, name) != _ddb_string(attempt, name):
                raise SagaError("durable ECS active pointer binding differs")

    def _active(self, *, stage: str | None = None) -> dict[str, Any]:
        item = self._read(_ACTIVE_RECORD_ID)
        if item is None:
            raise SagaError("durable ECS active pointer does not exist")
        self._validate_active_item(item, stage=stage)
        return item

    def _receipt(self, item: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_item(item)
        stage = _ddb_string(item, "stage")
        if stage not in _RECEIPT_STAGES:
            raise SagaError("durable ECS saga receipt stage is invalid")
        receipt = {
            "kind": "teamagent-ecs-service-apply-saga-receipt",
            "schema_version": _SCHEMA_VERSION,
            "record_id": self.record_id,
            "stage": stage,
            "plan_sha256": self.plan_sha256,
            "apply_attempt_id": self.apply_attempt_id,
            "baseline_sha256": _ddb_string(item, "baseline_sha256"),
            "planned_sha256": _ddb_string(item, "planned_sha256"),
            "ledger_item_sha256": _digest(item),
        }
        receipt["receipt_sha256"] = _digest(receipt)
        return receipt

    def _baseline_from_item(self, item: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        baseline_json = _ddb_string(item, "baseline_json")
        try:
            raw = json.loads(baseline_json, object_pairs_hook=_reject_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SagaError("durable ECS rollback baseline is invalid") from exc
        baseline = _validate_consumer_baseline(
            raw,
            specs=self.specs,
            pre_media_cutover_sync_image=self.plan.pre_media_cutover_sync_image,
        )
        if not hmac.compare_digest(
            _ddb_string(item, "baseline_sha256"), _digest(baseline)
        ) or not hmac.compare_digest(
            baseline_json,
            _canonical_bytes(baseline).decode("utf-8"),
        ):
            raise SagaError("durable ECS rollback baseline digest differs")
        return baseline

    def begin(self) -> dict[str, Any]:
        self._assert_registry_current()
        baseline, raw = _read_consumers(
            self.cli,
            self.specs,
            pre_media_cutover_sync_image=self.plan.pre_media_cutover_sync_image,
        )
        _assert_stable(self.cli, raw, baseline, self.specs)
        baseline = _validate_consumer_baseline(
            baseline,
            specs=self.specs,
            pre_media_cutover_sync_image=self.plan.pre_media_cutover_sync_image,
        )
        self._assert_registry_current()
        baseline_json = _canonical_bytes(baseline).decode("utf-8")
        planned_json = _canonical_bytes(self.plan.binding).decode("utf-8")
        item = _ddb_item(
            {
                "apply_attempt_id": self.apply_attempt_id,
                "baseline_json": baseline_json,
                "baseline_sha256": _digest(baseline),
                "plan_sha256": self.plan_sha256,
                "planned_json": planned_json,
                "planned_sha256": _digest(self.plan.binding),
                "record_id": self.record_id,
                "record_type": _RECORD_TYPE,
                "schema_version": _SCHEMA_VERSION,
                "stage": "APPLYING",
            }
        )
        if len(_canonical_bytes(item)) > _MAX_RECORD_BYTES:
            raise SagaError("durable ECS saga record exceeds its bound")
        active = _ddb_item(
            {
                "apply_attempt_id": self.apply_attempt_id,
                "attempt_record_id": self.record_id,
                "baseline_sha256": _digest(baseline),
                "plan_sha256": self.plan_sha256,
                "planned_sha256": _digest(self.plan.binding),
                "record_id": _ACTIVE_RECORD_ID,
                "record_type": _ACTIVE_RECORD_TYPE,
                "schema_version": _SCHEMA_VERSION,
                "scope_id": _SCOPE_ID,
                "stage": "APPLYING",
            }
        )
        try:
            self.cli.run(
                "dynamodb",
                "transact-write-items",
                [
                    "--transact-items",
                    json.dumps(
                        [
                            {
                                "Put": {
                                    "TableName": _TABLE_NAME,
                                    "Item": item,
                                    "ConditionExpression": "attribute_not_exists(record_id)",
                                }
                            },
                            {
                                "Put": {
                                    "TableName": _TABLE_NAME,
                                    "Item": active,
                                    "ConditionExpression": (
                                        "attribute_not_exists(record_id) OR "
                                        "#stage = :applied OR #stage = :restored"
                                    ),
                                    "ExpressionAttributeNames": {"#stage": "stage"},
                                    "ExpressionAttributeValues": _ddb_item(
                                        {":applied": "APPLIED", ":restored": "RESTORED"}
                                    ),
                                }
                            },
                        ],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ],
            )
        except Exception as exc:
            # A begin is deliberately one-shot. Even an ambiguous successful Put must be
            # reconciled through finish; replaying begin could capture a post-apply baseline.
            raise SagaError("durable ECS saga already exists or could not begin") from exc
        confirmed = self._read()
        if confirmed is None:
            raise SagaError("durable ECS saga baseline was not confirmed")
        self._active(stage="APPLYING")
        receipt = self._receipt(confirmed)
        if receipt["stage"] != "APPLYING":
            raise SagaError("durable ECS saga baseline stage is invalid")
        return receipt

    def verify(self) -> dict[str, Any]:
        self._assert_registry_current()
        item = self._read_required()
        if _ddb_string(item, "stage") != "APPLYING":
            raise SagaError("durable ECS saga is not the exact applying attempt")
        self._active(stage="APPLYING")
        live, raw_live = _read_consumers(
            self.cli,
            self.specs,
            pre_media_cutover_sync_image=self.plan.pre_media_cutover_sync_image,
        )
        self._verify_planned(live, raw_live)
        _assert_stable(self.cli, raw_live, live, self.specs)
        self._assert_registry_current()
        receipt = self._receipt(item)
        receipt["stage"] = "VERIFIED_APPLIED"
        receipt["receipt_sha256"] = _digest(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        return receipt

    def _verify_planned(
        self,
        live: Mapping[str, Mapping[str, Any]],
        raw_live: Mapping[str, Mapping[str, Any]],
    ) -> None:
        planned_consumers = self.plan.binding.get("consumers")
        if type(planned_consumers) is not dict or frozenset(planned_consumers) != frozenset(
            self.specs
        ):
            raise SagaError("planned consumer binding is invalid")
        for key, spec in self.specs.items():
            expected = planned_consumers.get(key)
            observed = live.get(key)
            raw = raw_live.get(key)
            if (
                type(expected) is not dict
                or frozenset(expected)
                != {
                    "activation",
                    "activationChanged",
                    "activatorType",
                    "imageDigest",
                    "taskDefinitionSha256",
                }
                or type(observed) is not dict
                or type(raw) is not dict
                or expected.get("activatorType") != spec.activator_type
                or type(expected.get("activationChanged")) is not bool
            ):
                raise SagaError("planned consumer binding is incomplete")
            expected_activation = expected.get("activation")
            observed_activation = observed.get("activation")
            if type(expected_activation) is not dict or type(observed_activation) is not dict:
                raise SagaError("planned consumer activation binding is incomplete")
            pointer = expected_activation.get("taskDefinition")
            task_definition = observed.get("taskDefinition")
            if type(pointer) is not dict:
                raise SagaError("planned consumer task pointer is invalid")
            if pointer.get("kind") == "arn":
                if task_definition != pointer.get("taskDefinition"):
                    raise SagaError("live consumer does not use the planned task definition")
            elif (
                pointer.get("kind") != "artifact"
                or pointer.get("family") != spec.task_family
                or frozenset(pointer) != {"family", "kind"}
            ):
                raise SagaError("planned consumer task pointer is invalid")

            expected_task_sha256 = expected.get("taskDefinitionSha256")
            expected_image_digest = expected.get("imageDigest")
            if (
                type(expected_task_sha256) is not str
                or _SHA256_RE.fullmatch(expected_task_sha256) is None
                or observed.get("taskDefinitionSha256") != expected_task_sha256
                or type(expected_image_digest) is not str
                or _IMAGE_DIGEST_RE.fullmatch(expected_image_digest) is None
                or observed.get("imageDigest") != expected_image_digest
            ):
                raise SagaError("live consumer task artifact differs from the saved plan")
            if observed_activation.get("taskDefinition") != task_definition:
                raise SagaError("live consumer activation pointer is inconsistent")
            if spec.activator_type == "ecs_service":
                if (
                    frozenset(expected_activation) != {"desiredCount", "taskDefinition"}
                    or expected_activation.get("desiredCount") != 1
                    or observed_activation.get("desiredCount") != 1
                ):
                    raise SagaError("planned ECS service activation differs")
            elif spec.activator_type == "eventbridge_rule_ecs_target":
                if (
                    frozenset(expected_activation) != {"state", "taskDefinition"}
                    or expected_activation.get("state") not in _RULE_STATES
                    or observed_activation.get("state") != expected_activation.get("state")
                ):
                    raise SagaError("live EventBridge activation differs from the saved plan")
            elif spec.activator_type == _HYBRID_ACTIVATOR_TYPE:
                if (
                    frozenset(expected_activation) != {"state", "taskDefinition"}
                    or expected_activation.get("state") not in _RULE_STATES
                    or observed_activation.get("state") != expected_activation.get("state")
                ):
                    raise SagaError("live hybrid activation differs from the saved plan")
            elif spec.activator_type == "lambda_taskdef_arn_environment":
                if frozenset(expected_activation) != {"taskDefinition"}:
                    raise SagaError("planned Lambda activation differs")
            else:
                raise SagaError("planned consumer activator type is unsupported")

    def _restore(self, baseline: Mapping[str, Mapping[str, Any]]) -> None:
        for key, spec in self.specs.items():
            state = baseline.get(key)
            if type(state) is not dict:
                raise SagaError("durable ECS rollback baseline is incomplete")
            activation = state.get("activation")
            if type(activation) is not dict:
                raise SagaError("durable consumer rollback activation is incomplete")
            if spec.activator_type in {
                "eventbridge_rule_ecs_target",
                _HYBRID_ACTIVATOR_TYPE,
                "lambda_taskdef_arn_environment",
            }:
                continue
            if spec.activator_type != "ecs_service":
                raise SagaError("consumer activator type is unsupported")
            response = self.cli.json(
                "ecs",
                "update-service",
                [
                    "--cluster",
                    _CLUSTER_ARN,
                    "--service",
                    spec.service_arn,
                    "--task-definition",
                    str(activation["taskDefinition"]),
                    "--desired-count",
                    str(activation["desiredCount"]),
                    "--deployment-configuration",
                    json.dumps(
                        activation["deploymentConfiguration"],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "--network-configuration",
                    json.dumps(
                        activation["networkConfiguration"],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ],
            )
            returned = response.get("service")
            if type(returned) is not dict:
                raise SagaError("ECS rollback update response is invalid")
            expected_service = {
                name: activation[name]
                for name in (
                    "deploymentConfiguration",
                    "desiredCount",
                    "networkConfiguration",
                    "taskDefinition",
                )
            }
            if _canonical_service(returned, spec=spec) != expected_service:
                raise SagaError("ECS rollback update did not accept the exact baseline")

        # EventBridge/Lambda rollback execution is owned by the apply controller.
        # Their complete, exact restore payloads remain durably bound in ``baseline``.
        self.cli.run(
            "ecs",
            "wait services-stable",
            [
                "--cluster",
                _CLUSTER_ARN,
                "--services",
                *(
                    spec.service_arn
                    for spec in self.specs.values()
                    if spec.activator_type == "ecs_service"
                ),
            ],
            timeout_seconds=900,
        )
        restored, raw_restored = _read_consumers(
            self.cli,
            self.specs,
            pre_media_cutover_sync_image=self.plan.pre_media_cutover_sync_image,
        )
        if restored != baseline:
            raise SagaError("exact consumer rollback baseline could not be verified")
        _assert_stable(self.cli, raw_restored, restored, self.specs)

    def _transition(
        self,
        item: Mapping[str, Any],
        *,
        desired: str,
    ) -> dict[str, Any]:
        values = _ddb_item(
            {
                ":applying": "APPLYING",
                ":attempt": self.apply_attempt_id,
                ":baseline": _ddb_string(item, "baseline_sha256"),
                ":desired": desired,
                ":plan": self.plan_sha256,
                ":planned": _ddb_string(item, "planned_sha256"),
            }
        )
        self._active(stage="APPLYING")
        active_values = _ddb_item(
            {
                ":active": "APPLYING",
                ":attempt": self.apply_attempt_id,
                ":attempt_record": self.record_id,
                ":baseline": _ddb_string(item, "baseline_sha256"),
                ":desired": desired,
                ":plan": self.plan_sha256,
                ":planned": _ddb_string(item, "planned_sha256"),
                ":scope": _SCOPE_ID,
            }
        )
        try:
            self.cli.run(
                "dynamodb",
                "transact-write-items",
                [
                    "--transact-items",
                    json.dumps(
                        [
                            {
                                "Update": {
                                    "TableName": _TABLE_NAME,
                                    "Key": _ddb_item({"record_id": self.record_id}),
                                    "UpdateExpression": "SET #stage = :desired",
                                    "ConditionExpression": (
                                        "#stage = :applying AND plan_sha256 = :plan"
                                        " AND apply_attempt_id = :attempt"
                                        " AND baseline_sha256 = :baseline"
                                        " AND planned_sha256 = :planned"
                                    ),
                                    "ExpressionAttributeNames": {"#stage": "stage"},
                                    "ExpressionAttributeValues": values,
                                }
                            },
                            {
                                "Update": {
                                    "TableName": _TABLE_NAME,
                                    "Key": _ddb_item({"record_id": _ACTIVE_RECORD_ID}),
                                    "UpdateExpression": "SET #stage = :desired",
                                    "ConditionExpression": (
                                        "#stage = :active AND scope_id = :scope"
                                        " AND attempt_record_id = :attempt_record"
                                        " AND plan_sha256 = :plan"
                                        " AND apply_attempt_id = :attempt"
                                        " AND baseline_sha256 = :baseline"
                                        " AND planned_sha256 = :planned"
                                    ),
                                    "ExpressionAttributeNames": {"#stage": "stage"},
                                    "ExpressionAttributeValues": active_values,
                                }
                            },
                        ],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ],
            )
        except Exception as exc:
            confirmed = self._read()
            if confirmed is None:
                raise SagaError("durable ECS saga completion CAS failed") from exc
            self._validate_item(confirmed)
            if _ddb_string(confirmed, "stage") != desired:
                raise SagaError("durable ECS saga completion CAS failed") from exc
            try:
                self._active(stage=desired)
            except SagaError:
                raise SagaError("durable ECS saga completion CAS failed") from exc
            return confirmed
        confirmed = self._read()
        if confirmed is None:
            raise SagaError("durable ECS saga completion was not persisted")
        self._validate_item(confirmed)
        if _ddb_string(confirmed, "stage") != desired:
            raise SagaError("durable ECS saga completion was not persisted")
        self._active(stage=desired)
        return confirmed

    def finish(self, *, outcome: str) -> dict[str, Any]:
        self._assert_registry_current()
        desired = {"applied": "APPLIED", "failed": "RESTORED"}.get(outcome)
        if desired is None:
            raise SagaError("ECS saga outcome is invalid")
        item = self._read()
        if item is None:
            raise SagaError("durable ECS saga does not exist")
        self._validate_item(item)
        stage = _ddb_string(item, "stage")
        if stage == desired:
            return self._receipt(item)
        if stage != "APPLYING":
            raise SagaError("durable ECS saga stage is not reconcilable")
        baseline = self._baseline_from_item(item)
        if outcome == "failed":
            self._restore(baseline)
        else:
            live, raw_live = _read_consumers(
                self.cli,
                self.specs,
                pre_media_cutover_sync_image=self.plan.pre_media_cutover_sync_image,
            )
            self._verify_planned(live, raw_live)
            _assert_stable(self.cli, raw_live, live, self.specs)
        self._assert_registry_current()
        return self._receipt(self._transition(item, desired=desired))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("begin", "finish", "verify"))
    parser.add_argument("--aws-bin", type=Path, required=True)
    parser.add_argument("--terraform-bin", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--apply-attempt-id", required=True)
    parser.add_argument("--outcome", choices=("applied", "failed"))
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    cli: AwsCli | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            _SHA256_RE.fullmatch(args.plan_sha256) is None
            or _UUID_RE.fullmatch(args.apply_attempt_id) is None
        ):
            raise SagaError("ECS saga identity is invalid")
        if args.action == "finish" and args.outcome is None:
            raise SagaError("finish requires an outcome")
        if args.action != "finish" and args.outcome is not None:
            raise SagaError(f"{args.action} rejects outcomes")
        aws_bin = _trusted_executable(args.aws_bin, label="AWS CLI")
        terraform_bin = _trusted_executable(
            args.terraform_bin,
            label="Terraform",
        )
        saved_plan = _load_saved_plan(
            args.plan,
            expected_sha256=args.plan_sha256,
            terraform_bin=terraform_bin,
        )
        saga = EcsServiceApplySaga(
            plan=_analyze_plan(saved_plan),
            plan_sha256=args.plan_sha256,
            apply_attempt_id=args.apply_attempt_id,
            cli=cli or _SubprocessAwsCli(aws_bin),
        )
        if args.action == "begin":
            result = saga.begin()
        elif args.action == "finish":
            assert args.outcome is not None
            result = saga.finish(outcome=args.outcome)
        else:
            result = saga.verify()
    except Exception:
        print('{"code":"ecs_service_apply_saga_failed","ok":false}')
        return 2
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
