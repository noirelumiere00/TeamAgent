#!/usr/bin/env python3
"""Durable apply-level EventBridge saga with exact baseline restoration."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from scripts.hmac_rollout_gate import (  # noqa: E402
    RolloutGateError,
    _canonical_event_rule,
    _canonical_json_value,
    _trusted_epoch,
    load_control,
)
from scripts.terraform_hmac_payload import (  # noqa: E402
    hmac_release_bindings_from_plan,
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


class SagaError(RuntimeError):
    """The durable EventBridge apply transaction cannot be proven safe."""


class ClientFactory(Protocol):
    def client(self, service_name: str, *, region_name: str) -> Any:
        """Return one AWS client."""


class _BotoFactory:
    def client(self, service_name: str, *, region_name: str) -> Any:
        import boto3

        return boto3.client(service_name, region_name=region_name)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SagaError("control is unreadable") from exc
    if type(value) is not dict:
        raise SagaError("control is invalid")
    return value


def _show_plan(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["terraform", "show", "-json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise SagaError("saved plan is unreadable") from exc
    if type(value) is not dict:
        raise SagaError("saved plan is invalid")
    return value


def _plan_control(path: Path) -> tuple[dict[str, Any], Path]:
    plan = _show_plan(path)
    release = hmac_release_bindings_from_plan(plan)
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
    return _load_json(control_path), control_path


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


def _list_targets(events: Any, *, rule: str) -> tuple[list[dict[str, Any]], int]:
    targets: list[dict[str, Any]] = []
    token: str | None = None
    trusted_times: list[int] = []
    seen_tokens: set[str] = set()
    while True:
        arguments: dict[str, object] = {"Rule": rule, "Limit": 100}
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


def _baseline(events: Any, *, expected_rule: dict[str, object]) -> tuple[dict[str, Any], int]:
    rule_name = str(expected_rule["Name"])
    response = events.describe_rule(Name=rule_name)
    trusted_now = _trusted_epoch(response)
    if trusted_now is None:
        raise SagaError("EventBridge rule lacks trusted time")
    observed_rule = _canonical_event_rule(response)
    if observed_rule != expected_rule:
        raise SagaError("EventBridge rule differs from the reviewed full binding")
    targets, target_now = _list_targets(events, rule=rule_name)
    baseline = {"rule": observed_rule, "targets": targets}
    encoded = json.dumps(baseline, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > _MAX_BASELINE_BYTES:
        raise SagaError("EventBridge rollback baseline exceeds its durable bound")
    return baseline, max(trusted_now, target_now)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


class EventBridgeApplySaga:
    def __init__(
        self,
        *,
        control: dict[str, Any],
        plan_sha256: str,
        apply_attempt_id: str,
        clients: ClientFactory,
    ) -> None:
        import re

        if (
            re.fullmatch(r"[a-f0-9]{64}", plan_sha256) is None
            or re.fullmatch(_UUID_RE, apply_attempt_id) is None
        ):
            raise SagaError("saga identity is invalid")
        try:
            parsed = load_control(control)
        except RolloutGateError as exc:
            raise SagaError("rollout control is invalid") from exc
        self.control = parsed
        self.plan_sha256 = plan_sha256
        self.apply_attempt_id = apply_attempt_id
        self.events = clients.client("events", region_name=parsed.region)
        self.ddb = clients.client("dynamodb", region_name=parsed.region)
        # One stable active record makes an interrupted prior attempt discoverable. Terminal
        # records are archived under their attempt IDs before this slot is reused.
        self.record = f"EVENTBRIDGE_APPLY#{parsed.rotation_epoch}"

    def _key(self, record: str | None = None) -> dict[str, dict[str, str]]:
        return {
            "scope": {"S": self.control.scope},
            "record": {"S": record or self.record},
        }

    def _read(self, record: str | None = None) -> dict[str, Any] | None:
        response = self.ddb.get_item(
            TableName=self.control.state_table,
            Key=self._key(record),
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

    def _baseline_from_item(self, item: dict[str, Any]) -> dict[str, Any]:
        try:
            baseline = json.loads(self._string(item, "baseline_json"))
        except json.JSONDecodeError as exc:
            raise SagaError("durable saga baseline is invalid") from exc
        if (
            type(baseline) is not dict
            or _digest(baseline) != self._string(item, "baseline_sha256")
            or _canonical_event_rule(baseline.get("rule"))
            != self.control.morning_digest.expected_rule
        ):
            raise SagaError("durable saga baseline digest differs")
        return baseline

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
        try:
            self.ddb.update_item(
                TableName=self.control.state_table,
                Key=self._key(),
                UpdateExpression=(
                    "SET #stage = :desired, finished_at = :finished, revision = revision + :one"
                ),
                ConditionExpression=(
                    "#stage = :applying AND revision = :revision"
                    " AND plan_sha256 = :plan AND apply_attempt_id = :attempt"
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
                },
            )
        except Exception as exc:
            confirmed = self._read()
            if (
                confirmed is None
                or self._string(confirmed, "stage") != desired
                or self._string(confirmed, "plan_sha256") != plan_sha256
                or self._string(confirmed, "apply_attempt_id") != apply_attempt_id
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
        audit_record = f"EVENTBRIDGE_APPLY_AUDIT#{self.control.rotation_epoch}#{previous_attempt}"
        audit = copy.deepcopy(previous)
        audit["record"] = {"S": audit_record}
        audit["active_record"] = {"S": self.record}
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.control.state_table,
                            "Item": audit,
                            "ConditionExpression": "attribute_not_exists(#record)",
                            "ExpressionAttributeNames": {"#record": "record"},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.control.state_table,
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

    def begin(self) -> None:
        existing = self._read()
        if existing is not None:
            if self._string(existing, "rotation_epoch") != self.control.rotation_epoch:
                raise SagaError("durable saga rotation epoch differs")
            if (
                self._string(existing, "plan_sha256") == self.plan_sha256
                and self._string(existing, "apply_attempt_id") == self.apply_attempt_id
                and self._string(existing, "stage") in {"applying", "complete", "restored"}
            ):
                return
            if self._string(existing, "stage") == "applying":
                # A previous runner died after acquiring the deployment lock. Restore its exact
                # baseline before this newly locked attempt is allowed to capture a baseline.
                recovered_at = self._restore(self._baseline_from_item(existing))
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
        baseline, now = _baseline(
            self.events,
            expected_rule=self.control.morning_digest.expected_rule,
        )
        baseline_json = json.dumps(baseline, separators=(",", ":"), sort_keys=True)
        baseline_digest = _digest(baseline)
        item = {
            **self._key(),
            "stage": {"S": "applying"},
            "revision": {"N": "1"},
            "rotation_epoch": {"S": self.control.rotation_epoch},
            "plan_sha256": {"S": self.plan_sha256},
            "apply_attempt_id": {"S": self.apply_attempt_id},
            "baseline_sha256": {"S": baseline_digest},
            "baseline_json": {"S": baseline_json},
            "started_at": {"N": str(now)},
        }
        if existing is not None:
            item["revision"] = {"N": str(self._number(existing, "revision") + 1)}
            self._archive_and_replace(existing, item)
            return
        try:
            self.ddb.put_item(
                TableName=self.control.state_table,
                Item=item,
                ConditionExpression="attribute_not_exists(#record)",
                ExpressionAttributeNames={"#record": "record"},
            )
        except Exception as exc:
            confirmed = self._read()
            if (
                confirmed is None
                or self._string(confirmed, "baseline_sha256") != baseline_digest
                or self._string(confirmed, "plan_sha256") != self.plan_sha256
                or self._string(confirmed, "apply_attempt_id") != self.apply_attempt_id
                or self._string(confirmed, "stage") != "applying"
            ):
                raise SagaError("durable saga could not begin") from exc

    def _restore(self, baseline: dict[str, Any]) -> int:
        rule = _canonical_event_rule(baseline.get("rule"))
        targets = _canonical_targets(baseline.get("targets"))
        arguments: dict[str, str] = {
            "Name": str(rule["Name"]),
            "EventBusName": str(rule["EventBusName"]),
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
        current, current_now = _list_targets(self.events, rule=str(rule["Name"]))
        trusted_now = max(trusted_now, current_now)
        baseline_ids = {str(target["Id"]) for target in targets}
        remove_ids = sorted(
            str(target["Id"]) for target in current if str(target["Id"]) not in baseline_ids
        )
        for offset in range(0, len(remove_ids), 10):
            response = self.events.remove_targets(
                Rule=str(rule["Name"]),
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
                Rule=str(rule["Name"]),
                Targets=targets[offset : offset + 10],
            )
            observed_now = _trusted_epoch(response)
            if observed_now is None:
                raise SagaError("EventBridge target restoration lacks trusted time")
            trusted_now = max(trusted_now, observed_now)
            if response.get("FailedEntryCount") != 0 or response.get("FailedEntries") != []:
                raise SagaError("EventBridge target restoration was partial")
        restored, verified_now = _baseline(self.events, expected_rule=rule)
        if restored != {"rule": rule, "targets": targets}:
            raise SagaError("EventBridge exact baseline restoration could not be verified")
        return max(trusted_now, verified_now)

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
        baseline = self._baseline_from_item(item)
        if outcome == "failed":
            finished_at = self._restore(baseline)
        else:
            response = self.events.describe_rule(
                Name=self.control.morning_digest.rule,
            )
            finished_at = _trusted_epoch(response)
            if finished_at is None:
                raise SagaError("EventBridge rule verification lacks trusted time")
            if _canonical_event_rule(response) != self.control.morning_digest.expected_rule:
                raise SagaError("EventBridge rule changed outside its reviewed binding")
        self._transition(item, desired=desired, finished_at=finished_at)


def main(
    argv: Sequence[str] | None = None,
    *,
    clients: ClientFactory | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("begin", "finish"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--apply-attempt-id", required=True)
    parser.add_argument("--outcome", choices=("applied", "failed"))
    args = parser.parse_args(argv)
    try:
        if hashlib.sha256(args.plan.read_bytes()).hexdigest() != args.plan_sha256:
            raise SagaError("saved plan digest differs")
        control, _path = _plan_control(args.plan)
        saga = EventBridgeApplySaga(
            control=control,
            plan_sha256=args.plan_sha256,
            apply_attempt_id=args.apply_attempt_id,
            clients=clients or _BotoFactory(),
        )
        if args.action == "begin":
            if args.outcome is not None:
                raise SagaError("begin does not accept an outcome")
            saga.begin()
        else:
            if args.outcome is None:
                raise SagaError("finish requires an outcome")
            saga.finish(outcome=args.outcome)
    except Exception:
        print('{"code":"eventbridge_apply_saga_failed","ok":false}')
        return 2
    print('{"code":"ok","ok":true}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
