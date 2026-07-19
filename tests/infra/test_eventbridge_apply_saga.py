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


def _rule() -> dict[str, object]:
    return {
        "Arn": ("arn:aws:events:ap-northeast-1:123456789012:rule/teamagent-dev-morning-digest"),
        "CreatedBy": "123456789012",
        "Description": "reviewed schedule",
        "EventBusName": "default",
        "EventPattern": None,
        "ManagedBy": None,
        "Name": "teamagent-dev-morning-digest",
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
        },
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": 3600,
            "MaximumRetryAttempts": 1,
        },
    }


class _Events:
    def __init__(self) -> None:
        self.rule = _rule()
        self.targets = [_target("morning", 44), _target("audit", 43)]
        self.fail_put_targets_once = False

    def describe_rule(self, **_kwargs: object) -> dict[str, object]:
        return _response(**copy.deepcopy(self.rule))

    def list_targets_by_rule(self, **_kwargs: object) -> dict[str, object]:
        return _response(Targets=copy.deepcopy(self.targets))

    def put_rule(self, **kwargs: object) -> dict[str, object]:
        for name in (
            "Description",
            "EventPattern",
            "RoleArn",
            "ScheduleExpression",
        ):
            self.rule[name] = kwargs.get(name)
        self.rule["State"] = kwargs["State"]
        self.rule["Name"] = kwargs["Name"]
        self.rule["EventBusName"] = kwargs["EventBusName"]
        return _response(RuleArn=self.rule["Arn"])

    def remove_targets(self, **kwargs: object) -> dict[str, object]:
        removed = set(kwargs["Ids"])  # type: ignore[arg-type]
        self.targets = [target for target in self.targets if str(target["Id"]) not in removed]
        return _response(FailedEntryCount=0, FailedEntries=[])

    def put_targets(self, **kwargs: object) -> dict[str, object]:
        if self.fail_put_targets_once:
            self.fail_put_targets_once = False
            return _response(
                FailedEntryCount=1,
                FailedEntries=[{"ErrorCode": "InternalException"}],
            )
        for replacement in copy.deepcopy(kwargs["Targets"]):  # type: ignore[union-attr]
            self.targets = [target for target in self.targets if target["Id"] != replacement["Id"]]
            self.targets.append(replacement)
        return _response(FailedEntryCount=0, FailedEntries=[])


class _Ddb:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    @property
    def item(self) -> dict[str, Any] | None:
        active = [
            item
            for record, item in self.items.items()
            if record.startswith("EVENTBRIDGE_APPLY#")
            and not record.startswith("EVENTBRIDGE_APPLY_AUDIT#")
        ]
        assert len(active) <= 1
        return active[0] if active else None

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        record = str(key["record"]["S"])
        item = self.items.get(record)
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, **kwargs: object) -> dict[str, object]:
        item = copy.deepcopy(kwargs["Item"])
        assert isinstance(item, dict)
        record = str(item["record"]["S"])
        assert record not in self.items
        self.items[record] = item
        return {}

    def update_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        record = str(key["record"]["S"])
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
        audit_record = str(audit["record"]["S"])
        current_record = str(current["record"]["S"])
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


def _saga(
    monkeypatch: pytest.MonkeyPatch,
    clients: _Clients,
    *,
    attempt: str = ATTEMPT,
    plan_sha256: str = PLAN_SHA256,
) -> Any:
    parsed = SimpleNamespace(
        region="ap-northeast-1",
        scope="teamagent/dev",
        state_table="teamagent-dev-hmac-state",
        rotation_epoch="hmac-2026-07",
        morning_digest=SimpleNamespace(
            expected_rule=SAGA._canonical_event_rule(_rule()),
            rule="teamagent-dev-morning-digest",
        ),
    )
    monkeypatch.setattr(SAGA, "load_control", lambda _value: parsed)
    return SAGA.EventBridgeApplySaga(
        control={},
        plan_sha256=plan_sha256,
        apply_attempt_id=attempt,
        clients=clients,
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
    audit_record = "EVENTBRIDGE_APPLY_AUDIT#hmac-2026-07#" + ATTEMPT
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
