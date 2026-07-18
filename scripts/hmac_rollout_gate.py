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
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from scripts.preflight_hmac_rotation import validate_rendered_tasks
from teamagent.hmac_durable_state import (
    HmacRuntimeExpectation,
    runtime_expectations_digest,
)
from teamagent.hmac_keyring import (
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
_TASK_DOMAINS = {
    "mcp": frozenset({"mail_action", "report_link"}),
    "connect_web": frozenset({"report_link"}),
    "morning_digest": frozenset({"mail_action"}),
}
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
    rule: str
    target_id: str
    legacy_task_definition: str
    provenance: str
    rollback_provenance: str
    rollback_task_definition: str
    rollback_image: str


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
                "rule",
                "target_id",
                "legacy_task_definition",
                "provenance",
                "rollback_provenance",
                "rollback_task_definition",
                "rollback_image",
            }
        ),
    )
    return ScheduledControl(
        rule=_bounded_text(item["rule"]),
        target_id=_bounded_text(item["target_id"]),
        legacy_task_definition=_task_definition(item["legacy_task_definition"]),
        provenance=_provenance(item["provenance"]),
        rollback_provenance=_provenance(item["rollback_provenance"]),
        rollback_task_definition=_task_definition(item["rollback_task_definition"]),
        rollback_image=_image(item["rollback_image"]),
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
    rollback_tasks = {
        control.mcp.rollback_task_definition,
        control.connect_web.rollback_task_definition,
        control.morning_digest.rollback_task_definition,
    }
    legacy_tasks = {
        control.mcp.legacy_task_definition,
        control.connect_web.legacy_task_definition,
        control.morning_digest.legacy_task_definition,
    }
    if rollback_tasks & forbidden_set or rollback_tasks & legacy_tasks:
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


class LiveRolloutGate:
    """AWS-backed control-plane gate with injectable clients for offline tests."""

    def __init__(
        self,
        *,
        control: RolloutControl,
        manifest: dict[str, Any],
        clients: AwsClientFactory,
    ) -> None:
        self.control = control
        self.manifest = manifest
        self.ecs = clients.client("ecs", region_name=control.region)
        self.events = clients.client("events", region_name=control.region)
        self.secrets = clients.client("secretsmanager", region_name=control.region)
        self.ddb = clients.client("dynamodb", region_name=control.region)
        self._observed_times: list[int] = []

    def _observe(self, response: object) -> None:
        self._observed_times.append(_trusted_epoch(response))

    def _now(self) -> int:
        if not self._observed_times:
            raise RolloutGateError("trusted_clock_unavailable")
        if max(self._observed_times) - min(self._observed_times) > _MAX_AWS_CLOCK_SPREAD_S:
            raise RolloutGateError("trusted_clock_disagreement")
        now = max(self._observed_times)
        manifest_now = self.manifest.get("now")
        if type(manifest_now) is not int or abs(manifest_now - now) > _MAX_CLOCK_SKEW_S:
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
        response = self.ecs.describe_task_definition(taskDefinition=task_definition)
        self._observe(response)
        definition = response.get("taskDefinition") if type(response) is dict else None
        return _mapping(definition)

    def _service_task(self, workload: WorkloadControl) -> tuple[str, dict[str, Any]]:
        response = self.ecs.describe_services(
            cluster=workload.cluster,
            services=[workload.service],
        )
        self._observe(response)
        services = response.get("services") if type(response) is dict else None
        failures = response.get("failures") if type(response) is dict else None
        if type(services) is not list or len(services) != 1 or failures:
            raise RolloutGateError("live_service_unavailable")
        service = _mapping(services[0])
        task_definition = service.get("taskDefinition")
        if type(task_definition) is not str:
            raise RolloutGateError("live_service_unavailable")
        return task_definition, self._describe_task(task_definition)

    def _scheduled_task(self) -> tuple[str, dict[str, Any]]:
        scheduled = self.control.morning_digest
        response = self.events.list_targets_by_rule(Rule=scheduled.rule)
        self._observe(response)
        targets = response.get("Targets") if type(response) is dict else None
        if type(targets) is not list:
            raise RolloutGateError("scheduled_target_unavailable", scope="morning_digest")
        matches = [
            target for target in targets if _mapping(target).get("Id") == scheduled.target_id
        ]
        if len(matches) != 1:
            raise RolloutGateError("scheduled_target_unavailable", scope="morning_digest")
        ecs_parameters = _mapping(matches[0].get("EcsParameters"))
        task_definition = ecs_parameters.get("TaskDefinitionArn")
        if type(task_definition) is not str:
            raise RolloutGateError("scheduled_target_unavailable", scope="morning_digest")
        return task_definition, self._describe_task(task_definition)

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
        if type(raw) is not dict or type(raw.get("S")) is not str:
            raise RolloutGateError("durable_state_invalid")
        return raw["S"]

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
        self._service_stable_and_drained(self.control.mcp, arns["mcp"])
        self._service_stable_and_drained(self.control.connect_web, arns["connect_web"])

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
            self._service_stable_and_drained(self.control.mcp, arns["mcp"])
            self._service_stable_and_drained(
                self.control.connect_web,
                arns["connect_web"],
            )

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
                next_item = self._domain_item(
                    domain,
                    proposed[domain],
                    now,
                    revision=revision + 1,
                    high_water=prior_high_water,
                    retired_generations=retired_generations,
                    retired_provenances=retired_provenances,
                )
                transaction.append(
                    {
                        "Put": {
                            "TableName": self.control.state_table,
                            "Item": next_item,
                            "ConditionExpression": (
                                "revision = :revision AND rotation_epoch = :old_epoch"
                                " AND #stage = :complete"
                            ),
                            "ExpressionAttributeNames": {"#stage": "stage"},
                            "ExpressionAttributeValues": {
                                ":revision": {"N": str(revision)},
                                ":old_epoch": {"S": epoch},
                                ":complete": {"S": "complete"},
                            },
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
        if "mail_action" not in _TASK_DOMAINS[task]:
            return
        container = _one_container(definition)
        environment = _named(container.get("environment", []), "value")
        secrets = _named(container.get("secrets", []), "valueFrom")
        expected = self._durable_legacy_worker_generation()
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
    ) -> str:
        durable = self._durable_proposed()
        legacy_worker = self._durable_legacy_worker_generation() or ""
        if task == "mcp":
            if image is None or _IMAGE_DIGEST_RE.fullmatch(image) is None:
                raise RolloutGateError("image_not_digest", scope=task)
            values = {
                "image": image,
                "mail": str(durable["mail_action"]["primary_generation"]),
                "report": str(durable["report_link"]["primary_generation"]),
                "legacy_worker": legacy_worker,
                "rotation_epoch": self.control.rotation_epoch,
                "workload": "mcp",
            }
        elif task == "connect_web":
            if image is None or _IMAGE_DIGEST_RE.fullmatch(image) is None:
                raise RolloutGateError("image_not_digest", scope=task)
            values = {
                "image": image,
                "report": str(durable["report_link"]["primary_generation"]),
                "rotation_epoch": self.control.rotation_epoch,
                "workload": "connect_web",
            }
        elif task == "morning_digest":
            if image is None or _IMAGE_DIGEST_RE.fullmatch(image) is None:
                raise RolloutGateError("image_not_digest", scope=task)
            values = {
                "image": image,
                "mail": str(durable["mail_action"]["primary_generation"]),
                "legacy_worker": legacy_worker,
                "rotation_epoch": self.control.rotation_epoch,
                "workload": "morning_digest",
            }
        elif task == "worker":
            if artifact_sha256 is None or _PROVENANCE_RE.fullmatch(artifact_sha256) is None:
                raise RolloutGateError("worker_artifact_drift", scope=task)
            values = {
                "artifact": artifact_sha256,
                "mail": str(durable["mail_action"]["primary_generation"]),
                "report": str(durable["report_link"]["primary_generation"]),
                "legacy_worker": legacy_worker,
                "rotation_epoch": self.control.rotation_epoch,
                "workload": "worker",
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
        container = _one_container(definition)
        image = container.get("image")
        if type(image) is not str or provenance != self._expected_provenance(
            task=task,
            image=image,
        ):
            raise RolloutGateError("provenance_binding_drift", scope=task)
        self._validate_runtime_metadata(
            task=task,
            definition=definition,
            provenance=provenance,
        )
        self._validate_legacy_worker_reference(task=task, definition=definition)

    def terraform_pre_register(self, *, task: str, definition: dict[str, Any]) -> None:
        """Gate Terraform task registration and make broad/targeted apply stage-aware."""

        expected_stages = {
            "connect_web": frozenset({"initialized", "mcp_stable_and_old_drained", "complete"}),
            "mcp": frozenset({"worker_verified", "complete"}),
            "morning_digest": frozenset({"mcp_stable_and_old_drained", "complete"}),
        }
        if task not in expected_stages:
            raise RolloutGateError("unknown_task")
        ledger = self._ledger()
        if ledger.stage not in expected_stages[task]:
            raise RolloutGateError("stage_order_violation", scope=task)
        self.validate_candidate(task=task, definition=definition)
        self._validate_all_rollbacks()
        self._assert_cutover_open(task)

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
        if container.get("image") != image:
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
        if task not in candidate_stages or mode not in {"candidate", "rollback"}:
            raise RolloutGateError("unknown_task")
        allowed_stages = candidate_stages if mode == "candidate" else rollback_stages
        ledger = self._ledger()
        if ledger.stage not in allowed_stages[task]:
            raise RolloutGateError("stage_order_violation", scope=task)
        if task_definition in self.control.forbidden_signing_task_definitions:
            raise RolloutGateError("forbidden_signing_revision", scope=task)
        if mode == "rollback":
            approved = {
                "mcp": self.control.mcp.rollback_task_definition,
                "connect_web": self.control.connect_web.rollback_task_definition,
                "morning_digest": self.control.morning_digest.rollback_task_definition,
            }[task]
            if task_definition != approved:
                raise RolloutGateError("rollback_task_not_approved", scope=task)
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
        self._validate_all_rollbacks()
        if mode == "candidate":
            self._assert_cutover_open(task)
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
            live_arn, _definition = self._scheduled_task()
            if live_arn != task_definition:
                raise RolloutGateError("scheduled_target_not_updated", scope=task)
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
    ) -> None:
        worker = self.control.worker
        if mode not in {"candidate", "rollback"}:
            raise RolloutGateError("worker_mode_invalid", scope="worker")
        expected_provenance = (
            worker.provenance if mode == "candidate" else worker.rollback_provenance
        )
        item = self._read_item(f"WORKER#{expected_provenance}")
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
            or config_digest != self._worker_config_digest(provenance=expected_provenance)
            or loaded != frozenset(_DOMAIN_MAX_TTL)
            or (after is not None and checked_at <= after)
            or self._now() - checked_at > 120
            or checked_at > self._now() + _MAX_CLOCK_SKEW_S
            or expires_at <= self._now()
        ):
            raise RolloutGateError("worker_attestation_invalid", scope="worker")

    def _worker_config_digest(self, *, provenance: str | None = None) -> str:
        durable = self._durable_proposed()
        expected_provenance = provenance or self.control.worker.provenance
        legacy_worker = self._durable_legacy_worker_generation()
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

    def pre_restart(
        self,
        *,
        rollback_artifact: Path,
        mode: str = "candidate",
    ) -> None:
        """Revalidate worker attestation and immutable metadata immediately before restart."""

        if self._ledger().stage not in {
            "worker_verified",
            "mcp_stable_and_old_drained",
            "complete",
        }:
            raise RolloutGateError("stage_order_violation", scope="worker")
        self._worker_attested(mode=mode)
        self.verify_worker_rollback_artifact(rollback_artifact)
        self._validate_all_rollbacks()
        if mode == "candidate":
            self._assert_cutover_open("worker")

    def pre_worker_upload(
        self,
        *,
        artifact: Path,
        rollback_artifact: Path,
        mode: str = "candidate",
    ) -> None:
        """Gate worker artifact upload before the remote readiness attestation exists."""

        ledger = self._ledger()
        if mode not in {"candidate", "rollback"}:
            raise RolloutGateError("worker_mode_invalid", scope="worker")
        if ledger.stage not in {
            "connect_web_preloaded",
            "worker_verified",
            "mcp_stable_and_old_drained",
            "complete",
        }:
            raise RolloutGateError("stage_order_violation", scope="worker")
        self._assert_manifest_matches_durable()
        if mode == "candidate":
            self.verify_worker_artifact(artifact)
        else:
            self.verify_worker_rollback_artifact(artifact)
        self.verify_worker_rollback_artifact(rollback_artifact)
        self._validate_all_rollbacks()
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
        if type(services) is not list or len(services) != 1:
            raise RolloutGateError("service_not_stable")
        service = _mapping(services[0])
        deployments = service.get("deployments")
        stable = (
            service.get("taskDefinition") == expected_task_definition
            and service.get("desiredCount") == service.get("runningCount")
            and service.get("pendingCount") == 0
            and type(deployments) is list
            and len(deployments) == 1
            and _mapping(deployments[0]).get("rolloutState") == "COMPLETED"
        )
        if not stable:
            raise RolloutGateError("service_not_stable")
        listed = self.ecs.list_tasks(
            cluster=workload.cluster,
            serviceName=workload.service,
            desiredStatus="RUNNING",
        )
        self._observe(listed)
        task_arns = listed.get("taskArns") if type(listed) is dict else None
        if (
            type(task_arns) is not list
            or not task_arns
            or (type(listed) is dict and listed.get("nextToken") is not None)
        ):
            raise RolloutGateError("old_tasks_not_drained")
        described = self.ecs.describe_tasks(cluster=workload.cluster, tasks=task_arns)
        self._observe(described)
        tasks = described.get("tasks") if type(described) is dict else None
        if (
            type(tasks) is not list
            or len(tasks) != len(task_arns)
            or any(
                _mapping(task).get("taskDefinitionArn") != expected_task_definition
                or _mapping(task).get("lastStatus") != "RUNNING"
                for task in tasks
            )
        ):
            raise RolloutGateError("old_tasks_not_drained")

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
        task_definition, definition = self._service_task(self.control.connect_web)
        if task_definition in self.control.forbidden_signing_task_definitions:
            raise RolloutGateError("connect_verifier_not_preloaded")
        self.validate_candidate(task="connect_web", definition=definition)
        self._service_stable_and_drained(
            self.control.connect_web,
            task_definition,
        )
        self._assert_cutover_open("connect_web")
        self._transition_ledger(
            expected_stage="initialized",
            next_stage="connect_web_preloaded",
            runtime_stage="preload",
        )

    def worker_verified(self, *, rollback_artifact: Path) -> None:
        ledger = self._ledger()
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
        task_definition, definition = self._service_task(self.control.mcp)
        if task_definition in self.control.forbidden_signing_task_definitions:
            raise RolloutGateError("forbidden_signing_revision", scope="mcp")
        self.validate_candidate(task="mcp", definition=definition)
        # The readiness record used to enter worker_verified was written before restart. Require
        # a strictly newer record so a failed restart cannot authorize MCP issuer cutover.
        self._worker_attested(after=ledger.updated_at)
        self._service_stable_and_drained(self.control.mcp, task_definition)
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
        self._service_stable_and_drained(self.control.mcp, mcp_arn)
        self._service_stable_and_drained(self.control.connect_web, connect_arn)
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

    def retire_previous(self, *, domain: str) -> None:
        """CAS-remove one expired previous generation while preserving immutable history."""

        if domain not in _DOMAIN_MAX_TTL:
            raise RolloutGateError("unknown_domain")
        if self._ledger().stage != "complete":
            raise RolloutGateError("stage_order_violation", scope=domain)
        arns, definitions = self._live_tasks()
        self._service_stable_and_drained(self.control.mcp, arns["mcp"])
        self._service_stable_and_drained(
            self.control.connect_web,
            arns["connect_web"],
        )
        deployed = self._observed_deployed(definitions)
        proposed = self._assert_transition(deployed)
        candidate = proposed[domain]
        if (
            candidate.get("primary_generation") != deployed[domain]["primary_generation"]
            or candidate.get("previous_generation") is not None
            or candidate.get("rotation_started_at") is not None
        ):
            raise RolloutGateError("retirement_manifest_invalid", scope=domain)

        domain_items = {
            item_domain: self._read_item(f"DOMAIN#{item_domain}") for item_domain in _DOMAIN_MAX_TTL
        }
        for item_domain, domain_item in domain_items.items():
            if self._domain_config_from_item(domain_item) != deployed[item_domain]:
                raise RolloutGateError(
                    "manifest_durable_drift",
                    scope=item_domain,
                )
        item = domain_items[domain]
        epoch = self._ddb_string(item, "rotation_epoch")
        revision = self._ddb_number(item, "revision")
        high_water = self._ddb_number(item, "high_water")
        previous = self._ddb_string(item, "previous_generation", optional=True)
        t0 = self._ddb_number(item, "rotation_started_at", optional=True)
        deadline = self._ddb_number(item, "deadline", optional=True)
        legacy_worker = self._ddb_string(
            item,
            "legacy_worker_generation",
            optional=True,
        )
        legacy_worker_deadline = self._ddb_number(
            item,
            "legacy_worker_deadline",
            optional=True,
        )
        if (
            epoch != self.control.rotation_epoch
            or revision is None
            or high_water is None
            or previous is None
            or t0 is None
            or deadline is None
            or ((legacy_worker is None) != (legacy_worker_deadline is None))
            or (legacy_worker_deadline is not None and legacy_worker_deadline != deadline)
        ):
            raise RolloutGateError("retirement_state_invalid", scope=domain)
        now = self._now()
        if now < deadline:
            raise RolloutGateError("previous_window_active", scope=domain)
        effective_now = max(now, high_water)

        retired = [previous]
        if legacy_worker is not None:
            retired.append(legacy_worker)
        history_item: dict[str, Any] = {
            "scope": {"S": self.control.scope},
            "record": {"S": f"RETIREMENT#{domain}#{epoch}"},
            "domain": {"S": domain},
            "rotation_epoch": {"S": epoch},
            "primary_generation": {"S": str(deployed[domain]["primary_generation"])},
            "previous_generation": {"S": previous},
            "rotation_started_at": {"N": str(t0)},
            "deadline": {"N": str(deadline)},
            "retired_at": {"N": str(effective_now)},
            "source_revision": {"N": str(revision)},
        }
        if legacy_worker is not None:
            history_item["legacy_worker_generation"] = {"S": legacy_worker}
        transaction = [
            {
                "Put": {
                    "TableName": self.control.state_table,
                    "Item": history_item,
                    "ConditionExpression": "attribute_not_exists(#record)",
                    "ExpressionAttributeNames": {"#record": "record"},
                }
            },
            {
                "Update": {
                    "TableName": self.control.state_table,
                    "Key": {
                        "scope": {"S": self.control.scope},
                        "record": {"S": f"DOMAIN#{domain}"},
                    },
                    "UpdateExpression": (
                        "SET previous_retired = :true, high_water = :now,"
                        " revision = revision + :one"
                        " REMOVE previous_generation, rotation_started_at, deadline,"
                        " legacy_worker_generation, legacy_worker_deadline"
                        " ADD retired_generations :retired"
                    ),
                    "ConditionExpression": (
                        "revision = :revision AND rotation_epoch = :epoch"
                        " AND high_water = :high_water AND previous_generation = :previous"
                        " AND deadline = :deadline AND #stage = :complete"
                    ),
                    "ExpressionAttributeNames": {"#stage": "stage"},
                    "ExpressionAttributeValues": {
                        ":true": {"BOOL": True},
                        ":now": {"N": str(effective_now)},
                        ":one": {"N": "1"},
                        ":revision": {"N": str(revision)},
                        ":epoch": {"S": epoch},
                        ":high_water": {"N": str(high_water)},
                        ":previous": {"S": previous},
                        ":deadline": {"N": str(deadline)},
                        ":complete": {"S": "complete"},
                        ":retired": {"SS": retired},
                    },
                }
            },
        ]
        try:
            response = self.ddb.transact_write_items(TransactItems=transaction)
            self._observe(response)
        except Exception as exc:
            raise RolloutGateError("retirement_cas_failed", scope=domain) from exc

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
            "post-update",
            "pre-connect-preload",
            "pre-connect-final",
            "pre-worker-upload",
            "pre-restart",
            "connect-web-preloaded",
            "worker-verified",
            "mcp-stable-and-old-drained",
            "complete",
            "retire-previous",
        ),
    )
    parser.add_argument("--task", choices=tuple(_TASK_DOMAINS))
    parser.add_argument("--domain", choices=tuple(_DOMAIN_MAX_TTL))
    parser.add_argument("--mode", choices=("candidate", "rollback"), default="candidate")
    parser.add_argument("--task-definition-json")
    parser.add_argument("--task-definition-arn")
    parser.add_argument("--worker-rollback-artifact")
    parser.add_argument("--worker-artifact")
    parser.add_argument(
        "--refresh-manifest-now",
        action="store_true",
        help="Replace only manifest.now with the local clock; AWS response time remains authoritative.",
    )
    args = parser.parse_args(argv)
    try:
        control = load_control(_load_json(args.control))
        manifest = _load_json(args.manifest)
        if args.refresh_manifest_now:
            manifest["now"] = int(time.time())
        gate = LiveRolloutGate(
            control=control,
            manifest=manifest,
            clients=clients or _Boto3Factory(),
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
            )
        elif args.action == "pre-update":
            if args.task is None or args.task_definition_arn is None:
                raise RolloutGateError("missing_action_argument")
            gate.pre_update(
                task=args.task,
                task_definition=args.task_definition_arn,
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
            if args.worker_artifact is None or args.worker_rollback_artifact is None:
                raise RolloutGateError("missing_action_argument")
            gate.pre_worker_upload(
                artifact=Path(args.worker_artifact),
                rollback_artifact=Path(args.worker_rollback_artifact),
                mode=args.mode,
            )
        elif args.action == "pre-restart":
            if args.worker_rollback_artifact is None:
                raise RolloutGateError("missing_action_argument")
            gate.pre_restart(
                rollback_artifact=Path(args.worker_rollback_artifact),
                mode=args.mode,
            )
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
        elif args.action == "retire-previous":
            if args.domain is None:
                raise RolloutGateError("missing_action_argument")
            gate.retire_previous(domain=args.domain)
        else:
            raise RolloutGateError("unknown_action")
        result = _result()
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
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
