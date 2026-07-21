from __future__ import annotations

import copy
import importlib.util
import sys
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "terraform" / "eventbridge_apply_saga.py"
NOW = 2_000_000_000
ATTEMPT = "12345678-1234-4123-8123-123456789abc"
SECOND_ATTEMPT = "87654321-4321-4123-8123-123456789abc"
PLAN_SHA256 = "a" * 64


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "eventbridge_apply_saga_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SAGA = _load_module()


def _response(**values: object) -> dict[str, object]:
    return {
        **values,
        "ResponseMetadata": {"HTTPHeaders": {"date": formatdate(NOW, usegmt=True)}},
    }


def _rule(name: str = "teamagent-dev-morning-digest-weekday") -> dict[str, object]:
    return {
        "Arn": f"arn:aws:events:ap-northeast-1:123456789012:rule/{name}",
        "CreatedBy": "123456789012",
        "Description": "reviewed schedule",
        "EventBusName": "default",
        "EventPattern": None,
        "ManagedBy": None,
        "Name": name,
        "RoleArn": None,
        "ScheduleExpression": "cron(0 22 * * ? *)",
        "State": "DISABLED",
    }


def _target(target_id: str, revision: int) -> dict[str, object]:
    return {
        "Id": target_id,
        "Arn": "arn:aws:ecs:ap-northeast-1:123456789012:cluster/teamagent-dev",
        "RoleArn": "arn:aws:iam::123456789012:role/events-morning",
        "Input": "{}",
        "EcsParameters": {
            "TaskDefinitionArn": (
                "arn:aws:ecs:ap-northeast-1:123456789012:"
                f"task-definition/teamagent-dev-morning-digest:{revision}"
            ),
            "TaskCount": 1,
            "LaunchType": "FARGATE",
            "PlatformVersion": "LATEST",
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": ["subnet-b", "subnet-a"],
                    "SecurityGroups": ["sg-morning"],
                    "AssignPublicIp": "ENABLED",
                }
            },
        },
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": 3600,
            "MaximumRetryAttempts": 1,
        },
    }


class _Events:
    def __init__(self) -> None:
        self.rules = {key: _rule(spec[2]) for key, spec in SAGA._RULE_SPECS.items()}
        self.targets_by_rule = {
            "canary": [],
            "ingest": [],
            "morning": [_target("morning", 44), _target("audit", 43)],
        }
        self.fail_put_targets_once = False

    def _key(self, name: object) -> str:
        return next(key for key, rule in self.rules.items() if rule["Name"] == name)

    @property
    def rule(self) -> dict[str, object]:
        return self.rules["morning"]

    @property
    def targets(self) -> list[dict[str, object]]:
        return self.targets_by_rule["morning"]

    @targets.setter
    def targets(self, value: list[dict[str, object]]) -> None:
        self.targets_by_rule["morning"] = value

    def describe_rule(self, **kwargs: object) -> dict[str, object]:
        return _response(**copy.deepcopy(self.rules[self._key(kwargs["Name"])]))

    def list_targets_by_rule(self, **kwargs: object) -> dict[str, object]:
        return _response(Targets=copy.deepcopy(self.targets_by_rule[self._key(kwargs["Rule"])]))

    def put_rule(self, **kwargs: object) -> dict[str, object]:
        rule = self.rules[self._key(kwargs["Name"])]
        for name in (
            "Description",
            "EventPattern",
            "RoleArn",
            "ScheduleExpression",
        ):
            rule[name] = kwargs.get(name)
        rule["State"] = kwargs["State"]
        rule["Name"] = kwargs["Name"]
        rule["EventBusName"] = kwargs["EventBusName"]
        return _response(RuleArn=rule["Arn"])

    def remove_targets(self, **kwargs: object) -> dict[str, object]:
        removed = set(kwargs["Ids"])  # type: ignore[arg-type]
        key = self._key(kwargs["Rule"])
        self.targets_by_rule[key] = [
            target for target in self.targets_by_rule[key] if str(target["Id"]) not in removed
        ]
        return _response(FailedEntryCount=0, FailedEntries=[])

    def put_targets(self, **kwargs: object) -> dict[str, object]:
        if self.fail_put_targets_once:
            self.fail_put_targets_once = False
            return _response(
                FailedEntryCount=1,
                FailedEntries=[{"ErrorCode": "InternalException"}],
            )
        key = self._key(kwargs["Rule"])
        for replacement in copy.deepcopy(kwargs["Targets"]):  # type: ignore[union-attr]
            self.targets_by_rule[key] = [
                target for target in self.targets_by_rule[key] if target["Id"] != replacement["Id"]
            ]
            self.targets_by_rule[key].append(replacement)
        return _response(FailedEntryCount=0, FailedEntries=[])


class _Ddb:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    @property
    def item(self) -> dict[str, Any] | None:
        active = [
            item
            for record, item in self.items.items()
            if record.startswith("ecs-service-apply#eventbridge#active#")
        ]
        assert len(active) <= 1
        return active[0] if active else None

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        record = str(key["record_id"]["S"])
        item = self.items.get(record)
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, **kwargs: object) -> dict[str, object]:
        item = copy.deepcopy(kwargs["Item"])
        assert isinstance(item, dict)
        record = str(item["record_id"]["S"])
        assert record not in self.items
        self.items[record] = item
        return {}

    def update_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        record = str(key["record_id"]["S"])
        item = self.items[record]
        values = kwargs["ExpressionAttributeValues"]
        item["stage"] = copy.deepcopy(values[":desired"])  # type: ignore[index]
        item["finished_at"] = copy.deepcopy(values[":finished"])  # type: ignore[index]
        item["revision"] = {"N": str(int(item["revision"]["N"]) + 1)}
        return {}

    def transact_write_items(self, **kwargs: object) -> dict[str, object]:
        transaction = kwargs["TransactItems"]
        assert isinstance(transaction, list)
        assert len(transaction) == 2
        audit = copy.deepcopy(transaction[0]["Put"]["Item"])
        current = copy.deepcopy(transaction[1]["Put"]["Item"])
        audit_record = str(audit["record_id"]["S"])
        current_record = str(current["record_id"]["S"])
        assert audit_record not in self.items
        assert current_record in self.items
        self.items[audit_record] = audit
        self.items[current_record] = current
        return {}


class _Clients:
    def __init__(self) -> None:
        self.events = _Events()
        self.ddb = _Ddb()

    def client(self, service_name: str, *, region_name: str) -> object:
        assert region_name == "ap-northeast-1"
        return {"events": self.events, "dynamodb": self.ddb}[service_name]


def _rule_plans(*, target_mutates: bool) -> tuple[Any, ...]:
    target_after = _target("morning", 45) if target_mutates else None
    return tuple(
        SAGA.RulePlan(
            key=key,
            address=spec[0],
            before=SAGA._canonical_rule_configuration(_rule(spec[2])),
            after=SAGA._canonical_rule_configuration(_rule(spec[2])),
            target_policy="promoted" if key == "morning" and target_mutates else "unchanged",
            target_after=target_after if key == "morning" and target_mutates else None,
        )
        for key, spec in sorted(SAGA._RULE_SPECS.items())
    )


def _saga(
    monkeypatch: pytest.MonkeyPatch,
    clients: _Clients,
    *,
    attempt: str = ATTEMPT,
    plan_sha256: str = PLAN_SHA256,
    target_mutates: bool = False,
    gate_mode: str | None = None,
) -> Any:
    if target_mutates and gate_mode is None:
        gate_mode = "candidate"
    parsed = SimpleNamespace(
        region="ap-northeast-1",
        scope="teamagent/dev",
        state_table="teamagent-dev-hmac-state",
        rotation_epoch="hmac-2026-07",
        morning_digest=SimpleNamespace(
            expected_rule=SAGA._canonical_event_rule(_rule()),
            rule="teamagent-dev-morning-digest-weekday",
            target_id="morning",
            cluster="arn:aws:ecs:ap-northeast-1:123456789012:cluster/teamagent-dev",
            rollback_target_digest="f" * 64,
        ),
    )
    monkeypatch.setattr(SAGA, "load_control", lambda _value: parsed)
    return SAGA.EventBridgeApplySaga(
        control={},
        plan_sha256=plan_sha256,
        apply_attempt_id=attempt,
        clients=clients,
        rule_plans=_rule_plans(target_mutates=target_mutates),
        gate_mode=gate_mode,
        target_mutates=target_mutates,
    )


def test_failed_apply_restores_full_rule_and_exact_target_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _Clients()
    saga = _saga(monkeypatch, clients)
    baseline_rule = copy.deepcopy(clients.events.rule)
    baseline_targets = copy.deepcopy(clients.events.targets)
    saga.begin()

    clients.events.rule.update(
        {
            "Description": "partially changed",
            "RoleArn": "arn:aws:iam::123456789012:role/unreviewed",
            "ScheduleExpression": "rate(1 minute)",
            "State": "ENABLED",
        }
    )
    clients.events.targets = [_target("morning", 99), _target("unexpected", 98)]

    saga.finish(outcome="failed")

    assert clients.events.rule == baseline_rule
    assert sorted(clients.events.targets, key=lambda item: str(item["Id"])) == sorted(
        baseline_targets,
        key=lambda item: str(item["Id"]),
    )
    assert clients.ddb.item is not None
    assert clients.ddb.item["stage"] == {"S": "restored"}
    saga.finish(outcome="failed")


def test_new_attempt_recovers_and_archives_interrupted_active_saga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _Clients()
    first = _saga(monkeypatch, clients)
    baseline_rule = copy.deepcopy(clients.events.rule)
    baseline_targets = copy.deepcopy(clients.events.targets)
    first.begin()
    clients.events.rule["State"] = "ENABLED"
    clients.events.targets = [_target("unexpected", 99)]

    second = _saga(
        monkeypatch,
        clients,
        attempt=SECOND_ATTEMPT,
        plan_sha256="b" * 64,
    )
    second.begin()

    assert clients.events.rule == baseline_rule
    assert sorted(clients.events.targets, key=lambda item: str(item["Id"])) == sorted(
        baseline_targets,
        key=lambda item: str(item["Id"]),
    )
    assert clients.ddb.item is not None
    assert clients.ddb.item["stage"] == {"S": "applying"}
    assert clients.ddb.item["apply_attempt_id"] == {"S": SECOND_ATTEMPT}
    audit_record = "ecs-service-apply#eventbridge#audit#hmac-2026-07#" + ATTEMPT
    assert clients.ddb.items[audit_record]["stage"] == {"S": "restored"}


def test_partial_restore_remains_reconcilable_until_exact_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _Clients()
    saga = _saga(monkeypatch, clients)
    baseline_rule = copy.deepcopy(clients.events.rule)
    baseline_targets = copy.deepcopy(clients.events.targets)
    saga.begin()
    clients.events.rule["State"] = "ENABLED"
    clients.events.targets = [_target("unexpected", 99)]
    clients.events.fail_put_targets_once = True

    with pytest.raises(SAGA.SagaError, match="partial"):
        saga.finish(outcome="failed")

    assert clients.ddb.item is not None
    assert clients.ddb.item["stage"] == {"S": "applying"}

    saga.finish(outcome="failed")

    assert clients.events.rule == baseline_rule
    assert sorted(clients.events.targets, key=lambda item: str(item["Id"])) == sorted(
        baseline_targets,
        key=lambda item: str(item["Id"]),
    )
    assert clients.ddb.item["stage"] == {"S": "restored"}


def test_applied_saga_rejects_target_drift_when_plan_did_not_mutate_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _Clients()
    saga = _saga(monkeypatch, clients)
    saga.begin()
    clients.events.targets[1] = _target("audit", 99)

    with pytest.raises(SAGA.SagaError, match="outside the reviewed plan"):
        saga.finish(outcome="applied")

    assert clients.ddb.item is not None
    assert clients.ddb.item["stage"] == {"S": "applying"}


def test_applied_saga_accepts_only_exact_promoted_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _Clients()
    clients.events.targets = [_target("morning", 44)]
    saga = _saga(monkeypatch, clients, target_mutates=True)
    saga.begin()
    promoted = _target("morning", 45)
    clients.ddb.items["LEDGER#hmac-2026-07"] = {
        "candidate_morning_digest_target_digest": {"S": SAGA._canonical_target_digest(promoted)}
    }
    clients.events.targets = [promoted]

    saga.finish(outcome="applied")

    assert clients.ddb.item is not None
    assert clients.ddb.item["stage"] == {"S": "complete"}


@pytest.mark.parametrize("scenario", ["extra-target", "wrong-target"])
def test_applied_saga_rejects_nonexact_promoted_target_set(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    clients = _Clients()
    clients.events.targets = [_target("morning", 44)]
    saga = _saga(monkeypatch, clients, target_mutates=True)
    saga.begin()
    promoted = _target("morning", 45)
    clients.ddb.items["LEDGER#hmac-2026-07"] = {
        "candidate_morning_digest_target_digest": {"S": SAGA._canonical_target_digest(promoted)}
    }
    clients.events.targets = (
        [promoted, _target("audit", 45)] if scenario == "extra-target" else [_target("morning", 46)]
    )

    with pytest.raises(SAGA.SagaError, match="final target"):
        saga.finish(outcome="applied")

    assert clients.ddb.item is not None
    assert clients.ddb.item["stage"] == {"S": "applying"}
