"""tiktok-acquire ディスパッチャ Lambda。
SQS(tiktok-jobs)からジョブJSONをデキューし、ECS RunTask(Fargate)を起動する。
ジョブ本文(JSON文字列)を containerOverrides の env TIKTOK_JOB_JSON にそのまま注入する。
RunTask/PassRole 権限はこのLambdaロールだけが持つ(=MCP/共有botから分離)。
"""
import json
import os

import boto3

ecs = boto3.client("ecs")


def handler(event, context):
    cluster = os.environ["CLUSTER_ARN"]
    taskdef = os.environ["TASKDEF_ARN"]
    subnets = [s for s in os.environ["SUBNETS"].split(",") if s]
    sg = os.environ["SG_ID"]
    container = os.environ.get("CONTAINER", "acquire")

    started = []
    for rec in event.get("Records", []):
        body = rec.get("body", "")
        # body はジョブ仕様(JSON文字列)。簡易バリデーション。
        try:
            spec = json.loads(body)
            if not spec.get("job_id") or not spec.get("keywords"):
                raise ValueError("job spec needs job_id and keywords")
        except Exception as e:  # 不正メッセージは捨てる(再試行→DLQ行きにしない)
            print(f"[dispatch] skip invalid message: {e}")
            continue

        resp = ecs.run_task(
            cluster=cluster,
            taskDefinition=taskdef,
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
            # 起動失敗は例外を上げてSQSに戻す(指数バックオフ→最終的にDLQ)
            raise RuntimeError(f"run_task failures: {failures}")
        tasks = resp.get("tasks", [])
        task_arn = tasks[0]["taskArn"] if tasks else None
        print(f"[dispatch] started job={spec.get('job_id')} task={task_arn}")
        started.append(task_arn)

    return {"started": started}
