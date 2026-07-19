"""tiktok-acquire dispatcher.

RunTaskの受付はジョブ完了ではないため、その時点ではSQSをackしない。SQS message IDから
決定的なECS clientTokenを作り、再配信でも同じtaskだけを参照する。DynamoDBが ``done`` に
なった配信だけをpartial-batch successとして返すため、Lambda停止・worker crash・状態更新失敗
のいずれでもmessageはsource queueまたはDLQに残る。
"""

import hashlib
import json
import os
import time

import boto3

ecs = boto3.client("ecs")
ddb = boto3.client("dynamodb")


def _failure(identifier):
    return {"itemIdentifier": identifier}


def _job_status(table, job_id):
    response = ddb.get_item(
        TableName=table,
        Key={"job_id": {"S": job_id}},
        ConsistentRead=True,
    )
    item = response.get("Item") or {}
    return item.get("status", {}).get("S", "missing")


def _record_dispatch(table, job_id, task_arn):
    try:
        ddb.update_item(
            TableName=table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression=(
                "SET #s = :dispatched, dispatch_task_arn = :task, dispatched_at = :at"
            ),
            ConditionExpression="#s = :queued",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":queued": {"S": "queued"},
                ":dispatched": {"S": "dispatched"},
                ":task": {"S": task_arn},
                ":at": {"N": str(int(time.time()))},
            },
        )
    except Exception as exc:
        # messageは必ずbatchItemFailuresへ入る。ここで状態記録に失敗しても、
        # 次回は同じclientTokenで同一taskへ収束するため二重起動しない。
        print(f"[dispatch] state update deferred: {type(exc).__name__}")


def handler(event, context):
    cluster = os.environ["CLUSTER_ARN"]
    taskdef = os.environ["TASKDEF_ARN"]
    subnets = [s for s in os.environ["SUBNETS"].split(",") if s]
    sg = os.environ["SG_ID"]
    container = os.environ.get("CONTAINER", "acquire")
    table = os.environ["JOBS_TABLE"]

    started = []
    batch_failures = []
    for rec in event.get("Records", []):
        identifier = rec.get("messageId")
        if not identifier:
            # SQS event contract外では個別failureを安全に返せないため、batch全体を再試行する。
            raise ValueError("SQS record is missing messageId")
        body = rec.get("body", "")
        try:
            spec = json.loads(body)
            if not spec.get("job_id") or not spec.get("keywords"):
                raise ValueError("job spec needs job_id and keywords")
        except Exception as exc:
            # poison messageもackせず、redrive policyにより監査可能なDLQへ送る。
            print(f"[dispatch] invalid message retained: {type(exc).__name__}")
            batch_failures.append(_failure(identifier))
            continue

        job_id = str(spec["job_id"])
        try:
            status = _job_status(table, job_id)
        except Exception as exc:
            print(f"[dispatch] status read failed job={job_id}: {type(exc).__name__}")
            batch_failures.append(_failure(identifier))
            continue

        if status == "done":
            print(f"[dispatch] completed job={job_id}; acknowledging message")
            continue
        if status != "queued":
            # dispatched/running/failed/missing は起動し直さない。doneになるまで保持し、
            # failed/missingは最終的にDLQへ送って調査・明示retry可能にする。
            print(f"[dispatch] retained job={job_id} status={status}")
            batch_failures.append(_failure(identifier))
            continue

        client_token = hashlib.sha256(
            f"teamagent-tiktok-v1\0{identifier}\0{taskdef}".encode()
        ).hexdigest()
        resp = ecs.run_task(
            cluster=cluster,
            taskDefinition=taskdef,
            clientToken=client_token,
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnets,
                    "securityGroups": [sg],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": container,
                        "environment": [{"name": "TIKTOK_JOB_JSON", "value": body}],
                    }
                ]
            },
        )
        failures = resp.get("failures", [])
        if failures:
            print(f"[dispatch] run_task rejected job={job_id}")
            batch_failures.append(_failure(identifier))
            continue
        tasks = resp.get("tasks", [])
        task_arn = tasks[0]["taskArn"] if tasks else None
        if not task_arn:
            print(f"[dispatch] run_task returned no task job={job_id}")
            batch_failures.append(_failure(identifier))
            continue
        _record_dispatch(table, job_id, task_arn)
        print(f"[dispatch] started job={job_id} task={task_arn}; message retained")
        started.append(task_arn)
        # RunTask成功だけではackしない。workerのdone更新を次回配信で確認する。
        batch_failures.append(_failure(identifier))

    return {"batchItemFailures": batch_failures, "started": started}
