#!/usr/bin/env python3
"""Durable apply-level rollback saga for the MCP and connect-web ECS services."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
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
}
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
class ServiceSpec:
    key: str
    service_name: str
    service_address: str
    task_address: str
    task_family: str

    @property
    def service_arn(self) -> str:
        return f"arn:aws:ecs:{_REGION}:{_ACCOUNT_ID}:service/{_CLUSTER_NAME}/{self.service_name}"


_SERVICE_SPECS = {
    "mcp": ServiceSpec(
        key="mcp",
        service_name="teamagent-dev-mcp",
        service_address="aws_ecs_service.mcp[0]",
        task_address="aws_ecs_task_definition.mcp",
        task_family="teamagent-dev-mcp",
    ),
    "connect_web": ServiceSpec(
        key="connect_web",
        service_name="teamagent-dev-connect-web",
        service_address="aws_ecs_service.connect_web[0]",
        task_address="aws_ecs_task_definition.connect_web[0]",
        task_family="teamagent-dev-connect-web",
    ),
}
_ALLOWED_ECS_ADDRESSES = frozenset(
    address
    for spec in _SERVICE_SPECS.values()
    for address in (spec.service_address, spec.task_address)
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
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise SagaError("non-JSON value is invalid")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _canonical_json_value(value),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


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


def _validate_resource_identity(item: Mapping[str, Any], *, address: str) -> None:
    expected_type = (
        "aws_ecs_service" if ".aws_ecs_service." in f".{address}" else ("aws_ecs_task_definition")
    )
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
        raise SagaError("saved plan ECS resource identity is not exact")


@dataclass(frozen=True)
class PlanAnalysis:
    binding: dict[str, Any]


def _analyze_plan(plan: Mapping[str, Any]) -> PlanAnalysis:
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
        if not is_ecs:
            continue
        actions = _actions(raw_item, label=address)
        if address not in _ALLOWED_ECS_ADDRESSES:
            if actions not in {("no-op",), ("read",)}:
                raise SagaError("saved plan mutates an ECS resource outside the saga scope")
            continue
        if address in matches:
            raise SagaError("saved plan repeats an ECS baseline address")
        _validate_resource_identity(raw_item, address=address)
        matches[address] = raw_item

    if frozenset(matches) != _ALLOWED_ECS_ADDRESSES:
        raise SagaError("saved plan does not contain every exact ECS baseline address")

    planned_services: dict[str, dict[str, str]] = {}
    for key, spec in _SERVICE_SPECS.items():
        service_after, service_unknown = _resource_after(
            matches[spec.service_address],
            label=spec.service_address,
            allowed_actions=frozenset({("no-op",), ("read",), ("update",)}),
        )
        task_after, _task_unknown = _resource_after(
            matches[spec.task_address],
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
        if (
            service_after.get("name") != spec.service_name
            or service_after.get("cluster") not in {_CLUSTER_NAME, _CLUSTER_ARN}
            or task_after.get("family") != spec.task_family
        ):
            raise SagaError("saved plan ECS baseline identity differs")
        planned_task = service_after.get("task_definition")
        task_unknown = service_unknown.get("task_definition")
        if type(planned_task) is str:
            if task_unknown not in {None, False}:
                raise SagaError("saved plan task-definition value is ambiguously known")
            _validate_task_definition(planned_task, expected_family=spec.task_family)
            planned_services[key] = {
                "kind": "arn",
                "taskDefinition": planned_task,
            }
            continue
        if planned_task is not None or task_unknown is not True:
            raise SagaError("saved plan does not determine the desired task definition")
        try:
            payload = task_from_change(task_after, task=key)
            artifact_sha256 = _task_artifact_digest(payload)
        except RolloutGateError as exc:
            raise SagaError("saved plan task-definition payload is invalid") from exc
        planned_services[key] = {
            "kind": "artifact",
            "family": spec.task_family,
            "registerableSha256": artifact_sha256,
        }

    return PlanAnalysis(
        binding={
            "schemaVersion": _SCHEMA_VERSION,
            "services": planned_services,
        }
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


def _canonical_service(raw: object, *, spec: ServiceSpec) -> dict[str, Any]:
    if type(raw) is not dict:
        raise SagaError("ECS service response is invalid")
    if (
        raw.get("serviceArn") != spec.service_arn
        or raw.get("serviceName") != spec.service_name
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


def _read_services(cli: AwsCli) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    expected_arns = {spec.service_arn for spec in _SERVICE_SPECS.values()}
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
            *(spec.service_arn for spec in _SERVICE_SPECS.values()),
        ],
    )
    if frozenset(response) - {"failures", "services"}:
        raise SagaError("ECS service description has unknown fields")
    services = response.get("services")
    failures = response.get("failures")
    if type(services) is not list or failures != [] or len(services) != len(_SERVICE_SPECS):
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
    for key, spec in _SERVICE_SPECS.items():
        raw = by_arn[spec.service_arn]
        baseline[key] = _canonical_service(raw, spec=spec)
        raw_by_key[key] = raw
    return baseline, raw_by_key


def _assert_stable(
    raw_services: Mapping[str, Mapping[str, Any]],
    expected: Mapping[str, Mapping[str, Any]],
) -> None:
    for key, spec in _SERVICE_SPECS.items():
        raw = raw_services.get(key)
        state = expected.get(key)
        if type(raw) is not dict or type(state) is not dict:
            raise SagaError("ECS stable service verification is incomplete")
        running = raw.get("runningCount")
        pending = raw.get("pendingCount")
        deployments = raw.get("deployments")
        desired = state.get("desiredCount")
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
            raise SagaError(f"ECS service {spec.service_name} is not exactly stable")


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
        ):
            raise SagaError("ECS saga identity is invalid")
        self.plan = plan
        self.plan_sha256 = plan_sha256
        self.apply_attempt_id = apply_attempt_id
        self.cli = cli
        self.record_id = f"{_RECORD_PREFIX}{apply_attempt_id}"

    def _read(self) -> dict[str, Any] | None:
        response = self.cli.json(
            "dynamodb",
            "get-item",
            [
                "--table-name",
                _TABLE_NAME,
                "--key",
                json.dumps(
                    _ddb_item({"record_id": self.record_id}),
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
            or _ddb_string(item, "stage") not in {"APPLYING", "APPLIED", "RESTORED"}
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

    def _receipt(self, item: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_item(item)
        receipt = {
            "kind": "teamagent-ecs-service-apply-saga-receipt",
            "schema_version": _SCHEMA_VERSION,
            "record_id": self.record_id,
            "stage": _ddb_string(item, "stage"),
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
        if type(raw) is not dict or frozenset(raw) != frozenset(_SERVICE_SPECS):
            raise SagaError("durable ECS rollback baseline is incomplete")
        baseline: dict[str, dict[str, Any]] = {}
        for key, spec in _SERVICE_SPECS.items():
            value = raw.get(key)
            if type(value) is not dict or frozenset(value) != {
                "deploymentConfiguration",
                "desiredCount",
                "networkConfiguration",
                "taskDefinition",
            }:
                raise SagaError("durable ECS rollback baseline is invalid")
            desired = value.get("desiredCount")
            if type(desired) is not int or desired != 1:
                raise SagaError("durable ECS rollback baseline desired count is invalid")
            baseline[key] = {
                "taskDefinition": _validate_task_definition(
                    value.get("taskDefinition"),
                    expected_family=spec.task_family,
                ),
                "deploymentConfiguration": _canonical_deployment_configuration(
                    value.get("deploymentConfiguration")
                ),
                "networkConfiguration": _canonical_network_configuration(
                    value.get("networkConfiguration")
                ),
                "desiredCount": desired,
            }
        if not hmac.compare_digest(
            _ddb_string(item, "baseline_sha256"), _digest(baseline)
        ) or not hmac.compare_digest(
            baseline_json,
            _canonical_bytes(baseline).decode("utf-8"),
        ):
            raise SagaError("durable ECS rollback baseline digest differs")
        return baseline

    def begin(self) -> dict[str, Any]:
        baseline, raw = _read_services(self.cli)
        _assert_stable(raw, baseline)
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
        try:
            self.cli.run(
                "dynamodb",
                "put-item",
                [
                    "--table-name",
                    _TABLE_NAME,
                    "--item",
                    json.dumps(item, separators=(",", ":"), sort_keys=True),
                    "--condition-expression",
                    "attribute_not_exists(record_id)",
                    "--return-consumed-capacity",
                    "NONE",
                ],
            )
        except Exception as exc:
            # A begin is deliberately one-shot. Even an ambiguous successful Put must be
            # reconciled through finish; replaying begin could capture a post-apply baseline.
            raise SagaError("durable ECS saga already exists or could not begin") from exc
        confirmed = self._read()
        if confirmed is None:
            raise SagaError("durable ECS saga baseline was not confirmed")
        receipt = self._receipt(confirmed)
        if receipt["stage"] != "APPLYING":
            raise SagaError("durable ECS saga baseline stage is invalid")
        return receipt

    def _verify_planned(
        self,
        live: Mapping[str, Mapping[str, Any]],
        raw_live: Mapping[str, Mapping[str, Any]],
    ) -> None:
        planned_services = self.plan.binding.get("services")
        if type(planned_services) is not dict:
            raise SagaError("planned ECS service binding is invalid")
        for key, spec in _SERVICE_SPECS.items():
            expected = planned_services.get(key)
            observed = live.get(key)
            raw = raw_live.get(key)
            if type(expected) is not dict or type(observed) is not dict or type(raw) is not dict:
                raise SagaError("planned ECS service binding is incomplete")
            task_definition = observed.get("taskDefinition")
            if expected.get("kind") == "arn":
                if task_definition != expected.get("taskDefinition"):
                    raise SagaError("live ECS service does not use the planned task definition")
                continue
            if (
                expected.get("kind") != "artifact"
                or expected.get("family") != spec.task_family
                or type(expected.get("registerableSha256")) is not str
            ):
                raise SagaError("planned ECS task artifact binding is invalid")
            response = self.cli.json(
                "ecs",
                "describe-task-definition",
                [
                    "--task-definition",
                    str(task_definition),
                    "--include",
                    "TAGS",
                ],
            )
            definition = response.get("taskDefinition")
            if (
                type(definition) is not dict
                or definition.get("taskDefinitionArn") != task_definition
                or definition.get("family") != spec.task_family
            ):
                raise SagaError("live ECS task artifact identity differs")
            try:
                live_digest = _task_artifact_digest(response)
            except RolloutGateError as exc:
                raise SagaError("live ECS task artifact is invalid") from exc
            if not hmac.compare_digest(
                live_digest,
                str(expected["registerableSha256"]),
            ):
                raise SagaError("live ECS service task artifact differs from the saved plan")

    def _restore(self, baseline: Mapping[str, Mapping[str, Any]]) -> None:
        for key, spec in _SERVICE_SPECS.items():
            state = baseline.get(key)
            if type(state) is not dict:
                raise SagaError("durable ECS rollback baseline is incomplete")
            response = self.cli.json(
                "ecs",
                "update-service",
                [
                    "--cluster",
                    _CLUSTER_ARN,
                    "--service",
                    spec.service_arn,
                    "--task-definition",
                    str(state["taskDefinition"]),
                    "--desired-count",
                    str(state["desiredCount"]),
                    "--deployment-configuration",
                    json.dumps(
                        state["deploymentConfiguration"],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "--network-configuration",
                    json.dumps(
                        state["networkConfiguration"],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ],
            )
            returned = response.get("service")
            if type(returned) is not dict:
                raise SagaError("ECS rollback update response is invalid")
            if _canonical_service(returned, spec=spec) != state:
                raise SagaError("ECS rollback update did not accept the exact baseline")
        self.cli.run(
            "ecs",
            "wait services-stable",
            [
                "--cluster",
                _CLUSTER_ARN,
                "--services",
                *(spec.service_arn for spec in _SERVICE_SPECS.values()),
            ],
            timeout_seconds=900,
        )
        restored, raw_restored = _read_services(self.cli)
        if restored != baseline:
            raise SagaError("ECS exact rollback baseline could not be verified")
        _assert_stable(raw_restored, baseline)

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
        try:
            self.cli.run(
                "dynamodb",
                "update-item",
                [
                    "--table-name",
                    _TABLE_NAME,
                    "--key",
                    json.dumps(
                        _ddb_item({"record_id": self.record_id}),
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "--update-expression",
                    "SET #stage = :desired",
                    "--condition-expression",
                    (
                        "#stage = :applying AND plan_sha256 = :plan"
                        " AND apply_attempt_id = :attempt"
                        " AND baseline_sha256 = :baseline"
                        " AND planned_sha256 = :planned"
                    ),
                    "--expression-attribute-names",
                    '{"#stage":"stage"}',
                    "--expression-attribute-values",
                    json.dumps(values, separators=(",", ":"), sort_keys=True),
                    "--return-values",
                    "NONE",
                ],
            )
        except Exception as exc:
            confirmed = self._read()
            if confirmed is None:
                raise SagaError("durable ECS saga completion CAS failed") from exc
            self._validate_item(confirmed)
            if _ddb_string(confirmed, "stage") != desired:
                raise SagaError("durable ECS saga completion CAS failed") from exc
            return confirmed
        confirmed = self._read()
        if confirmed is None:
            raise SagaError("durable ECS saga completion was not persisted")
        self._validate_item(confirmed)
        if _ddb_string(confirmed, "stage") != desired:
            raise SagaError("durable ECS saga completion was not persisted")
        return confirmed

    def finish(self, *, outcome: str) -> dict[str, Any]:
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
            live, raw_live = _read_services(self.cli)
            self._verify_planned(live, raw_live)
            _assert_stable(raw_live, live)
        return self._receipt(self._transition(item, desired=desired))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("begin", "finish"))
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
        if (args.action == "begin") != (args.outcome is None):
            raise SagaError("begin rejects outcomes and finish requires one")
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
        else:
            assert args.outcome is not None
            result = saga.finish(outcome=args.outcome)
    except Exception:
        print('{"code":"ecs_service_apply_saga_failed","ok":false}')
        return 2
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
