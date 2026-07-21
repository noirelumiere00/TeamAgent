#!/usr/bin/env python3
"""Durable apply-level EventBridge saga with exact baseline restoration."""

from __future__ import annotations

import argparse
import copy
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
    _canonical_event_rule,
    _canonical_json_value,
    _canonical_target_digest,
    _trusted_epoch,
    load_control,
)
from scripts.terraform_hmac_payload import (  # noqa: E402
    _canonical_planned_event_target,
    active_hmac_release_bindings_from_plan,
    hmac_release_bindings_from_plan,
    morning_target_mutates_from_plan,
    validate_saved_plan_runtime_mutations,
)

_UUID_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TARGET_FIELDS = frozenset(
    {
        "AppSyncParameters",
        "Arn",
        "BatchParameters",
        "DeadLetterConfig",
        "EcsParameters",
        "HttpParameters",
        "Id",
        "Input",
        "InputPath",
        "InputTransformer",
        "KinesisParameters",
        "RedshiftDataParameters",
        "RetryPolicy",
        "RoleArn",
        "RunCommandParameters",
        "SageMakerPipelineParameters",
        "SqsParameters",
    }
)
_MAX_BASELINE_BYTES = 300_000
_MAX_PLAN_BYTES = 4 * 1024 * 1024 * 1024
_LEDGER_TABLE = "teamagent-dev-image-deployment-intents"
_RECORD_TYPE = "teamagent.eventbridge-apply-saga-active"
_RECEIPT_KIND = "teamagent-eventbridge-apply-saga-receipt"
_SCHEMA_VERSION = 2
_ACTIVATION_MIGRATION_ID = "2026-07-enable-ingest-canary-v1"
_RULE_PLAN_FIELDS = frozenset(
    {
        "description",
        "event_bus_name",
        "event_pattern",
        "name",
        "role_arn",
        "schedule_expression",
        "state",
    }
)
_RESTORABLE_TERRAFORM_FIELDS = _RULE_PLAN_FIELDS | frozenset({"is_enabled"})
_RULE_CONFIG_FIELDS = (
    "Description",
    "EventBusName",
    "EventPattern",
    "Name",
    "RoleArn",
    "ScheduleExpression",
    "State",
)
_RULE_SPECS = {
    "canary": (
        "aws_cloudwatch_event_rule.canary_hourly[0]",
        "canary_hourly",
        "teamagent-dev-canary-hourly",
    ),
    "ingest": (
        "aws_cloudwatch_event_rule.ingest_weekly[0]",
        "ingest_weekly",
        "teamagent-dev-ingest-weekly",
    ),
    "morning": (
        "aws_cloudwatch_event_rule.morning_digest_weekday[0]",
        "morning_digest_weekday",
        "teamagent-dev-morning-digest-weekday",
    ),
}
_RULE_ADDRESS_TO_KEY = {spec[0]: key for key, spec in _RULE_SPECS.items()}
_MORNING_TARGET_ADDRESS = "aws_cloudwatch_event_target.morning_digest_run_task[0]"
_EVENT_RULE_ADDRESS_RE = re.compile(r"(?:^|\.)aws_cloudwatch_event_rule\.")
_ACTIVE_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "baseline_json",
        "baseline_sha256",
        "plan_sha256",
        "planned_json",
        "planned_sha256",
        "record_id",
        "record_type",
        "revision",
        "rotation_epoch",
        "schema_version",
        "stage",
        "started_at",
    }
)


class SagaError(RuntimeError):
    """The durable EventBridge apply transaction cannot be proven safe."""


class ClientFactory(Protocol):
    def client(self, service_name: str, *, region_name: str) -> Any:
        """Return one AWS client."""


class _BotoFactory:
    def client(self, service_name: str, *, region_name: str) -> Any:
        import boto3

        return boto3.client(service_name, region_name=region_name)


@dataclass(frozen=True)
class RulePlan:
    """Exact before/after binding for one reviewed EventBridge rule."""

    key: str
    address: str
    before: dict[str, object]
    after: dict[str, object]
    target_policy: str
    target_after: dict[str, Any] | None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SagaError("control is unreadable") from exc
    if type(value) is not dict:
        raise SagaError("control is invalid")
    return value


def _descriptor_digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise SagaError("saved plan is unreadable") from exc
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


def _terraform_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(
        {
            "CHECKPOINT_DISABLE": "1",
            "TF_CLI_CONFIG_FILE": "/dev/null",
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
        }
    )
    return environment


def _show_plan_descriptor(
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
        value = json.loads(completed.stdout)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        raise SagaError("saved plan is unreadable") from exc
    if type(value) is not dict:
        raise SagaError("saved plan is invalid")
    return value


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
        plan = _show_plan_descriptor(descriptor, terraform_bin=terraform_bin)
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


def _has_unknown(value: object) -> bool:
    if value in (None, False):
        return False
    if value is True:
        return True
    if type(value) is dict:
        return any(_has_unknown(item) for item in value.values())
    if type(value) is list:
        return any(_has_unknown(item) for item in value)
    return True


def _canonical_rule_configuration(value: object) -> dict[str, object]:
    try:
        rule = _canonical_event_rule(value)
    except RolloutGateError as exc:
        raise SagaError("EventBridge rule is invalid") from exc
    return {name: rule[name] for name in _RULE_CONFIG_FIELDS}


def _canonical_planned_rule(
    value: object,
    *,
    key: str,
) -> dict[str, object]:
    if type(value) is not dict or _RULE_PLAN_FIELDS - frozenset(value):
        raise SagaError("saved plan EventBridge rule is incomplete")
    spec = _RULE_SPECS[key]
    normalized = {
        "Arn": f"arn:aws:events:ap-northeast-1:718959508629:rule/{value.get('name')}",
        "CreatedBy": "718959508629",
        "Description": value.get("description"),
        "EventBusName": value.get("event_bus_name"),
        "EventPattern": value.get("event_pattern"),
        "ManagedBy": None,
        "Name": value.get("name"),
        "RoleArn": value.get("role_arn"),
        "ScheduleExpression": value.get("schedule_expression"),
        "State": value.get("state"),
    }
    configuration = _canonical_rule_configuration(normalized)
    if (
        configuration["Name"] != spec[2]
        or configuration["EventBusName"] != "default"
    ):
        raise SagaError("saved plan EventBridge rule identity differs")
    return configuration


def _rule_plan_from_change(
    item: Mapping[str, Any],
    *,
    key: str,
    target_mutates: bool,
    target_after: dict[str, Any] | None,
) -> RulePlan:
    address, terraform_name, _rule_name = _RULE_SPECS[key]
    if (
        item.get("address") != address
        or item.get("mode") != "managed"
        or item.get("type") != "aws_cloudwatch_event_rule"
        or item.get("name") != terraform_name
        or item.get("index") != 0
        or item.get("deposed") is not None
        or item.get("previous_address") is not None
    ):
        raise SagaError("saved plan EventBridge rule identity is not exact")
    change = item.get("change")
    if type(change) is not dict or change.get("importing") not in (None, {}):
        raise SagaError("saved plan EventBridge rule change is invalid")
    before = change.get("before")
    after = change.get("after")
    after_unknown = change.get("after_unknown", {})
    if type(before) is not dict or type(after) is not dict or type(after_unknown) is not dict:
        raise SagaError("saved plan EventBridge rule values are invalid")
    changed_fields = {
        name
        for name in frozenset(before) | frozenset(after)
        if before.get(name) != after.get(name)
    }
    if changed_fields - _RESTORABLE_TERRAFORM_FIELDS:
        raise SagaError("saved plan changes an EventBridge rule field the saga cannot restore")
    if before.get("name") != after.get("name") or before.get("event_bus_name") != after.get(
        "event_bus_name"
    ):
        raise SagaError("saved plan changes EventBridge rule identity")
    if any(after_unknown.get(name) not in (None, False) for name in _RULE_PLAN_FIELDS):
        raise SagaError("saved plan EventBridge rule has unknown final values")
    return RulePlan(
        key=key,
        address=address,
        before=_canonical_planned_rule(before, key=key),
        after=_canonical_planned_rule(after, key=key),
        target_policy=(
            "promoted" if key == "morning" and target_mutates else "unchanged"
        ),
        target_after=target_after if key == "morning" and target_mutates else None,
    )


def _planned_morning_target(
    plan: Mapping[str, Any],
    *,
    target_mutates: bool,
) -> dict[str, Any] | None:
    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise SagaError("saved plan resource changes are unavailable")
    matches = [
        item
        for item in changes
        if type(item) is dict and item.get("address") == _MORNING_TARGET_ADDRESS
    ]
    if len(matches) != 1:
        raise SagaError("saved plan does not bind the exact morning EventBridge target")
    item = matches[0]
    if (
        item.get("mode") != "managed"
        or item.get("type") != "aws_cloudwatch_event_target"
        or item.get("name") != "morning_digest_run_task"
        or item.get("index") != 0
        or item.get("deposed") is not None
        or item.get("previous_address") is not None
    ):
        raise SagaError("saved plan morning target identity is not exact")
    actions = _actions(item, label=_MORNING_TARGET_ADDRESS)
    if target_mutates != (actions not in {("no-op",), ("read",)}):
        raise SagaError("saved plan morning target mutation binding differs")
    if not target_mutates:
        return None
    change = item.get("change")
    after = change.get("after") if type(change) is dict else None
    after_unknown = change.get("after_unknown", {}) if type(change) is dict else None
    if type(after_unknown) is not dict or _has_unknown(after_unknown):
        raise SagaError("saved plan morning target has unknown final values")
    try:
        target = _canonical_planned_event_target(after)
        canonical = _canonical_targets([target])[0]
        _canonical_target_digest(canonical)
    except RolloutGateError as exc:
        raise SagaError("saved plan morning target is invalid") from exc
    return canonical


def _event_rule_plans(
    plan: Mapping[str, Any],
    *,
    target_mutates: bool,
) -> tuple[RulePlan, ...]:
    changes = plan.get("resource_changes")
    if type(changes) is not list:
        raise SagaError("saved plan resource changes are unavailable")
    matches: dict[str, Mapping[str, Any]] = {}
    mutating: set[str] = set()
    for raw_item in changes:
        if type(raw_item) is not dict:
            raise SagaError("saved plan resource change is invalid")
        address = raw_item.get("address")
        resource_type = raw_item.get("type")
        if type(address) is not str:
            raise SagaError("saved plan resource address is invalid")
        if (
            resource_type != "aws_cloudwatch_event_rule"
            and _EVENT_RULE_ADDRESS_RE.search(address) is None
        ):
            continue
        actions = _actions(raw_item, label=address)
        key = _RULE_ADDRESS_TO_KEY.get(address)
        if key is None:
            if actions not in {("no-op",), ("read",)}:
                raise SagaError("saved plan mutates an EventBridge rule outside the saga scope")
            continue
        if key in matches:
            raise SagaError("saved plan repeats an EventBridge rule")
        matches[key] = raw_item
        if actions not in {("no-op",), ("read",)}:
            if actions != ("update",):
                raise SagaError("saved plan EventBridge rule mutation is not restorable")
            mutating.add(key)
    if frozenset(matches) != frozenset(_RULE_SPECS):
        raise SagaError("saved plan does not bind all three EventBridge rules")
    target_after = _planned_morning_target(plan, target_mutates=target_mutates)
    bindings = tuple(
        _rule_plan_from_change(
            matches[key],
            key=key,
            target_mutates=target_mutates,
            target_after=target_after,
        )
        for key in sorted(_RULE_SPECS)
    )
    variables = plan.get("variables")
    live_variable = variables.get("runtime_guard_live") if type(variables) is dict else None
    live_value = live_variable.get("value") if type(live_variable) is dict else None
    migration_id = live_value.get("migration_id") if type(live_value) is dict else None
    if migration_id == _ACTIVATION_MIGRATION_ID:
        if mutating != set(_RULE_SPECS) or any(
            binding.before["State"] != "DISABLED"
            or binding.after["State"] != "ENABLED"
            or {
                name
                for name in _RULE_CONFIG_FIELDS
                if binding.before[name] != binding.after[name]
            }
            != {"State"}
            for binding in bindings
        ):
            raise SagaError("activation plan does not exactly enable all three EventBridge rules")
    return bindings


def _plan_control(
    plan: dict[str, Any],
) -> tuple[
    dict[str, Any],
    Path,
    dict[str, object] | None,
    bool,
    tuple[RulePlan, ...],
]:
    release = hmac_release_bindings_from_plan(plan)
    active_release = active_hmac_release_bindings_from_plan(plan)
    validate_saved_plan_runtime_mutations(plan)
    target_mutates = morning_target_mutates_from_plan(plan)
    if target_mutates and active_release is None:
        raise SagaError("saved plan mutates the morning target without HMAC promotion")
    variables = plan.get("variables")
    control_variable = (
        variables.get("hmac_rollout_control_path") if type(variables) is dict else None
    )
    control_raw = control_variable.get("value") if type(control_variable) is dict else None
    if type(control_raw) is not str or not control_raw:
        raise SagaError("saved plan does not bind a rollout control path")
    control_path = Path(control_raw).resolve(strict=True)
    try:
        digest = hashlib.sha256(control_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SagaError("rollout control is unreadable") from exc
    if digest != release.get("rollout_control_sha256"):
        raise SagaError("rollout control differs from the saved plan")
    return (
        _load_json(control_path),
        control_path,
        active_release,
        target_mutates,
        _event_rule_plans(plan, target_mutates=target_mutates),
    )


def _canonical_targets(value: object) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise SagaError("EventBridge targets are invalid")
    targets: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in value:
        if type(raw) is not dict or frozenset(raw) - _TARGET_FIELDS:
            raise SagaError("EventBridge target has unknown fields")
        target_id = raw.get("Id")
        arn = raw.get("Arn")
        if (
            type(target_id) is not str
            or not target_id
            or type(arn) is not str
            or not arn
            or target_id in ids
        ):
            raise SagaError("EventBridge target identity is invalid")
        ids.add(target_id)
        targets.append(copy.deepcopy(_canonical_json_value(raw)))
    return sorted(targets, key=lambda item: str(item["Id"]))


def _list_targets(
    events: Any,
    *,
    rule: str,
    event_bus: str,
) -> tuple[list[dict[str, Any]], int]:
    targets: list[dict[str, Any]] = []
    token: str | None = None
    trusted_times: list[int] = []
    seen_tokens: set[str] = set()
    while True:
        arguments: dict[str, object] = {
            "EventBusName": event_bus,
            "Rule": rule,
            "Limit": 100,
        }
        if token is not None:
            arguments["NextToken"] = token
        response = events.list_targets_by_rule(**arguments)
        trusted_now = _trusted_epoch(response)
        if trusted_now is None:
            raise SagaError("EventBridge target inventory lacks trusted time")
        trusted_times.append(trusted_now)
        page = response.get("Targets") if type(response) is dict else None
        next_token = response.get("NextToken") if type(response) is dict else None
        if type(page) is not list:
            raise SagaError("EventBridge target inventory is unavailable")
        targets.extend(page)
        if next_token is None:
            return _canonical_targets(targets), max(trusted_times)
        if type(next_token) is not str or not next_token or next_token in seen_tokens:
            raise SagaError("EventBridge target pagination is invalid")
        seen_tokens.add(next_token)
        token = next_token


def _observe_rule(
    events: Any,
    *,
    expected: dict[str, object],
) -> tuple[dict[str, Any], int]:
    rule_name = str(expected["Name"])
    event_bus = str(expected["EventBusName"])
    response = events.describe_rule(Name=rule_name, EventBusName=event_bus)
    trusted_now = _trusted_epoch(response)
    if trusted_now is None:
        raise SagaError("EventBridge rule lacks trusted time")
    try:
        observed_rule = _canonical_event_rule(response)
    except RolloutGateError as exc:
        raise SagaError("EventBridge rule is invalid") from exc
    if _canonical_rule_configuration(observed_rule) != expected:
        raise SagaError("EventBridge rule differs from the reviewed plan binding")
    targets, target_now = _list_targets(
        events,
        rule=rule_name,
        event_bus=event_bus,
    )
    return {"rule": observed_rule, "targets": targets}, max(trusted_now, target_now)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _decode_canonical_json(raw: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SagaError(f"{label} is invalid") from exc
    if (
        type(value) is not dict
        or json.dumps(value, separators=(",", ":"), sort_keys=True) != raw
    ):
        raise SagaError(f"{label} is not canonical")
    return value


def _planned_payload(rule_plans: Sequence[RulePlan]) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "rules": {
            binding.key: {
                "address": binding.address,
                "after": binding.after,
                "before": binding.before,
                "target_after": binding.target_after,
                "target_policy": binding.target_policy,
            }
            for binding in sorted(rule_plans, key=lambda item: item.key)
        },
    }


def _validate_planned_payload(value: object) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != {"schema_version", "rules"}:
        raise SagaError("durable saga planned binding is invalid")
    rules = value.get("rules")
    if value.get("schema_version") != _SCHEMA_VERSION or type(rules) is not dict:
        raise SagaError("durable saga planned binding is invalid")
    if frozenset(rules) != frozenset(_RULE_SPECS):
        raise SagaError("durable saga planned binding does not contain exactly three rules")
    normalized: dict[str, Any] = {}
    for key in sorted(_RULE_SPECS):
        raw = rules.get(key)
        if type(raw) is not dict or frozenset(raw) != {
            "address",
            "after",
            "before",
            "target_after",
            "target_policy",
        }:
            raise SagaError("durable saga planned rule is invalid")
        address = raw.get("address")
        target_policy = raw.get("target_policy")
        target_after = raw.get("target_after")
        if (
            address != _RULE_SPECS[key][0]
            or target_policy not in {"unchanged", "promoted"}
            or (target_policy == "promoted" and key != "morning")
            or (target_policy == "unchanged" and target_after is not None)
        ):
            raise SagaError("durable saga planned rule identity differs")
        if target_policy == "promoted":
            try:
                target_after = _canonical_targets([target_after])[0]
                _canonical_target_digest(target_after)
            except RolloutGateError as exc:
                raise SagaError("durable saga planned target is invalid") from exc
        before = raw.get("before")
        after = raw.get("after")
        if type(before) is not dict or type(after) is not dict:
            raise SagaError("durable saga planned rule binding is invalid")
        normalized_before = _canonical_planned_configuration(before, key=key)
        normalized_after = _canonical_planned_configuration(after, key=key)
        if (
            normalized_before["Name"] != normalized_after["Name"]
            or normalized_before["EventBusName"] != normalized_after["EventBusName"]
        ):
            raise SagaError("durable saga planned rule identity changes")
        normalized[key] = {
            "address": address,
            "after": normalized_after,
            "before": normalized_before,
            "target_after": target_after,
            "target_policy": target_policy,
        }
    return {"schema_version": _SCHEMA_VERSION, "rules": normalized}


def _canonical_planned_configuration(
    value: dict[str, Any],
    *,
    key: str,
) -> dict[str, object]:
    if frozenset(value) != frozenset(_RULE_CONFIG_FIELDS):
        raise SagaError("durable saga planned rule configuration is invalid")
    normalized = {
        "Arn": f"arn:aws:events:ap-northeast-1:718959508629:rule/{value.get('Name')}",
        "CreatedBy": "718959508629",
        "Description": value.get("Description"),
        "EventBusName": value.get("EventBusName"),
        "EventPattern": value.get("EventPattern"),
        "ManagedBy": None,
        "Name": value.get("Name"),
        "RoleArn": value.get("RoleArn"),
        "ScheduleExpression": value.get("ScheduleExpression"),
        "State": value.get("State"),
    }
    configuration = _canonical_rule_configuration(normalized)
    if (
        configuration["Name"] != _RULE_SPECS[key][2]
        or configuration["EventBusName"] != "default"
    ):
        raise SagaError("durable saga planned rule identity differs")
    return configuration


def _validate_baseline_payload(
    value: object,
    *,
    planned: dict[str, Any],
) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != {"schema_version", "rules"}:
        raise SagaError("durable saga baseline is invalid")
    rules = value.get("rules")
    planned_rules = planned["rules"]
    if (
        value.get("schema_version") != _SCHEMA_VERSION
        or type(rules) is not dict
        or frozenset(rules) != frozenset(_RULE_SPECS)
    ):
        raise SagaError("durable saga baseline does not contain exactly three rules")
    normalized: dict[str, Any] = {}
    for key in sorted(_RULE_SPECS):
        raw = rules.get(key)
        if type(raw) is not dict or frozenset(raw) != {"address", "rule", "targets"}:
            raise SagaError("durable saga baseline rule is invalid")
        try:
            rule = _canonical_event_rule(raw.get("rule"))
        except RolloutGateError as exc:
            raise SagaError("durable saga baseline rule is invalid") from exc
        targets = _canonical_targets(raw.get("targets"))
        if (
            raw.get("address") != _RULE_SPECS[key][0]
            or _canonical_rule_configuration(rule) != planned_rules[key]["before"]
        ):
            raise SagaError("durable saga baseline differs from its planned binding")
        normalized[key] = {
            "address": raw["address"],
            "rule": rule,
            "targets": targets,
        }
    baseline = {"schema_version": _SCHEMA_VERSION, "rules": normalized}
    if len(json.dumps(baseline, separators=(",", ":"), sort_keys=True).encode()) > (
        _MAX_BASELINE_BYTES
    ):
        raise SagaError("EventBridge rollback baseline exceeds its durable bound")
    return baseline


class EventBridgeApplySaga:
    def __init__(
        self,
        *,
        control: dict[str, Any],
        plan_sha256: str,
        apply_attempt_id: str,
        clients: ClientFactory,
        rule_plans: Sequence[RulePlan],
        gate_mode: str | None = None,
        cleanup_domain: str = "",
        target_mutates: bool = False,
    ) -> None:
        if (
            re.fullmatch(r"[a-f0-9]{64}", plan_sha256) is None
            or re.fullmatch(_UUID_RE, apply_attempt_id) is None
            or gate_mode not in {None, "candidate", "cleanup", "rollback"}
            or (gate_mode == "cleanup") != bool(cleanup_domain)
            or (cleanup_domain and cleanup_domain not in {"mail_action", "report_link"})
            or (target_mutates and gate_mode is None)
        ):
            raise SagaError("saga identity is invalid")
        try:
            parsed = load_control(control)
        except RolloutGateError as exc:
            raise SagaError("rollout control is invalid") from exc
        planned = _validate_planned_payload(_planned_payload(rule_plans))
        morning_binding = planned["rules"]["morning"]
        if (
            _canonical_rule_configuration(parsed.morning_digest.expected_rule)
            != morning_binding["before"]
            or (morning_binding["target_policy"] == "promoted") != target_mutates
        ):
            raise SagaError("morning rule plan differs from rollout control")
        self.control = parsed
        self.plan_sha256 = plan_sha256
        self.apply_attempt_id = apply_attempt_id
        self.gate_mode = gate_mode
        self.cleanup_domain = cleanup_domain
        self.target_mutates = target_mutates
        self.planned = planned
        self.planned_sha256 = _digest(planned)
        self.events = clients.client("events", region_name=parsed.region)
        self.ddb = clients.client("dynamodb", region_name=parsed.region)
        # One stable active record makes an interrupted prior attempt discoverable. Terminal
        # records are archived under their attempt IDs before this slot is reused.
        self.record_id = (
            "ecs-service-apply#eventbridge#active#"
            f"{parsed.rotation_epoch}"
        )

    def _key(self, record_id: str | None = None) -> dict[str, dict[str, str]]:
        return {"record_id": {"S": record_id or self.record_id}}

    def _read(self, record_id: str | None = None) -> dict[str, Any] | None:
        response = self.ddb.get_item(
            TableName=_LEDGER_TABLE,
            Key=self._key(record_id),
            ConsistentRead=True,
        )
        item = response.get("Item") if type(response) is dict else None
        return item if type(item) is dict else None

    @staticmethod
    def _string(item: dict[str, Any], name: str) -> str:
        raw = item.get(name)
        value = raw.get("S") if type(raw) is dict else None
        if type(value) is not str:
            raise SagaError("durable saga record is invalid")
        return value

    @staticmethod
    def _number(item: dict[str, Any], name: str) -> int:
        raw = item.get(name)
        value = raw.get("N") if type(raw) is dict else None
        if type(value) is not str or not value.isdecimal():
            raise SagaError("durable saga record is invalid")
        return int(value)

    def _record_payloads(
        self,
        item: dict[str, Any],
        *,
        require_current: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        stage = self._string(item, "stage")
        expected_fields = (
            _ACTIVE_FIELDS | {"finished_at"}
            if stage in {"complete", "restored"}
            else _ACTIVE_FIELDS
        )
        if (
            frozenset(item) != expected_fields
            or self._string(item, "record_id") != self.record_id
            or self._string(item, "record_type") != _RECORD_TYPE
            or self._number(item, "schema_version") != _SCHEMA_VERSION
            or self._string(item, "rotation_epoch") != self.control.rotation_epoch
            or stage not in {"applying", "complete", "restored"}
        ):
            raise SagaError("durable saga active record schema is invalid")
        planned_raw = self._string(item, "planned_json")
        baseline_raw = self._string(item, "baseline_json")
        planned = _validate_planned_payload(
            _decode_canonical_json(
                planned_raw,
                label="durable saga planned binding",
            )
        )
        baseline = _validate_baseline_payload(
            _decode_canonical_json(
                baseline_raw,
                label="durable saga baseline",
            ),
            planned=planned,
        )
        if (
            _digest(planned) != self._string(item, "planned_sha256")
            or _digest(baseline) != self._string(item, "baseline_sha256")
            or (require_current and planned != self.planned)
            or (require_current and self._string(item, "planned_sha256") != self.planned_sha256)
        ):
            raise SagaError("durable saga binding digest differs")
        return baseline, planned

    def _verify_applied(
        self,
        baseline: dict[str, Any],
        planned: dict[str, Any],
    ) -> int:
        verified_times: list[int] = []
        for key in sorted(_RULE_SPECS):
            binding = planned["rules"][key]
            observed, verified_at = _observe_rule(
                self.events,
                expected=binding["after"],
            )
            verified_times.append(verified_at)
            targets = observed["targets"]
            baseline_targets = baseline["rules"][key]["targets"]
            if binding["target_policy"] == "unchanged":
                if targets != baseline_targets:
                    raise SagaError("EventBridge target changed outside the reviewed plan")
                continue
            if (
                key != "morning"
                or len(targets) != 1
                or targets[0].get("Id") != self.control.morning_digest.target_id
                or targets[0].get("Arn") != self.control.morning_digest.cluster
            ):
                raise SagaError("EventBridge final target set is not exact")
            if targets[0] != binding["target_after"]:
                raise SagaError("EventBridge final target differs from the planned binding")
        return max(verified_times)

    def verify(self) -> dict[str, Any]:
        item = self._read()
        if item is None:
            raise SagaError("durable saga does not exist")
        if (
            self._string(item, "plan_sha256") != self.plan_sha256
            or self._string(item, "apply_attempt_id") != self.apply_attempt_id
            or self._string(item, "stage") != "applying"
        ):
            raise SagaError("durable saga is not the exact applying attempt")
        baseline, planned = self._record_payloads(item, require_current=True)
        verified_at = self._verify_applied(baseline, planned)
        receipt = {
            "apply_attempt_id": self.apply_attempt_id,
            "baseline_sha256": self._string(item, "baseline_sha256"),
            "kind": _RECEIPT_KIND,
            "ledger_item_sha256": _digest(item),
            "plan_sha256": self.plan_sha256,
            "planned_sha256": self._string(item, "planned_sha256"),
            "record_id": self.record_id,
            "rotation_epoch": self.control.rotation_epoch,
            "schema_version": _SCHEMA_VERSION,
            "stage": "verified_applied",
            "verified_at": verified_at,
        }
        receipt["receipt_sha256"] = _digest(receipt)
        return receipt

    def _transition(
        self,
        item: dict[str, Any],
        *,
        desired: str,
        finished_at: int,
    ) -> None:
        revision = self._number(item, "revision")
        plan_sha256 = self._string(item, "plan_sha256")
        apply_attempt_id = self._string(item, "apply_attempt_id")
        baseline_sha256 = self._string(item, "baseline_sha256")
        planned_sha256 = self._string(item, "planned_sha256")
        try:
            self.ddb.update_item(
                TableName=_LEDGER_TABLE,
                Key=self._key(),
                UpdateExpression=(
                    "SET #stage = :desired, finished_at = :finished, revision = revision + :one"
                ),
                ConditionExpression=(
                    "#stage = :applying AND revision = :revision"
                    " AND plan_sha256 = :plan AND apply_attempt_id = :attempt"
                    " AND baseline_sha256 = :baseline AND planned_sha256 = :planned"
                    " AND record_type = :record_type AND schema_version = :schema"
                ),
                ExpressionAttributeNames={"#stage": "stage"},
                ExpressionAttributeValues={
                    ":desired": {"S": desired},
                    ":applying": {"S": "applying"},
                    ":finished": {"N": str(finished_at)},
                    ":revision": {"N": str(revision)},
                    ":one": {"N": "1"},
                    ":plan": {"S": plan_sha256},
                    ":attempt": {"S": apply_attempt_id},
                    ":baseline": {"S": baseline_sha256},
                    ":planned": {"S": planned_sha256},
                    ":record_type": {"S": _RECORD_TYPE},
                    ":schema": {"N": str(_SCHEMA_VERSION)},
                },
            )
        except Exception as exc:
            confirmed = self._read()
            if (
                confirmed is None
                or self._string(confirmed, "stage") != desired
                or self._string(confirmed, "plan_sha256") != plan_sha256
                or self._string(confirmed, "apply_attempt_id") != apply_attempt_id
                or self._string(confirmed, "baseline_sha256") != baseline_sha256
                or self._string(confirmed, "planned_sha256") != planned_sha256
            ):
                raise SagaError("durable saga completion CAS failed") from exc

    def _archive_and_replace(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        previous_stage = self._string(previous, "stage")
        previous_revision = self._number(previous, "revision")
        previous_plan = self._string(previous, "plan_sha256")
        previous_attempt = self._string(previous, "apply_attempt_id")
        if previous_stage not in {"complete", "restored"}:
            raise SagaError("prior durable saga is not terminal")
        audit_record = (
            "ecs-service-apply#eventbridge#audit#"
            f"{self.control.rotation_epoch}#{previous_attempt}"
        )
        audit = copy.deepcopy(previous)
        audit["record_id"] = {"S": audit_record}
        audit["active_record_id"] = {"S": self.record_id}
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": _LEDGER_TABLE,
                            "Item": audit,
                            "ConditionExpression": "attribute_not_exists(record_id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": _LEDGER_TABLE,
                            "Item": current,
                            "ConditionExpression": (
                                "#stage = :terminal AND revision = :revision"
                                " AND plan_sha256 = :old_plan"
                                " AND apply_attempt_id = :old_attempt"
                            ),
                            "ExpressionAttributeNames": {"#stage": "stage"},
                            "ExpressionAttributeValues": {
                                ":terminal": {"S": previous_stage},
                                ":revision": {"N": str(previous_revision)},
                                ":old_plan": {"S": previous_plan},
                                ":old_attempt": {"S": previous_attempt},
                            },
                        }
                    },
                ]
            )
        except Exception as exc:
            confirmed = self._read()
            archived = self._read(audit_record)
            if (
                confirmed is None
                or archived is None
                or self._string(confirmed, "plan_sha256") != self.plan_sha256
                or self._string(confirmed, "apply_attempt_id") != self.apply_attempt_id
                or self._string(confirmed, "stage") != "applying"
                or self._string(archived, "plan_sha256") != previous_plan
                or self._string(archived, "apply_attempt_id") != previous_attempt
            ):
                raise SagaError("durable saga could not rotate its active record") from exc

    def _capture_baseline(self) -> tuple[dict[str, Any], int]:
        rules: dict[str, Any] = {}
        trusted_times: list[int] = []
        for key in sorted(_RULE_SPECS):
            binding = self.planned["rules"][key]
            observed, trusted_now = _observe_rule(
                self.events,
                expected=binding["before"],
            )
            trusted_times.append(trusted_now)
            rules[key] = {
                "address": binding["address"],
                "rule": observed["rule"],
                "targets": observed["targets"],
            }
        baseline = _validate_baseline_payload(
            {
                "schema_version": _SCHEMA_VERSION,
                "rules": rules,
            },
            planned=self.planned,
        )
        return baseline, max(trusted_times)

    def begin(self) -> None:
        existing = self._read()
        if existing is not None:
            if (
                self._string(existing, "plan_sha256") == self.plan_sha256
                and self._string(existing, "apply_attempt_id") == self.apply_attempt_id
                and self._string(existing, "stage") in {"applying", "complete", "restored"}
            ):
                self._record_payloads(existing, require_current=True)
                return
            if self._string(existing, "stage") == "applying":
                # A previous runner died after acquiring the deployment lock. Restore its exact
                # baseline before this newly locked attempt is allowed to capture a baseline.
                interrupted_baseline, _interrupted_planned = self._record_payloads(
                    existing,
                    require_current=False,
                )
                recovered_at = self._restore(interrupted_baseline)
                self._transition(
                    existing,
                    desired="restored",
                    finished_at=recovered_at,
                )
                existing = self._read()
                if existing is None or self._string(existing, "stage") != "restored":
                    raise SagaError("interrupted EventBridge saga was not reconciled")
            elif self._string(existing, "stage") not in {"complete", "restored"}:
                raise SagaError("durable saga identity already exists")
            else:
                self._record_payloads(existing, require_current=False)
        baseline, now = self._capture_baseline()
        baseline_json = json.dumps(baseline, separators=(",", ":"), sort_keys=True)
        baseline_digest = _digest(baseline)
        planned_json = json.dumps(self.planned, separators=(",", ":"), sort_keys=True)
        item = {
            **self._key(),
            "apply_attempt_id": {"S": self.apply_attempt_id},
            "baseline_json": {"S": baseline_json},
            "baseline_sha256": {"S": baseline_digest},
            "plan_sha256": {"S": self.plan_sha256},
            "planned_json": {"S": planned_json},
            "planned_sha256": {"S": self.planned_sha256},
            "record_type": {"S": _RECORD_TYPE},
            "revision": {"N": "1"},
            "rotation_epoch": {"S": self.control.rotation_epoch},
            "schema_version": {"N": str(_SCHEMA_VERSION)},
            "stage": {"S": "applying"},
            "started_at": {"N": str(now)},
        }
        if existing is not None:
            item["revision"] = {"N": str(self._number(existing, "revision") + 1)}
            self._archive_and_replace(existing, item)
            return
        try:
            self.ddb.put_item(
                TableName=_LEDGER_TABLE,
                Item=item,
                ConditionExpression="attribute_not_exists(record_id)",
            )
        except Exception as exc:
            confirmed = self._read()
            if (
                confirmed is None
                or self._string(confirmed, "baseline_sha256") != baseline_digest
                or self._string(confirmed, "planned_sha256") != self.planned_sha256
                or self._string(confirmed, "plan_sha256") != self.plan_sha256
                or self._string(confirmed, "apply_attempt_id") != self.apply_attempt_id
                or self._string(confirmed, "stage") != "applying"
            ):
                raise SagaError("durable saga could not begin") from exc
            self._record_payloads(confirmed, require_current=True)

    def _restore_rule(self, baseline: dict[str, Any]) -> int:
        try:
            rule = _canonical_event_rule(baseline.get("rule"))
        except RolloutGateError as exc:
            raise SagaError("EventBridge rollback rule is invalid") from exc
        targets = _canonical_targets(baseline.get("targets"))
        rule_name = str(rule["Name"])
        event_bus = str(rule["EventBusName"])
        arguments: dict[str, str] = {
            "Name": rule_name,
            "EventBusName": event_bus,
            "State": str(rule["State"]),
        }
        for name in (
            "Description",
            "EventPattern",
            "RoleArn",
            "ScheduleExpression",
        ):
            value = rule[name]
            if value is not None:
                arguments[name] = str(value)
        response = self.events.put_rule(**arguments)
        trusted_now = _trusted_epoch(response)
        if trusted_now is None:
            raise SagaError("EventBridge rule restoration lacks trusted time")
        current, current_now = _list_targets(
            self.events,
            rule=rule_name,
            event_bus=event_bus,
        )
        trusted_now = max(trusted_now, current_now)
        baseline_ids = {str(target["Id"]) for target in targets}
        remove_ids = sorted(
            str(target["Id"]) for target in current if str(target["Id"]) not in baseline_ids
        )
        for offset in range(0, len(remove_ids), 10):
            response = self.events.remove_targets(
                EventBusName=event_bus,
                Rule=rule_name,
                Ids=remove_ids[offset : offset + 10],
                Force=True,
            )
            observed_now = _trusted_epoch(response)
            if observed_now is None:
                raise SagaError("EventBridge target removal lacks trusted time")
            trusted_now = max(trusted_now, observed_now)
            if response.get("FailedEntryCount") != 0 or response.get("FailedEntries") != []:
                raise SagaError("EventBridge target removal was partial")
        for offset in range(0, len(targets), 10):
            response = self.events.put_targets(
                EventBusName=event_bus,
                Rule=rule_name,
                Targets=targets[offset : offset + 10],
            )
            observed_now = _trusted_epoch(response)
            if observed_now is None:
                raise SagaError("EventBridge target restoration lacks trusted time")
            trusted_now = max(trusted_now, observed_now)
            if response.get("FailedEntryCount") != 0 or response.get("FailedEntries") != []:
                raise SagaError("EventBridge target restoration was partial")
        restored, verified_now = _observe_rule(
            self.events,
            expected=_canonical_rule_configuration(rule),
        )
        if restored != {"rule": rule, "targets": targets}:
            raise SagaError("EventBridge exact baseline restoration could not be verified")
        return max(trusted_now, verified_now)

    def _restore(self, baseline: dict[str, Any]) -> int:
        restored_times = [
            self._restore_rule(baseline["rules"][key])
            for key in sorted(_RULE_SPECS)
        ]
        return max(restored_times)

    def finish(self, *, outcome: str) -> None:
        if outcome not in {"applied", "failed"}:
            raise SagaError("saga outcome is invalid")
        item = self._read()
        if item is None:
            raise SagaError("durable saga does not exist")
        if (
            self._string(item, "plan_sha256") != self.plan_sha256
            or self._string(item, "apply_attempt_id") != self.apply_attempt_id
            or self._string(item, "rotation_epoch") != self.control.rotation_epoch
        ):
            raise SagaError("durable saga identity differs")
        stage = self._string(item, "stage")
        desired = "complete" if outcome == "applied" else "restored"
        if stage == desired:
            return
        if stage != "applying":
            raise SagaError("durable saga stage is not reconcilable")
        baseline, planned = self._record_payloads(item, require_current=True)
        if outcome == "failed":
            finished_at = self._restore(baseline)
        else:
            finished_at = self._verify_applied(baseline, planned)
        self._transition(item, desired=desired, finished_at=finished_at)


def main(
    argv: Sequence[str] | None = None,
    *,
    clients: ClientFactory | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("begin", "finish", "verify"))
    parser.add_argument("--terraform-bin", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--apply-attempt-id", required=True)
    parser.add_argument("--outcome", choices=("applied", "failed"))
    args = parser.parse_args(argv)
    try:
        terraform_bin = _trusted_executable(args.terraform_bin, label="Terraform")
        plan = _load_saved_plan(
            args.plan,
            expected_sha256=args.plan_sha256,
            terraform_bin=terraform_bin,
        )
        control, _path, release, target_mutates, rule_plans = _plan_control(plan)
        saga = EventBridgeApplySaga(
            control=control,
            plan_sha256=args.plan_sha256,
            apply_attempt_id=args.apply_attempt_id,
            clients=clients or _BotoFactory(),
            rule_plans=rule_plans,
            gate_mode=str(release["gate_mode"]) if release is not None else None,
            cleanup_domain=str(release["cleanup_domain"]) if release is not None else "",
            target_mutates=target_mutates,
        )
        if args.action == "begin":
            if args.outcome is not None:
                raise SagaError("begin does not accept an outcome")
            saga.begin()
            result: dict[str, object] = {"code": "ok", "ok": True}
        elif args.action == "finish":
            if args.outcome is None:
                raise SagaError("finish requires an outcome")
            saga.finish(outcome=args.outcome)
            result = {"code": "ok", "ok": True}
        else:
            if args.outcome is not None:
                raise SagaError("verify does not accept an outcome")
            result = saga.verify()
    except Exception:
        print('{"code":"eventbridge_apply_saga_failed","ok":false}')
        return 2
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
