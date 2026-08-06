"""ingest Fargate タスクの二重起動と長時間滞留を防ぐ dispatcher。"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, datetime
from typing import Any

import boto3

ecs = boto3.client("ecs")


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True, default=str))


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _max_runtime_hours() -> float:
    raw = os.environ.get("INGEST_MAX_RUNTIME_HOURS", "20").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("INGEST_MAX_RUNTIME_HOURS must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("INGEST_MAX_RUNTIME_HOURS must be a positive number")
    return value


def _subnets() -> list[str]:
    subnets = [value.strip() for value in os.environ["SUBNETS"].split(",") if value.strip()]
    if not subnets:
        raise ValueError("SUBNETS must contain at least one subnet")
    return subnets


def _elapsed_hours(task: dict[str, Any], now: datetime) -> float:
    """走行中タスクの経過時間（時）。

    ``startedAt`` は PROVISIONING/PENDING の間まだ付かない（実機で確認）。ここで例外を投げると
    「起動途中の1本が居るとディスパッチが毎回クラッシュして以降ずっと走れない」という、
    本来防ぎたかった居残り閉塞そのものを別経路で作る。よって ``createdAt`` に落とす。
    createdAt を使うことで PROVISIONING のまま張り付いたタスクも上限時間で回収できる。
    どちらも取れない場合だけ「起動直後」とみなし 0 を返す（＝停止せず skip 側に倒す）。
    """
    stamp = task.get("startedAt")
    if not isinstance(stamp, datetime):
        stamp = task.get("createdAt")
    if not isinstance(stamp, datetime):
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, (now - stamp.astimezone(UTC)).total_seconds() / 3600.0)


def _client_token(event: dict[str, Any]) -> str | None:
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    value = f"teamagent-ingest-v1\0{event_id}\0{os.environ['TASKDEF_ARN']}"
    return hashlib.sha256(value.encode()).hexdigest()


def _run_task(cluster: str, client_token: str | None) -> str:
    request: dict[str, Any] = {
        "cluster": cluster,
        "taskDefinition": os.environ["TASKDEF_ARN"],
        "launchType": "FARGATE",
        "platformVersion": "LATEST",
        "count": 1,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": _subnets(),
                "securityGroups": [os.environ["SG_ID"]],
                "assignPublicIp": "ENABLED",
            }
        },
    }
    if client_token is not None:
        request["clientToken"] = client_token
    response = ecs.run_task(**request)
    failures = response.get("failures") or []
    if failures:
        raise RuntimeError(f"ecs:RunTask returned {len(failures)} failure(s)")
    tasks = response.get("tasks") or []
    task_arn = tasks[0].get("taskArn") if tasks else None
    if not task_arn:
        raise RuntimeError("ecs:RunTask returned no task ARN")
    return str(task_arn)


def _dispatch(event: dict[str, Any]) -> dict[str, Any]:
    cluster = os.environ["CLUSTER_ARN"]
    family = os.environ["TASK_FAMILY"]
    max_runtime_hours = _max_runtime_hours()
    client_token = _client_token(event)
    response = ecs.list_tasks(
        cluster=cluster,
        family=family,
        desiredStatus="RUNNING",
    )
    task_arns = [str(value) for value in response.get("taskArns") or []]

    if not task_arns:
        task_arn = _run_task(cluster, client_token)
        _log(
            "ingest_dispatch_started",
            task_arn=task_arn,
            previous_running_task_count=0,
        )
        return {"ok": True, "action": "started", "task_arn": task_arn}

    described = ecs.describe_tasks(cluster=cluster, tasks=task_arns)
    tasks = described.get("tasks") or []
    described_by_arn = {
        str(task.get("taskArn")): task for task in tasks if task.get("taskArn") is not None
    }
    missing = [task_arn for task_arn in task_arns if task_arn not in described_by_arn]
    if missing:
        # list/describe 間の競合時は fail-close とし、二重起動を優先して防ぐ。
        raise RuntimeError("ecs:DescribeTasks did not return every running task")

    now = _now_utc()
    running = [
        (task_arn, _elapsed_hours(described_by_arn[task_arn], now)) for task_arn in task_arns
    ]
    fresh = [item for item in running if item[1] <= max_runtime_hours]
    stale = [item for item in running if item[1] > max_runtime_hours]
    stopped_task_arns: list[str] = []
    for task_arn, elapsed_hours in stale:
        ecs.stop_task(
            cluster=cluster,
            task=task_arn,
            reason="INGEST_MAX_RUNTIME_HOURS exceeded",
        )
        stopped_task_arns.append(task_arn)
        _log(
            "ingest_dispatch_stale_task_stopped",
            task_arn=task_arn,
            elapsed_hours=round(elapsed_hours, 3),
            max_runtime_hours=max_runtime_hours,
        )

    if fresh:
        # 期限超過分を停止した後も正常な1本が残るため、
        # 追加起動しない。
        task_arn, elapsed_hours = max(fresh, key=lambda item: item[1])
        rounded_elapsed = round(elapsed_hours, 3)
        _log(
            "ingest_dispatch_skipped",
            task_arn=task_arn,
            elapsed_hours=rounded_elapsed,
            max_runtime_hours=max_runtime_hours,
            running_task_count=len(running),
            stopped_task_arns=stopped_task_arns,
        )
        return {
            "ok": True,
            "action": "skipped",
            "task_arn": task_arn,
            "elapsed_hours": rounded_elapsed,
        }

    task_arn = _run_task(cluster, client_token)
    _log(
        "ingest_dispatch_restarted",
        task_arn=task_arn,
        stopped_task_arns=stopped_task_arns,
    )
    return {
        "ok": True,
        "action": "restarted",
        "task_arn": task_arn,
        "stopped_task_arns": stopped_task_arns,
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    try:
        return _dispatch(event)
    except Exception as exc:
        # 失敗を正常終了にせず、EventBridge の retry policy に委ねる。
        _log(
            "ingest_dispatch_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise
