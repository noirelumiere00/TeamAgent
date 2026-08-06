from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_HANDLER = (
    Path(__file__).parents[2] / "infra" / "terraform" / "lambda" / "ingest_dispatch" / "handler.py"
)
_NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
_OLD_TASK = "arn:aws:ecs:ap-northeast-1:123456789012:task/teamagent-dev/old"
_NEW_TASK = "arn:aws:ecs:ap-northeast-1:123456789012:task/teamagent-dev/new"


class _Ecs:
    def __init__(
        self,
        *,
        task_arns: list[str],
        started_at: datetime | None = None,
        started_at_by_arn: dict[str, datetime] | None = None,
        list_error: Exception | None = None,
        created_at: datetime | None = None,
        omit_started_at: bool = False,
    ) -> None:
        self.task_arns = task_arns
        self.started_at = started_at
        self.started_at_by_arn = started_at_by_arn or {}
        self.list_error = list_error
        # 実機の ECS は PROVISIONING/PENDING の間 startedAt キー自体を返さない。
        # 「None が入っている」ではなく「キーが無い」を再現しないと本番の失敗を取り逃す。
        self.created_at = created_at
        self.omit_started_at = omit_started_at
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tasks(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_tasks", kwargs))
        if self.list_error is not None:
            raise self.list_error
        return {"taskArns": self.task_arns}

    def describe_tasks(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_tasks", kwargs))
        tasks: list[dict[str, Any]] = []
        for task_arn in self.task_arns:
            task: dict[str, Any] = {"taskArn": task_arn}
            if not self.omit_started_at:
                task["startedAt"] = self.started_at_by_arn.get(task_arn, self.started_at)
            if self.created_at is not None:
                task["createdAt"] = self.created_at
            tasks.append(task)
        return {"tasks": tasks}

    def stop_task(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("stop_task", kwargs))
        return {"task": {"taskArn": kwargs["task"]}}

    def run_task(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run_task", kwargs))
        return {"tasks": [{"taskArn": _NEW_TASK}], "failures": []}


def _load_handler(monkeypatch: pytest.MonkeyPatch, ecs: _Ecs) -> Any:
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name: ecs if name == "ecs" else None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    name = f"_teamagent_ingest_dispatch_{id(ecs)}"
    spec = importlib.util.spec_from_file_location(name, _HANDLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CLUSTER_ARN", "arn:aws:ecs:ap-northeast-1:123456789012:cluster/teamagent-dev"
    )
    monkeypatch.setenv(
        "TASKDEF_ARN",
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-ingest:45",
    )
    monkeypatch.setenv("TASK_FAMILY", "teamagent-dev-ingest")
    monkeypatch.setenv("SUBNETS", "subnet-a,subnet-b")
    monkeypatch.setenv("SG_ID", "sg-ingest")
    monkeypatch.setenv("INGEST_MAX_RUNTIME_HOURS", "20")


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_no_running_task_starts_fargate_with_the_scheduled_network_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ecs = _Ecs(task_arns=[])
    module = _load_handler(monkeypatch, ecs)
    _configure(monkeypatch)

    result = module.handler({"id": "scheduled-event-1"}, None)

    assert result == {"ok": True, "action": "started", "task_arn": _NEW_TASK}
    assert [name for name, _kwargs in ecs.calls] == ["list_tasks", "run_task"]
    assert ecs.calls[0][1] == {
        "cluster": "arn:aws:ecs:ap-northeast-1:123456789012:cluster/teamagent-dev",
        "family": "teamagent-dev-ingest",
        "desiredStatus": "RUNNING",
    }
    assert ecs.calls[1][1] == {
        "cluster": "arn:aws:ecs:ap-northeast-1:123456789012:cluster/teamagent-dev",
        "taskDefinition": (
            "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-ingest:45"
        ),
        "launchType": "FARGATE",
        "platformVersion": "LATEST",
        "count": 1,
        "clientToken": hashlib.sha256(
            (
                "teamagent-ingest-v1\0scheduled-event-1\0"
                "arn:aws:ecs:ap-northeast-1:123456789012:"
                "task-definition/teamagent-dev-ingest:45"
            ).encode()
        ).hexdigest(),
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": ["subnet-a", "subnet-b"],
                "securityGroups": ["sg-ingest"],
                "assignPublicIp": "ENABLED",
            }
        },
    }
    assert _events(capsys) == [
        {
            "event": "ingest_dispatch_started",
            "previous_running_task_count": 0,
            "task_arn": _NEW_TASK,
        }
    ]


def test_recent_running_task_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ecs = _Ecs(task_arns=[_OLD_TASK], started_at=_NOW - timedelta(hours=3))
    module = _load_handler(monkeypatch, ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module, "_now_utc", lambda: _NOW)

    result = module.handler({}, None)

    assert result == {
        "ok": True,
        "action": "skipped",
        "task_arn": _OLD_TASK,
        "elapsed_hours": 3.0,
    }
    assert [name for name, _kwargs in ecs.calls] == ["list_tasks", "describe_tasks"]
    assert _events(capsys) == [
        {
            "elapsed_hours": 3.0,
            "event": "ingest_dispatch_skipped",
            "max_runtime_hours": 20.0,
            "running_task_count": 1,
            "stopped_task_arns": [],
            "task_arn": _OLD_TASK,
        }
    ]


def test_stale_running_task_is_stopped_before_replacement_starts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ecs = _Ecs(task_arns=[_OLD_TASK], started_at=_NOW - timedelta(hours=21))
    module = _load_handler(monkeypatch, ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module, "_now_utc", lambda: _NOW)

    result = module.handler({}, None)

    assert result == {
        "ok": True,
        "action": "restarted",
        "task_arn": _NEW_TASK,
        "stopped_task_arns": [_OLD_TASK],
    }
    assert [name for name, _kwargs in ecs.calls] == [
        "list_tasks",
        "describe_tasks",
        "stop_task",
        "run_task",
    ]
    assert ecs.calls[2][1]["task"] == _OLD_TASK
    assert [event["event"] for event in _events(capsys)] == [
        "ingest_dispatch_stale_task_stopped",
        "ingest_dispatch_restarted",
    ]


def test_stale_duplicate_is_stopped_without_starting_when_a_recent_task_remains(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recent_task = "arn:aws:ecs:ap-northeast-1:123456789012:task/teamagent-dev/recent"
    ecs = _Ecs(
        task_arns=[_OLD_TASK, recent_task],
        started_at_by_arn={
            _OLD_TASK: _NOW - timedelta(hours=21),
            recent_task: _NOW - timedelta(hours=3),
        },
    )
    module = _load_handler(monkeypatch, ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module, "_now_utc", lambda: _NOW)

    result = module.handler({}, None)

    assert result == {
        "ok": True,
        "action": "skipped",
        "task_arn": recent_task,
        "elapsed_hours": 3.0,
    }
    assert [name for name, _kwargs in ecs.calls] == [
        "list_tasks",
        "describe_tasks",
        "stop_task",
    ]
    assert ecs.calls[2][1]["task"] == _OLD_TASK
    assert [event["event"] for event in _events(capsys)] == [
        "ingest_dispatch_stale_task_stopped",
        "ingest_dispatch_skipped",
    ]


def test_list_tasks_failure_never_starts_a_task(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ecs = _Ecs(task_arns=[], list_error=RuntimeError("list unavailable"))
    module = _load_handler(monkeypatch, ecs)
    _configure(monkeypatch)

    with pytest.raises(RuntimeError, match="list unavailable"):
        module.handler({}, None)

    assert [name for name, _kwargs in ecs.calls] == ["list_tasks"]
    assert _events(capsys) == [
        {
            "error_message": "list unavailable",
            "error_type": "RuntimeError",
            "event": "ingest_dispatch_failed",
        }
    ]


def test_provisioning_task_without_started_at_is_skipped_not_crashed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """起動直後(PROVISIONING/PENDING)は startedAt キーが無い——実機で確認した本番の失敗モード。

    ここで例外を投げると「起動途中の1本が居るとディスパッチが毎回落ちて以降ずっと走れない」
    という、このガードが防ぐはずの居残り閉塞そのものを別経路で作る。
    """
    ecs = _Ecs(
        task_arns=[_OLD_TASK],
        omit_started_at=True,
        created_at=_NOW - timedelta(minutes=2),
    )
    module = _load_handler(monkeypatch, ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module, "_now_utc", lambda: _NOW)

    result = module.handler({"id": "e1"}, None)

    assert result["action"] == "skipped"
    assert "run_task" not in [name for name, _kwargs in ecs.calls]
    assert "stop_task" not in [name for name, _kwargs in ecs.calls]


def test_task_stuck_provisioning_past_limit_is_stopped_and_restarted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROVISIONING のまま上限を超えて張り付いたタスクも createdAt で回収できること。"""
    ecs = _Ecs(
        task_arns=[_OLD_TASK],
        omit_started_at=True,
        created_at=_NOW - timedelta(hours=40),
    )
    module = _load_handler(monkeypatch, ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module, "_now_utc", lambda: _NOW)

    result = module.handler({"id": "e2"}, None)

    assert result["action"] == "restarted"
    assert result["stopped_task_arns"] == [_OLD_TASK]
    assert "run_task" in [name for name, _kwargs in ecs.calls]


def test_task_without_any_timestamp_is_treated_as_fresh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """startedAt も createdAt も取れない場合は停止せず skip 側に倒す（誤停止を防ぐ）。"""
    ecs = _Ecs(task_arns=[_OLD_TASK], omit_started_at=True, created_at=None)
    module = _load_handler(monkeypatch, ecs)
    _configure(monkeypatch)
    monkeypatch.setattr(module, "_now_utc", lambda: _NOW)

    result = module.handler({"id": "e3"}, None)

    assert result["action"] == "skipped"
    assert "stop_task" not in [name for name, _kwargs in ecs.calls]
    assert "run_task" not in [name for name, _kwargs in ecs.calls]
