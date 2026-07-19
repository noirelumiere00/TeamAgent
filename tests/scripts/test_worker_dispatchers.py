"""SQS dispatcherがworker完了前のmessageを恒久ackしない契約テスト。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


class FakeEcs:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_task(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"tasks": [{"taskArn": "arn:aws:ecs:ap-northeast-1:718959508629:task/one"}]}


class FakeDdb:
    def __init__(self, status: str = "queued") -> None:
        self.status = status
        self.updates: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return {"Item": {"status": {"S": self.status}}}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        self.status = "dispatched"
        return {}


def _load_dispatcher(relative: str, ecs: FakeEcs, ddb: FakeDdb) -> Any:
    fake_boto3 = types.SimpleNamespace(
        client=lambda service: ecs if service == "ecs" else ddb,
    )
    name = f"_dispatcher_{relative.replace('/', '_')}_{id(ecs)}"
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        spec.loader.exec_module(module)
    return module


CASES = [
    (
        "infra/terraform/lambda/x_dispatch/handler.py",
        {"job_id": "x_1", "keyword": "test"},
        "X_JOB_JSON",
    ),
]


@pytest.mark.parametrize(("relative", "body", "job_env"), CASES)
def test_run_task_success_is_retained_and_idempotent(
    relative: str,
    body: dict[str, Any],
    job_env: str,
) -> None:
    ecs = FakeEcs()
    ddb = FakeDdb()
    module = _load_dispatcher(relative, ecs, ddb)
    event = {"Records": [{"messageId": "message-1", "body": json.dumps(body)}]}
    environment = {
        "CLUSTER_ARN": "cluster",
        "TASKDEF_ARN": "taskdef:7",
        "SUBNETS": "subnet-a,subnet-b",
        "SG_ID": "sg-1",
        "JOBS_TABLE": "jobs",
    }

    with patch.dict(os.environ, environment, clear=True):
        first = module.handler(event, None)
        second = module.handler(event, None)

    assert first["batchItemFailures"] == [{"itemIdentifier": "message-1"}]
    assert second["batchItemFailures"] == [{"itemIdentifier": "message-1"}]
    assert len(ecs.calls) == 1
    assert len(ecs.calls[0]["clientToken"]) == 64
    override = ecs.calls[0]["overrides"]["containerOverrides"][0]["environment"]
    assert override == [{"name": job_env, "value": json.dumps(body)}]
    assert ddb.updates[0]["ConditionExpression"] == "#s = :queued"


@pytest.mark.parametrize(("relative", "body", "_job_env"), CASES)
def test_only_done_status_is_acknowledged(
    relative: str,
    body: dict[str, Any],
    _job_env: str,
) -> None:
    ecs = FakeEcs()
    ddb = FakeDdb(status="done")
    module = _load_dispatcher(relative, ecs, ddb)
    event = {"Records": [{"messageId": "message-2", "body": json.dumps(body)}]}

    with patch.dict(
        os.environ,
        {
            "CLUSTER_ARN": "cluster",
            "TASKDEF_ARN": "taskdef:7",
            "SUBNETS": "subnet-a",
            "SG_ID": "sg-1",
            "JOBS_TABLE": "jobs",
        },
        clear=True,
    ):
        result = module.handler(event, None)

    assert result["batchItemFailures"] == []
    assert ecs.calls == []


@pytest.mark.parametrize(("relative", "_body", "_job_env"), CASES)
def test_invalid_messages_are_preserved_for_dlq(
    relative: str,
    _body: dict[str, Any],
    _job_env: str,
) -> None:
    ecs = FakeEcs()
    module = _load_dispatcher(relative, ecs, FakeDdb())
    event = {"Records": [{"messageId": "poison", "body": "{}"}]}

    with patch.dict(
        os.environ,
        {
            "CLUSTER_ARN": "cluster",
            "TASKDEF_ARN": "taskdef:7",
            "SUBNETS": "subnet-a",
            "SG_ID": "sg-1",
            "JOBS_TABLE": "jobs",
        },
        clear=True,
    ):
        result = module.handler(event, None)

    assert result["batchItemFailures"] == [{"itemIdentifier": "poison"}]
    assert ecs.calls == []
