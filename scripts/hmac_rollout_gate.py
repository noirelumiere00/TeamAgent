#!/usr/bin/env python3
"""Live, secret-free gate for the purpose-separated HMAC rollout.

The reviewed transition manifest remains an assertion, never an authority. This gate obtains:

* current ECS service and EventBridge task definitions;
* exact Secrets Manager VersionIds through ``ListSecretVersionIds`` only;
* worker effective generation metadata from its durable readiness attestation; and
* time from signed AWS response headers.

It then compares those observations with the manifest and performs conditional DynamoDB
transitions. It never calls ``GetSecretValue`` and never emits generation identifiers, task JSON,
tokens, or key material.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from scripts.preflight_hmac_rotation import (  # noqa: E402
    validate_rendered_tasks,
    validate_worker_env,
)
from scripts.verify_worker_bundle_provenance import (  # noqa: E402
    ProvenanceBinding,
)
from scripts.verify_worker_bundle_provenance import (  # noqa: E402
    verify as verify_worker_provenance,
)
from teamagent.hmac_durable_state import (  # noqa: E402
    HmacRuntimeExpectation,
    runtime_expectations_digest,
)
from teamagent.hmac_keyring import (  # noqa: E402
    MAIL_ACTION_MAX_TOKEN_TTL_S,
    REPORT_LINK_MAX_TOKEN_TTL_S,
    validate_hmac_rotation_transition,
)

_MAX_CLOCK_SKEW_S = 60
_MAX_AWS_CLOCK_SPREAD_S = 10
_ISSUER_CUTOVER_S = 900
_PROVENANCE_RE = re.compile(r"^[a-f0-9]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_ROTATION_EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_TASK_DEFINITION_RE = re.compile(
    r"^arn:aws[a-z-]*:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$"
)
_IMAGE_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
_RESTART_NONCE_RE = re.compile(r"^[a-f0-9]{64}$")
_RELEASE_ROOT_RE = re.compile(r"^/opt/teamagent/releases/[a-f0-9]{64}$")
_APPLY_ATTEMPT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_WORKER_EXPORT_RE = re.compile(r"^export ([A-Z][A-Z0-9_]*)='([^']*)'$")
_TASK_DOMAINS = {
    "mcp": frozenset({"mail_action", "report_link"}),
    "connect_web": frozenset({"report_link"}),
    "morning_digest": frozenset({"mail_action"}),
}
_WORKER_BINDING_NAMES = frozenset(
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
        "promotion_attester",
        "provenance_verifier",
        "release_measurer",
        "reviewed_manifest",
        "rollback_artifact",
        "rollback_env",
        "rollback_receipt",
        "rollback_signature",
        "rollout_control",
        "runtime_lock",
    }
)
_DOMAIN_MAX_TTL = {
    "mail_action": MAIL_ACTION_MAX_TOKEN_TTL_S,
    "report_link": REPORT_LINK_MAX_TOKEN_TTL_S,
}
_DOMAIN_NAMES = {
    "mail_action": {
        "primary_generation": "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
        "previous_generation": "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
        "t0": "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        "primary_secret": "MAIL_ACTION_HMAC_SECRET",
        "previous_secret": "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    },
    "report_link": {
        "primary_generation": "REPORT_LINK_HMAC_PRIMARY_GENERATION",
        "previous_generation": "REPORT_LINK_HMAC_PREVIOUS_GENERATION",
        "t0": "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        "primary_secret": "REPORT_LINK_HMAC_SECRET",
        "previous_secret": "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    },
}
_RUNTIME_ENV = {
    "required": "TEAMAGENT_HMAC_STATE_REQUIRED",
    "table": "TEAMAGENT_HMAC_STATE_TABLE",
    "scope": "TEAMAGENT_HMAC_STATE_SCOPE",
    "epoch": "TEAMAGENT_HMAC_ROTATION_EPOCH",
    "provenance": "TEAMAGENT_HMAC_PROVENANCE",
}
_LEDGER_STAGES = (
    "initialized",
    "connect_web_preloaded",
    "worker_verified",
    "mcp_stable_and_old_drained",
    "complete",
)
_CLEANUP_STAGES = frozenset({"aborted", "authorized", "complete"})
_DEPLOYMENT_INTENT_TABLE = "teamagent-dev-image-deployment-intents"
_TASK_INVENTORY_LIMIT = 10_000
_DESCRIBE_TASK_BATCH = 100
_TASK_REGISTERABLE_KEYS = frozenset(
    {
        "containerDefinitions",
        "cpu",
        "enableFaultInjection",
        "ephemeralStorage",
        "executionRoleArn",
        "family",
        "inferenceAccelerators",
        "ipcMode",
        "memory",
        "networkMode",
        "pidMode",
        "placementConstraints",
        "proxyConfiguration",
        "requiresCompatibilities",
        "runtimePlatform",
        "tags",
        "taskRoleArn",
        "volumes",
    }
)
_TASK_SERVER_KEYS = frozenset(
    {
        "compatibilities",
        "deregisteredAt",
        "registeredAt",
        "registeredBy",
        "requiresAttributes",
        "revision",
        "status",
        "taskDefinitionArn",
    }
)
_TASK_REQUIRED_KEYS = frozenset(
    {
        "containerDefinitions",
        "cpu",
        "executionRoleArn",
        "family",
        "memory",
        "networkMode",
        "requiresCompatibilities",
        "runtimePlatform",
        "taskRoleArn",
        "volumes",
    }
)


class RolloutGateError(RuntimeError):
    """One secret-free rollout invariant failed."""

    def __init__(self, code: str, *, scope: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.scope = scope


class AwsClientFactory(Protocol):
    def client(self, service_name: str, *, region_name: str) -> Any:
        """Return a low-level AWS client."""


@dataclass(frozen=True)
class WorkloadControl:
    cluster: str
    service: str
    legacy_task_definition: str
    provenance: str
    rollback_provenance: str
    rollback_task_definition: str
    rollback_image: str


@dataclass(frozen=True)
class ScheduledControl:
    cluster: str
    rule: str
    target_id: str
    legacy_task_definition: str
    provenance: str
    rollback_provenance: str
    rollback_task_definition: str
    rollback_image: str
    legacy_target_digest: str
    rollback_target_digest: str
    expected_rule: dict[str, object]


@dataclass(frozen=True)
class WorkerControl:
    instance_id: str
    provenance: str
    artifact_sha256: str
    rollback_provenance: str
    rollback_artifact_sha256: str


@dataclass(frozen=True)
class CanaryControl:
    rule: str
    target_id: str
    task_definition: str


@dataclass(frozen=True)
class RolloutControl:
    region: str
    scope: str
    state_table: str
    rotation_epoch: str
    mcp: WorkloadControl
    connect_web: WorkloadControl
    morning_digest: ScheduledControl
    worker: WorkerControl
    canary: CanaryControl
    forbidden_signing_task_definitions: frozenset[str]


@dataclass(frozen=True)
class Ledger:
    stage: str
    revision: int
    updated_at: int
    trusted_now: int


@dataclass(frozen=True)
class CleanupLedger:
    domain: str
    stage: str
    revision: int
    authorized_at: int
    old_provenances: dict[str, frozenset[str]]
    new_provenances: dict[str, frozenset[str]]
    candidate_digests: dict[str, str]
    rollback_digests: dict[str, str]
    proposed: dict[str, dict[str, object]]
    legacy_database_generation: str
    legacy_worker_generation: str | None
    candidate_worker_env_digest: str
    rollback_worker_env_digest: str
    candidate_arns: dict[str, str | None]
    prepared_plan_sha256: str
    prepared_intent_id: str
    baseline_arns: dict[str, str]
    baseline_digests: dict[str, str]
    baseline_provenances: dict[str, frozenset[str]]
    candidate_worker_provenance: ProvenanceBinding
    rollback_worker_provenance: ProvenanceBinding
    worker_bindings: dict[str, str]


@dataclass(frozen=True)
class DeploymentIntent:
    plan_sha256: str
    apply_attempt_id: str


def _mapping(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise RolloutGateError("invalid_control")
    return value


def _exact_keys(value: dict[str, Any], expected: frozenset[str]) -> None:
    if frozenset(value) != expected:
        raise RolloutGateError("invalid_control")


def _bounded_text(value: object, *, maximum: int = 255) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)
    ):
        raise RolloutGateError("invalid_control")
    return value


def _provenance(value: object) -> str:
    text = _bounded_text(value, maximum=64)
    if _PROVENANCE_RE.fullmatch(text) is None:
        raise RolloutGateError("invalid_control")
    return text


def _task_definition(value: object) -> str:
    text = _bounded_text(value, maximum=512)
    if _TASK_DEFINITION_RE.fullmatch(text) is None:
        raise RolloutGateError("invalid_control")
    return text


def _image(value: object) -> str:
    text = _bounded_text(value, maximum=2048)
    if _IMAGE_DIGEST_RE.fullmatch(text) is None:
        raise RolloutGateError("invalid_control")
    return text


def _workload_control(value: object) -> WorkloadControl:
    item = _mapping(value)
    _exact_keys(
        item,
        frozenset(
            {
                "cluster",
                "service",
                "legacy_task_definition",
                "provenance",
                "rollback_provenance",
                "rollback_task_definition",
                "rollback_image",
            }
        ),
    )
    return WorkloadControl(
        cluster=_bounded_text(item["cluster"]),
        service=_bounded_text(item["service"]),
        legacy_task_definition=_task_definition(item["legacy_task_definition"]),
        provenance=_provenance(item["provenance"]),
        rollback_provenance=_provenance(item["rollback_provenance"]),
        rollback_task_definition=_task_definition(item["rollback_task_definition"]),
        rollback_image=_image(item["rollback_image"]),
    )


def _scheduled_control(value: object) -> ScheduledControl:
    item = _mapping(value)
    _exact_keys(
        item,
        frozenset(
            {
                "cluster",
                "rule",
                "target_id",
                "legacy_task_definition",
                "provenance",
                "rollback_provenance",
                "rollback_task_definition",
                "rollback_image",
                "legacy_target_digest",
                "rollback_target_digest",
                "expected_rule",
            }
        ),
    )
    expected_rule = _canonical_event_rule(item["expected_rule"])
    if expected_rule["Name"] != item["rule"] or expected_rule["State"] != "DISABLED":
        raise RolloutGateError("invalid_control")
    return ScheduledControl(
        cluster=_bounded_text(item["cluster"], maximum=512),
        rule=_bounded_text(item["rule"]),
        target_id=_bounded_text(item["target_id"]),
        legacy_task_definition=_task_definition(item["legacy_task_definition"]),
        provenance=_provenance(item["provenance"]),
        rollback_provenance=_provenance(item["rollback_provenance"]),
        rollback_task_definition=_task_definition(item["rollback_task_definition"]),
        rollback_image=_image(item["rollback_image"]),
        legacy_target_digest=_provenance(item["legacy_target_digest"]),
        rollback_target_digest=_provenance(item["rollback_target_digest"]),
        expected_rule=expected_rule,
    )


def _worker_control(value: object) -> WorkerControl:
    item = _mapping(value)
    _exact_keys(
        item,
        frozenset(
            {
                "instance_id",
                "provenance",
                "artifact_sha256",
                "rollback_provenance",
                "rollback_artifact_sha256",
            }
        ),
    )
    artifact_hash = _provenance(item["rollback_artifact_sha256"])
    return WorkerControl(
        instance_id=_bounded_text(item["instance_id"]),
        provenance=_provenance(item["provenance"]),
        artifact_sha256=_provenance(item["artifact_sha256"]),
        rollback_provenance=_provenance(item["rollback_provenance"]),
        rollback_artifact_sha256=artifact_hash,
    )


def _canary_control(value: object) -> CanaryControl:
    item = _mapping(value)
    _exact_keys(item, frozenset({"rule", "target_id", "task_definition"}))
    task_definition = _task_definition(item["task_definition"])
    if not task_definition.endswith(":14"):
        raise RolloutGateError("canary_anchor_changed", scope="canary")
    return CanaryControl(
        rule=_bounded_text(item["rule"]),
        target_id=_bounded_text(item["target_id"]),
        task_definition=task_definition,
    )


def load_control(value: object) -> RolloutControl:
    """Parse the exact secret-free live-control contract."""

    item = _mapping(value)
    _exact_keys(
        item,
        frozenset(
            {
                "schema",
                "region",
                "scope",
                "state_table",
                "rotation_epoch",
                "services",
                "morning_digest",
                "worker",
                "canary",
                "forbidden_signing_task_definitions",
            }
        ),
    )
    if item["schema"] != 1:
        raise RolloutGateError("invalid_control")
    services = _mapping(item["services"])
    _exact_keys(services, frozenset({"mcp", "connect_web"}))
    region = _bounded_text(item["region"])
    scope = _bounded_text(item["scope"], maximum=128)
    table = _bounded_text(item["state_table"])
    epoch = _bounded_text(item["rotation_epoch"], maximum=128)
    if (
        _SCOPE_RE.fullmatch(scope) is None
        or _TABLE_RE.fullmatch(table) is None
        or _ROTATION_EPOCH_RE.fullmatch(epoch) is None
    ):
        raise RolloutGateError("invalid_control")
    forbidden = item["forbidden_signing_task_definitions"]
    if type(forbidden) is not list or not forbidden:
        raise RolloutGateError("invalid_control")
    forbidden_set = frozenset(_task_definition(value) for value in forbidden)
    if not any(value.endswith(":53") for value in forbidden_set):
        raise RolloutGateError("td53_contract_missing", scope="connect_web")
    control = RolloutControl(
        region=region,
        scope=scope,
        state_table=table,
        rotation_epoch=epoch,
        mcp=_workload_control(services["mcp"]),
        connect_web=_workload_control(services["connect_web"]),
        morning_digest=_scheduled_control(item["morning_digest"]),
        worker=_worker_control(item["worker"]),
        canary=_canary_control(item["canary"]),
        forbidden_signing_task_definitions=forbidden_set,
    )
    rollback_tasks = (
        control.mcp.rollback_task_definition,
        control.connect_web.rollback_task_definition,
        control.morning_digest.rollback_task_definition,
    )
    legacy_tasks = (
        control.mcp.legacy_task_definition,
        control.connect_web.legacy_task_definition,
        control.morning_digest.legacy_task_definition,
    )
    workload_families = (
        (_task_family(control.mcp.legacy_task_definition), _task_family(rollback_tasks[0])),
        (
            _task_family(control.connect_web.legacy_task_definition),
            _task_family(rollback_tasks[1]),
        ),
        (
            _task_family(control.morning_digest.legacy_task_definition),
            _task_family(rollback_tasks[2]),
        ),
    )
    provenances = (
        control.mcp.provenance,
        control.mcp.rollback_provenance,
        control.connect_web.provenance,
        control.connect_web.rollback_provenance,
        control.morning_digest.provenance,
        control.morning_digest.rollback_provenance,
        control.worker.provenance,
        control.worker.rollback_provenance,
    )
    if (
        len(set(rollback_tasks)) != len(rollback_tasks)
        or set(rollback_tasks) & forbidden_set
        or set(rollback_tasks) & set(legacy_tasks)
        or any(legacy != rollback for legacy, rollback in workload_families)
        or len({legacy for legacy, _rollback in workload_families}) != len(workload_families)
        or len(set(provenances)) != len(provenances)
        or control.worker.artifact_sha256 == control.worker.rollback_artifact_sha256
    ):
        raise RolloutGateError("invalid_control")
    return control


def _trusted_epoch(response: object) -> int:
    if type(response) is not dict:
        raise RolloutGateError("trusted_clock_unavailable")
    metadata = response.get("ResponseMetadata")
    headers = metadata.get("HTTPHeaders") if type(metadata) is dict else None
    raw = headers.get("date") if type(headers) is dict else None
    if type(raw) is not str:
        raise RolloutGateError("trusted_clock_unavailable")
    try:
        parsed = parsedate_to_datetime(raw)
        epoch = int(parsed.timestamp()) if parsed.tzinfo is not None else -1
    except (OverflowError, TypeError, ValueError) as exc:
        raise RolloutGateError("trusted_clock_unavailable") from exc
    if epoch < 0 or epoch > 9_999_999_999:
        raise RolloutGateError("trusted_clock_unavailable")
    return epoch


def _named(entries: object, key: str) -> dict[str, str]:
    if type(entries) is not list:
        raise RolloutGateError("live_task_invalid")
    values: dict[str, str] = {}
    for raw in entries:
        item = _mapping(raw)
        name = item.get("name")
        value = item.get(key)
        if type(name) is not str or type(value) is not str or name in values:
            raise RolloutGateError("live_task_invalid")
        values[name] = value
    return values


def _one_container(definition: object) -> dict[str, Any]:
    item = _mapping(definition)
    if "taskDefinition" in item:
        item = _mapping(item["taskDefinition"])
    containers = item.get("containerDefinitions")
    if type(containers) is not list or len(containers) != 1:
        raise RolloutGateError("live_task_invalid")
    return _mapping(containers[0])


def _content_provenance(values: dict[str, str]) -> str:
    encoded = json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _secret_reference(reference: str) -> tuple[str, str | None]:
    resource, separator, version = reference.rpartition(":::")
    if separator:
        if _VERSION_RE.fullmatch(version) is None:
            raise RolloutGateError("secret_version_invalid")
        return resource, version
    return reference, None


def _task_family(task_definition: str) -> str:
    family_revision = task_definition.rsplit("/", maxsplit=1)[-1]
    family, separator, revision = family_revision.rpartition(":")
    if not separator or not family or not revision.isascii() or not revision.isdecimal():
        raise RolloutGateError("live_task_invalid")
    return family


def _canonical_json_value(value: object) -> object:
    """Normalize JSON-compatible AWS request values without dropping any field."""

    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise RolloutGateError("live_task_invalid")
        return {str(key): _canonical_json_value(item) for key, item in sorted(value.items())}
    if type(value) is list:
        return [_canonical_json_value(item) for item in value]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise RolloutGateError("live_task_invalid")


def _canonical_registerable_task_definition(definition: dict[str, Any]) -> dict[str, object]:
    """Return the complete RegisterTaskDefinition payload.

    ECS response-only attributes are the only task-definition fields removed. Unknown fields fail
    closed so a newly introduced registerable attribute cannot silently escape the artifact hash.
    """

    wrapper = _mapping(definition)
    raw = _mapping(wrapper["taskDefinition"]) if "taskDefinition" in wrapper else wrapper
    unknown = frozenset(raw) - _TASK_REGISTERABLE_KEYS - _TASK_SERVER_KEYS
    if unknown or not _TASK_REQUIRED_KEYS.issubset(raw):
        raise RolloutGateError("task_artifact_incomplete")
    family = raw.get("family")
    arn = raw.get("taskDefinitionArn")
    if (
        type(family) is not str
        or not family
        or (
            arn is not None
            and (
                type(arn) is not str
                or _TASK_DEFINITION_RE.fullmatch(arn) is None
                or _task_family(arn) != family
            )
        )
    ):
        raise RolloutGateError("task_artifact_incomplete")
    containers = raw.get("containerDefinitions")
    if type(containers) is not list or not containers:
        raise RolloutGateError("task_artifact_incomplete")
    payload = {
        key: _canonical_json_value(raw[key])
        for key in sorted(_TASK_REGISTERABLE_KEYS)
        if key in raw and key != "tags"
    }
    requires = payload.get("requiresCompatibilities")
    if type(requires) is not list or any(type(value) is not str for value in requires):
        raise RolloutGateError("task_artifact_incomplete")
    payload["requiresCompatibilities"] = sorted(requires)
    for name, identity in (
        ("inferenceAccelerators", "deviceName"),
        ("volumes", "name"),
    ):
        values = payload.get(name)
        if values is None:
            continue
        if type(values) is not list or any(
            type(value) is not dict or type(value.get(identity)) is not str for value in values
        ):
            raise RolloutGateError("task_artifact_incomplete")
        if len({str(value[identity]) for value in values}) != len(values):
            raise RolloutGateError("task_artifact_incomplete")
        payload[name] = sorted(values, key=lambda value: str(value[identity]))
    constraints = payload.get("placementConstraints")
    if constraints is not None:
        if type(constraints) is not list or any(type(value) is not dict for value in constraints):
            raise RolloutGateError("task_artifact_incomplete")
        payload["placementConstraints"] = sorted(
            constraints,
            key=lambda value: json.dumps(value, separators=(",", ":"), sort_keys=True),
        )
    tags = wrapper.get("tags") if "taskDefinition" in wrapper else raw.get("tags")
    if tags is not None:
        if type(tags) is not list:
            raise RolloutGateError("task_artifact_incomplete")
        normalized_tags: list[dict[str, str]] = []
        for raw_tag in tags:
            tag = _mapping(raw_tag)
            if frozenset(tag) != frozenset({"key", "value"}):
                raise RolloutGateError("task_artifact_incomplete")
            key = tag.get("key")
            value = tag.get("value")
            if type(key) is not str or type(value) is not str:
                raise RolloutGateError("task_artifact_incomplete")
            normalized_tags.append({"key": key, "value": value})
        if len({tag["key"] for tag in normalized_tags}) != len(normalized_tags):
            raise RolloutGateError("task_artifact_incomplete")
        payload["tags"] = sorted(normalized_tags, key=lambda tag: (tag["key"], tag["value"]))
    return payload


def _task_artifact_digest(definition: dict[str, Any]) -> str:
    """Digest the exact complete payload accepted by RegisterTaskDefinition."""

    payload = _canonical_registerable_task_definition(definition)
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_EVENT_RULE_FIELDS = frozenset(
    {
        "Arn",
        "CreatedBy",
        "Description",
        "EventBusName",
        "EventPattern",
        "ManagedBy",
        "Name",
        "RoleArn",
        "ScheduleExpression",
        "State",
    }
)


def _canonical_event_rule(value: object) -> dict[str, object]:
    """Normalize every DescribeRule field; omitted optional fields become explicit nulls."""

    raw = _mapping(value)
    unknown = frozenset(raw) - (_EVENT_RULE_FIELDS | {"ResponseMetadata"})
    if unknown:
        raise RolloutGateError("scheduled_rule_invalid", scope="morning_digest")
    normalized: dict[str, object] = {}
    for name in _EVENT_RULE_FIELDS:
        item = raw.get(name)
        if item is not None and (
            type(item) is not str
            or not item
            or len(item) > 4096
            or item != item.strip()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in item)
        ):
            raise RolloutGateError("scheduled_rule_invalid", scope="morning_digest")
        normalized[name] = item
    if (
        type(normalized["Name"]) is not str
        or type(normalized["Arn"]) is not str
        or normalized["State"]
        not in {
            "DISABLED",
            "ENABLED",
            "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS",
        }
        or type(normalized["EventBusName"]) is not str
        or (normalized["ScheduleExpression"] is None) == (normalized["EventPattern"] is None)
    ):
        raise RolloutGateError("scheduled_rule_invalid", scope="morning_digest")
    event_pattern = normalized["EventPattern"]
    if event_pattern is not None:
        try:
            parsed_pattern = json.loads(str(event_pattern))
        except json.JSONDecodeError as exc:
            raise RolloutGateError(
                "scheduled_rule_invalid",
                scope="morning_digest",
            ) from exc
        if type(parsed_pattern) is not dict:
            raise RolloutGateError("scheduled_rule_invalid", scope="morning_digest")
        normalized["EventPattern"] = json.dumps(
            _canonical_json_value(parsed_pattern),
            separators=(",", ":"),
            sort_keys=True,
        )
    return {name: normalized[name] for name in sorted(normalized)}


def _canonical_event_rule_digest(value: object) -> str:
    encoded = json.dumps(
        _canonical_event_rule(value),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_target_digest(target: object) -> str:
    item = _mapping(target)
    required = frozenset(
        {
            "Id",
            "Arn",
            "RoleArn",
            "EcsParameters",
            "RetryPolicy",
        }
    )
    if not required.issubset(item):
        raise RolloutGateError("scheduled_target_invalid", scope="morning_digest")
    ecs_parameters = _mapping(item.get("EcsParameters"))
    network = _mapping(ecs_parameters.get("NetworkConfiguration"))
    awsvpc = _mapping(network.get("awsvpcConfiguration"))
    retry = _mapping(item.get("RetryPolicy"))
    subnets = awsvpc.get("Subnets")
    security_groups = awsvpc.get("SecurityGroups")
    if (
        type(item.get("Id")) is not str
        or not item.get("Id")
        or type(item.get("Arn")) is not str
        or not item.get("Arn")
        or type(item.get("RoleArn")) is not str
        or not item.get("RoleArn")
        or ("Input" in item and type(item.get("Input")) is not str)
        or type(ecs_parameters.get("TaskDefinitionArn")) is not str
        or type(ecs_parameters.get("TaskCount")) is not int
        or ecs_parameters.get("TaskCount", 0) < 1
        or ecs_parameters.get("LaunchType") != "FARGATE"
        or type(ecs_parameters.get("PlatformVersion")) is not str
        or not ecs_parameters.get("PlatformVersion")
        or type(subnets) is not list
        or not subnets
        or any(type(value) is not str or not value for value in subnets)
        or len(set(subnets)) != len(subnets)
        or type(security_groups) is not list
        or not security_groups
        or any(type(value) is not str or not value for value in security_groups)
        or len(set(security_groups)) != len(security_groups)
        or awsvpc.get("AssignPublicIp") not in {"ENABLED", "DISABLED"}
        or type(retry.get("MaximumEventAgeInSeconds")) is not int
        or retry.get("MaximumEventAgeInSeconds", 0) < 60
        or type(retry.get("MaximumRetryAttempts")) is not int
        or retry.get("MaximumRetryAttempts", -1) < 0
    ):
        raise RolloutGateError("scheduled_target_invalid", scope="morning_digest")
    normalized = copy.deepcopy(item)
    normalized_ecs = _mapping(normalized["EcsParameters"])
    normalized_network = _mapping(normalized_ecs["NetworkConfiguration"])
    normalized_awsvpc = _mapping(normalized_network["awsvpcConfiguration"])
    normalized_awsvpc["Subnets"] = sorted(subnets)
    normalized_awsvpc["SecurityGroups"] = sorted(security_groups)
    encoded = json.dumps(
        _canonical_json_value(normalized),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_provenance(definition: dict[str, Any], *, task: str) -> str:
    container = _one_container(definition)
    environment = _named(container.get("environment", []), "value")
    try:
        return _provenance(environment[_RUNTIME_ENV["provenance"]])
    except KeyError as exc:
        raise RolloutGateError("runtime_metadata_drift", scope=task) from exc


def _reserved_task_definitions(control: RolloutControl) -> frozenset[str]:
    return frozenset(
        {
            control.mcp.legacy_task_definition,
            control.connect_web.legacy_task_definition,
            control.morning_digest.legacy_task_definition,
            control.mcp.rollback_task_definition,
            control.connect_web.rollback_task_definition,
            control.morning_digest.rollback_task_definition,
            *control.forbidden_signing_task_definitions,
        }
    )


def _task_control(
    control: RolloutControl,
    task: str,
) -> WorkloadControl | ScheduledControl:
    if task == "mcp":
        return control.mcp
    if task == "connect_web":
        return control.connect_web
    if task == "morning_digest":
        return control.morning_digest
    raise RolloutGateError("unknown_task")


class LiveRolloutGate:
    """AWS-backed control-plane gate with injectable clients for offline tests."""

    def __init__(
        self,
        *,
        control: RolloutControl,
        manifest: dict[str, Any],
        clients: AwsClientFactory,
        deployment_intent: DeploymentIntent | None = None,
    ) -> None:
        self.control = control
        self.manifest = manifest
        self.ecs = clients.client("ecs", region_name=control.region)
        self.events = clients.client("events", region_name=control.region)
        self.secrets = clients.client("secretsmanager", region_name=control.region)
        self.ddb = clients.client("dynamodb", region_name=control.region)
        self._clients = clients
        self.deployment_intent = deployment_intent
        self._started_monotonic = time.monotonic()
        self._observed_times: list[tuple[int, float]] = []

    def _observe(self, response: object) -> None:
        self._observed_times.append((_trusted_epoch(response), time.monotonic()))

    def _now(self) -> int:
        if not self._observed_times:
            raise RolloutGateError("trusted_clock_unavailable")
        offsets = [server_epoch - received_at for server_epoch, received_at in self._observed_times]
        if max(offsets) - min(offsets) > _MAX_AWS_CLOCK_SPREAD_S:
            raise RolloutGateError("trusted_clock_disagreement")
        monotonic_now = time.monotonic()
        now = int(
            max(
                server_epoch + (monotonic_now - received_at)
                for server_epoch, received_at in self._observed_times
            )
        )
        manifest_now = self.manifest.get("now")
        projected_manifest_now = (
            manifest_now + (monotonic_now - self._started_monotonic)
            if type(manifest_now) is int
            else None
        )
        if projected_manifest_now is None or abs(projected_manifest_now - now) > _MAX_CLOCK_SKEW_S:
            raise RolloutGateError("manifest_time_stale")
        return now

    def _version_for_reference(self, reference: str) -> tuple[str, str]:
        resource, pinned = _secret_reference(reference)
        if pinned is None:
            raise RolloutGateError("secret_reference_unpinned")
        response = self.secrets.list_secret_version_ids(
            SecretId=resource,
            IncludeDeprecated=True,
        )
        self._observe(response)
        versions = response.get("Versions") if type(response) is dict else None
        if type(versions) is not list:
            raise RolloutGateError("secret_generation_unavailable")
        matches = [
            version
            for version in versions
            if type(version) is dict and version.get("VersionId") == pinned
        ]
        if len(matches) != 1:
            raise RolloutGateError("secret_generation_unavailable")
        version_id = matches[0].get("VersionId")
        if type(version_id) is not str or _VERSION_RE.fullmatch(version_id) is None:
            raise RolloutGateError("secret_generation_unavailable")
        return resource, version_id

    def _describe_task(self, task_definition: str) -> dict[str, Any]:
        response = self.ecs.describe_task_definition(
            taskDefinition=task_definition,
            include=["TAGS"],
        )
        self._observe(response)
        definition = response.get("taskDefinition") if type(response) is dict else None
        item = _mapping(definition)
        if item.get("taskDefinitionArn") != task_definition:
            raise RolloutGateError("live_task_invalid")
        tags = response.get("tags") if type(response) is dict else None
        if tags is not None:
            if type(tags) is not list:
                raise RolloutGateError("live_task_invalid")
            item["tags"] = copy.deepcopy(tags)
        return item

    def _service_task(self, workload: WorkloadControl) -> tuple[str, dict[str, Any]]:
        response = self.ecs.describe_services(
            cluster=workload.cluster,
            services=[workload.service],
        )
        self._observe(response)
        services = response.get("services") if type(response) is dict else None
        failures = response.get("failures") if type(response) is dict else None
        if (
            type(services) is not list
            or len(services) != 1
            or type(failures) is not list
            or failures
        ):
            raise RolloutGateError("live_service_unavailable")
        service = _mapping(services[0])
        task_definition = service.get("taskDefinition")
        if type(task_definition) is not str:
            raise RolloutGateError("live_service_unavailable")
        return task_definition, self._describe_task(task_definition)

    def _event_targets(self, rule: str) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        tokens: set[str] = set()
        next_token: str | None = None
        while True:
            arguments: dict[str, object] = {"Rule": rule, "Limit": 100}
            if next_token is not None:
                arguments["NextToken"] = next_token
            response = self.events.list_targets_by_rule(**arguments)
            self._observe(response)
            page = response.get("Targets") if type(response) is dict else None
            token = response.get("NextToken") if type(response) is dict else None
            if (
                type(page) is not list
                or (token is not None and (type(token) is not str or not token))
                or len(targets) + len(page) > _TASK_INVENTORY_LIMIT
            ):
                raise RolloutGateError(
                    "scheduled_target_unavailable",
                    scope="morning_digest",
                )
            targets.extend(copy.deepcopy(_mapping(target)) for target in page)
            if token is None:
                return targets
            if token in tokens:
                raise RolloutGateError(
                    "scheduled_target_unavailable",
                    scope="morning_digest",
                )
            tokens.add(token)
            next_token = token

    def _assert_morning_rule_disabled(self) -> None:
        scheduled = self.control.morning_digest
        response = self.events.describe_rule(Name=scheduled.rule)
        self._observe(response)
        observed = _canonical_event_rule(response)
        if observed != scheduled.expected_rule:
            raise RolloutGateError("scheduled_rule_drift", scope="morning_digest")

    def _morning_target(self) -> dict[str, Any]:
        scheduled = self.control.morning_digest
        self._assert_morning_rule_disabled()
        targets = self._event_targets(scheduled.rule)
        if len(targets) != 1 or targets[0].get("Id") != scheduled.target_id:
            raise RolloutGateError("scheduled_target_unavailable", scope="morning_digest")
        target = targets[0]
        if target.get("Arn") != scheduled.cluster:
            raise RolloutGateError("scheduled_target_unavailable", scope="morning_digest")
        _canonical_target_digest(target)
        return target

    def _scheduled_task(self) -> tuple[str, dict[str, Any]]:
        target = self._morning_target()
        ecs_parameters = _mapping(target.get("EcsParameters"))
        task_definition = ecs_parameters.get("TaskDefinitionArn")
        if type(task_definition) is not str:
            raise RolloutGateError("scheduled_target_unavailable", scope="morning_digest")
        digest = _canonical_target_digest(target)
        scheduled = self.control.morning_digest
        expected_digest: str | None
        if task_definition == scheduled.legacy_task_definition:
            expected_digest = scheduled.legacy_target_digest
        elif task_definition == scheduled.rollback_task_definition:
            expected_digest = scheduled.rollback_target_digest
        else:
            cleanup = self._active_cleanup()
            cleanup_item = (
                self._read_item(self._cleanup_record_name(cleanup.domain))
                if cleanup is not None
                else None
            )
            cleanup_arn = (
                self._ddb_string(
                    cleanup_item,
                    "candidate_morning_digest_arn",
                    optional=True,
                )
                if cleanup_item is not None
                else None
            )
            record = (
                self._cleanup_record_name(cleanup.domain)
                if cleanup is not None and cleanup_arn == task_definition
                else f"LEDGER#{self.control.rotation_epoch}"
            )
            item = self._read_item(record)
            expected_digest = self._ddb_string(
                item,
                "candidate_morning_digest_target_digest",
                optional=True,
            )
            expected_arn = self._ddb_string(
                item,
                "candidate_morning_digest_arn",
                optional=True,
            )
            if expected_arn != task_definition:
                raise RolloutGateError("scheduled_target_drift", scope="morning_digest")
        if digest != expected_digest:
            raise RolloutGateError("scheduled_target_drift", scope="morning_digest")
        return task_definition, self._describe_task(task_definition)

    def _listed_task_arns(
        self,
        *,
        cluster: str,
        desired_status: str,
        service_name: str | None = None,
        family: str | None = None,
    ) -> list[str]:
        task_arns: list[str] = []
        seen_tokens: set[str] = set()
        next_token: str | None = None
        while True:
            arguments: dict[str, object] = {
                "cluster": cluster,
                "desiredStatus": desired_status,
            }
            if service_name is not None:
                arguments["serviceName"] = service_name
            if family is not None:
                arguments["family"] = family
            if next_token is not None:
                arguments["nextToken"] = next_token
            response = self.ecs.list_tasks(**arguments)
            self._observe(response)
            page = response.get("taskArns") if type(response) is dict else None
            token = response.get("nextToken") if type(response) is dict else None
            if (
                type(page) is not list
                or any(type(task_arn) is not str or not task_arn for task_arn in page)
                or (token is not None and (type(token) is not str or not token))
            ):
                raise RolloutGateError("task_inventory_incomplete")
            task_arns.extend(page)
            if len(task_arns) > _TASK_INVENTORY_LIMIT or len(set(task_arns)) != len(task_arns):
                raise RolloutGateError("task_inventory_incomplete")
            if token is None:
                return task_arns
            if token in seen_tokens:
                raise RolloutGateError("task_inventory_incomplete")
            seen_tokens.add(token)
            next_token = token

    def _described_tasks(
        self,
        *,
        cluster: str,
        task_arns: list[str],
    ) -> dict[str, dict[str, Any]]:
        described: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(task_arns), _DESCRIBE_TASK_BATCH):
            batch = task_arns[offset : offset + _DESCRIBE_TASK_BATCH]
            response = self.ecs.describe_tasks(cluster=cluster, tasks=batch)
            self._observe(response)
            tasks = response.get("tasks") if type(response) is dict else None
            failures = response.get("failures") if type(response) is dict else None
            if (
                type(tasks) is not list
                or type(failures) is not list
                or failures
                or len(tasks) != len(batch)
            ):
                raise RolloutGateError("task_inventory_incomplete")
            for raw_task in tasks:
                task = _mapping(raw_task)
                task_arn = task.get("taskArn")
                if type(task_arn) is not str or task_arn not in batch or task_arn in described:
                    raise RolloutGateError("task_inventory_incomplete")
                described[task_arn] = task
        if set(described) != set(task_arns):
            raise RolloutGateError("task_inventory_incomplete")
        return described

    def _task_inventory(
        self,
        *,
        cluster: str,
        service_name: str | None = None,
        family: str | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        running_arns = self._listed_task_arns(
            cluster=cluster,
            desired_status="RUNNING",
            service_name=service_name,
            family=family,
        )
        stopped_arns = self._listed_task_arns(
            cluster=cluster,
            desired_status="STOPPED",
            service_name=service_name,
            family=family,
        )
        if set(running_arns) & set(stopped_arns):
            raise RolloutGateError("task_inventory_incomplete")
        running = self._described_tasks(cluster=cluster, task_arns=running_arns)
        stopped = self._described_tasks(cluster=cluster, task_arns=stopped_arns)
        confirmed_running = self._listed_task_arns(
            cluster=cluster,
            desired_status="RUNNING",
            service_name=service_name,
            family=family,
        )
        confirmed_stopped = self._listed_task_arns(
            cluster=cluster,
            desired_status="STOPPED",
            service_name=service_name,
            family=family,
        )
        if (
            len(confirmed_running) != len(running_arns)
            or set(confirmed_running) != set(running_arns)
            or len(confirmed_stopped) != len(stopped_arns)
            or set(confirmed_stopped) != set(stopped_arns)
        ):
            raise RolloutGateError("task_inventory_incomplete")
        for task in running.values():
            if (
                type(task.get("taskDefinitionArn")) is not str
                or task.get("desiredStatus") != "RUNNING"
                or type(task.get("lastStatus")) is not str
                or task.get("lastStatus") == "STOPPED"
            ):
                raise RolloutGateError("task_inventory_incomplete")
        for task in stopped.values():
            if (
                type(task.get("taskDefinitionArn")) is not str
                or task.get("desiredStatus") != "STOPPED"
                or type(task.get("lastStatus")) is not str
            ):
                raise RolloutGateError("task_inventory_incomplete")
            if task.get("lastStatus") != "STOPPED":
                raise RolloutGateError("old_tasks_not_drained")
        return running, stopped

    def _scheduled_tasks_drained(self, expected_task_definition: str) -> None:
        scheduled = self.control.morning_digest
        family = _task_family(expected_task_definition)
        if any(
            _task_family(task_definition) != family
            for task_definition in (
                scheduled.legacy_task_definition,
                scheduled.rollback_task_definition,
            )
        ):
            raise RolloutGateError("invalid_control")
        running, _stopped = self._task_inventory(
            cluster=scheduled.cluster,
            family=family,
        )
        if any(
            task.get("taskDefinitionArn") != expected_task_definition
            or task.get("lastStatus") != "RUNNING"
            for task in running.values()
        ):
            raise RolloutGateError("old_tasks_not_drained", scope="morning_digest")

    def _check_canary_anchor(self) -> None:
        canary = self.control.canary
        response = self.events.list_targets_by_rule(Rule=canary.rule)
        self._observe(response)
        targets = response.get("Targets") if type(response) is dict else None
        if type(targets) is not list:
            raise RolloutGateError("canary_anchor_changed", scope="canary")
        matches = [target for target in targets if _mapping(target).get("Id") == canary.target_id]
        if len(matches) != 1:
            raise RolloutGateError("canary_anchor_changed", scope="canary")
        ecs_parameters = _mapping(matches[0].get("EcsParameters"))
        if ecs_parameters.get("TaskDefinitionArn") != canary.task_definition:
            raise RolloutGateError("canary_anchor_changed", scope="canary")

    def _live_domain_config(
        self,
        *,
        task: str,
        definition: dict[str, Any],
        domain: str,
    ) -> dict[str, object]:
        container = _one_container(definition)
        environment = _named(container.get("environment", []), "value")
        secrets = _named(container.get("secrets", []), "valueFrom")
        names = _DOMAIN_NAMES[domain]
        primary_reference = secrets.get(names["primary_secret"])
        primary_generation = environment.get(names["primary_generation"])

        # The independently confirmed td53 defect used MAIL_ACTION_HMAC_SECRET as the report
        # verifier's database credential. This alias is accepted only while observing deployed
        # legacy state; candidate validation below never permits it.
        if (
            task in {"mcp", "connect_web"}
            and domain == "report_link"
            and primary_reference is None
            and "MAIL_ACTION_HMAC_SECRET" in secrets
        ):
            primary_reference = secrets["MAIL_ACTION_HMAC_SECRET"]
        if primary_reference is None:
            raise RolloutGateError("live_generation_missing", scope=task)
        resource, version = self._version_for_reference(primary_reference)
        observed_primary = f"{resource}@{version}"
        if primary_generation is not None and primary_generation != observed_primary:
            raise RolloutGateError("live_generation_drift", scope=task)

        previous_reference = secrets.get(names["previous_secret"])
        previous_generation = environment.get(names["previous_generation"])
        t0 = environment.get(names["t0"])
        if previous_reference is None and previous_generation is None and t0 is None:
            return {
                "primary_generation": observed_primary,
                "previous_generation": None,
                "rotation_started_at": None,
            }
        if previous_reference is None or previous_generation is None or t0 is None:
            raise RolloutGateError("live_generation_drift", scope=task)
        previous_resource, previous_version = self._version_for_reference(previous_reference)
        observed_previous = f"{previous_resource}@{previous_version}"
        if previous_generation != observed_previous or not t0.isascii() or not t0.isdecimal():
            raise RolloutGateError("live_generation_drift", scope=task)
        return {
            "primary_generation": observed_primary,
            "previous_generation": observed_previous,
            "rotation_started_at": int(t0),
        }

    def _live_tasks(self) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
        mcp_arn, mcp = self._service_task(self.control.mcp)
        connect_arn, connect = self._service_task(self.control.connect_web)
        morning_arn, morning = self._scheduled_task()
        self._check_canary_anchor()
        return (
            {
                "mcp": mcp_arn,
                "connect_web": connect_arn,
                "morning_digest": morning_arn,
            },
            {
                "mcp": mcp,
                "connect_web": connect,
                "morning_digest": morning,
            },
        )

    def _observed_deployed(self, definitions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        observed: dict[str, Any] = {}
        for domain in _DOMAIN_MAX_TTL:
            domain_observations = [
                self._live_domain_config(task=task, definition=definition, domain=domain)
                for task, definition in definitions.items()
                if domain in _TASK_DOMAINS[task]
            ]
            first = domain_observations[0]
            if any(value != first for value in domain_observations[1:]):
                raise RolloutGateError("live_domain_disagreement", scope=domain)
            observed[domain] = first
        return observed

    def _assert_transition(self, deployed: dict[str, Any]) -> dict[str, Any]:
        domains = _mapping(self.manifest.get("domains"))
        if frozenset(domains) != frozenset(_DOMAIN_MAX_TTL):
            raise RolloutGateError("invalid_manifest")
        now = self._now()
        proposed: dict[str, Any] = {}
        for domain, maximum_ttl in _DOMAIN_MAX_TTL.items():
            item = _mapping(domains[domain])
            asserted_deployed = _mapping(item.get("deployed"))
            candidate = _mapping(item.get("proposed"))
            if asserted_deployed != deployed[domain]:
                raise RolloutGateError("manifest_live_drift", scope=domain)
            result = validate_hmac_rotation_transition(
                deployed_primary_generation=deployed[domain]["primary_generation"],
                deployed_previous_generation=deployed[domain]["previous_generation"],
                deployed_rotation_started_at=deployed[domain]["rotation_started_at"],
                proposed_primary_generation=candidate.get("primary_generation"),
                proposed_previous_generation=candidate.get("previous_generation"),
                proposed_rotation_started_at=candidate.get("rotation_started_at"),
                now=now,
                max_token_ttl_s=maximum_ttl,
            )
            if not result["ok"]:
                raise RolloutGateError(result["code"], scope=domain)
            proposed[domain] = candidate
        legacy = self.manifest.get("legacy_database_generation")
        if type(legacy) is not str:
            raise RolloutGateError("invalid_manifest")
        legacy_resource, separator, _legacy_version = legacy.rpartition("@")
        primary_resources = []
        for domain in _DOMAIN_MAX_TTL:
            primary = proposed[domain].get("primary_generation")
            if type(primary) is not str:
                raise RolloutGateError("invalid_manifest")
            resource, primary_separator, _version = primary.rpartition("@")
            if (
                not separator
                or not primary_separator
                or resource == legacy_resource
                or "slack" in resource.casefold()
            ):
                raise RolloutGateError("forbidden_signing_primary", scope=domain)
            primary_resources.append(resource)
        if len(set(primary_resources)) != len(primary_resources):
            raise RolloutGateError("purpose_generation_reuse")
        legacy_worker = self.manifest.get("legacy_worker_generation")
        if proposed["mail_action"].get("previous_generation") == legacy:
            if type(legacy_worker) is not str:
                raise RolloutGateError(
                    "invalid_legacy_worker_generation",
                    scope="mail_action",
                )
            worker_resource, worker_separator, _worker_version = legacy_worker.rpartition("@")
            if (
                not worker_separator
                or "slack" not in worker_resource.casefold()
                or legacy_worker
                in {
                    legacy,
                    proposed["mail_action"].get("primary_generation"),
                    proposed["report_link"].get("primary_generation"),
                }
            ):
                raise RolloutGateError(
                    "invalid_legacy_worker_generation",
                    scope="mail_action",
                )
        elif legacy_worker is not None:
            raise RolloutGateError(
                "unexpected_legacy_worker_generation",
                scope="mail_action",
            )
        return proposed

    def _assert_proposed_generations_exist(self, proposed: dict[str, Any]) -> None:
        generations: set[str] = set()
        for config in proposed.values():
            for name in ("primary_generation", "previous_generation"):
                generation = config.get(name)
                if type(generation) is str:
                    generations.add(generation)
        legacy_worker = self.manifest.get("legacy_worker_generation")
        if type(legacy_worker) is str:
            generations.add(legacy_worker)
        for generation in generations:
            resource, separator, version = generation.rpartition("@")
            if not separator:
                raise RolloutGateError("secret_generation_unavailable")
            observed_resource, observed_version = self._version_for_reference(
                f"{resource}:::{version}"
            )
            if f"{observed_resource}@{observed_version}" != generation:
                raise RolloutGateError("secret_generation_unavailable")

    def _domain_item(
        self,
        domain: str,
        config: dict[str, Any],
        now: int,
        *,
        revision: int = 1,
        high_water: int | None = None,
        retired_generations: frozenset[str] = frozenset(),
        retired_provenances: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        maximum_ttl = _DOMAIN_MAX_TTL[domain]
        t0 = config.get("rotation_started_at")
        previous = config.get("previous_generation")
        item: dict[str, Any] = {
            "scope": {"S": self.control.scope},
            "record": {"S": f"DOMAIN#{domain}"},
            "domain": {"S": domain},
            "revision": {"N": str(revision)},
            "clock_revision": {"N": "0"},
            "primary_generation": {"S": config["primary_generation"]},
            "rotation_epoch": {"S": self.control.rotation_epoch},
            "high_water": {"N": str(max(now, high_water if high_water is not None else now))},
            "previous_retired": {"BOOL": False},
            "stage": {"S": "preload"},
        }
        if previous is not None and type(t0) is int:
            item.update(
                {
                    "previous_generation": {"S": previous},
                    "rotation_started_at": {"N": str(t0)},
                    "deadline": {"N": str(t0 + _ISSUER_CUTOVER_S + maximum_ttl)},
                }
            )
            legacy_worker = self.manifest.get("legacy_worker_generation")
            if domain == "mail_action" and type(legacy_worker) is str:
                item.update(
                    {
                        "legacy_worker_generation": {"S": legacy_worker},
                        "legacy_worker_deadline": {"N": str(t0 + _ISSUER_CUTOVER_S + maximum_ttl)},
                    }
                )
        if retired_generations:
            item["retired_generations"] = {"SS": sorted(retired_generations)}
        if retired_provenances:
            item["retired_provenances"] = {"SS": sorted(retired_provenances)}
        return item

    @staticmethod
    def _ddb_string(item: dict[str, Any], name: str, *, optional: bool = False) -> str | None:
        raw = item.get(name)
        if raw is None and optional:
            return None
        value = raw.get("S") if type(raw) is dict else None
        if type(value) is not str:
            raise RolloutGateError("durable_state_invalid")
        return value

    @staticmethod
    def _ddb_number(item: dict[str, Any], name: str, *, optional: bool = False) -> int | None:
        raw = item.get(name)
        if raw is None and optional:
            return None
        value = raw.get("N") if type(raw) is dict else None
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise RolloutGateError("durable_state_invalid")
        return int(value)

    @staticmethod
    def _ddb_string_set(item: dict[str, Any], name: str) -> frozenset[str]:
        raw = item.get(name)
        if raw is None:
            return frozenset()
        values = raw.get("SS") if type(raw) is dict else None
        if type(values) is not list or any(type(value) is not str for value in values):
            raise RolloutGateError("durable_state_invalid")
        return frozenset(values)

    @staticmethod
    def _ddb_bool(item: dict[str, Any], name: str) -> bool:
        raw = item.get(name)
        value = raw.get("BOOL") if type(raw) is dict else None
        if type(value) is not bool:
            raise RolloutGateError("durable_state_invalid")
        return value

    def _read_item_optional(self, record: str) -> dict[str, Any] | None:
        response = self.ddb.get_item(
            TableName=self.control.state_table,
            Key={"scope": {"S": self.control.scope}, "record": {"S": record}},
            ConsistentRead=True,
        )
        self._observe(response)
        item = response.get("Item") if type(response) is dict else None
        if item is None:
            return None
        return _mapping(item)

    def _domain_config_from_item(self, item: dict[str, Any]) -> dict[str, object]:
        previous = self._ddb_string(item, "previous_generation", optional=True)
        t0 = self._ddb_number(item, "rotation_started_at", optional=True)
        if (previous is None) != (t0 is None):
            raise RolloutGateError("durable_state_invalid")
        return {
            "primary_generation": self._ddb_string(item, "primary_generation"),
            "previous_generation": previous,
            "rotation_started_at": t0,
        }

    def _legacy_bindings_drained(
        self,
        arns: dict[str, str],
        definitions: dict[str, dict[str, Any]],
    ) -> None:
        expected = {
            "mcp": self.control.mcp.legacy_task_definition,
            "connect_web": self.control.connect_web.legacy_task_definition,
            "morning_digest": self.control.morning_digest.legacy_task_definition,
        }
        if arns != expected:
            raise RolloutGateError("pinned_legacy_revision_required")
        # Parsing every HMAC reference proves each observed legacy generation is version pinned.
        self._observed_deployed(definitions)
        self._full_task_inventory(arns)

    def _full_task_inventory(self, arns: dict[str, str]) -> None:
        self._service_stable_and_drained(self.control.mcp, arns["mcp"])
        self._service_stable_and_drained(self.control.connect_web, arns["connect_web"])
        scheduled_arn, _definition = self._scheduled_task()
        if scheduled_arn != arns["morning_digest"]:
            raise RolloutGateError("task_inventory_incomplete", scope="morning_digest")
        self._scheduled_tasks_drained(arns["morning_digest"])

    def initialize(self) -> None:
        """CAS-create the first epoch or advance a completed primary-only epoch."""

        arns, definitions = self._live_tasks()
        existing = {
            domain: self._read_item_optional(f"DOMAIN#{domain}") for domain in _DOMAIN_MAX_TTL
        }
        if any(item is None for item in existing.values()) and any(
            item is not None for item in existing.values()
        ):
            raise RolloutGateError("durable_state_partial")
        first_epoch = all(item is None for item in existing.values())
        if first_epoch:
            self._legacy_bindings_drained(arns, definitions)
        else:
            self._full_task_inventory(arns)

        deployed = self._observed_deployed(definitions)
        proposed = self._assert_transition(deployed)
        self._assert_proposed_generations_exist(proposed)
        now = self._now()
        transaction: list[dict[str, Any]] = []
        prior_epoch: str | None = None
        ledger_updated_at = now

        if first_epoch:
            for domain in _DOMAIN_MAX_TTL:
                transaction.append(
                    {
                        "Put": {
                            "TableName": self.control.state_table,
                            "Item": self._domain_item(domain, proposed[domain], now),
                            "ConditionExpression": "attribute_not_exists(#record)",
                            "ExpressionAttributeNames": {"#record": "record"},
                        }
                    }
                )
        else:
            prior_epochs: set[str] = set()
            for domain, raw_item in existing.items():
                if raw_item is None:
                    raise RolloutGateError("durable_state_partial")
                epoch = self._ddb_string(raw_item, "rotation_epoch")
                stage = self._ddb_string(raw_item, "stage")
                revision = self._ddb_number(raw_item, "revision")
                if (
                    type(epoch) is not str
                    or epoch == self.control.rotation_epoch
                    or stage != "complete"
                    or revision is None
                    or not self._ddb_bool(raw_item, "previous_retired")
                    or self._domain_config_from_item(raw_item) != deployed[domain]
                    or self._ddb_string(raw_item, "previous_generation", optional=True) is not None
                    or self._ddb_string(
                        raw_item,
                        "legacy_worker_generation",
                        optional=True,
                    )
                    is not None
                ):
                    raise RolloutGateError("next_epoch_not_ready", scope=domain)
                prior_high_water = self._ddb_number(raw_item, "high_water")
                retired_generations = self._ddb_string_set(
                    raw_item,
                    "retired_generations",
                )
                retired_provenances = self._ddb_string_set(
                    raw_item,
                    "retired_provenances",
                )
                if (
                    prior_high_water is None
                    or proposed[domain].get("primary_generation") in retired_generations
                ):
                    raise RolloutGateError("next_epoch_not_ready", scope=domain)
                ledger_updated_at = max(ledger_updated_at, prior_high_water)
                prior_epochs.add(epoch)
                history_record = f"EPOCH_HISTORY#{domain}#{epoch}"
                transaction.append(
                    {
                        "Put": {
                            "TableName": self.control.state_table,
                            "Item": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": history_record},
                                "domain": {"S": domain},
                                "rotation_epoch": {"S": epoch},
                                "primary_generation": {
                                    "S": str(deployed[domain]["primary_generation"])
                                },
                                "closed_at": {"N": str(max(now, prior_high_water))},
                                "revision": {"N": str(revision)},
                                "clock_revision": copy.deepcopy(
                                    raw_item.get("clock_revision", {"N": "0"})
                                ),
                                "high_water": {"N": str(prior_high_water)},
                                **(
                                    {"retired_generations": {"SS": sorted(retired_generations)}}
                                    if retired_generations
                                    else {}
                                ),
                                **(
                                    {"retired_provenances": {"SS": sorted(retired_provenances)}}
                                    if retired_provenances
                                    else {}
                                ),
                            },
                            "ConditionExpression": "attribute_not_exists(#record)",
                            "ExpressionAttributeNames": {"#record": "record"},
                        }
                    }
                )
                next_config = proposed[domain]
                next_primary = next_config.get("primary_generation")
                next_previous = next_config.get("previous_generation")
                next_t0 = next_config.get("rotation_started_at")
                if type(next_primary) is not str or (next_previous is None) != (next_t0 is None):
                    raise RolloutGateError("next_epoch_not_ready", scope=domain)
                update_expression = (
                    "SET revision = revision + :one,"
                    " clock_revision = if_not_exists(clock_revision, :zero),"
                    " primary_generation = :primary, rotation_epoch = :new_epoch,"
                    " previous_retired = :false, #stage = :preload"
                )
                remove_fields = ["issuer_provenances", "cleanup_stage"]
                update_values: dict[str, Any] = {
                    ":one": {"N": "1"},
                    ":zero": {"N": "0"},
                    ":primary": {"S": next_primary},
                    ":new_epoch": {"S": self.control.rotation_epoch},
                    ":false": {"BOOL": False},
                    ":preload": {"S": "preload"},
                    ":revision": {"N": str(revision)},
                    ":old_epoch": {"S": epoch},
                    ":complete": {"S": "complete"},
                    ":retired": {"BOOL": True},
                    ":deployed_primary": {"S": str(deployed[domain]["primary_generation"])},
                }
                if type(next_previous) is str and type(next_t0) is int:
                    update_expression += (
                        ", previous_generation = :previous,"
                        " rotation_started_at = :t0, deadline = :deadline"
                    )
                    update_values.update(
                        {
                            ":previous": {"S": next_previous},
                            ":t0": {"N": str(next_t0)},
                            ":deadline": {
                                "N": str(next_t0 + _ISSUER_CUTOVER_S + _DOMAIN_MAX_TTL[domain])
                            },
                        }
                    )
                    legacy_worker = self.manifest.get("legacy_worker_generation")
                    if domain == "mail_action" and type(legacy_worker) is str:
                        update_expression += (
                            ", legacy_worker_generation = :legacy_worker,"
                            " legacy_worker_deadline = :deadline"
                        )
                        update_values[":legacy_worker"] = {"S": legacy_worker}
                    else:
                        remove_fields.extend(["legacy_worker_generation", "legacy_worker_deadline"])
                else:
                    remove_fields.extend(
                        [
                            "previous_generation",
                            "rotation_started_at",
                            "deadline",
                            "legacy_worker_generation",
                            "legacy_worker_deadline",
                        ]
                    )
                update_expression += " REMOVE " + ", ".join(remove_fields)
                transaction.append(
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": f"DOMAIN#{domain}"},
                            },
                            "UpdateExpression": update_expression,
                            "ConditionExpression": (
                                "revision = :revision AND rotation_epoch = :old_epoch"
                                " AND #stage = :complete AND previous_retired = :retired"
                                " AND primary_generation = :deployed_primary"
                                " AND attribute_not_exists(previous_generation)"
                                " AND attribute_not_exists(legacy_worker_generation)"
                            ),
                            "ExpressionAttributeNames": {"#stage": "stage"},
                            "ExpressionAttributeValues": update_values,
                        }
                    }
                )
            if len(prior_epochs) != 1:
                raise RolloutGateError("next_epoch_not_ready")
            prior_epoch = next(iter(prior_epochs))
            prior_ledger = self._read_item_optional(f"LEDGER#{prior_epoch}")
            if prior_ledger is None or self._ddb_string(prior_ledger, "stage") != "complete":
                raise RolloutGateError("next_epoch_not_ready")
            transaction.insert(
                0,
                {
                    "ConditionCheck": {
                        "TableName": self.control.state_table,
                        "Key": {
                            "scope": {"S": self.control.scope},
                            "record": {"S": f"LEDGER#{prior_epoch}"},
                        },
                        "ConditionExpression": "#stage = :complete",
                        "ExpressionAttributeNames": {"#stage": "stage"},
                        "ExpressionAttributeValues": {":complete": {"S": "complete"}},
                    }
                },
            )

        transaction.append(
            {
                "Put": {
                    "TableName": self.control.state_table,
                    "Item": {
                        "scope": {"S": self.control.scope},
                        "record": {"S": f"LEDGER#{self.control.rotation_epoch}"},
                        "rotation_epoch": {"S": self.control.rotation_epoch},
                        "stage": {"S": "initialized"},
                        "revision": {"N": "1"},
                        "updated_at": {"N": str(ledger_updated_at)},
                        **(
                            {"previous_rotation_epoch": {"S": prior_epoch}}
                            if prior_epoch is not None
                            else {}
                        ),
                    },
                    "ConditionExpression": "attribute_not_exists(#record)",
                    "ExpressionAttributeNames": {"#record": "record"},
                }
            }
        )
        try:
            response = self.ddb.transact_write_items(TransactItems=transaction)
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("durable_initialize_cas_failed") from exc

    def _read_item(self, record: str) -> dict[str, Any]:
        response = self.ddb.get_item(
            TableName=self.control.state_table,
            Key={"scope": {"S": self.control.scope}, "record": {"S": record}},
            ConsistentRead=True,
        )
        self._observe(response)
        item = response.get("Item") if type(response) is dict else None
        if type(item) is not dict:
            raise RolloutGateError("durable_state_missing")
        return item

    def _ledger(self) -> Ledger:
        item = self._read_item(f"LEDGER#{self.control.rotation_epoch}")
        try:
            stage = item["stage"]["S"]
            revision = int(item["revision"]["N"])
            updated_at = int(item["updated_at"]["N"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutGateError("durable_ledger_invalid") from exc
        if stage not in _LEDGER_STAGES or revision < 1 or updated_at < 0:
            raise RolloutGateError("durable_ledger_invalid")
        return Ledger(
            stage=stage,
            revision=revision,
            updated_at=updated_at,
            trusted_now=self._now(),
        )

    def _durable_proposed(self) -> dict[str, dict[str, object]]:
        proposed: dict[str, dict[str, object]] = {}
        for domain in _DOMAIN_MAX_TTL:
            item = self._read_item(f"DOMAIN#{domain}")
            try:
                epoch = item["rotation_epoch"]["S"]
                primary = item["primary_generation"]["S"]
                previous = item.get("previous_generation", {}).get("S")
                t0_raw = item.get("rotation_started_at", {}).get("N")
                t0 = int(t0_raw) if t0_raw is not None else None
            except (KeyError, TypeError, ValueError) as exc:
                raise RolloutGateError("durable_state_invalid", scope=domain) from exc
            if epoch != self.control.rotation_epoch:
                raise RolloutGateError("rotation_epoch_drift", scope=domain)
            proposed[domain] = {
                "primary_generation": primary,
                "previous_generation": previous,
                "rotation_started_at": t0,
            }
        return proposed

    def _durable_legacy_worker_generation(self) -> str | None:
        item = self._read_item("DOMAIN#mail_action")
        return self._ddb_string(
            item,
            "legacy_worker_generation",
            optional=True,
        )

    def _assert_manifest_matches_durable(self) -> dict[str, dict[str, object]]:
        durable = self._durable_proposed()
        domains = _mapping(self.manifest.get("domains"))
        for domain in _DOMAIN_MAX_TTL:
            proposed = _mapping(_mapping(domains.get(domain)).get("proposed"))
            if proposed != durable[domain]:
                raise RolloutGateError("manifest_durable_drift", scope=domain)
        if (
            self.manifest.get("legacy_worker_generation")
            != self._durable_legacy_worker_generation()
        ):
            raise RolloutGateError("manifest_durable_drift", scope="mail_action")
        self._assert_proposed_generations_exist(durable)
        self._now()
        return durable

    def _validate_runtime_metadata(
        self,
        *,
        task: str,
        definition: dict[str, Any],
        provenance: str,
    ) -> None:
        container = _one_container(definition)
        environment = _named(container.get("environment", []), "value")
        expected = {
            _RUNTIME_ENV["required"]: "1",
            _RUNTIME_ENV["table"]: self.control.state_table,
            _RUNTIME_ENV["scope"]: self.control.scope,
            _RUNTIME_ENV["epoch"]: self.control.rotation_epoch,
            _RUNTIME_ENV["provenance"]: provenance,
        }
        if any(environment.get(name) != value for name, value in expected.items()):
            raise RolloutGateError("runtime_metadata_drift", scope=task)

    def _validate_legacy_worker_reference(
        self,
        *,
        task: str,
        definition: dict[str, Any],
    ) -> None:
        self._validate_legacy_worker_reference_for_generation(
            task=task,
            definition=definition,
            expected=self._durable_legacy_worker_generation(),
        )

    def _validate_legacy_worker_reference_for_generation(
        self,
        *,
        task: str,
        definition: dict[str, Any],
        expected: str | None,
    ) -> None:
        if "mail_action" not in _TASK_DOMAINS[task]:
            return
        container = _one_container(definition)
        environment = _named(container.get("environment", []), "value")
        secrets = _named(container.get("secrets", []), "valueFrom")
        generation = environment.get("MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION")
        reference = secrets.get("MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET")
        if expected is None:
            if generation is not None or reference is not None:
                raise RolloutGateError("legacy_worker_reference_drift", scope=task)
            return
        if generation != expected or reference is None:
            raise RolloutGateError("legacy_worker_reference_drift", scope=task)
        resource, version = self._version_for_reference(reference)
        if f"{resource}@{version}" != expected:
            raise RolloutGateError("legacy_worker_reference_drift", scope=task)

    def _expected_provenance(
        self,
        *,
        task: str,
        image: str | None = None,
        artifact_sha256: str | None = None,
        configs: dict[str, dict[str, object]] | None = None,
        legacy_worker_generation: str | None = None,
    ) -> str:
        durable = configs if configs is not None else self._durable_proposed()
        legacy_worker = (
            legacy_worker_generation
            if configs is not None
            else self._durable_legacy_worker_generation()
        ) or ""
        mail = durable["mail_action"]
        report = durable["report_link"]
        mail_values = {
            "mail_primary": str(mail["primary_generation"]),
            "mail_previous": str(mail["previous_generation"] or ""),
            "mail_t0": (
                str(mail["rotation_started_at"]) if mail["rotation_started_at"] is not None else ""
            ),
        }
        report_values = {
            "report_primary": str(report["primary_generation"]),
            "report_previous": str(report["previous_generation"] or ""),
            "report_t0": (
                str(report["rotation_started_at"])
                if report["rotation_started_at"] is not None
                else ""
            ),
        }
        if task == "mcp":
            if image is None or _IMAGE_DIGEST_RE.fullmatch(image) is None:
                raise RolloutGateError("image_not_digest", scope=task)
            values = {
                "image": image,
                "legacy_worker": legacy_worker,
                "rotation_epoch": self.control.rotation_epoch,
                "workload": "mcp",
                **mail_values,
                **report_values,
            }
        elif task == "connect_web":
            if image is None or _IMAGE_DIGEST_RE.fullmatch(image) is None:
                raise RolloutGateError("image_not_digest", scope=task)
            values = {
                "image": image,
                "rotation_epoch": self.control.rotation_epoch,
                "workload": "connect_web",
                **report_values,
            }
        elif task == "morning_digest":
            if image is None or _IMAGE_DIGEST_RE.fullmatch(image) is None:
                raise RolloutGateError("image_not_digest", scope=task)
            values = {
                "image": image,
                "legacy_worker": legacy_worker,
                "rotation_epoch": self.control.rotation_epoch,
                "workload": "morning_digest",
                **mail_values,
            }
        elif task == "worker":
            if artifact_sha256 is None or _PROVENANCE_RE.fullmatch(artifact_sha256) is None:
                raise RolloutGateError("worker_artifact_drift", scope=task)
            values = {
                "artifact": artifact_sha256,
                "legacy_worker": legacy_worker,
                "rotation_epoch": self.control.rotation_epoch,
                "workload": "worker",
                **mail_values,
                **report_values,
            }
        else:
            raise RolloutGateError("unknown_task")
        return _content_provenance(values)

    def validate_candidate(self, *, task: str, definition: dict[str, Any]) -> None:
        """Validate an exact rendered candidate immediately before registration."""

        if task not in _TASK_DOMAINS:
            raise RolloutGateError("unknown_task")
        self._assert_manifest_matches_durable()
        trusted_manifest = copy.deepcopy(self.manifest)
        trusted_manifest["now"] = self._now()
        result = validate_rendered_tasks(trusted_manifest, {task: definition})
        if not result["ok"]:
            scope = result.get("scope")
            raise RolloutGateError(
                str(result["code"]),
                scope=str(scope) if scope is not None else task,
            )
        provenance = {
            "mcp": self.control.mcp.provenance,
            "connect_web": self.control.connect_web.provenance,
            "morning_digest": self.control.morning_digest.provenance,
        }[task]
        task_control = _task_control(self.control, task)
        payload = _canonical_registerable_task_definition(definition)
        container = _one_container(definition)
        image = container.get("image")
        candidate_arn = definition.get("taskDefinitionArn")
        if (
            type(image) is not str
            or image == task_control.rollback_image
            or payload.get("family") != _task_family(task_control.rollback_task_definition)
            or (
                candidate_arn is not None
                and (
                    type(candidate_arn) is not str
                    or _TASK_DEFINITION_RE.fullmatch(candidate_arn) is None
                    or candidate_arn in _reserved_task_definitions(self.control)
                )
            )
            or provenance
            != self._expected_provenance(
                task=task,
                image=image,
            )
        ):
            raise RolloutGateError("provenance_binding_drift", scope=task)
        self._validate_runtime_metadata(
            task=task,
            definition=definition,
            provenance=provenance,
        )
        self._validate_legacy_worker_reference(task=task, definition=definition)

    def _manifest_proposed(self) -> dict[str, dict[str, object]]:
        domains = _mapping(self.manifest.get("domains"))
        proposed: dict[str, dict[str, object]] = {}
        for domain in _DOMAIN_MAX_TTL:
            config = _mapping(_mapping(domains.get(domain)).get("proposed"))
            if frozenset(config) != frozenset(
                {
                    "primary_generation",
                    "previous_generation",
                    "rotation_started_at",
                }
            ):
                raise RolloutGateError("invalid_manifest", scope=domain)
            proposed[domain] = config
        return proposed

    def _cleanup_transition(
        self,
        *,
        domain: str,
        deployed: dict[str, dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        domains = _mapping(self.manifest.get("domains"))
        if frozenset(domains) != frozenset(_DOMAIN_MAX_TTL):
            raise RolloutGateError("cleanup_manifest_invalid", scope=domain)
        now = self._now()
        proposed: dict[str, dict[str, object]] = {}
        for item_domain in _DOMAIN_MAX_TTL:
            manifest_domain = _mapping(domains[item_domain])
            asserted_deployed = _mapping(manifest_domain.get("deployed"))
            candidate = _mapping(manifest_domain.get("proposed"))
            current = deployed[item_domain]
            if asserted_deployed != current:
                raise RolloutGateError("manifest_live_drift", scope=item_domain)
            result = validate_hmac_rotation_transition(
                deployed_primary_generation=current["primary_generation"],
                deployed_previous_generation=current["previous_generation"],
                deployed_rotation_started_at=current["rotation_started_at"],
                proposed_primary_generation=candidate.get("primary_generation"),
                proposed_previous_generation=candidate.get("previous_generation"),
                proposed_rotation_started_at=candidate.get("rotation_started_at"),
                now=now,
                max_token_ttl_s=_DOMAIN_MAX_TTL[item_domain],
            )
            if item_domain == domain:
                if (
                    not result["ok"]
                    or current.get("previous_generation") is None
                    or candidate
                    != {
                        "primary_generation": current["primary_generation"],
                        "previous_generation": None,
                        "rotation_started_at": None,
                    }
                ):
                    raise RolloutGateError("cleanup_manifest_invalid", scope=domain)
            else:
                expired_but_durably_retired = False
                if not result["ok"] and result["code"] == "expired_previous_not_removed":
                    durable = self._read_item(f"DOMAIN#{item_domain}")
                    deadline = self._ddb_number(durable, "deadline", optional=True)
                    expired_but_durably_retired = (
                        self._ddb_bool(durable, "previous_retired")
                        and deadline is not None
                        and now >= deadline
                        and self._domain_config_from_item(durable) == current
                    )
                if candidate != current or (not result["ok"] and not expired_but_durably_retired):
                    raise RolloutGateError("cleanup_manifest_invalid", scope=item_domain)
            proposed[item_domain] = candidate
        legacy = self.manifest.get("legacy_database_generation")
        if type(legacy) is not str:
            raise RolloutGateError("cleanup_manifest_invalid", scope=domain)
        legacy_resource = legacy.rpartition("@")[0]
        primary_resources: list[str] = []
        for item_domain, candidate in proposed.items():
            primary = candidate.get("primary_generation")
            resource = primary.rpartition("@")[0] if type(primary) is str else ""
            if not resource or resource == legacy_resource or "slack" in resource.casefold():
                raise RolloutGateError("forbidden_signing_primary", scope=item_domain)
            primary_resources.append(resource)
        if len(set(primary_resources)) != len(primary_resources):
            raise RolloutGateError("purpose_generation_reuse")
        legacy_worker = self.manifest.get("legacy_worker_generation")
        if proposed["mail_action"].get("previous_generation") == legacy:
            if type(legacy_worker) is not str or "slack" not in legacy_worker.casefold():
                raise RolloutGateError("invalid_legacy_worker_generation", scope="mail_action")
        elif legacy_worker is not None:
            raise RolloutGateError("unexpected_legacy_worker_generation", scope="mail_action")
        return proposed

    def _validate_cleanup_task(
        self,
        *,
        task: str,
        definition: dict[str, Any],
        proposed: dict[str, dict[str, object]],
        legacy_database_generation: str,
        legacy_worker_generation: str | None,
        rollback: bool,
        prepared: bool = False,
    ) -> str:
        if prepared:
            for domain in _TASK_DOMAINS[task]:
                if (
                    self._live_domain_config(
                        task=task,
                        definition=definition,
                        domain=domain,
                    )
                    != proposed[domain]
                ):
                    raise RolloutGateError("cleanup_artifact_drift", scope=task)
        else:
            trusted_manifest = self._cleanup_artifact_manifest(
                proposed,
                legacy_database_generation=legacy_database_generation,
                legacy_worker_generation=legacy_worker_generation,
            )
            result = validate_rendered_tasks(trusted_manifest, {task: definition})
            if not result["ok"]:
                raise RolloutGateError(str(result["code"]), scope=task)
        task_control = _task_control(self.control, task)
        payload = _canonical_registerable_task_definition(definition)
        expected_provenance = (
            task_control.rollback_provenance if rollback else task_control.provenance
        )
        container = _one_container(definition)
        image = container.get("image")
        task_definition = definition.get("taskDefinitionArn")
        candidate_identity_invalid = (
            not rollback
            and task_definition is not None
            and (
                type(task_definition) is not str
                or _TASK_DEFINITION_RE.fullmatch(task_definition) is None
                or task_definition in _reserved_task_definitions(self.control)
            )
        )
        if (
            type(image) is not str
            or _IMAGE_DIGEST_RE.fullmatch(image) is None
            or payload.get("family") != _task_family(task_control.rollback_task_definition)
            or (rollback and image != task_control.rollback_image)
            or (not rollback and image == task_control.rollback_image)
            or (rollback and task_definition != task_control.rollback_task_definition)
            or candidate_identity_invalid
            or expected_provenance
            != self._expected_provenance(
                task=task,
                image=image,
                configs=proposed,
                legacy_worker_generation=legacy_worker_generation,
            )
        ):
            raise RolloutGateError(
                "rollback_provenance_binding_drift" if rollback else "provenance_binding_drift",
                scope=task,
            )
        self._validate_runtime_metadata(
            task=task,
            definition=definition,
            provenance=expected_provenance,
        )
        self._validate_legacy_worker_reference_for_generation(
            task=task,
            definition=definition,
            expected=legacy_worker_generation,
        )
        return _task_artifact_digest(definition)

    def _cleanup_artifact_manifest(
        self,
        proposed: dict[str, dict[str, object]],
        *,
        legacy_database_generation: str,
        legacy_worker_generation: str | None,
    ) -> dict[str, Any]:
        """Build a deterministic parity-only manifest from the already-authorized proposal."""

        validation: dict[str, Any] = {
            "now": 0,
            "legacy_database_generation": legacy_database_generation,
            "legacy_worker_generation": legacy_worker_generation,
        }
        deadlines: list[int] = []
        validation["domains"] = {}
        for domain, maximum_ttl in _DOMAIN_MAX_TTL.items():
            config = copy.deepcopy(proposed[domain])
            validation["domains"][domain] = {
                "deployed": copy.deepcopy(config),
                "proposed": copy.deepcopy(config),
            }
            t0 = config.get("rotation_started_at")
            if type(t0) is int:
                deadlines.append(t0 + _ISSUER_CUTOVER_S + maximum_ttl)
        if deadlines:
            validation["now"] = min(deadlines) - 1
        tasks: dict[str, dict[str, object]] = {}
        for task, domains in _TASK_DOMAINS.items():
            tasks[task] = {domain: copy.deepcopy(proposed[domain]) for domain in domains}
        tasks["worker"] = {domain: copy.deepcopy(config) for domain, config in proposed.items()}
        validation["tasks"] = tasks
        return validation

    @staticmethod
    def _worker_env_values(text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in text.splitlines():
            if not line:
                continue
            match = _WORKER_EXPORT_RE.fullmatch(line)
            if match is None or match.group(1) in values:
                raise RolloutGateError("worker_env_drift", scope="worker")
            values[match.group(1)] = match.group(2)
        return values

    def _validate_cleanup_worker(
        self,
        *,
        env_path: Path,
        artifact_path: Path,
        proposed: dict[str, dict[str, object]],
        legacy_database_generation: str,
        legacy_worker_generation: str | None,
        rollback: bool,
    ) -> str:
        try:
            env_bytes = env_path.read_bytes()
            env_text = env_bytes.decode("utf-8")
            artifact_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        except (OSError, UnicodeError) as exc:
            raise RolloutGateError("worker_artifact_unreadable", scope="worker") from exc
        trusted_manifest = self._cleanup_artifact_manifest(
            proposed,
            legacy_database_generation=legacy_database_generation,
            legacy_worker_generation=legacy_worker_generation,
        )
        result = validate_worker_env(trusted_manifest, env_text)
        if not result["ok"]:
            raise RolloutGateError(str(result["code"]), scope="worker")
        values = self._worker_env_values(env_text)
        expected_digest = (
            self.control.worker.rollback_artifact_sha256
            if rollback
            else self.control.worker.artifact_sha256
        )
        expected_provenance = (
            self.control.worker.rollback_provenance if rollback else self.control.worker.provenance
        )
        if (
            artifact_digest != expected_digest
            or values.get("TEAMAGENT_HMAC_ARTIFACT_SHA256") != expected_digest
            or values.get("TEAMAGENT_HMAC_PROVENANCE") != expected_provenance
            or values.get("TEAMAGENT_HMAC_STATE_TABLE") != self.control.state_table
            or values.get("TEAMAGENT_HMAC_STATE_SCOPE") != self.control.scope
            or values.get("TEAMAGENT_HMAC_ROTATION_EPOCH") != self.control.rotation_epoch
            or expected_provenance
            != self._expected_provenance(
                task="worker",
                artifact_sha256=artifact_digest,
                configs=proposed,
                legacy_worker_generation=legacy_worker_generation,
            )
        ):
            raise RolloutGateError(
                "worker_rollback_artifact_drift" if rollback else "worker_artifact_drift",
                scope="worker",
            )
        return hashlib.sha256(env_bytes).hexdigest()

    @staticmethod
    def _issuer_provenances(control: RolloutControl) -> dict[str, frozenset[str]]:
        shared = {
            control.mcp.provenance,
            control.mcp.rollback_provenance,
            control.worker.provenance,
            control.worker.rollback_provenance,
        }
        return {
            "mail_action": frozenset(
                shared
                | {
                    control.morning_digest.provenance,
                    control.morning_digest.rollback_provenance,
                }
            ),
            "report_link": frozenset(shared),
        }

    def _cleanup_record_name(self, domain: str) -> str:
        return f"CLEANUP#{self.control.rotation_epoch}#{domain}"

    def _cleanup_ledger(self, domain: str) -> CleanupLedger:
        item = self._read_item(self._cleanup_record_name(domain))
        try:
            item_domain = item["domain"]["S"]
            epoch = item["rotation_epoch"]["S"]
            stage = item["stage"]["S"]
            revision = int(item["revision"]["N"])
            authorized_at = int(item["authorized_at"]["N"])
            prepared_plan_sha256 = item["prepared_plan_sha256"]["S"]
            prepared_intent_id = item["prepared_intent_id"]["S"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutGateError("cleanup_state_invalid", scope=domain) from exc
        if (
            item_domain != domain
            or epoch != self.control.rotation_epoch
            or stage not in _CLEANUP_STAGES
            or revision < 1
            or authorized_at < 0
            or _PROVENANCE_RE.fullmatch(prepared_plan_sha256) is None
            or _APPLY_ATTEMPT_RE.fullmatch(prepared_intent_id) is None
        ):
            raise RolloutGateError("cleanup_state_invalid", scope=domain)
        old_provenances = {
            item_domain: self._ddb_string_set(item, f"old_{item_domain}_provenances")
            for item_domain in _DOMAIN_MAX_TTL
        }
        new_provenances = {
            item_domain: self._ddb_string_set(item, f"new_{item_domain}_provenances")
            for item_domain in _DOMAIN_MAX_TTL
        }
        baseline_provenances = {
            item_domain: self._ddb_string_set(
                item,
                f"baseline_{item_domain}_provenances",
            )
            for item_domain in _DOMAIN_MAX_TTL
        }
        baseline_arns = {
            task: str(self._ddb_string(item, f"baseline_{task}_arn")) for task in _TASK_DOMAINS
        }
        baseline_digests = {
            task: str(self._ddb_string(item, f"baseline_{task}_digest")) for task in _TASK_DOMAINS
        }
        candidate_digests = {
            task: str(self._ddb_string(item, f"candidate_{task}_digest"))
            for task in (*_TASK_DOMAINS, "worker")
        }
        rollback_digests = {
            task: str(self._ddb_string(item, f"rollback_{task}_digest"))
            for task in (*_TASK_DOMAINS, "worker")
        }
        proposed: dict[str, dict[str, object]] = {}
        for proposed_domain in _DOMAIN_MAX_TTL:
            primary = self._ddb_string(item, f"proposed_{proposed_domain}_primary")
            previous = self._ddb_string(
                item,
                f"proposed_{proposed_domain}_previous",
                optional=True,
            )
            t0 = self._ddb_number(
                item,
                f"proposed_{proposed_domain}_t0",
                optional=True,
            )
            if type(primary) is not str or (previous is None) != (t0 is None):
                raise RolloutGateError("cleanup_state_invalid", scope=domain)
            proposed[proposed_domain] = {
                "primary_generation": primary,
                "previous_generation": previous,
                "rotation_started_at": t0,
            }
        legacy_database_generation = self._ddb_string(
            item,
            "proposed_legacy_database_generation",
        )
        candidate_worker_env_digest = self._ddb_string(
            item,
            "candidate_worker_env_digest",
        )
        rollback_worker_env_digest = self._ddb_string(
            item,
            "rollback_worker_env_digest",
        )
        if (
            type(legacy_database_generation) is not str
            or type(candidate_worker_env_digest) is not str
            or type(rollback_worker_env_digest) is not str
        ):
            raise RolloutGateError("cleanup_state_invalid", scope=domain)
        legacy_resource, legacy_separator, legacy_version = legacy_database_generation.rpartition(
            "@"
        )
        if (
            not legacy_separator
            or _VERSION_RE.fullmatch(legacy_version) is None
            or not legacy_resource.endswith("/database-url")
            or _PROVENANCE_RE.fullmatch(candidate_worker_env_digest) is None
            or _PROVENANCE_RE.fullmatch(rollback_worker_env_digest) is None
            or candidate_worker_env_digest == rollback_worker_env_digest
        ):
            raise RolloutGateError("cleanup_state_invalid", scope=domain)
        legacy_worker_generation = self._ddb_string(
            item,
            "proposed_legacy_worker_generation",
            optional=True,
        )
        if legacy_worker_generation is not None:
            resource, separator, version = legacy_worker_generation.rpartition("@")
            if (
                not separator
                or _VERSION_RE.fullmatch(version) is None
                or "slack" not in resource.casefold()
                or legacy_worker_generation
                in {
                    proposed["mail_action"]["primary_generation"],
                    proposed["mail_action"]["previous_generation"],
                    proposed["report_link"]["primary_generation"],
                    proposed["report_link"]["previous_generation"],
                }
            ):
                raise RolloutGateError("cleanup_state_invalid", scope=domain)
        candidate_arns = {
            task: self._ddb_string(item, f"candidate_{task}_arn", optional=True)
            for task in _TASK_DOMAINS
        }
        if any(
            arn is not None and _TASK_DEFINITION_RE.fullmatch(arn) is None
            for arn in candidate_arns.values()
        ):
            raise RolloutGateError("cleanup_state_invalid", scope=domain)
        all_digests = tuple(candidate_digests.values()) + tuple(rollback_digests.values())
        if (
            any(not values for values in new_provenances.values())
            or any(not values for values in baseline_provenances.values())
            or any(_TASK_DEFINITION_RE.fullmatch(arn) is None for arn in baseline_arns.values())
            or any(_PROVENANCE_RE.fullmatch(digest) is None for digest in baseline_digests.values())
            or any(_PROVENANCE_RE.fullmatch(digest) is None for digest in all_digests)
            or any(
                candidate_digests[task] == rollback_digests[task]
                for task in (*_TASK_DOMAINS, "worker")
            )
        ):
            raise RolloutGateError("cleanup_state_invalid", scope=domain)
        worker_provenance: dict[str, ProvenanceBinding] = {}
        for kind in ("candidate", "rollback"):
            try:
                worker_provenance[kind] = ProvenanceBinding(
                    artifact_sha256=str(self._ddb_string(item, f"{kind}_worker_artifact_sha256")),
                    canonical_receipt_sha256=str(
                        self._ddb_string(item, f"{kind}_worker_canonical_receipt_sha256")
                    ),
                    key_arn=str(self._ddb_string(item, f"{kind}_worker_key_arn")),
                    receipt_sha256=str(self._ddb_string(item, f"{kind}_worker_receipt_sha256")),
                    signature_sha256=str(self._ddb_string(item, f"{kind}_worker_signature_sha256")),
                    source_branch=str(self._ddb_string(item, f"{kind}_worker_source_branch")),
                    source_commit=str(self._ddb_string(item, f"{kind}_worker_source_commit")),
                    source_origin=str(self._ddb_string(item, f"{kind}_worker_source_origin")),
                    source_tree=str(self._ddb_string(item, f"{kind}_worker_source_tree")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RolloutGateError("cleanup_state_invalid", scope=domain) from exc
            binding = worker_provenance[kind]
            expected_worker_digest = (
                candidate_digests["worker"] if kind == "candidate" else rollback_digests["worker"]
            )
            if (
                _PROVENANCE_RE.fullmatch(binding.artifact_sha256) is None
                or _PROVENANCE_RE.fullmatch(binding.canonical_receipt_sha256) is None
                or _PROVENANCE_RE.fullmatch(binding.receipt_sha256) is None
                or _PROVENANCE_RE.fullmatch(binding.signature_sha256) is None
                or not binding.key_arn.startswith(f"arn:aws:kms:{self.control.region}:")
                or binding.source_origin != "git@github.com:noirelumiere00/TeamAgent.git"
                or binding.source_branch != "dev"
                or re.fullmatch(r"[a-f0-9]{40}", binding.source_commit) is None
                or re.fullmatch(r"[a-f0-9]{40}", binding.source_tree) is None
                or binding.artifact_sha256 != expected_worker_digest
            ):
                raise RolloutGateError("cleanup_state_invalid", scope=domain)
        worker_bindings = {
            name: str(self._ddb_string(item, f"worker_binding_{name}"))
            for name in _WORKER_BINDING_NAMES
        }
        if any(_PROVENANCE_RE.fullmatch(value) is None for value in worker_bindings.values()):
            raise RolloutGateError("cleanup_state_invalid", scope=domain)
        return CleanupLedger(
            domain=domain,
            stage=stage,
            revision=revision,
            authorized_at=authorized_at,
            old_provenances=old_provenances,
            new_provenances=new_provenances,
            candidate_digests=candidate_digests,
            rollback_digests=rollback_digests,
            proposed=proposed,
            legacy_database_generation=legacy_database_generation,
            legacy_worker_generation=legacy_worker_generation,
            candidate_worker_env_digest=candidate_worker_env_digest,
            rollback_worker_env_digest=rollback_worker_env_digest,
            candidate_arns=candidate_arns,
            prepared_plan_sha256=prepared_plan_sha256,
            prepared_intent_id=prepared_intent_id,
            baseline_arns=baseline_arns,
            baseline_digests=baseline_digests,
            baseline_provenances=baseline_provenances,
            candidate_worker_provenance=worker_provenance["candidate"],
            rollback_worker_provenance=worker_provenance["rollback"],
            worker_bindings=worker_bindings,
        )

    def _active_cleanup(self) -> CleanupLedger | None:
        active: list[CleanupLedger] = []
        for domain in _DOMAIN_MAX_TTL:
            item = self._read_item_optional(self._cleanup_record_name(domain))
            if item is None:
                continue
            stage = self._ddb_string(item, "stage")
            if stage == "authorized":
                active.append(self._cleanup_ledger(domain))
            elif stage not in {"aborted", "complete"}:
                raise RolloutGateError("cleanup_state_invalid", scope=domain)
        if len(active) > 1:
            raise RolloutGateError("cleanup_state_invalid")
        return active[0] if active else None

    def _deployment_intent_item(self, intent_id: str) -> dict[str, Any]:
        response = self.ddb.get_item(
            TableName=_DEPLOYMENT_INTENT_TABLE,
            Key={"record_id": {"S": f"intent#{intent_id}"}},
            ConsistentRead=True,
        )
        self._observe(response)
        item = response.get("Item") if type(response) is dict else None
        if type(item) is not dict:
            raise RolloutGateError("cleanup_reconciliation_intent_invalid")
        return item

    def _cleanup_live_state(
        self,
        cleanup: CleanupLedger,
    ) -> tuple[dict[str, dict[str, str]], str]:
        arns, definitions = self._live_tasks()
        self._full_task_inventory(arns)
        state: dict[str, dict[str, str]] = {}
        rollback_arns = {
            "mcp": self.control.mcp.rollback_task_definition,
            "connect_web": self.control.connect_web.rollback_task_definition,
            "morning_digest": self.control.morning_digest.rollback_task_definition,
        }
        for task, definition in definitions.items():
            digest = _task_artifact_digest(definition)
            arn = arns[task]
            if arn == cleanup.baseline_arns[task] and digest == cleanup.baseline_digests[task]:
                classification = "baseline"
            elif digest == cleanup.candidate_digests[task] and (
                cleanup.candidate_arns[task] is None or arn == cleanup.candidate_arns[task]
            ):
                classification = "candidate"
            elif arn == rollback_arns[task] and digest == cleanup.rollback_digests[task]:
                classification = "rollback"
            else:
                raise RolloutGateError("cleanup_live_state_unproved", scope=task)
            state[task] = {
                "arn": arn,
                "classification": classification,
                "digest": digest,
            }
        state["worker"] = self._cleanup_worker_live_state(cleanup)
        encoded = json.dumps(
            {
                "domain": cleanup.domain,
                "rotation_epoch": self.control.rotation_epoch,
                "resources": state,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return state, hashlib.sha256(encoded).hexdigest()

    def _cleanup_worker_live_state(
        self,
        cleanup: CleanupLedger,
    ) -> dict[str, str]:
        """Classify the active worker from the request-before-switch audit record."""

        cleanup_item = self._read_item(self._cleanup_record_name(cleanup.domain))
        bound_plan = self._ddb_string(
            cleanup_item,
            "candidate_worker_plan_sha256",
            optional=True,
        )
        bound_attempt = self._ddb_string(
            cleanup_item,
            "candidate_worker_apply_attempt_id",
            optional=True,
        )
        if (bound_plan is None) != (bound_attempt is None) or (
            bound_plan is not None
            and (
                _PROVENANCE_RE.fullmatch(bound_plan) is None
                or _APPLY_ATTEMPT_RE.fullmatch(str(bound_attempt)) is None
                or bound_plan != cleanup.prepared_plan_sha256
            )
        ):
            raise RolloutGateError("cleanup_live_state_unproved", scope="worker")

        record_name = self._restart_record_name("cleanup")
        restart = self._read_item_optional(record_name)
        missing_digest = hashlib.sha256(b"missing-worker-restart-record").hexdigest()
        if restart is None:
            prior_digest = self._ddb_string(
                cleanup_item,
                "reconciliation_worker_restart_record_sha256",
                optional=True,
            )
            if prior_digest is not None and prior_digest != missing_digest:
                raise RolloutGateError("cleanup_live_state_unproved", scope="worker")
            return {
                "apply_attempt_id": str(bound_attempt or ""),
                "classification": "baseline",
                "evidence": "no-restart-request",
                "plan_sha256": str(bound_plan or ""),
                "restart_record_present": "false",
                "restart_record_revision": "",
                "restart_record_sha256": missing_digest,
                "restart_record_stage": "missing",
            }

        revision = self._ddb_number(restart, "revision")
        stage = self._ddb_string(restart, "stage")
        mode = self._ddb_string(restart, "mode")
        request_plan = self._ddb_string(restart, "plan_sha256", optional=True)
        request_attempt = self._ddb_string(
            restart,
            "apply_attempt_id",
            optional=True,
        )
        restart_digest = hashlib.sha256(
            json.dumps(restart, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        if (
            revision is None
            or type(stage) is not str
            or type(mode) is not str
            or self._ddb_string(restart, "rotation_epoch") != self.control.rotation_epoch
        ):
            raise RolloutGateError("cleanup_live_state_unproved", scope="worker")

        directly_bound = (
            bound_plan is not None
            and request_plan == bound_plan
            and request_attempt == bound_attempt
            and mode == "cleanup"
        )
        prior_digest = self._ddb_string(
            cleanup_item,
            "reconciliation_worker_restart_record_sha256",
            optional=True,
        )
        prior_classification = self._ddb_string(
            cleanup_item,
            "reconciliation_worker_classification",
            optional=True,
        )
        if directly_bound:
            if (
                self._ddb_string(restart, "provenance") != self.control.worker.provenance
                or self._ddb_string(restart, "artifact_sha256")
                != cleanup.candidate_digests["worker"]
            ):
                raise RolloutGateError("cleanup_live_state_unproved", scope="worker")
            if stage == "complete":
                release_root = self._ddb_string(restart, "release_root", optional=True)
                release_tree = self._ddb_string(
                    restart,
                    "release_tree_sha256",
                    optional=True,
                )
                executable = self._ddb_string(
                    restart,
                    "runtime_executable_sha256",
                    optional=True,
                )
                completed_at = self._ddb_number(
                    restart,
                    "completed_at",
                    optional=True,
                )
                if (
                    type(release_root) is not str
                    or type(release_tree) is not str
                    or type(executable) is not str
                    or completed_at is None
                    or _RELEASE_ROOT_RE.fullmatch(release_root) is None
                    or _PROVENANCE_RE.fullmatch(release_tree) is None
                    or release_root.rsplit("/", maxsplit=1)[-1] != release_tree
                    or _PROVENANCE_RE.fullmatch(executable) is None
                ):
                    raise RolloutGateError("cleanup_live_state_unproved", scope="worker")
                classification = "candidate"
                evidence = "completed-restart"
            elif stage == "reconciled":
                if (
                    self._ddb_string(
                        restart,
                        "reconciliation_outcome",
                        optional=True,
                    )
                    != "rolled-back"
                    or self._ddb_string(
                        restart,
                        "reconciliation_plan_sha256",
                        optional=True,
                    )
                    != request_plan
                    or self._ddb_string(
                        restart,
                        "reconciliation_apply_attempt_id",
                        optional=True,
                    )
                    != request_attempt
                ):
                    raise RolloutGateError("cleanup_live_state_unproved", scope="worker")
                classification = "baseline"
                evidence = "audited-rollback"
            else:
                raise RolloutGateError("cleanup_live_state_unproved", scope="worker")
        elif prior_digest == restart_digest and prior_classification in {"baseline", "candidate"}:
            classification = str(prior_classification)
            evidence = "prior-reconciliation"
        elif (
            mode != "cleanup"
            and stage == "complete"
            and self._ddb_string(restart, "provenance") == self.control.worker.provenance
            and self._ddb_string(restart, "artifact_sha256") == cleanup.candidate_digests["worker"]
            and self._ddb_number(restart, "completed_at", optional=True) is not None
        ):
            classification = "baseline"
            evidence = "pre-cleanup-restart"
        else:
            raise RolloutGateError("cleanup_live_state_unproved", scope="worker")

        return {
            "apply_attempt_id": str(request_attempt or ""),
            "classification": classification,
            "evidence": evidence,
            "plan_sha256": str(request_plan or ""),
            "restart_record_present": "true",
            "restart_record_revision": str(revision),
            "restart_record_sha256": restart_digest,
            "restart_record_stage": stage,
        }

    def _cleanup_worker_condition(
        self,
        worker_state: dict[str, str],
    ) -> dict[str, Any]:
        key = {
            "scope": {"S": self.control.scope},
            "record": {"S": self._restart_record_name("cleanup")},
        }
        if worker_state.get("restart_record_present") == "false":
            return {
                "ConditionCheck": {
                    "TableName": self.control.state_table,
                    "Key": key,
                    "ConditionExpression": "attribute_not_exists(#record)",
                    "ExpressionAttributeNames": {"#record": "record"},
                }
            }
        revision = worker_state.get("restart_record_revision")
        stage = worker_state.get("restart_record_stage")
        if (
            type(revision) is not str
            or not revision.isdecimal()
            or type(stage) is not str
            or not stage
        ):
            raise RolloutGateError("cleanup_live_state_unproved", scope="worker")
        return {
            "ConditionCheck": {
                "TableName": self.control.state_table,
                "Key": key,
                "ConditionExpression": ("revision = :revision AND #stage = :stage"),
                "ExpressionAttributeNames": {"#stage": "stage"},
                "ExpressionAttributeValues": {
                    ":revision": {"N": revision},
                    ":stage": {"S": stage},
                },
            }
        }

    def reconcile_cleanup(
        self,
        *,
        domain: str,
        decision: str,
        fresh_plan_sha256: str | None = None,
        fresh_intent_id: str | None = None,
        fresh_candidate_definitions: dict[str, dict[str, Any]] | None = None,
        fresh_worker_bindings: dict[str, str] | None = None,
    ) -> None:
        """CAS-rebind a fresh one-use plan or abort only from a proved exact live state."""

        cleanup = self._cleanup_ledger(domain)
        if cleanup.stage != "authorized" or decision not in {"abort", "rebind"}:
            raise RolloutGateError("cleanup_reconciliation_invalid", scope=domain)
        live_state, live_state_digest = self._cleanup_live_state(cleanup)
        worker_state = live_state.get("worker")
        if type(worker_state) is not dict:
            raise RolloutGateError("cleanup_live_state_unproved", scope="worker")
        worker_condition = self._cleanup_worker_condition(worker_state)
        old_intent = self._deployment_intent_item(cleanup.prepared_intent_id)
        old_state = self._ddb_string(old_intent, "state")
        if (
            self._ddb_string(old_intent, "intent_id") != cleanup.prepared_intent_id
            or self._ddb_string(old_intent, "plan_sha256") != cleanup.prepared_plan_sha256
            or old_state not in {"PREPARED", "RECONCILE_REQUIRED"}
        ):
            raise RolloutGateError("cleanup_reconciliation_intent_invalid", scope=domain)
        now = self._now()
        cleanup_key = {
            "scope": {"S": self.control.scope},
            "record": {"S": self._cleanup_record_name(domain)},
        }

        if decision == "abort":
            if any(item["classification"] != "baseline" for item in live_state.values()):
                raise RolloutGateError("cleanup_abort_live_mutation", scope=domain)
            domain_items = {
                item_domain: self._read_item(f"DOMAIN#{item_domain}")
                for item_domain in _DOMAIN_MAX_TTL
            }
            transaction: list[dict[str, Any]] = [
                {
                    "Update": {
                        "TableName": _DEPLOYMENT_INTENT_TABLE,
                        "Key": {"record_id": {"S": f"intent#{cleanup.prepared_intent_id}"}},
                        "UpdateExpression": (
                            "SET #state = :aborted, cleanup_aborted_at = :now,"
                            " cleanup_live_state_sha256 = :live"
                        ),
                        "ConditionExpression": (
                            "plan_sha256 = :plan AND (#state = :prepared OR #state = :reconcile)"
                        ),
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {
                            ":plan": {"S": cleanup.prepared_plan_sha256},
                            ":prepared": {"S": "PREPARED"},
                            ":reconcile": {"S": "RECONCILE_REQUIRED"},
                            ":aborted": {"S": "ABORTED"},
                            ":now": {"N": str(now)},
                            ":live": {"S": live_state_digest},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": self.control.state_table,
                        "Key": cleanup_key,
                        "UpdateExpression": (
                            "SET #stage = :aborted, aborted_at = :now,"
                            " reconciliation_live_state_sha256 = :live,"
                            " reconciliation_worker_classification = :worker_class,"
                            " reconciliation_worker_restart_record_sha256 = :worker_record,"
                            " reconciliation_prior_plan_sha256 = :plan,"
                            " revision = revision + :one"
                        ),
                        "ConditionExpression": (
                            "#stage = :authorized AND revision = :revision"
                            " AND prepared_plan_sha256 = :plan"
                        ),
                        "ExpressionAttributeNames": {"#stage": "stage"},
                        "ExpressionAttributeValues": {
                            ":aborted": {"S": "aborted"},
                            ":authorized": {"S": "authorized"},
                            ":now": {"N": str(now)},
                            ":live": {"S": live_state_digest},
                            ":worker_class": {
                                "S": worker_state["classification"],
                            },
                            ":worker_record": {
                                "S": worker_state["restart_record_sha256"],
                            },
                            ":plan": {"S": cleanup.prepared_plan_sha256},
                            ":revision": {"N": str(cleanup.revision)},
                            ":one": {"N": "1"},
                        },
                    }
                },
            ]
            for item_domain, item in domain_items.items():
                revision = self._ddb_number(item, "revision")
                current = self._ddb_string_set(item, "issuer_provenances")
                expected = (
                    cleanup.baseline_provenances[item_domain] | cleanup.new_provenances[item_domain]
                )
                if revision is None or current != expected:
                    raise RolloutGateError("cleanup_abort_state_drift", scope=item_domain)
                update_expression = "SET issuer_provenances = :baseline, revision = revision + :one"
                if item_domain == domain:
                    update_expression += " REMOVE cleanup_stage"
                transaction.append(
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": f"DOMAIN#{item_domain}"},
                            },
                            "UpdateExpression": update_expression,
                            "ConditionExpression": (
                                "revision = :revision AND issuer_provenances = :current"
                            ),
                            "ExpressionAttributeValues": {
                                ":revision": {"N": str(revision)},
                                ":current": {"SS": sorted(current)},
                                ":baseline": {
                                    "SS": sorted(cleanup.baseline_provenances[item_domain])
                                },
                                ":one": {"N": "1"},
                            },
                        }
                    }
                )
            transaction.append(worker_condition)
        else:
            if (
                old_state != "RECONCILE_REQUIRED"
                or type(fresh_plan_sha256) is not str
                or _PROVENANCE_RE.fullmatch(fresh_plan_sha256) is None
                or fresh_plan_sha256 == cleanup.prepared_plan_sha256
                or type(fresh_intent_id) is not str
                or _APPLY_ATTEMPT_RE.fullmatch(fresh_intent_id) is None
                or fresh_intent_id == cleanup.prepared_intent_id
                or fresh_candidate_definitions is None
                or fresh_worker_bindings is None
            ):
                raise RolloutGateError("cleanup_reconciliation_invalid", scope=domain)
            if frozenset(fresh_candidate_definitions) != frozenset(_TASK_DOMAINS):
                raise RolloutGateError("cleanup_reconciliation_invalid", scope=domain)
            for task, definition in fresh_candidate_definitions.items():
                if _task_artifact_digest(definition) != cleanup.candidate_digests[task]:
                    raise RolloutGateError("cleanup_reconciliation_artifact_drift", scope=task)
            if fresh_worker_bindings != cleanup.worker_bindings:
                raise RolloutGateError("cleanup_reconciliation_artifact_drift", scope="worker")
            fresh_intent = self._deployment_intent_item(fresh_intent_id)
            expires_at = self._ddb_number(fresh_intent, "authorization_expires_at")
            if (
                self._ddb_string(fresh_intent, "state") != "PREPARED"
                or self._ddb_string(fresh_intent, "intent_id") != fresh_intent_id
                or self._ddb_string(fresh_intent, "plan_sha256") != fresh_plan_sha256
                or expires_at is None
                or expires_at <= now
            ):
                raise RolloutGateError("cleanup_reconciliation_intent_invalid", scope=domain)
            remove_names: dict[str, str] = {}
            for kind in ("candidate", "rollback"):
                for task in (*_TASK_DOMAINS, "worker"):
                    remove_names[f"#{kind}_{task}_plan"] = f"{kind}_{task}_plan_sha256"
                    remove_names[f"#{kind}_{task}_attempt"] = f"{kind}_{task}_apply_attempt_id"
            remove_expression = ", ".join(remove_names)
            transaction = [
                {
                    "Update": {
                        "TableName": _DEPLOYMENT_INTENT_TABLE,
                        "Key": {"record_id": {"S": f"intent#{cleanup.prepared_intent_id}"}},
                        "UpdateExpression": (
                            "SET #state = :superseded,"
                            " superseded_by_intent_id = :new_intent,"
                            " superseded_by_plan_sha256 = :new_plan,"
                            " superseded_at = :now"
                        ),
                        "ConditionExpression": ("#state = :reconcile AND plan_sha256 = :old_plan"),
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {
                            ":reconcile": {"S": "RECONCILE_REQUIRED"},
                            ":superseded": {"S": "SUPERSEDED"},
                            ":old_plan": {"S": cleanup.prepared_plan_sha256},
                            ":new_plan": {"S": fresh_plan_sha256},
                            ":new_intent": {"S": fresh_intent_id},
                            ":now": {"N": str(now)},
                        },
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": _DEPLOYMENT_INTENT_TABLE,
                        "Key": {"record_id": {"S": f"intent#{fresh_intent_id}"}},
                        "ConditionExpression": (
                            "#state = :prepared AND plan_sha256 = :new_plan"
                            " AND authorization_expires_at > :now"
                        ),
                        "ExpressionAttributeNames": {"#state": "state"},
                        "ExpressionAttributeValues": {
                            ":prepared": {"S": "PREPARED"},
                            ":new_plan": {"S": fresh_plan_sha256},
                            ":now": {"N": str(now)},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": self.control.state_table,
                        "Key": cleanup_key,
                        "UpdateExpression": (
                            "SET prepared_plan_sha256 = :new_plan,"
                            " prepared_intent_id = :new_intent,"
                            " reconciliation_prior_plan_sha256 = :old_plan,"
                            " reconciliation_prior_intent_id = :old_intent,"
                            " reconciliation_live_state_sha256 = :live,"
                            " reconciliation_worker_classification = :worker_class,"
                            " reconciliation_worker_restart_record_sha256 = :worker_record,"
                            " reconciled_at = :now, revision = revision + :one"
                            f" REMOVE {remove_expression}"
                        ),
                        "ConditionExpression": (
                            "#stage = :authorized AND revision = :revision"
                            " AND prepared_plan_sha256 = :old_plan"
                            " AND prepared_intent_id = :old_intent"
                        ),
                        "ExpressionAttributeNames": {
                            "#stage": "stage",
                            **remove_names,
                        },
                        "ExpressionAttributeValues": {
                            ":authorized": {"S": "authorized"},
                            ":revision": {"N": str(cleanup.revision)},
                            ":new_plan": {"S": fresh_plan_sha256},
                            ":new_intent": {"S": fresh_intent_id},
                            ":old_plan": {"S": cleanup.prepared_plan_sha256},
                            ":old_intent": {"S": cleanup.prepared_intent_id},
                            ":live": {"S": live_state_digest},
                            ":worker_class": {
                                "S": worker_state["classification"],
                            },
                            ":worker_record": {
                                "S": worker_state["restart_record_sha256"],
                            },
                            ":now": {"N": str(now)},
                            ":one": {"N": "1"},
                        },
                    }
                },
                worker_condition,
            ]
        try:
            response = self.ddb.transact_write_items(TransactItems=transaction)
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("cleanup_reconciliation_cas_failed", scope=domain) from exc

    def _cleanup_proposed_from_ledger(
        self,
        cleanup: CleanupLedger,
    ) -> dict[str, dict[str, object]]:
        """Return the immutable proposal authorized at prepare-cleanup."""

        return copy.deepcopy(cleanup.proposed)

    @staticmethod
    def _conditional_failure(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        error = response.get("Error") if type(response) is dict else None
        return type(error) is dict and error.get("Code") in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        }

    def _authorize_cleanup_cas(
        self,
        *,
        domain: str,
        deployed: dict[str, dict[str, object]],
        proposed: dict[str, dict[str, object]],
        candidate_digests: dict[str, str],
        rollback_digests: dict[str, str],
        prepared_plan_sha256: str,
        prepared_intent_id: str,
        baseline_arns: dict[str, str],
        baseline_digests: dict[str, str],
        legacy_database_generation: str,
        legacy_worker_generation: str | None,
        candidate_worker_env_digest: str,
        rollback_worker_env_digest: str,
        candidate_worker_provenance: ProvenanceBinding,
        rollback_worker_provenance: ProvenanceBinding,
        worker_bindings: dict[str, str],
    ) -> None:
        """Retry only clock/high-water races while keeping the reviewed proposal immutable."""

        new_provenances = self._issuer_provenances(self.control)
        for attempt in range(12):
            domain_items = {
                item_domain: self._read_item(f"DOMAIN#{item_domain}")
                for item_domain in _DOMAIN_MAX_TTL
            }
            now = self._now()
            target = domain_items[domain]
            previous = self._ddb_string(target, "previous_generation", optional=True)
            t0 = self._ddb_number(target, "rotation_started_at", optional=True)
            deadline = self._ddb_number(target, "deadline", optional=True)
            legacy_worker = self._ddb_string(
                target,
                "legacy_worker_generation",
                optional=True,
            )
            if (
                previous is None
                or t0 is None
                or deadline is None
                or now < deadline
                or self._ddb_string(target, "cleanup_stage", optional=True) is not None
            ):
                raise RolloutGateError("previous_window_active", scope=domain)

            old_provenances: dict[str, frozenset[str]] = {}
            revisions: dict[str, int] = {}
            high_waters: dict[str, int] = {}
            for item_domain, item in domain_items.items():
                current = self._ddb_string_set(item, "issuer_provenances")
                retired = self._ddb_string_set(item, "retired_provenances")
                revision = self._ddb_number(item, "revision")
                high_water = self._ddb_number(item, "high_water")
                if (
                    self._ddb_string(item, "rotation_epoch") != self.control.rotation_epoch
                    or self._ddb_string(item, "stage") != "complete"
                    or self._domain_config_from_item(item) != deployed[item_domain]
                    or not current
                    or revision is None
                    or high_water is None
                    or current & retired
                    or new_provenances[item_domain] & retired
                ):
                    raise RolloutGateError("cleanup_state_invalid", scope=item_domain)
                old_provenances[item_domain] = current - new_provenances[item_domain]
                revisions[item_domain] = revision
                high_waters[item_domain] = high_water
            if not old_provenances[domain]:
                raise RolloutGateError("cleanup_provenance_not_distinct", scope=domain)

            cleanup_item: dict[str, Any] = {
                "scope": {"S": self.control.scope},
                "record": {"S": self._cleanup_record_name(domain)},
                "domain": {"S": domain},
                "rotation_epoch": {"S": self.control.rotation_epoch},
                "stage": {"S": "authorized"},
                "revision": {"N": "1"},
                "authorized_at": {"N": str(now)},
                "prepared_plan_sha256": {"S": prepared_plan_sha256},
                "prepared_intent_id": {"S": prepared_intent_id},
                "proposed_legacy_database_generation": {"S": legacy_database_generation},
                "candidate_worker_env_digest": {"S": candidate_worker_env_digest},
                "rollback_worker_env_digest": {"S": rollback_worker_env_digest},
            }
            if legacy_worker_generation is not None:
                cleanup_item["proposed_legacy_worker_generation"] = {"S": legacy_worker_generation}
            for proposed_domain, config in proposed.items():
                cleanup_item[f"proposed_{proposed_domain}_primary"] = {
                    "S": str(config["primary_generation"])
                }
                if config["previous_generation"] is not None:
                    cleanup_item[f"proposed_{proposed_domain}_previous"] = {
                        "S": str(config["previous_generation"])
                    }
                    cleanup_item[f"proposed_{proposed_domain}_t0"] = {
                        "N": str(config["rotation_started_at"])
                    }
            for item_domain in _DOMAIN_MAX_TTL:
                if old_provenances[item_domain]:
                    cleanup_item[f"old_{item_domain}_provenances"] = {
                        "SS": sorted(old_provenances[item_domain])
                    }
                cleanup_item[f"new_{item_domain}_provenances"] = {
                    "SS": sorted(new_provenances[item_domain])
                }
                cleanup_item[f"baseline_{item_domain}_provenances"] = {
                    "SS": sorted(
                        self._ddb_string_set(domain_items[item_domain], "issuer_provenances")
                    )
                }
            for task in _TASK_DOMAINS:
                cleanup_item[f"baseline_{task}_arn"] = {"S": baseline_arns[task]}
                cleanup_item[f"baseline_{task}_digest"] = {"S": baseline_digests[task]}
            for task, digest in candidate_digests.items():
                cleanup_item[f"candidate_{task}_digest"] = {"S": digest}
            for task, digest in rollback_digests.items():
                cleanup_item[f"rollback_{task}_digest"] = {"S": digest}
            for kind, binding in (
                ("candidate", candidate_worker_provenance),
                ("rollback", rollback_worker_provenance),
            ):
                for name, value in {
                    "artifact_sha256": binding.artifact_sha256,
                    "canonical_receipt_sha256": binding.canonical_receipt_sha256,
                    "key_arn": binding.key_arn,
                    "receipt_sha256": binding.receipt_sha256,
                    "signature_sha256": binding.signature_sha256,
                    "source_branch": binding.source_branch,
                    "source_commit": binding.source_commit,
                    "source_origin": binding.source_origin,
                    "source_tree": binding.source_tree,
                }.items():
                    cleanup_item[f"{kind}_worker_{name}"] = {"S": value}
            if frozenset(worker_bindings) != _WORKER_BINDING_NAMES:
                raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
            for name, digest in worker_bindings.items():
                if _PROVENANCE_RE.fullmatch(digest) is None:
                    raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
                cleanup_item[f"worker_binding_{name}"] = {"S": digest}

            transaction: list[dict[str, Any]] = [
                {
                    "Put": {
                        "TableName": self.control.state_table,
                        "Item": cleanup_item,
                        "ConditionExpression": "attribute_not_exists(#record)",
                        "ExpressionAttributeNames": {"#record": "record"},
                    }
                }
            ]
            for item_domain, item in domain_items.items():
                existing_issuers = self._ddb_string_set(item, "issuer_provenances")
                temporary = existing_issuers | new_provenances[item_domain]
                values: dict[str, Any] = {
                    ":revision": {"N": str(revisions[item_domain])},
                    ":epoch": {"S": self.control.rotation_epoch},
                    ":complete": {"S": "complete"},
                    ":old_issuers": {"SS": sorted(existing_issuers)},
                    ":temporary": {"SS": sorted(temporary)},
                    ":one": {"N": "1"},
                }
                expression = "SET issuer_provenances = :temporary, revision = revision + :one"
                condition = (
                    "revision = :revision AND rotation_epoch = :epoch"
                    " AND #stage = :complete AND issuer_provenances = :old_issuers"
                )
                names = {"#stage": "stage"}
                if item_domain == domain:
                    effective_now = max(now, high_waters[item_domain])
                    expression += (
                        ", previous_retired = :true, high_water = :now,"
                        " cleanup_stage = :authorized"
                        " ADD retired_generations :retired"
                    )
                    condition += (
                        " AND high_water = :high_water"
                        " AND previous_generation = :previous AND deadline = :deadline"
                    )
                    values.update(
                        {
                            ":true": {"BOOL": True},
                            ":now": {"N": str(effective_now)},
                            ":authorized": {"S": "authorized"},
                            ":high_water": {"N": str(high_waters[item_domain])},
                            ":previous": {"S": previous},
                            ":deadline": {"N": str(deadline)},
                            ":retired": {
                                "SS": sorted(
                                    {previous}
                                    | ({legacy_worker} if legacy_worker is not None else set())
                                )
                            },
                        }
                    )
                transaction.append(
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": f"DOMAIN#{item_domain}"},
                            },
                            "UpdateExpression": expression,
                            "ConditionExpression": condition,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": values,
                        }
                    }
                )
            try:
                response = self.ddb.transact_write_items(TransactItems=transaction)
                self._observe(response)
                return
            except Exception as exc:
                if not self._conditional_failure(exc):
                    raise RolloutGateError("cleanup_prepare_cas_failed", scope=domain) from exc
                if attempt >= 3:
                    time.sleep(min(0.002 * (2 ** min(attempt - 3, 4)), 0.02))
        raise RolloutGateError("cleanup_prepare_cas_failed", scope=domain)

    def prepare_cleanup(
        self,
        *,
        domain: str,
        candidate_definitions: dict[str, dict[str, Any]],
        worker_env: Path,
        worker_rollback_env: Path,
        worker_artifact: Path,
        worker_rollback_artifact: Path,
        worker_provenance_receipt: Path,
        worker_provenance_signature: Path,
        worker_rollback_provenance_receipt: Path,
        worker_rollback_provenance_signature: Path,
        worker_provenance_key_arn: str,
        worker_bindings: dict[str, str],
        prepared_intent_id: str,
        prepared_plan_sha256: str,
    ) -> None:
        """Authorize a reviewed primary-only replacement without reopening the expired key."""

        if domain not in _DOMAIN_MAX_TTL:
            raise RolloutGateError("unknown_domain")
        if _PROVENANCE_RE.fullmatch(prepared_plan_sha256) is None:
            raise RolloutGateError("terraform_plan_unreadable", scope=domain)
        if _APPLY_ATTEMPT_RE.fullmatch(prepared_intent_id) is None:
            raise RolloutGateError("deployment_intent_missing", scope=domain)
        if frozenset(candidate_definitions) != frozenset(_TASK_DOMAINS):
            raise RolloutGateError("cleanup_candidate_incomplete", scope=domain)
        if self._ledger().stage != "complete" or self._active_cleanup() is not None:
            raise RolloutGateError("stage_order_violation", scope=domain)
        arns, definitions = self._live_tasks()
        self._full_task_inventory(arns)
        deployed = self._observed_deployed(definitions)
        proposed = self._cleanup_transition(domain=domain, deployed=deployed)
        self._assert_proposed_generations_exist(proposed)
        manifest_legacy_database = self.manifest.get("legacy_database_generation")
        if type(manifest_legacy_database) is not str:
            raise RolloutGateError("cleanup_manifest_invalid", scope=domain)
        legacy_database_generation = manifest_legacy_database
        manifest_legacy_worker = self.manifest.get("legacy_worker_generation")
        legacy_worker_generation = (
            manifest_legacy_worker if type(manifest_legacy_worker) is str else None
        )
        # Cryptographic provenance is authority, not a post-authorization diagnostic. Verify both
        # sides before any cleanup/domain CAS can authorize the fresh issuer provenances.
        kms = self._clients.client("kms", region_name=self.control.region)
        try:
            candidate_worker_provenance = verify_worker_provenance(
                artifact=worker_artifact,
                receipt_path=worker_provenance_receipt,
                signature_path=worker_provenance_signature,
                expected_key_arn=worker_provenance_key_arn,
                kms=kms,
            )
            rollback_worker_provenance = verify_worker_provenance(
                artifact=worker_rollback_artifact,
                receipt_path=worker_rollback_provenance_receipt,
                signature_path=worker_rollback_provenance_signature,
                expected_key_arn=worker_provenance_key_arn,
                kms=kms,
            )
        except Exception as exc:
            raise RolloutGateError("worker_provenance_invalid", scope="worker") from exc
        if (
            candidate_worker_provenance.artifact_sha256 != self.control.worker.artifact_sha256
            or rollback_worker_provenance.artifact_sha256
            != self.control.worker.rollback_artifact_sha256
            or frozenset(worker_bindings) != _WORKER_BINDING_NAMES
            or any(_PROVENANCE_RE.fullmatch(digest) is None for digest in worker_bindings.values())
            or worker_bindings["candidate_artifact"] != candidate_worker_provenance.artifact_sha256
            or worker_bindings["candidate_receipt"] != candidate_worker_provenance.receipt_sha256
            or worker_bindings["candidate_signature"]
            != candidate_worker_provenance.signature_sha256
            or worker_bindings["rollback_artifact"] != rollback_worker_provenance.artifact_sha256
            or worker_bindings["rollback_receipt"] != rollback_worker_provenance.receipt_sha256
            or worker_bindings["rollback_signature"] != rollback_worker_provenance.signature_sha256
        ):
            raise RolloutGateError("worker_provenance_invalid", scope="worker")

        candidate_identities = [
            definition.get("taskDefinitionArn")
            for definition in candidate_definitions.values()
            if definition.get("taskDefinitionArn") is not None
        ]
        if len(candidate_identities) != len(set(candidate_identities)) or any(
            type(identity) is not str or _TASK_DEFINITION_RE.fullmatch(identity) is None
            for identity in candidate_identities
        ):
            raise RolloutGateError("cleanup_artifacts_not_distinct", scope=domain)
        candidate_digests = {
            task: self._validate_cleanup_task(
                task=task,
                definition=candidate_definitions[task],
                proposed=proposed,
                legacy_database_generation=legacy_database_generation,
                legacy_worker_generation=legacy_worker_generation,
                rollback=False,
            )
            for task in _TASK_DOMAINS
        }
        rollback_definitions = {
            "mcp": self._describe_task(self.control.mcp.rollback_task_definition),
            "connect_web": self._describe_task(self.control.connect_web.rollback_task_definition),
            "morning_digest": self._describe_task(
                self.control.morning_digest.rollback_task_definition
            ),
        }
        rollback_digests = {
            task: self._validate_cleanup_task(
                task=task,
                definition=definition,
                proposed=proposed,
                legacy_database_generation=legacy_database_generation,
                legacy_worker_generation=legacy_worker_generation,
                rollback=True,
            )
            for task, definition in rollback_definitions.items()
        }
        if any(candidate_digests[task] == rollback_digests[task] for task in _TASK_DOMAINS):
            raise RolloutGateError("cleanup_artifacts_not_distinct", scope=domain)
        candidate_worker_env_digest = self._validate_cleanup_worker(
            env_path=worker_env,
            artifact_path=worker_artifact,
            proposed=proposed,
            legacy_database_generation=legacy_database_generation,
            legacy_worker_generation=legacy_worker_generation,
            rollback=False,
        )
        rollback_worker_env_digest = self._validate_cleanup_worker(
            env_path=worker_rollback_env,
            artifact_path=worker_rollback_artifact,
            proposed=proposed,
            legacy_database_generation=legacy_database_generation,
            legacy_worker_generation=legacy_worker_generation,
            rollback=True,
        )
        candidate_digests["worker"] = self.control.worker.artifact_sha256
        rollback_digests["worker"] = self.control.worker.rollback_artifact_sha256
        self._authorize_cleanup_cas(
            domain=domain,
            deployed=deployed,
            proposed=proposed,
            candidate_digests=candidate_digests,
            rollback_digests=rollback_digests,
            prepared_plan_sha256=prepared_plan_sha256,
            prepared_intent_id=prepared_intent_id,
            baseline_arns=arns,
            baseline_digests={
                task: _task_artifact_digest(definition) for task, definition in definitions.items()
            },
            legacy_database_generation=legacy_database_generation,
            legacy_worker_generation=legacy_worker_generation,
            candidate_worker_env_digest=candidate_worker_env_digest,
            rollback_worker_env_digest=rollback_worker_env_digest,
            candidate_worker_provenance=candidate_worker_provenance,
            rollback_worker_provenance=rollback_worker_provenance,
            worker_bindings=worker_bindings,
        )

    def _validate_prepared_cleanup_task(
        self,
        *,
        cleanup: CleanupLedger,
        task: str,
        definition: dict[str, Any],
        rollback: bool,
    ) -> None:
        if cleanup.stage != "authorized":
            raise RolloutGateError("stage_order_violation", scope=task)
        proposed = self._cleanup_proposed_from_ledger(cleanup)
        digest = self._validate_cleanup_task(
            task=task,
            definition=definition,
            proposed=proposed,
            legacy_database_generation=cleanup.legacy_database_generation,
            legacy_worker_generation=cleanup.legacy_worker_generation,
            rollback=rollback,
            prepared=True,
        )
        expected = cleanup.rollback_digests[task] if rollback else cleanup.candidate_digests[task]
        if digest != expected:
            raise RolloutGateError("cleanup_artifact_drift", scope=task)

    def _deployment_intent(self) -> DeploymentIntent:
        intent = self.deployment_intent
        if (
            intent is None
            or _PROVENANCE_RE.fullmatch(intent.plan_sha256) is None
            or _APPLY_ATTEMPT_RE.fullmatch(intent.apply_attempt_id) is None
        ):
            raise RolloutGateError("deployment_intent_missing")
        return intent

    def _bind_deployment_intent(
        self,
        *,
        task: str,
        cleanup: CleanupLedger | None,
        kind: str = "candidate",
    ) -> None:
        if kind not in {"candidate", "rollback"}:
            raise RolloutGateError("deployment_intent_missing", scope=task)
        intent = self._deployment_intent()
        record = (
            self._cleanup_record_name(cleanup.domain)
            if cleanup is not None
            else f"LEDGER#{self.control.rotation_epoch}"
        )
        item = self._read_item(record)
        revision = self._ddb_number(item, "revision")
        plan_name = f"{kind}_{task}_plan_sha256"
        attempt_name = f"{kind}_{task}_apply_attempt_id"
        existing_plan = self._ddb_string(item, plan_name, optional=True)
        existing_attempt = self._ddb_string(item, attempt_name, optional=True)
        if (
            revision is None
            or (
                cleanup is not None
                and kind == "candidate"
                and cleanup.prepared_plan_sha256 != intent.plan_sha256
            )
            or (existing_plan is not None and existing_plan != intent.plan_sha256)
            or (existing_attempt is not None and existing_attempt != intent.apply_attempt_id)
            or (existing_plan is None) != (existing_attempt is None)
        ):
            raise RolloutGateError("deployment_intent_drift", scope=task)
        if existing_plan is not None:
            return
        names = {"#plan": plan_name, "#attempt": attempt_name}
        condition = (
            "revision = :revision AND attribute_not_exists(#plan)"
            " AND attribute_not_exists(#attempt)"
        )
        values: dict[str, Any] = {
            ":plan": {"S": intent.plan_sha256},
            ":attempt": {"S": intent.apply_attempt_id},
            ":revision": {"N": str(revision)},
            ":one": {"N": "1"},
        }
        if cleanup is not None and kind == "candidate":
            names["#prepared"] = "prepared_plan_sha256"
            condition += " AND #prepared = :plan"
        try:
            response = self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": record},
                            },
                            "UpdateExpression": (
                                "SET #plan = :plan, #attempt = :attempt, revision = revision + :one"
                            ),
                            "ConditionExpression": condition,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": values,
                        }
                    }
                ]
            )
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("deployment_intent_cas_failed", scope=task) from exc

    def _assert_deployment_intent(
        self,
        *,
        task: str,
        cleanup: CleanupLedger | None,
        kind: str = "candidate",
    ) -> None:
        if kind not in {"candidate", "rollback"}:
            raise RolloutGateError("deployment_intent_missing", scope=task)
        intent = self._deployment_intent()
        record = (
            self._cleanup_record_name(cleanup.domain)
            if cleanup is not None
            else f"LEDGER#{self.control.rotation_epoch}"
        )
        item = self._read_item(record)
        if (
            self._ddb_string(item, f"{kind}_{task}_plan_sha256", optional=True)
            != intent.plan_sha256
            or self._ddb_string(
                item,
                f"{kind}_{task}_apply_attempt_id",
                optional=True,
            )
            != intent.apply_attempt_id
        ):
            raise RolloutGateError("deployment_intent_drift", scope=task)

    def _bind_candidate_digest(
        self,
        *,
        task: str,
        digest: str,
        cleanup: CleanupLedger | None,
    ) -> None:
        self._bind_deployment_intent(task=task, cleanup=cleanup)
        record = (
            self._cleanup_record_name(cleanup.domain)
            if cleanup is not None
            else f"LEDGER#{self.control.rotation_epoch}"
        )
        item = self._read_item(record)
        revision = self._ddb_number(item, "revision")
        existing = self._ddb_string(item, f"candidate_{task}_digest", optional=True)
        if revision is None or (existing is not None and existing != digest):
            raise RolloutGateError("candidate_artifact_drift", scope=task)
        if existing == digest:
            return
        try:
            response = self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": record},
                            },
                            "UpdateExpression": (
                                "SET #digest = :digest, revision = revision + :one"
                            ),
                            "ConditionExpression": (
                                "revision = :revision AND attribute_not_exists(#digest)"
                            ),
                            "ExpressionAttributeNames": {
                                "#digest": f"candidate_{task}_digest",
                            },
                            "ExpressionAttributeValues": {
                                ":digest": {"S": digest},
                                ":revision": {"N": str(revision)},
                                ":one": {"N": "1"},
                            },
                        }
                    }
                ]
            )
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("candidate_artifact_cas_failed", scope=task) from exc

    def _bind_candidate_arn(
        self,
        *,
        task: str,
        task_definition: str,
        digest: str,
        cleanup: CleanupLedger | None,
    ) -> None:
        self._assert_deployment_intent(task=task, cleanup=cleanup)
        task_control = _task_control(self.control, task)
        if task_definition in {
            task_control.legacy_task_definition,
            task_control.rollback_task_definition,
            *self.control.forbidden_signing_task_definitions,
        }:
            raise RolloutGateError("candidate_identity_drift", scope=task)
        record = (
            self._cleanup_record_name(cleanup.domain)
            if cleanup is not None
            else f"LEDGER#{self.control.rotation_epoch}"
        )
        item = self._read_item(record)
        revision = self._ddb_number(item, "revision")
        expected_digest = self._ddb_string(item, f"candidate_{task}_digest", optional=True)
        existing_arn = self._ddb_string(item, f"candidate_{task}_arn", optional=True)
        if (
            revision is None
            or expected_digest != digest
            or (existing_arn is not None and existing_arn != task_definition)
        ):
            raise RolloutGateError("candidate_identity_drift", scope=task)
        if existing_arn == task_definition:
            return
        try:
            response = self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": record},
                            },
                            "UpdateExpression": "SET #arn = :arn, revision = revision + :one",
                            "ConditionExpression": (
                                "revision = :revision AND #digest = :digest"
                                " AND attribute_not_exists(#arn)"
                            ),
                            "ExpressionAttributeNames": {
                                "#arn": f"candidate_{task}_arn",
                                "#digest": f"candidate_{task}_digest",
                            },
                            "ExpressionAttributeValues": {
                                ":arn": {"S": task_definition},
                                ":digest": {"S": digest},
                                ":revision": {"N": str(revision)},
                                ":one": {"N": "1"},
                            },
                        }
                    }
                ]
            )
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("candidate_identity_cas_failed", scope=task) from exc

    def _bind_candidate_target(
        self,
        *,
        digest: str,
        cleanup: CleanupLedger | None,
    ) -> None:
        task = "morning_digest"
        self._assert_deployment_intent(task=task, cleanup=cleanup)
        record = (
            self._cleanup_record_name(cleanup.domain)
            if cleanup is not None
            else f"LEDGER#{self.control.rotation_epoch}"
        )
        item = self._read_item(record)
        revision = self._ddb_number(item, "revision")
        candidate_arn = self._ddb_string(item, f"candidate_{task}_arn", optional=True)
        existing = self._ddb_string(
            item,
            f"candidate_{task}_target_digest",
            optional=True,
        )
        existing_rule_digest = self._ddb_string(
            item,
            f"candidate_{task}_rule_sha256",
            optional=True,
        )
        expected_rule_digest = _canonical_event_rule_digest(
            self.control.morning_digest.expected_rule
        )
        if (
            revision is None
            or candidate_arn is None
            or (existing is None) != (existing_rule_digest is None)
            or (existing is not None and existing != digest)
            or (existing_rule_digest is not None and existing_rule_digest != expected_rule_digest)
        ):
            raise RolloutGateError("scheduled_target_drift", scope=task)
        if existing == digest and existing_rule_digest == expected_rule_digest:
            return
        try:
            response = self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": record},
                            },
                            "UpdateExpression": (
                                "SET #target = :target, #rule_digest = :rule_digest,"
                                " revision = revision + :one"
                            ),
                            "ConditionExpression": (
                                "revision = :revision AND #arn = :arn"
                                " AND attribute_not_exists(#target)"
                                " AND attribute_not_exists(#rule_digest)"
                            ),
                            "ExpressionAttributeNames": {
                                "#arn": f"candidate_{task}_arn",
                                "#target": f"candidate_{task}_target_digest",
                                "#rule_digest": f"candidate_{task}_rule_sha256",
                            },
                            "ExpressionAttributeValues": {
                                ":arn": {"S": candidate_arn},
                                ":target": {"S": digest},
                                ":rule_digest": {"S": expected_rule_digest},
                                ":revision": {"N": str(revision)},
                                ":one": {"N": "1"},
                            },
                        }
                    }
                ]
            )
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("scheduled_target_cas_failed", scope=task) from exc

    def pre_event_update(
        self,
        *,
        task_definition: str,
        target: dict[str, Any],
        mode: str = "candidate",
    ) -> None:
        """Bind the full planned EventBridge target before Terraform mutates it."""

        self.pre_update(
            task="morning_digest",
            task_definition=task_definition,
            mode=mode,
        )
        scheduled = self.control.morning_digest
        if (
            target.get("Id") != scheduled.target_id
            or target.get("Arn") != scheduled.cluster
            or _mapping(target.get("EcsParameters")).get("TaskDefinitionArn") != task_definition
        ):
            raise RolloutGateError("scheduled_target_drift", scope="morning_digest")
        digest = _canonical_target_digest(target)
        self._assert_morning_rule_disabled()
        # Prove the complete baseline immediately before mutation as well.
        self._scheduled_task()
        if mode == "rollback":
            self._bind_deployment_intent(
                task="morning_digest",
                cleanup=self._active_cleanup(),
                kind="rollback",
            )
            if digest != scheduled.rollback_target_digest:
                raise RolloutGateError("scheduled_target_drift", scope="morning_digest")
            return
        cleanup = self._active_cleanup()
        if mode == "cleanup" and cleanup is None:
            raise RolloutGateError("cleanup_state_missing", scope="morning_digest")
        self._bind_candidate_target(digest=digest, cleanup=cleanup)

    def _put_morning_target(self, target: dict[str, Any]) -> None:
        response = self.events.put_targets(
            Rule=self.control.morning_digest.rule,
            Targets=[copy.deepcopy(target)],
        )
        self._observe(response)
        failed_count = response.get("FailedEntryCount") if type(response) is dict else None
        failed_entries = response.get("FailedEntries") if type(response) is dict else None
        if (
            type(failed_count) is not int
            or failed_count != 0
            or type(failed_entries) is not list
            or failed_entries
        ):
            raise RolloutGateError(
                "scheduled_target_partial_failure",
                scope="morning_digest",
            )

    def _restore_morning_event_state(self, target: dict[str, Any]) -> None:
        response = self.events.disable_rule(Name=self.control.morning_digest.rule)
        self._observe(response)
        self._put_morning_target(target)
        restored = self._morning_target()
        if _canonical_target_digest(restored) != _canonical_target_digest(target):
            raise RolloutGateError(
                "scheduled_target_reconcile_required",
                scope="morning_digest",
            )

    def event_target_transaction(
        self,
        *,
        task_definition: str,
        target: dict[str, Any],
        mode: str = "candidate",
    ) -> None:
        """Replace the disabled EventBridge target and restore the exact baseline on failure."""

        self.pre_event_update(
            task_definition=task_definition,
            target=target,
            mode=mode,
        )
        baseline = self._morning_target()
        baseline_digest = _canonical_target_digest(baseline)
        desired_digest = _canonical_target_digest(target)
        if baseline_digest == desired_digest:
            self.post_update(
                task="morning_digest",
                task_definition=task_definition,
                mode=mode,
            )
            return

        mutation_attempted = False
        try:
            mutation_attempted = True
            self._put_morning_target(target)
            self.post_update(
                task="morning_digest",
                task_definition=task_definition,
                mode=mode,
            )
        except Exception as exc:
            if mutation_attempted:
                try:
                    self._restore_morning_event_state(baseline)
                except Exception as rollback_exc:
                    raise RolloutGateError(
                        "scheduled_target_reconcile_required",
                        scope="morning_digest",
                    ) from rollback_exc
            if isinstance(exc, RolloutGateError):
                raise
            raise RolloutGateError(
                "scheduled_target_update_failed",
                scope="morning_digest",
            ) from exc

    def terraform_pre_register(
        self,
        *,
        task: str,
        definition: dict[str, Any],
        mode: str = "candidate",
    ) -> None:
        """Gate Terraform task registration and make broad/targeted apply stage-aware."""

        expected_stages = {
            "connect_web": frozenset({"initialized", "mcp_stable_and_old_drained", "complete"}),
            "mcp": frozenset({"worker_verified", "complete"}),
            "morning_digest": frozenset({"mcp_stable_and_old_drained", "complete"}),
        }
        if task not in expected_stages or mode not in {"candidate", "cleanup"}:
            raise RolloutGateError("unknown_task")
        ledger = self._ledger()
        if ledger.stage not in expected_stages[task]:
            raise RolloutGateError("stage_order_violation", scope=task)
        if mode == "cleanup":
            cleanup = self._active_cleanup()
            if cleanup is None:
                raise RolloutGateError("cleanup_state_missing", scope=task)
            self._validate_prepared_cleanup_task(
                cleanup=cleanup,
                task=task,
                definition=definition,
                rollback=False,
            )
            self._bind_candidate_digest(
                task=task,
                digest=_task_artifact_digest(definition),
                cleanup=cleanup,
            )
        else:
            self.validate_candidate(task=task, definition=definition)
            self._validate_all_rollbacks()
            self._assert_cutover_open(task)
            candidate_digest = _task_artifact_digest(definition)
            rollback_definition = self._describe_task(
                _task_control(self.control, task).rollback_task_definition
            )
            if candidate_digest == _task_artifact_digest(rollback_definition):
                raise RolloutGateError("candidate_rollback_artifact_alias", scope=task)
            self._bind_candidate_digest(
                task=task,
                digest=candidate_digest,
                cleanup=None,
            )

    def _validate_rollback_task(
        self,
        *,
        task: str,
        task_definition: str,
        image: str,
    ) -> None:
        definition = self._describe_task(task_definition)
        self._assert_manifest_matches_durable()
        trusted_manifest = copy.deepcopy(self.manifest)
        trusted_manifest["now"] = self._now()
        result = validate_rendered_tasks(trusted_manifest, {task: definition})
        if not result["ok"]:
            raise RolloutGateError(str(result["code"]), scope=task)
        container = _one_container(definition)
        if container.get("image") != image or _canonical_registerable_task_definition(
            definition
        ).get("family") != _task_family(task_definition):
            raise RolloutGateError("rollback_image_drift", scope=task)
        rollback_provenance = {
            "mcp": self.control.mcp.rollback_provenance,
            "connect_web": self.control.connect_web.rollback_provenance,
            "morning_digest": self.control.morning_digest.rollback_provenance,
        }[task]
        if rollback_provenance != self._expected_provenance(task=task, image=image):
            raise RolloutGateError("rollback_provenance_binding_drift", scope=task)
        self._validate_runtime_metadata(
            task=task,
            definition=definition,
            provenance=rollback_provenance,
        )
        self._validate_legacy_worker_reference(task=task, definition=definition)

    def _validate_all_rollbacks(self) -> None:
        self._validate_rollback_task(
            task="mcp",
            task_definition=self.control.mcp.rollback_task_definition,
            image=self.control.mcp.rollback_image,
        )
        self._validate_rollback_task(
            task="connect_web",
            task_definition=self.control.connect_web.rollback_task_definition,
            image=self.control.connect_web.rollback_image,
        )
        self._validate_rollback_task(
            task="morning_digest",
            task_definition=self.control.morning_digest.rollback_task_definition,
            image=self.control.morning_digest.rollback_image,
        )

    def _validate_live_compatible_task(
        self,
        *,
        task: str,
        task_definition: str,
        definition: dict[str, Any],
    ) -> None:
        rollback_task = {
            "mcp": self.control.mcp.rollback_task_definition,
            "connect_web": self.control.connect_web.rollback_task_definition,
            "morning_digest": self.control.morning_digest.rollback_task_definition,
        }[task]
        if task_definition == rollback_task:
            rollback_image = {
                "mcp": self.control.mcp.rollback_image,
                "connect_web": self.control.connect_web.rollback_image,
                "morning_digest": self.control.morning_digest.rollback_image,
            }[task]
            self._validate_rollback_task(
                task=task,
                task_definition=task_definition,
                image=rollback_image,
            )
            return
        self.validate_candidate(task=task, definition=definition)
        ledger = self._read_item(f"LEDGER#{self.control.rotation_epoch}")
        if self._ddb_string(
            ledger, f"candidate_{task}_arn", optional=True
        ) != task_definition or self._ddb_string(
            ledger, f"candidate_{task}_digest", optional=True
        ) != _task_artifact_digest(definition):
            raise RolloutGateError("candidate_identity_drift", scope=task)

    def _assert_cutover_open(self, task: str) -> None:
        domains: tuple[str, ...]
        if task == "connect_web":
            domains = ("report_link",)
        elif task == "morning_digest":
            domains = ("mail_action",)
        elif task in {"mcp", "worker"}:
            domains = ("mail_action", "report_link")
        else:
            raise RolloutGateError("unknown_task")
        durable = self._assert_manifest_matches_durable()
        now = self._now()
        for domain in domains:
            t0 = durable[domain]["rotation_started_at"]
            if type(t0) is int and now >= t0 + _ISSUER_CUTOVER_S:
                raise RolloutGateError("issuer_cutover_deadline", scope=domain)

    def pre_update(
        self,
        *,
        task: str,
        task_definition: str,
        mode: str = "candidate",
    ) -> None:
        """Re-fetch and validate a registered candidate immediately before service update."""

        candidate_stages = {
            "connect_web": frozenset({"initialized", "mcp_stable_and_old_drained", "complete"}),
            "mcp": frozenset({"worker_verified", "mcp_stable_and_old_drained", "complete"}),
            "morning_digest": frozenset({"mcp_stable_and_old_drained", "complete"}),
        }
        rollback_stages = {
            "connect_web": frozenset(_LEDGER_STAGES),
            "mcp": frozenset({"worker_verified", "mcp_stable_and_old_drained", "complete"}),
            "morning_digest": frozenset({"mcp_stable_and_old_drained", "complete"}),
        }
        if task not in candidate_stages or mode not in {"candidate", "rollback", "cleanup"}:
            raise RolloutGateError("unknown_task")
        allowed_stages = candidate_stages if mode in {"candidate", "cleanup"} else rollback_stages
        ledger = self._ledger()
        if ledger.stage not in allowed_stages[task]:
            raise RolloutGateError("stage_order_violation", scope=task)
        if task_definition in self.control.forbidden_signing_task_definitions:
            raise RolloutGateError("forbidden_signing_revision", scope=task)
        cleanup = self._active_cleanup()
        if cleanup is not None and mode == "candidate":
            raise RolloutGateError("cleanup_mode_required", scope=task)
        if mode == "candidate":
            self._assert_cutover_open(task)
        if mode == "cleanup":
            if cleanup is None:
                raise RolloutGateError("cleanup_state_missing", scope=task)
            definition = self._describe_task(task_definition)
            self._validate_prepared_cleanup_task(
                cleanup=cleanup,
                task=task,
                definition=definition,
                rollback=False,
            )
            digest = _task_artifact_digest(definition)
            self._bind_candidate_arn(
                task=task,
                task_definition=task_definition,
                digest=digest,
                cleanup=cleanup,
            )
        elif mode == "rollback":
            self._bind_deployment_intent(
                task=task,
                cleanup=cleanup,
                kind="rollback",
            )
            approved = {
                "mcp": self.control.mcp.rollback_task_definition,
                "connect_web": self.control.connect_web.rollback_task_definition,
                "morning_digest": self.control.morning_digest.rollback_task_definition,
            }[task]
            if task_definition != approved:
                raise RolloutGateError("rollback_task_not_approved", scope=task)
            if cleanup is not None:
                definition = self._describe_task(task_definition)
                self._validate_prepared_cleanup_task(
                    cleanup=cleanup,
                    task=task,
                    definition=definition,
                    rollback=True,
                )
                candidate_digest = cleanup.candidate_digests[task]
                if _task_artifact_digest(definition) == candidate_digest:
                    raise RolloutGateError("candidate_rollback_artifact_alias", scope=task)
            else:
                image = {
                    "mcp": self.control.mcp.rollback_image,
                    "connect_web": self.control.connect_web.rollback_image,
                    "morning_digest": self.control.morning_digest.rollback_image,
                }[task]
                self._validate_rollback_task(
                    task=task,
                    task_definition=task_definition,
                    image=image,
                )
        else:
            definition = self._describe_task(task_definition)
            self.validate_candidate(task=task, definition=definition)
            digest = _task_artifact_digest(definition)
            rollback_definition = self._describe_task(
                _task_control(self.control, task).rollback_task_definition
            )
            if digest == _task_artifact_digest(rollback_definition):
                raise RolloutGateError("candidate_rollback_artifact_alias", scope=task)
            self._bind_candidate_arn(
                task=task,
                task_definition=task_definition,
                digest=digest,
                cleanup=None,
            )
        if cleanup is None:
            self._validate_all_rollbacks()
        if ledger.stage in {"mcp_stable_and_old_drained", "complete"} and task_definition.endswith(
            ":53"
        ):
            raise RolloutGateError("td53_after_issuer_cutover", scope="connect_web")

    def post_update(
        self,
        *,
        task: str,
        task_definition: str,
        mode: str = "candidate",
    ) -> None:
        """Validate the exact promoted artifact and prove all old service tasks drained."""

        self.pre_update(task=task, task_definition=task_definition, mode=mode)
        if task == "morning_digest":
            target = self._morning_target()
            live_arn = _mapping(target.get("EcsParameters")).get("TaskDefinitionArn")
            if live_arn != task_definition:
                raise RolloutGateError("scheduled_target_not_updated", scope=task)
            digest = _canonical_target_digest(target)
            expected_digest: str | None
            if mode == "rollback":
                expected_digest = self.control.morning_digest.rollback_target_digest
            else:
                cleanup = self._active_cleanup()
                record = (
                    self._cleanup_record_name(cleanup.domain)
                    if cleanup is not None
                    else f"LEDGER#{self.control.rotation_epoch}"
                )
                item = self._read_item(record)
                expected_digest = self._ddb_string(
                    item,
                    "candidate_morning_digest_target_digest",
                    optional=True,
                )
            if digest != expected_digest:
                raise RolloutGateError("scheduled_target_drift", scope=task)
            return
        workload = self.control.mcp if task == "mcp" else self.control.connect_web
        live_arn, _definition = self._service_task(workload)
        if live_arn != task_definition:
            raise RolloutGateError("service_not_stable", scope=task)
        self._service_stable_and_drained(workload, task_definition)

    def pre_connect_change(self, *, final: bool) -> None:
        """Read-only gate before connect-web build/upload work begins."""

        ledger = self._ledger()
        expected = "mcp_stable_and_old_drained" if final else "initialized"
        if ledger.stage != expected:
            raise RolloutGateError("stage_order_violation", scope="connect_web")
        self._assert_manifest_matches_durable()
        self._check_canary_anchor()
        self._validate_all_rollbacks()
        self._assert_cutover_open("connect_web")

    def _worker_attested(
        self,
        *,
        after: int | None = None,
        mode: str = "candidate",
        restart_nonce: str | None = None,
        release_root: str | None = None,
        release_tree_sha256: str | None = None,
        runtime_executable_sha256: str | None = None,
    ) -> int:
        worker = self.control.worker
        if mode not in {"candidate", "rollback"}:
            raise RolloutGateError("worker_mode_invalid", scope="worker")
        expected_provenance = (
            worker.provenance if mode == "candidate" else worker.rollback_provenance
        )
        cleanup = self._active_cleanup()
        configs = (
            self._cleanup_proposed_from_ledger(cleanup)
            if cleanup is not None and cleanup.stage == "authorized"
            else None
        )
        try:
            item = self._read_item(f"WORKER#{expected_provenance}")
        except RolloutGateError as exc:
            if exc.code != "durable_state_missing":
                raise
            raise RolloutGateError("worker_attestation_invalid", scope="worker") from exc
        try:
            provenance = item["provenance"]["S"]
            worker_id = item["worker_id"]["S"]
            epoch = item["rotation_epoch"]["S"]
            config_digest = item["config_digest"]["S"]
            loaded = frozenset(item["loaded_domains"]["SS"])
            checked_at = int(item["checked_at"]["N"])
            expires_at = int(item["expires_at"]["N"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutGateError("worker_attestation_invalid", scope="worker") from exc
        if (
            provenance != expected_provenance
            or worker_id != worker.instance_id
            or epoch != self.control.rotation_epoch
            or config_digest
            != self._worker_config_digest(
                provenance=expected_provenance,
                configs=configs,
                legacy_worker_generation=(
                    cleanup.legacy_worker_generation if cleanup is not None else None
                ),
            )
            or loaded != frozenset(_DOMAIN_MAX_TTL)
            or (after is not None and checked_at <= after)
            or self._now() - checked_at > 120
            or checked_at > self._now() + _MAX_CLOCK_SKEW_S
            or expires_at <= self._now()
        ):
            raise RolloutGateError("worker_attestation_invalid", scope="worker")
        if restart_nonce is not None:
            if (
                _RESTART_NONCE_RE.fullmatch(restart_nonce) is None
                or type(release_root) is not str
                or _RELEASE_ROOT_RE.fullmatch(release_root) is None
                or type(release_tree_sha256) is not str
                or _PROVENANCE_RE.fullmatch(release_tree_sha256) is None
                or release_root.rsplit("/", maxsplit=1)[-1] != release_tree_sha256
                or type(runtime_executable_sha256) is not str
                or _PROVENANCE_RE.fullmatch(runtime_executable_sha256) is None
            ):
                raise RolloutGateError("worker_attestation_invalid", scope="worker")
            expected_artifact = (
                worker.artifact_sha256 if mode == "candidate" else worker.rollback_artifact_sha256
            )
            service_checked: list[int] = []
            service_pids: set[int] = set()
            for service in ("bot", "connect"):
                try:
                    service_item = self._read_item(
                        f"WORKER_SERVICE#{expected_provenance}#{service}"
                    )
                except RolloutGateError as exc:
                    if exc.code != "durable_state_missing":
                        raise
                    raise RolloutGateError(
                        "worker_attestation_invalid",
                        scope="worker",
                    ) from exc
                service_checked_at = self._ddb_number(service_item, "checked_at")
                service_expires_at = self._ddb_number(service_item, "expires_at")
                main_pid = self._ddb_number(service_item, "main_pid")
                process_start = self._ddb_number(service_item, "process_start_ticks")
                process_started_at = self._ddb_number(service_item, "process_started_at")
                connect_port_invalid = service == "connect" and (
                    self._ddb_number(service_item, "active_port", optional=True) != 8788
                    or self._ddb_number(
                        service_item,
                        "port_owner_pid",
                        optional=True,
                    )
                    != main_pid
                    or self._ddb_string(
                        service_item,
                        "health_endpoint",
                        optional=True,
                    )
                    != "http://127.0.0.1:8788/healthz"
                )
                expected_health_kind = (
                    "slack_socket_auth_heartbeat" if service == "bot" else "connect_http_port_owner"
                )
                process_cwd = self._ddb_string(service_item, "process_cwd", optional=True)
                process_executable = self._ddb_string(
                    service_item,
                    "process_executable",
                    optional=True,
                )
                if (
                    service_checked_at is None
                    or service_expires_at is None
                    or main_pid is None
                    or process_start is None
                    or process_started_at is None
                    or main_pid <= 1
                    or process_start <= 0
                    or process_started_at < (after or 0)
                    or self._ddb_string(service_item, "service") != service
                    or self._ddb_string(service_item, "provenance") != expected_provenance
                    or self._ddb_string(service_item, "worker_id") != worker.instance_id
                    or self._ddb_string(service_item, "rotation_epoch")
                    != self.control.rotation_epoch
                    or self._ddb_string(service_item, "restart_nonce") != restart_nonce
                    or self._ddb_string(service_item, "artifact_sha256") != expected_artifact
                    or self._ddb_string(service_item, "config_digest") != config_digest
                    or self._ddb_string(service_item, "release_root", optional=True) != release_root
                    or self._ddb_string(
                        service_item,
                        "release_tree_sha256",
                        optional=True,
                    )
                    != release_tree_sha256
                    or self._ddb_string(
                        service_item,
                        "runtime_executable_sha256",
                        optional=True,
                    )
                    != runtime_executable_sha256
                    or process_cwd != f"{release_root}/app"
                    or type(process_executable) is not str
                    or not process_executable.startswith(f"{release_root}/app/.venv/bin/")
                    or self._ddb_string(service_item, "health_kind", optional=True)
                    != expected_health_kind
                    or not self._ddb_bool(service_item, "health_verified")
                    or (after is not None and service_checked_at <= after)
                    or self._now() - service_checked_at > 120
                    or service_checked_at > self._now() + _MAX_CLOCK_SKEW_S
                    or service_expires_at <= self._now()
                    or main_pid in service_pids
                    or connect_port_invalid
                ):
                    raise RolloutGateError("worker_attestation_invalid", scope="worker")
                service_pids.add(main_pid)
                service_checked.append(service_checked_at)
            checked_at = min(service_checked)
        return checked_at

    def _worker_config_digest(
        self,
        *,
        provenance: str | None = None,
        configs: dict[str, dict[str, object]] | None = None,
        legacy_worker_generation: str | None = None,
    ) -> str:
        durable = configs if configs is not None else self._durable_proposed()
        expected_provenance = provenance or self.control.worker.provenance
        legacy_worker = (
            legacy_worker_generation
            if configs is not None
            else self._durable_legacy_worker_generation()
        )
        expectations: list[HmacRuntimeExpectation] = []
        for domain, maximum_ttl in _DOMAIN_MAX_TTL.items():
            config = durable[domain]
            primary = config["primary_generation"]
            previous = config["previous_generation"]
            t0 = config["rotation_started_at"]
            if type(primary) is not str:
                raise RolloutGateError("durable_state_invalid", scope=domain)
            deadline = t0 + _ISSUER_CUTOVER_S + maximum_ttl if type(t0) is int else None
            expectations.append(
                HmacRuntimeExpectation(
                    domain=domain,
                    primary_generation=primary,
                    previous_generation=previous if type(previous) is str else None,
                    rotation_started_at=t0 if type(t0) is int else None,
                    deadline=deadline,
                    rotation_epoch=self.control.rotation_epoch,
                    provenance=expected_provenance,
                    legacy_worker_generation=(legacy_worker if domain == "mail_action" else None),
                    legacy_worker_deadline=(
                        deadline if domain == "mail_action" and legacy_worker is not None else None
                    ),
                )
            )
        digest = runtime_expectations_digest(tuple(expectations))
        if digest is None:
            raise RolloutGateError("durable_state_invalid", scope="worker")
        return digest

    def verify_worker_rollback_artifact(self, path: Path) -> None:
        """Verify a prebuilt worker rollback artifact without opening or executing it."""

        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RolloutGateError("worker_rollback_artifact_unreadable", scope="worker") from exc
        if digest != self.control.worker.rollback_artifact_sha256:
            raise RolloutGateError("worker_rollback_artifact_drift", scope="worker")
        if self.control.worker.rollback_provenance != self._expected_provenance(
            task="worker",
            artifact_sha256=digest,
        ):
            raise RolloutGateError("worker_rollback_provenance_drift", scope="worker")

    def verify_worker_artifact(self, path: Path) -> None:
        """Bind the staged worker archive to the provenance reviewed in live control."""

        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RolloutGateError("worker_artifact_unreadable", scope="worker") from exc
        if digest != self.control.worker.artifact_sha256:
            raise RolloutGateError("worker_artifact_drift", scope="worker")
        if self.control.worker.provenance != self._expected_provenance(
            task="worker",
            artifact_sha256=digest,
        ):
            raise RolloutGateError("worker_provenance_binding_drift", scope="worker")

    def _verify_prepared_worker_artifact(
        self,
        *,
        cleanup: CleanupLedger,
        path: Path,
        rollback: bool,
    ) -> None:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RolloutGateError("worker_artifact_unreadable", scope="worker") from exc
        expected = (
            cleanup.rollback_digests["worker"] if rollback else cleanup.candidate_digests["worker"]
        )
        if digest != expected:
            raise RolloutGateError(
                "worker_rollback_artifact_drift" if rollback else "worker_artifact_drift",
                scope="worker",
            )

    def _verify_prepared_worker_env(
        self,
        *,
        cleanup: CleanupLedger,
        path: Path,
        rollback: bool,
    ) -> None:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RolloutGateError("worker_env_drift", scope="worker") from exc
        expected = (
            cleanup.rollback_worker_env_digest if rollback else cleanup.candidate_worker_env_digest
        )
        if digest != expected:
            raise RolloutGateError("worker_env_drift", scope="worker")

    def _restart_record_name(self, mode: str) -> str:
        provenance = (
            self.control.worker.provenance
            if mode in {"candidate", "cleanup"}
            else self.control.worker.rollback_provenance
        )
        return f"RESTART#{self.control.rotation_epoch}#{provenance}"

    def pre_restart(
        self,
        *,
        rollback_artifact: Path,
        release_root: str,
        release_tree_sha256: str,
        runtime_executable_sha256: str,
        mode: str = "candidate",
    ) -> str:
        """Revalidate worker attestation and immutable metadata immediately before restart."""

        if (
            mode not in {"candidate", "rollback", "cleanup"}
            or _RELEASE_ROOT_RE.fullmatch(release_root) is None
            or _PROVENANCE_RE.fullmatch(release_tree_sha256) is None
            or release_root.rsplit("/", maxsplit=1)[-1] != release_tree_sha256
            or _PROVENANCE_RE.fullmatch(runtime_executable_sha256) is None
        ):
            raise RolloutGateError("worker_mode_invalid", scope="worker")
        if self._ledger().stage not in {
            "worker_verified",
            "mcp_stable_and_old_drained",
            "complete",
        }:
            raise RolloutGateError("stage_order_violation", scope="worker")
        attestation_mode = "candidate" if mode == "cleanup" else mode
        checked_at = self._worker_attested(mode=attestation_mode)
        cleanup = self._active_cleanup()
        if mode == "cleanup" and cleanup is None:
            raise RolloutGateError("cleanup_state_missing", scope="worker")
        if mode == "candidate" and cleanup is not None:
            raise RolloutGateError("cleanup_mode_required", scope="worker")
        if cleanup is not None:
            self._verify_prepared_worker_artifact(
                cleanup=cleanup,
                path=rollback_artifact,
                rollback=True,
            )
        else:
            self.verify_worker_rollback_artifact(rollback_artifact)
            self._validate_all_rollbacks()
        self._assert_deployment_intent(
            task="worker",
            cleanup=cleanup,
            kind="rollback" if mode == "rollback" else "candidate",
        )
        intent = self._deployment_intent()
        if mode == "candidate":
            self._assert_cutover_open("worker")
        now = self._now()
        record = self._restart_record_name(mode)
        existing = self._read_item_optional(record)
        revision = self._ddb_number(existing, "revision") if existing is not None else None
        if existing is not None and self._ddb_string(existing, "stage") not in {
            "complete",
            "reconciled",
        }:
            raise RolloutGateError("worker_restart_state_invalid", scope="worker")
        next_revision = 1 if revision is None else revision + 1
        restart_nonce = secrets.token_hex(32)
        configs = self._cleanup_proposed_from_ledger(cleanup) if cleanup is not None else None
        expected_provenance = (
            self.control.worker.provenance
            if attestation_mode == "candidate"
            else self.control.worker.rollback_provenance
        )
        artifact_sha256 = (
            self.control.worker.artifact_sha256
            if attestation_mode == "candidate"
            else self.control.worker.rollback_artifact_sha256
        )
        item = {
            "scope": {"S": self.control.scope},
            "record": {"S": record},
            "rotation_epoch": {"S": self.control.rotation_epoch},
            "provenance": {"S": expected_provenance},
            "artifact_sha256": {"S": artifact_sha256},
            "config_digest": {
                "S": self._worker_config_digest(
                    provenance=expected_provenance,
                    configs=configs,
                    legacy_worker_generation=(
                        cleanup.legacy_worker_generation if cleanup is not None else None
                    ),
                )
            },
            "release_root": {"S": release_root},
            "release_tree_sha256": {"S": release_tree_sha256},
            "runtime_executable_sha256": {"S": runtime_executable_sha256},
            "plan_sha256": {"S": intent.plan_sha256},
            "apply_attempt_id": {"S": intent.apply_attempt_id},
            "restart_nonce": {"S": restart_nonce},
            "stage": {"S": "requested"},
            "mode": {"S": mode},
            "revision": {"N": str(next_revision)},
            "after_checked_at": {"N": str(checked_at)},
            "requested_at": {"N": str(now)},
        }
        put: dict[str, Any] = {
            "TableName": self.control.state_table,
            "Item": item,
        }
        if revision is None:
            put.update(
                {
                    "ConditionExpression": "attribute_not_exists(#record)",
                    "ExpressionAttributeNames": {"#record": "record"},
                }
            )
        else:
            put.update(
                {
                    "ConditionExpression": (
                        "revision = :revision AND (#stage = :complete OR #stage = :reconciled)"
                    ),
                    "ExpressionAttributeNames": {"#stage": "stage"},
                    "ExpressionAttributeValues": {
                        ":revision": {"N": str(revision)},
                        ":complete": {"S": "complete"},
                        ":reconciled": {"S": "reconciled"},
                    },
                }
            )
        try:
            response = self.ddb.transact_write_items(TransactItems=[{"Put": put}])
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("worker_restart_cas_failed", scope="worker") from exc
        return restart_nonce

    def reconcile_restart(self, *, mode: str, outcome: str) -> None:
        """Audit an idempotent remote rollback after an interrupted/ambiguous SSM restart."""

        if mode not in {"candidate", "rollback", "cleanup"} or outcome != "rolled-back":
            raise RolloutGateError("worker_restart_reconciliation_invalid", scope="worker")
        if self.deployment_intent is None:
            raise RolloutGateError("deployment_intent_missing", scope="worker")
        record = self._restart_record_name(mode)
        item = self._read_item(record)
        revision = self._ddb_number(item, "revision")
        stage = self._ddb_string(item, "stage")
        if (
            revision is None
            or self._ddb_string(item, "mode") != mode
            or self._ddb_string(item, "rotation_epoch") != self.control.rotation_epoch
            or self._ddb_string(item, "plan_sha256") != self.deployment_intent.plan_sha256
            or self._ddb_string(item, "apply_attempt_id") != self.deployment_intent.apply_attempt_id
        ):
            raise RolloutGateError("worker_restart_state_invalid", scope="worker")
        if stage == "reconciled":
            if (
                self._ddb_string(item, "reconciliation_outcome", optional=True) == outcome
                and self._ddb_string(item, "reconciliation_plan_sha256", optional=True)
                == self.deployment_intent.plan_sha256
                and self._ddb_string(item, "reconciliation_apply_attempt_id", optional=True)
                == self.deployment_intent.apply_attempt_id
            ):
                return
            raise RolloutGateError("worker_restart_state_invalid", scope="worker")
        if stage not in {"requested", "complete"}:
            raise RolloutGateError("worker_restart_state_invalid", scope="worker")
        now = self._now()
        try:
            response = self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": record},
                            },
                            "UpdateExpression": (
                                "SET #stage = :reconciled,"
                                " reconciliation_outcome = :outcome,"
                                " reconciliation_plan_sha256 = :plan,"
                                " reconciliation_apply_attempt_id = :attempt,"
                                " reconciled_at = :now, revision = revision + :one"
                            ),
                            "ConditionExpression": (
                                "revision = :revision"
                                " AND (#stage = :requested OR #stage = :complete)"
                                " AND mode = :mode"
                            ),
                            "ExpressionAttributeNames": {"#stage": "stage"},
                            "ExpressionAttributeValues": {
                                ":reconciled": {"S": "reconciled"},
                                ":requested": {"S": "requested"},
                                ":complete": {"S": "complete"},
                                ":outcome": {"S": outcome},
                                ":plan": {"S": self.deployment_intent.plan_sha256},
                                ":attempt": {"S": self.deployment_intent.apply_attempt_id},
                                ":now": {"N": str(now)},
                                ":one": {"N": "1"},
                                ":revision": {"N": str(revision)},
                                ":mode": {"S": mode},
                            },
                        }
                    }
                ]
            )
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError(
                "worker_restart_reconciliation_failed",
                scope="worker",
            ) from exc

    def post_restart(self, *, mode: str = "candidate") -> None:
        """Require service-startup readiness to be newer than the durable restart request."""

        if mode not in {"candidate", "rollback", "cleanup"}:
            raise RolloutGateError("worker_mode_invalid", scope="worker")
        cleanup = self._active_cleanup()
        if mode == "cleanup" and cleanup is None:
            raise RolloutGateError("cleanup_state_missing", scope="worker")
        if mode == "candidate" and cleanup is not None:
            raise RolloutGateError("cleanup_mode_required", scope="worker")
        self._assert_deployment_intent(
            task="worker",
            cleanup=cleanup,
            kind="rollback" if mode == "rollback" else "candidate",
        )
        intent = self._deployment_intent()
        record = self._restart_record_name(mode)
        item = self._read_item(record)
        revision = self._ddb_number(item, "revision")
        after_checked_at = self._ddb_number(item, "after_checked_at")
        requested_at = self._ddb_number(item, "requested_at")
        restart_nonce = self._ddb_string(item, "restart_nonce", optional=True)
        release_root = self._ddb_string(item, "release_root", optional=True)
        release_tree_sha256 = self._ddb_string(
            item,
            "release_tree_sha256",
            optional=True,
        )
        runtime_executable_sha256 = self._ddb_string(
            item,
            "runtime_executable_sha256",
            optional=True,
        )
        expected_provenance = (
            self.control.worker.provenance
            if mode in {"candidate", "cleanup"}
            else self.control.worker.rollback_provenance
        )
        if (
            revision is None
            or after_checked_at is None
            or requested_at is None
            or restart_nonce is None
            or release_root is None
            or release_tree_sha256 is None
            or runtime_executable_sha256 is None
            or self._ddb_string(item, "stage") != "requested"
            or self._ddb_string(item, "mode") != mode
            or self._ddb_string(item, "rotation_epoch") != self.control.rotation_epoch
            or self._ddb_string(item, "provenance") != expected_provenance
            or self._ddb_string(item, "plan_sha256") != intent.plan_sha256
            or self._ddb_string(item, "apply_attempt_id") != intent.apply_attempt_id
        ):
            raise RolloutGateError("worker_restart_state_invalid", scope="worker")
        attestation_mode = "candidate" if mode == "cleanup" else mode
        checked_at = self._worker_attested(
            after=max(after_checked_at, requested_at),
            mode=attestation_mode,
            restart_nonce=restart_nonce,
            release_root=release_root,
            release_tree_sha256=release_tree_sha256,
            runtime_executable_sha256=runtime_executable_sha256,
        )
        try:
            response = self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.control.state_table,
                            "Key": {
                                "scope": {"S": self.control.scope},
                                "record": {"S": record},
                            },
                            "UpdateExpression": (
                                "SET #stage = :complete, completed_at = :checked,"
                                " revision = revision + :one"
                            ),
                            "ConditionExpression": ("#stage = :requested AND revision = :revision"),
                            "ExpressionAttributeNames": {"#stage": "stage"},
                            "ExpressionAttributeValues": {
                                ":complete": {"S": "complete"},
                                ":requested": {"S": "requested"},
                                ":revision": {"N": str(revision)},
                                ":checked": {"N": str(checked_at)},
                                ":one": {"N": "1"},
                            },
                        }
                    }
                ]
            )
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("worker_restart_cas_failed", scope="worker") from exc

    def _completed_restart_mode(self, *, after: int) -> str:
        completed: list[tuple[int, str, int, str, str, str, str]] = []
        intent = self._deployment_intent()
        for record_mode, attestation_mode in (
            ("candidate", "candidate"),
            ("cleanup", "candidate"),
            ("rollback", "rollback"),
        ):
            item = self._read_item_optional(self._restart_record_name(record_mode))
            if item is None:
                continue
            stage = self._ddb_string(item, "stage")
            completed_at = self._ddb_number(item, "completed_at", optional=True)
            requested_at = self._ddb_number(item, "requested_at", optional=True)
            after_checked_at = self._ddb_number(
                item,
                "after_checked_at",
                optional=True,
            )
            restart_nonce = self._ddb_string(item, "restart_nonce", optional=True)
            release_root = self._ddb_string(item, "release_root", optional=True)
            release_tree_sha256 = self._ddb_string(
                item,
                "release_tree_sha256",
                optional=True,
            )
            runtime_executable_sha256 = self._ddb_string(
                item,
                "runtime_executable_sha256",
                optional=True,
            )
            expected_provenance = (
                self.control.worker.provenance
                if attestation_mode == "candidate"
                else self.control.worker.rollback_provenance
            )
            if (
                stage == "complete"
                and self._ddb_string(item, "mode") == record_mode
                and self._ddb_string(item, "rotation_epoch") == self.control.rotation_epoch
                and self._ddb_string(item, "provenance") == expected_provenance
                and self._ddb_string(item, "plan_sha256") == intent.plan_sha256
                and self._ddb_string(item, "apply_attempt_id") == intent.apply_attempt_id
                and completed_at is not None
                and requested_at is not None
                and after_checked_at is not None
                and restart_nonce is not None
                and release_root is not None
                and release_tree_sha256 is not None
                and runtime_executable_sha256 is not None
                and completed_at > max(after, requested_at, after_checked_at)
            ):
                completed.append(
                    (
                        completed_at,
                        attestation_mode,
                        max(after, requested_at, after_checked_at),
                        restart_nonce,
                        release_root,
                        release_tree_sha256,
                        runtime_executable_sha256,
                    )
                )
        if not completed:
            raise RolloutGateError("worker_restart_state_invalid", scope="worker")
        (
            _completed_at,
            mode,
            attestation_after,
            restart_nonce,
            release_root,
            release_tree_sha256,
            runtime_executable_sha256,
        ) = max(
            completed,
            key=lambda value: value[0],
        )
        self._worker_attested(
            after=attestation_after,
            mode=mode,
            restart_nonce=restart_nonce,
            release_root=release_root,
            release_tree_sha256=release_tree_sha256,
            runtime_executable_sha256=runtime_executable_sha256,
        )
        return mode

    def pre_worker_upload(
        self,
        *,
        artifact: Path,
        rollback_artifact: Path,
        worker_env: Path,
        rollback_env: Path,
        mode: str = "candidate",
    ) -> None:
        """Gate worker artifact upload before the remote readiness attestation exists."""

        ledger = self._ledger()
        if mode not in {"candidate", "rollback", "cleanup"}:
            raise RolloutGateError("worker_mode_invalid", scope="worker")
        if ledger.stage not in {
            "connect_web_preloaded",
            "worker_verified",
            "mcp_stable_and_old_drained",
            "complete",
        }:
            raise RolloutGateError("stage_order_violation", scope="worker")
        cleanup = self._active_cleanup()
        if mode == "cleanup" and cleanup is None:
            raise RolloutGateError("cleanup_state_missing", scope="worker")
        if mode == "candidate" and cleanup is not None:
            raise RolloutGateError("cleanup_mode_required", scope="worker")
        self._bind_deployment_intent(
            task="worker",
            cleanup=cleanup,
            kind="rollback" if mode == "rollback" else "candidate",
        )
        if mode == "cleanup":
            assert cleanup is not None
            self._verify_prepared_worker_artifact(
                cleanup=cleanup,
                path=artifact,
                rollback=False,
            )
            self._verify_prepared_worker_env(
                cleanup=cleanup,
                path=worker_env,
                rollback=False,
            )
        elif mode == "candidate":
            self._assert_manifest_matches_durable()
            self.verify_worker_artifact(artifact)
        else:
            if cleanup is not None:
                self._verify_prepared_worker_artifact(
                    cleanup=cleanup,
                    path=artifact,
                    rollback=True,
                )
            else:
                self._assert_manifest_matches_durable()
                self.verify_worker_rollback_artifact(artifact)
        if cleanup is not None:
            self._verify_prepared_worker_artifact(
                cleanup=cleanup,
                path=rollback_artifact,
                rollback=True,
            )
            self._verify_prepared_worker_env(
                cleanup=cleanup,
                path=rollback_env,
                rollback=True,
            )
        else:
            self.verify_worker_rollback_artifact(rollback_artifact)
            self._validate_all_rollbacks()
            proposed = self._durable_proposed()
            legacy_database_generation = self.manifest.get("legacy_database_generation")
            legacy_worker_generation = self._durable_legacy_worker_generation()
            if type(legacy_database_generation) is not str:
                raise RolloutGateError("worker_env_drift", scope="worker")
            self._validate_cleanup_worker(
                env_path=worker_env,
                artifact_path=artifact,
                proposed=proposed,
                legacy_database_generation=legacy_database_generation,
                legacy_worker_generation=legacy_worker_generation,
                rollback=mode == "rollback",
            )
            self._validate_cleanup_worker(
                env_path=rollback_env,
                artifact_path=rollback_artifact,
                proposed=proposed,
                legacy_database_generation=legacy_database_generation,
                legacy_worker_generation=legacy_worker_generation,
                rollback=True,
            )
        if mode == "candidate":
            self._assert_cutover_open("worker")

    def _service_stable_and_drained(
        self,
        workload: WorkloadControl,
        expected_task_definition: str,
    ) -> None:
        response = self.ecs.describe_services(
            cluster=workload.cluster,
            services=[workload.service],
        )
        self._observe(response)
        services = response.get("services") if type(response) is dict else None
        failures = response.get("failures") if type(response) is dict else None
        if (
            type(services) is not list
            or len(services) != 1
            or type(failures) is not list
            or failures
        ):
            raise RolloutGateError("service_not_stable")
        service = _mapping(services[0])
        deployments = service.get("deployments")
        desired_count = service.get("desiredCount")
        running_count = service.get("runningCount")
        pending_count = service.get("pendingCount")
        deployment = (
            deployments[0]
            if type(deployments) is list and len(deployments) == 1 and type(deployments[0]) is dict
            else {}
        )
        stable = (
            service.get("status") == "ACTIVE"
            and service.get("taskDefinition") == expected_task_definition
            and type(desired_count) is int
            and type(running_count) is int
            and type(pending_count) is int
            and desired_count >= 1
            and running_count >= 0
            and pending_count >= 0
            and desired_count == running_count
            and pending_count == 0
            and deployment.get("rolloutState") == "COMPLETED"
            and deployment.get("taskDefinition") == expected_task_definition
            and type(deployment.get("desiredCount")) is int
            and type(deployment.get("runningCount")) is int
            and type(deployment.get("pendingCount")) is int
            and deployment.get("desiredCount") == desired_count
            and deployment.get("runningCount") == running_count
            and deployment.get("pendingCount") == pending_count
        )
        if not stable:
            raise RolloutGateError("service_not_stable")
        if (
            type(desired_count) is not int
            or type(running_count) is not int
            or type(pending_count) is not int
        ):
            raise RolloutGateError("service_not_stable")
        running, _stopped = self._task_inventory(
            cluster=workload.cluster,
            service_name=workload.service,
        )
        observed_running = sum(task.get("lastStatus") == "RUNNING" for task in running.values())
        observed_pending = len(running) - observed_running
        if (
            len(running) != running_count + pending_count
            or observed_running != running_count
            or observed_pending != pending_count
            or len(running) != desired_count
        ):
            raise RolloutGateError("task_inventory_count_drift")
        if any(
            task.get("taskDefinitionArn") != expected_task_definition
            or task.get("lastStatus") != "RUNNING"
            for task in running.values()
        ):
            raise RolloutGateError("old_tasks_not_drained")
        confirmation = self.ecs.describe_services(
            cluster=workload.cluster,
            services=[workload.service],
        )
        self._observe(confirmation)
        confirmed_services = confirmation.get("services") if type(confirmation) is dict else None
        confirmed_failures = confirmation.get("failures") if type(confirmation) is dict else None
        if (
            type(confirmed_services) is not list
            or len(confirmed_services) != 1
            or type(confirmed_failures) is not list
            or confirmed_failures
        ):
            raise RolloutGateError("task_inventory_count_drift")
        confirmed = _mapping(confirmed_services[0])
        if any(
            confirmed.get(name) != service.get(name)
            for name in ("status", "taskDefinition", "desiredCount", "runningCount", "pendingCount")
        ):
            raise RolloutGateError("task_inventory_count_drift")

    def _transition_ledger(
        self,
        *,
        expected_stage: str,
        next_stage: str,
        runtime_stage: str,
        mail_issuers: frozenset[str] = frozenset(),
        report_issuers: frozenset[str] = frozenset(),
    ) -> None:
        ledger = self._ledger()
        if ledger.stage != expected_stage:
            raise RolloutGateError("stage_order_violation")
        now = self._now()
        transaction: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.control.state_table,
                    "Key": {
                        "scope": {"S": self.control.scope},
                        "record": {"S": f"LEDGER#{self.control.rotation_epoch}"},
                    },
                    "UpdateExpression": (
                        "SET #stage = :next, revision = revision + :one, updated_at = :now"
                    ),
                    "ConditionExpression": "#stage = :expected AND revision = :revision",
                    "ExpressionAttributeNames": {"#stage": "stage"},
                    "ExpressionAttributeValues": {
                        ":next": {"S": next_stage},
                        ":expected": {"S": expected_stage},
                        ":revision": {"N": str(ledger.revision)},
                        ":one": {"N": "1"},
                        ":now": {"N": str(now)},
                    },
                }
            }
        ]
        issuers_by_domain = {
            "mail_action": mail_issuers,
            "report_link": report_issuers,
        }
        for domain, issuers in issuers_by_domain.items():
            values: dict[str, Any] = {
                ":epoch": {"S": self.control.rotation_epoch},
                ":stage": {"S": runtime_stage},
                ":one": {"N": "1"},
            }
            expression = "SET #stage = :stage, revision = revision + :one"
            if issuers:
                expression += ", issuer_provenances = :issuers"
                values[":issuers"] = {"SS": sorted(issuers)}
            transaction.append(
                {
                    "Update": {
                        "TableName": self.control.state_table,
                        "Key": {
                            "scope": {"S": self.control.scope},
                            "record": {"S": f"DOMAIN#{domain}"},
                        },
                        "UpdateExpression": expression,
                        "ConditionExpression": "rotation_epoch = :epoch",
                        "ExpressionAttributeNames": {"#stage": "stage"},
                        "ExpressionAttributeValues": values,
                    }
                }
            )
        try:
            response = self.ddb.transact_write_items(TransactItems=transaction)
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("stage_cas_failed") from exc

    def connect_web_preloaded(self) -> None:
        arns, definitions = self._live_tasks()
        task_definition = arns["connect_web"]
        definition = definitions["connect_web"]
        if task_definition in self.control.forbidden_signing_task_definitions:
            raise RolloutGateError("connect_verifier_not_preloaded")
        self.validate_candidate(task="connect_web", definition=definition)
        self._full_task_inventory(arns)
        self._assert_cutover_open("connect_web")
        self._transition_ledger(
            expected_stage="initialized",
            next_stage="connect_web_preloaded",
            runtime_stage="preload",
        )

    def worker_verified(self, *, rollback_artifact: Path) -> None:
        ledger = self._ledger()
        arns, _definitions = self._live_tasks()
        self._full_task_inventory(arns)
        self._worker_attested()
        self.verify_worker_rollback_artifact(rollback_artifact)
        if ledger.stage == "complete":
            return
        self._assert_cutover_open("worker")
        self._transition_ledger(
            expected_stage="connect_web_preloaded",
            next_stage="worker_verified",
            runtime_stage="preload",
        )

    def mcp_stable_and_old_drained(self) -> None:
        ledger = self._ledger()
        if ledger.stage != "worker_verified":
            raise RolloutGateError("stage_order_violation", scope="mcp")
        arns, definitions = self._live_tasks()
        task_definition = arns["mcp"]
        definition = definitions["mcp"]
        if task_definition in self.control.forbidden_signing_task_definitions:
            raise RolloutGateError("forbidden_signing_revision", scope="mcp")
        self.validate_candidate(task="mcp", definition=definition)
        # Aggregate preload readiness is insufficient. Require a completed one-use restart whose
        # bot/connect service attestations prove fresh MainPIDs and process starts.
        self._completed_restart_mode(after=ledger.updated_at)
        self._full_task_inventory(arns)
        self._assert_cutover_open("mcp")
        issuers = frozenset(
            {
                self.control.mcp.provenance,
                self.control.mcp.rollback_provenance,
                self.control.worker.provenance,
                self.control.worker.rollback_provenance,
            }
        )
        self._transition_ledger(
            expected_stage="worker_verified",
            next_stage="mcp_stable_and_old_drained",
            runtime_stage="issuing",
            mail_issuers=issuers,
            report_issuers=issuers,
        )

    def complete(self) -> None:
        mcp_arn, mcp = self._service_task(self.control.mcp)
        connect_arn, connect = self._service_task(self.control.connect_web)
        morning_arn, morning = self._scheduled_task()
        if (
            mcp_arn in self.control.forbidden_signing_task_definitions
            or connect_arn in self.control.forbidden_signing_task_definitions
            or connect_arn.endswith(":53")
            or morning_arn in self.control.forbidden_signing_task_definitions
        ):
            raise RolloutGateError("forbidden_signing_revision")
        self._validate_live_compatible_task(
            task="mcp",
            task_definition=mcp_arn,
            definition=mcp,
        )
        self._validate_live_compatible_task(
            task="connect_web",
            task_definition=connect_arn,
            definition=connect,
        )
        self._validate_live_compatible_task(
            task="morning_digest",
            task_definition=morning_arn,
            definition=morning,
        )
        self._validate_all_rollbacks()
        self._full_task_inventory(
            {
                "mcp": mcp_arn,
                "connect_web": connect_arn,
                "morning_digest": morning_arn,
            }
        )
        self._assert_cutover_open("mcp")
        shared = frozenset(
            {
                self.control.mcp.provenance,
                self.control.mcp.rollback_provenance,
                self.control.worker.provenance,
                self.control.worker.rollback_provenance,
            }
        )
        self._transition_ledger(
            expected_stage="mcp_stable_and_old_drained",
            next_stage="complete",
            runtime_stage="complete",
            mail_issuers=shared
            | {
                self.control.morning_digest.provenance,
                self.control.morning_digest.rollback_provenance,
            },
            report_issuers=shared,
        )

    def complete_cleanup(self, *, domain: str) -> None:
        """Finalize a prepared cleanup after every replacement and restart is proven."""

        if domain not in _DOMAIN_MAX_TTL:
            raise RolloutGateError("unknown_domain")
        if self._ledger().stage != "complete":
            raise RolloutGateError("stage_order_violation", scope=domain)
        cleanup = self._cleanup_ledger(domain)
        if cleanup.stage != "authorized":
            raise RolloutGateError("stage_order_violation", scope=domain)
        if any(cleanup.candidate_arns[task] is None for task in _TASK_DOMAINS) or len(
            {cleanup.candidate_arns[task] for task in _TASK_DOMAINS}
        ) != len(_TASK_DOMAINS):
            raise RolloutGateError("cleanup_replacement_not_complete", scope=domain)
        proposed = self._cleanup_proposed_from_ledger(cleanup)
        arns, definitions = self._live_tasks()
        for task, definition in definitions.items():
            task_control = _task_control(self.control, task)
            task_definition = arns[task]
            if (
                task_definition in self.control.forbidden_signing_task_definitions
                or task_definition == task_control.legacy_task_definition
                or (
                    task_definition != task_control.rollback_task_definition
                    and task_definition != cleanup.candidate_arns[task]
                )
            ):
                raise RolloutGateError("cleanup_replacement_not_complete", scope=task)
            self._validate_prepared_cleanup_task(
                cleanup=cleanup,
                task=task,
                definition=definition,
                rollback=task_definition == task_control.rollback_task_definition,
            )
        self._full_task_inventory(arns)
        self._completed_restart_mode(after=cleanup.authorized_at)

        domain_items = {
            item_domain: self._read_item(f"DOMAIN#{item_domain}") for item_domain in _DOMAIN_MAX_TTL
        }
        target = domain_items[domain]
        previous = self._ddb_string(target, "previous_generation", optional=True)
        t0 = self._ddb_number(target, "rotation_started_at", optional=True)
        deadline = self._ddb_number(target, "deadline", optional=True)
        legacy_worker = self._ddb_string(
            target,
            "legacy_worker_generation",
            optional=True,
        )
        if (
            previous is None
            or t0 is None
            or deadline is None
            or self._ddb_string(target, "cleanup_stage", optional=True) != "authorized"
            or proposed[domain]
            != {
                "primary_generation": self._ddb_string(target, "primary_generation"),
                "previous_generation": None,
                "rotation_started_at": None,
            }
        ):
            raise RolloutGateError("cleanup_state_invalid", scope=domain)
        now = self._now()
        source_revision = self._ddb_number(target, "revision")
        if source_revision is None:
            raise RolloutGateError("cleanup_state_invalid", scope=domain)
        history_item: dict[str, Any] = {
            "scope": {"S": self.control.scope},
            "record": {"S": f"RETIREMENT#{domain}#{self.control.rotation_epoch}"},
            "domain": {"S": domain},
            "rotation_epoch": {"S": self.control.rotation_epoch},
            "primary_generation": {"S": str(proposed[domain]["primary_generation"])},
            "previous_generation": {"S": previous},
            "rotation_started_at": {"N": str(t0)},
            "deadline": {"N": str(deadline)},
            "retired_at": {"N": str(now)},
            "source_revision": {"N": str(source_revision)},
        }
        if legacy_worker is not None:
            history_item["legacy_worker_generation"] = {"S": legacy_worker}
        transaction: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self.control.state_table,
                    "Item": history_item,
                    "ConditionExpression": "attribute_not_exists(#record)",
                    "ExpressionAttributeNames": {"#record": "record"},
                }
            }
        ]
        for item_domain, item in domain_items.items():
            revision = self._ddb_number(item, "revision")
            current_issuers = self._ddb_string_set(item, "issuer_provenances")
            expected_temporary = (
                cleanup.old_provenances[item_domain] | cleanup.new_provenances[item_domain]
            )
            if (
                revision is None
                or current_issuers != expected_temporary
                or self._ddb_string(item, "rotation_epoch") != self.control.rotation_epoch
            ):
                raise RolloutGateError("cleanup_state_invalid", scope=item_domain)
            values: dict[str, Any] = {
                ":revision": {"N": str(revision)},
                ":epoch": {"S": self.control.rotation_epoch},
                ":temporary": {"SS": sorted(expected_temporary)},
                ":new": {"SS": sorted(cleanup.new_provenances[item_domain])},
                ":one": {"N": "1"},
            }
            expression = "SET issuer_provenances = :new, revision = revision + :one"
            condition = (
                "revision = :revision AND rotation_epoch = :epoch"
                " AND issuer_provenances = :temporary"
            )
            names: dict[str, str] = {}
            if cleanup.old_provenances[item_domain]:
                expression += " ADD retired_provenances :retired_provenances"
                values[":retired_provenances"] = {
                    "SS": sorted(cleanup.old_provenances[item_domain])
                }
            if item_domain == domain:
                expression = (
                    "SET issuer_provenances = :new, previous_retired = :true,"
                    " revision = revision + :one"
                    " REMOVE previous_generation, rotation_started_at, deadline,"
                    " legacy_worker_generation, legacy_worker_deadline, cleanup_stage"
                    + (
                        " ADD retired_provenances :retired_provenances"
                        if cleanup.old_provenances[item_domain]
                        else ""
                    )
                )
                condition += (
                    " AND previous_generation = :previous"
                    " AND deadline = :deadline AND cleanup_stage = :authorized"
                    " AND #stage = :complete"
                )
                names["#stage"] = "stage"
                if self._ddb_number(item, "high_water") is None:
                    raise RolloutGateError("cleanup_state_invalid", scope=item_domain)
                values.update(
                    {
                        ":true": {"BOOL": True},
                        ":previous": {"S": previous},
                        ":deadline": {"N": str(deadline)},
                        ":authorized": {"S": "authorized"},
                        ":complete": {"S": "complete"},
                    }
                )
            update: dict[str, Any] = {
                "TableName": self.control.state_table,
                "Key": {
                    "scope": {"S": self.control.scope},
                    "record": {"S": f"DOMAIN#{item_domain}"},
                },
                "UpdateExpression": expression,
                "ConditionExpression": condition,
                "ExpressionAttributeValues": values,
            }
            if names:
                update["ExpressionAttributeNames"] = names
            transaction.append({"Update": update})
        transaction.append(
            {
                "Update": {
                    "TableName": self.control.state_table,
                    "Key": {
                        "scope": {"S": self.control.scope},
                        "record": {"S": self._cleanup_record_name(domain)},
                    },
                    "UpdateExpression": (
                        "SET #stage = :complete, completed_at = :now, revision = revision + :one"
                    ),
                    "ConditionExpression": ("#stage = :authorized AND revision = :revision"),
                    "ExpressionAttributeNames": {"#stage": "stage"},
                    "ExpressionAttributeValues": {
                        ":complete": {"S": "complete"},
                        ":authorized": {"S": "authorized"},
                        ":revision": {"N": str(cleanup.revision)},
                        ":now": {"N": str(now)},
                        ":one": {"N": "1"},
                    },
                }
            }
        )
        try:
            response = self.ddb.transact_write_items(TransactItems=transaction)
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("cleanup_complete_cas_failed", scope=domain) from exc

    def retire_previous(self, *, domain: str) -> None:
        """Reject the former one-shot cleanup path; cleanup is now an explicit staged CAS."""

        if domain not in _DOMAIN_MAX_TTL:
            raise RolloutGateError("unknown_domain")
        raise RolloutGateError("cleanup_staging_required", scope=domain)

    def inspect(self) -> None:
        """Read-only parity check for current task metadata, worker attestation, and anchors."""

        _arns, definitions = self._live_tasks()
        self._assert_manifest_matches_durable()
        ledger = self._ledger()
        if ledger.stage in {"worker_verified", "mcp_stable_and_old_drained", "complete"}:
            self._worker_attested()
        for task, definition in definitions.items():
            provenance = {
                "mcp": self.control.mcp.provenance,
                "connect_web": self.control.connect_web.provenance,
                "morning_digest": self.control.morning_digest.provenance,
            }[task]
            self._validate_runtime_metadata(
                task=task,
                definition=definition,
                provenance=provenance,
            )


class _Boto3Factory:
    def __init__(self) -> None:
        import boto3

        self._session = boto3.session.Session()

    def client(self, service_name: str, *, region_name: str) -> Any:
        return self._session.client(service_name, region_name=region_name)


def _load_json(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return _mapping(value)


def _result(error: RolloutGateError | None = None) -> dict[str, object]:
    if error is None:
        return {"code": "ok", "ok": True}
    result: dict[str, object] = {"code": error.code, "ok": False}
    if error.scope is not None:
        result["scope"] = error.scope
    return result


def main(argv: list[str] | None = None, *, clients: AwsClientFactory | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live secret-free HMAC rollout CAS gate.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--control", required=True)
    parser.add_argument(
        "--action",
        required=True,
        choices=(
            "initialize",
            "legacy-bindings-drained",
            "inspect",
            "pre-register",
            "pre-update",
            "pre-event-update",
            "post-update",
            "pre-connect-preload",
            "pre-connect-final",
            "pre-worker-upload",
            "pre-restart",
            "post-restart",
            "reconcile-restart",
            "connect-web-preloaded",
            "worker-verified",
            "mcp-stable-and-old-drained",
            "complete",
            "prepare-cleanup",
            "reconcile-cleanup",
            "complete-cleanup",
            "retire-previous",
        ),
    )
    parser.add_argument("--task", choices=tuple(_TASK_DOMAINS))
    parser.add_argument("--domain", choices=tuple(_DOMAIN_MAX_TTL))
    parser.add_argument(
        "--mode",
        choices=("candidate", "rollback", "cleanup"),
        default="candidate",
    )
    parser.add_argument("--task-definition-json")
    parser.add_argument("--saved-plan")
    parser.add_argument("--task-definition-arn")
    parser.add_argument("--event-target-json")
    parser.add_argument("--worker-rollback-artifact")
    parser.add_argument("--worker-artifact")
    parser.add_argument("--worker-env")
    parser.add_argument("--worker-rollback-env")
    parser.add_argument("--worker-provenance-receipt")
    parser.add_argument("--worker-provenance-signature")
    parser.add_argument("--worker-rollback-provenance-receipt")
    parser.add_argument("--worker-rollback-provenance-signature")
    parser.add_argument("--release-root")
    parser.add_argument("--release-tree-sha256")
    parser.add_argument("--runtime-executable-sha256")
    parser.add_argument("--restart-outcome", choices=("rolled-back",))
    parser.add_argument("--reconcile-decision", choices=("abort", "rebind"))
    parser.add_argument(
        "--refresh-manifest-now",
        action="store_true",
        help="Replace only manifest.now with the local clock; AWS response time remains authoritative.",
    )
    args = parser.parse_args(argv)
    success_details: dict[str, object] = {}
    try:
        control = load_control(_load_json(args.control))
        manifest = _load_json(args.manifest)
        if args.refresh_manifest_now:
            manifest["now"] = int(time.time())
        deployment_intent: DeploymentIntent | None = None
        plan_path = os.environ.get("TEAMAGENT_SAVED_PLAN_PATH")
        apply_attempt_id = os.environ.get("TEAMAGENT_APPLY_ATTEMPT_ID")
        if plan_path is not None or apply_attempt_id is not None:
            if plan_path is None or apply_attempt_id is None:
                raise RolloutGateError("deployment_intent_missing")
            from scripts.terraform_hmac_payload import saved_plan_sha256

            deployment_intent = DeploymentIntent(
                plan_sha256=saved_plan_sha256(Path(plan_path)),
                apply_attempt_id=apply_attempt_id,
            )
        gate = LiveRolloutGate(
            control=control,
            manifest=manifest,
            clients=clients or _Boto3Factory(),
            deployment_intent=deployment_intent,
        )
        if args.action == "initialize":
            gate.initialize()
        elif args.action == "legacy-bindings-drained":
            arns, definitions = gate._live_tasks()
            gate._legacy_bindings_drained(arns, definitions)
        elif args.action == "inspect":
            gate.inspect()
        elif args.action == "pre-register":
            if args.task is None or args.task_definition_json is None:
                raise RolloutGateError("missing_action_argument")
            gate.terraform_pre_register(
                task=args.task,
                definition=_load_json(args.task_definition_json),
                mode=args.mode,
            )
        elif args.action == "pre-update":
            if args.task is None or args.task_definition_arn is None:
                raise RolloutGateError("missing_action_argument")
            gate.pre_update(
                task=args.task,
                task_definition=args.task_definition_arn,
                mode=args.mode,
            )
        elif args.action == "pre-event-update":
            if args.task_definition_arn is None or args.event_target_json is None:
                raise RolloutGateError("missing_action_argument")
            gate.pre_event_update(
                task_definition=args.task_definition_arn,
                target=_load_json(args.event_target_json),
                mode=args.mode,
            )
        elif args.action == "post-update":
            if args.task is None or args.task_definition_arn is None:
                raise RolloutGateError("missing_action_argument")
            gate.post_update(
                task=args.task,
                task_definition=args.task_definition_arn,
                mode=args.mode,
            )
        elif args.action == "pre-connect-preload":
            gate.pre_connect_change(final=False)
        elif args.action == "pre-connect-final":
            gate.pre_connect_change(final=True)
        elif args.action == "pre-worker-upload":
            if (
                args.worker_artifact is None
                or args.worker_rollback_artifact is None
                or args.worker_env is None
                or args.worker_rollback_env is None
            ):
                raise RolloutGateError("missing_action_argument")
            gate.pre_worker_upload(
                artifact=Path(args.worker_artifact),
                rollback_artifact=Path(args.worker_rollback_artifact),
                worker_env=Path(args.worker_env),
                rollback_env=Path(args.worker_rollback_env),
                mode=args.mode,
            )
        elif args.action == "pre-restart":
            if (
                args.worker_rollback_artifact is None
                or args.release_root is None
                or args.release_tree_sha256 is None
                or args.runtime_executable_sha256 is None
            ):
                raise RolloutGateError("missing_action_argument")
            success_details["restart_nonce"] = gate.pre_restart(
                rollback_artifact=Path(args.worker_rollback_artifact),
                release_root=args.release_root,
                release_tree_sha256=args.release_tree_sha256,
                runtime_executable_sha256=args.runtime_executable_sha256,
                mode=args.mode,
            )
        elif args.action == "post-restart":
            gate.post_restart(mode=args.mode)
        elif args.action == "reconcile-restart":
            if args.restart_outcome is None:
                raise RolloutGateError("missing_action_argument")
            gate.reconcile_restart(mode=args.mode, outcome=args.restart_outcome)
        elif args.action == "connect-web-preloaded":
            gate.connect_web_preloaded()
        elif args.action == "worker-verified":
            if args.worker_rollback_artifact is None:
                raise RolloutGateError("missing_action_argument")
            gate.worker_verified(rollback_artifact=Path(args.worker_rollback_artifact))
        elif args.action == "mcp-stable-and-old-drained":
            gate.mcp_stable_and_old_drained()
        elif args.action == "complete":
            gate.complete()
        elif args.action == "prepare-cleanup":
            if (
                args.domain is None
                or args.saved_plan is None
                or args.worker_env is None
                or args.worker_rollback_env is None
                or args.worker_artifact is None
                or args.worker_rollback_artifact is None
                or args.worker_provenance_receipt is None
                or args.worker_provenance_signature is None
                or args.worker_rollback_provenance_receipt is None
                or args.worker_rollback_provenance_signature is None
            ):
                raise RolloutGateError("missing_action_argument")
            from scripts.terraform_hmac_payload import (
                candidates_from_plan,
                cleanup_worker_bindings_from_plan,
                deployment_intent_id_from_plan,
                hmac_release_bindings_from_plan,
                saved_plan_sha256,
                show_saved_plan,
            )

            saved_plan = Path(args.saved_plan)
            saved_plan_value = show_saved_plan(saved_plan)
            candidate_definitions = candidates_from_plan(saved_plan_value)
            worker_bindings = cleanup_worker_bindings_from_plan(
                saved_plan_value,
                domain=args.domain,
            )
            release_bindings = hmac_release_bindings_from_plan(saved_plan_value)
            provenance_key_arn = release_bindings.get("worker_provenance_key_arn")
            if type(provenance_key_arn) is not str or not provenance_key_arn:
                raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
            repository_root = Path(__file__).resolve().parents[1]
            bound_paths = {
                "atomic_switch": repository_root / "scripts" / "worker_atomic_release_switch.sh",
                "base_environment": repository_root / ".env.production",
                "base_env_renderer": repository_root / "scripts" / "render_ec2_base_env.py",
                "candidate_artifact": Path(args.worker_artifact),
                "rollback_artifact": Path(args.worker_rollback_artifact),
                "candidate_env": Path(args.worker_env),
                "candidate_receipt": Path(args.worker_provenance_receipt),
                "candidate_signature": Path(args.worker_provenance_signature),
                "deploy_overrides": repository_root / "infra" / "deploy" / "ec2.overrides.env",
                "deploy_script": repository_root / "scripts" / "deploy_to_ec2.sh",
                "provenance_verifier": repository_root
                / "scripts"
                / "verify_worker_bundle_provenance.py",
                "promotion_attester": repository_root / "scripts" / "worker_promotion_attest.sh",
                "release_measurer": repository_root / "scripts" / "measure_worker_release.py",
                "rollback_env": Path(args.worker_rollback_env),
                "rollback_receipt": Path(args.worker_rollback_provenance_receipt),
                "rollback_signature": Path(args.worker_rollback_provenance_signature),
                "reviewed_manifest": Path(args.manifest),
                "runtime_lock": repository_root / "requirements-worker.lock",
                "rollout_control": Path(args.control),
            }
            if any(
                worker_bindings[name] != saved_plan_sha256(path)
                for name, path in bound_paths.items()
            ):
                raise RolloutGateError("terraform_plan_worker_invalid", scope="worker")
            gate.prepare_cleanup(
                domain=args.domain,
                candidate_definitions=candidate_definitions,
                worker_env=Path(args.worker_env),
                worker_rollback_env=Path(args.worker_rollback_env),
                worker_artifact=Path(args.worker_artifact),
                worker_rollback_artifact=Path(args.worker_rollback_artifact),
                worker_provenance_receipt=Path(args.worker_provenance_receipt),
                worker_provenance_signature=Path(args.worker_provenance_signature),
                worker_rollback_provenance_receipt=Path(args.worker_rollback_provenance_receipt),
                worker_rollback_provenance_signature=Path(
                    args.worker_rollback_provenance_signature
                ),
                worker_provenance_key_arn=provenance_key_arn,
                worker_bindings=worker_bindings,
                prepared_intent_id=deployment_intent_id_from_plan(saved_plan_value),
                prepared_plan_sha256=saved_plan_sha256(saved_plan),
            )
        elif args.action == "reconcile-cleanup":
            if args.domain is None or args.reconcile_decision is None:
                raise RolloutGateError("missing_action_argument")
            if args.reconcile_decision == "abort":
                gate.reconcile_cleanup(domain=args.domain, decision="abort")
            else:
                if args.saved_plan is None:
                    raise RolloutGateError("missing_action_argument")
                from scripts.terraform_hmac_payload import (
                    candidates_from_plan,
                    cleanup_worker_bindings_from_plan,
                    deployment_intent_id_from_plan,
                    saved_plan_sha256,
                    show_saved_plan,
                    validate_saved_plan_runtime_mutations,
                )

                fresh_path = Path(args.saved_plan)
                fresh_plan = show_saved_plan(fresh_path)
                validate_saved_plan_runtime_mutations(fresh_plan)
                gate.reconcile_cleanup(
                    domain=args.domain,
                    decision="rebind",
                    fresh_plan_sha256=saved_plan_sha256(fresh_path),
                    fresh_intent_id=deployment_intent_id_from_plan(fresh_plan),
                    fresh_candidate_definitions=candidates_from_plan(
                        fresh_plan,
                        allow_noop=True,
                    ),
                    fresh_worker_bindings=cleanup_worker_bindings_from_plan(
                        fresh_plan,
                        domain=args.domain,
                        allow_noop=True,
                    ),
                )
        elif args.action == "complete-cleanup":
            if args.domain is None:
                raise RolloutGateError("missing_action_argument")
            gate.complete_cleanup(domain=args.domain)
        elif args.action == "retire-previous":
            if args.domain is None:
                raise RolloutGateError("missing_action_argument")
            gate.retire_previous(domain=args.domain)
        else:
            raise RolloutGateError("unknown_action")
        result = _result()
        result.update(success_details)
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        RolloutGateError,
    ) as exc:
        error = exc if isinstance(exc, RolloutGateError) else RolloutGateError("gate_unreadable")
        result = _result(error)
    except Exception:
        result = _result(RolloutGateError("gate_client_error"))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
