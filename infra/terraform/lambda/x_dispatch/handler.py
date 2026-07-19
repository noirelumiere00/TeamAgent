"""x-buzz dispatcher with completion-based SQS acknowledgement.

RunTask受付時はpartial batch failureを返し、DynamoDBが ``done`` の時だけackする。
SQS message ID由来のECS clientTokenによりLambda再実行時もtask起動は冪等になる。
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
        print(f"[x-dispatch] state update deferred: {type(exc).__name__}")


def handler(event, context):
    cluster = os.environ["CLUSTER_ARN"]
    taskdef = os.environ["TASKDEF_ARN"]
    subnets = [s for s in os.environ["SUBNETS"].split(",") if s]
    sg = os.environ["SG_ID"]
    container = os.environ.get("CONTAINER", "worker")
    table = os.environ["JOBS_TABLE"]

    started = []
    batch_failures = []
    for rec in event.get("Records", []):
        identifier = rec.get("messageId")
        if not identifier:
            raise ValueError("SQS record is missing messageId")
        body = rec.get("body", "")
        try:
            spec = json.loads(body)
            if not spec.get("job_id") or not spec.get("keyword"):
                raise ValueError("job spec needs job_id and keyword")
        except Exception as exc:
            print(f"[x-dispatch] invalid message retained: {type(exc).__name__}")
            batch_failures.append(_failure(identifier))
            continue

        job_id = str(spec["job_id"])
        try:
            status = _job_status(table, job_id)
        except Exception as exc:
            print(f"[x-dispatch] status read failed job={job_id}: {type(exc).__name__}")
            batch_failures.append(_failure(identifier))
            continue
        if status == "done":
            print(f"[x-dispatch] completed job={job_id}; acknowledging message")
            continue
        if status != "queued":
            print(f"[x-dispatch] retained job={job_id} status={status}")
            batch_failures.append(_failure(identifier))
            continue

        client_token = hashlib.sha256(
            f"teamagent-x-buzz-v1\0{identifier}\0{taskdef}".encode()
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
                        "environment": [{"name": "X_JOB_JSON", "value": body}],
                    }
                ]
            },
        )
        failures = resp.get("failures", [])
        if failures:
            print(f"[x-dispatch] run_task rejected job={job_id}")
            batch_failures.append(_failure(identifier))
            continue
        tasks = resp.get("tasks", [])
        task_arn = tasks[0]["taskArn"] if tasks else None
        if not task_arn:
            print(f"[x-dispatch] run_task returned no task job={job_id}")
            batch_failures.append(_failure(identifier))
            continue
        _record_dispatch(table, job_id, task_arn)
        print(f"[x-dispatch] started job={job_id} task={task_arn}; message retained")
        started.append(task_arn)
        batch_failures.append(_failure(identifier))

    return {"batchItemFailures": batch_failures, "started": started}
