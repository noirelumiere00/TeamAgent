"""Deterministically delete expired generic media job prefixes.

DynamoDB TTL and S3 lifecycle are backstops only.  This scheduled janitor
owner/version-fences each row, checks active consumer guards, deletes every
object under the exact job prefix, and only then conditionally deletes the row.
Any S3/DynamoDB cleanup error fails the invocation so EventBridge retries it.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

import boto3

ddb = boto3.client("dynamodb")
s3 = boto3.client("s3")

_JOB_ID = re.compile(r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$")
_MAX_ROWS = 100


def _number(item: dict[str, Any], name: str, default: int = 0) -> int:
    try:
        return int(item.get(name, {}).get("N", str(default)))
    except (TypeError, ValueError):
        return default


def _eligible(item: dict[str, Any], now: int) -> bool:
    status = item.get("status", {}).get("S", "")
    cleanup_at = _number(item, "cleanup_at")
    hard_cleanup_at = _number(item, "hard_cleanup_at")
    active = _number(item, "active_consumers")
    consumer_guard = _number(item, "consumer_guard_until")
    deadline = _number(item, "deadline")
    hard_due = hard_cleanup_at > 0 and hard_cleanup_at <= now
    normal_due = (
        cleanup_at > 0
        and cleanup_at <= now
        and active == 0
        and (consumer_guard == 0 or consumer_guard <= now)
    )
    terminal = status in {"done", "failed"}
    abandoned = status in {"queued", "running"} and deadline > 0 and deadline < now
    return (terminal or abandoned) and (hard_due or normal_due)


def _claim(table: str, item: dict[str, Any], owner: str, now: int) -> int | None:
    job_id = item.get("job_id", {}).get("S", "")
    version = _number(item, "version")
    try:
        response = ddb.update_item(
            TableName=table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression=(
                "SET cleanup_owner = :owner, cleanup_status = :deleting, "
                "cleanup_lease_expires_at = :lease, updated_at = :now "
                "ADD #version :one"
            ),
            ConditionExpression=(
                "#version = :version AND "
                "(attribute_not_exists(cleanup_owner) OR cleanup_lease_expires_at < :now) AND "
                "("
                "#status = :done OR #status = :failed OR "
                "((#status = :queued OR #status = :running) AND deadline < :now)"
                ") AND "
                "("
                "(hard_cleanup_at > :zero AND hard_cleanup_at <= :now) OR "
                "("
                "cleanup_at > :zero AND cleanup_at <= :now AND "
                "(attribute_not_exists(active_consumers) OR active_consumers = :zero) AND "
                "(attribute_not_exists(consumer_guard_until) OR consumer_guard_until <= :now)"
                ")"
                ")"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={
                ":owner": {"S": owner},
                ":deleting": {"S": "deleting"},
                ":lease": {"N": str(now + 240)},
                ":now": {"N": str(now)},
                ":zero": {"N": "0"},
                ":done": {"S": "done"},
                ":failed": {"S": "failed"},
                ":queued": {"S": "queued"},
                ":running": {"S": "running"},
                ":version": {"N": str(version)},
                ":one": {"N": "1"},
            },
            ReturnValues="ALL_NEW",
        )
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
        if code == "ConditionalCheckFailedException":
            return None
        raise
    return _number(response.get("Attributes", {}), "version")


def _delete_prefix(bucket: str, prefix: str) -> int:
    deleted_count = 0
    continuation: str | None = None
    while True:
        arguments: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": prefix,
            "MaxKeys": 1000,
        }
        if continuation:
            arguments["ContinuationToken"] = continuation
        response = s3.list_objects_v2(**arguments)
        objects = [{"Key": value["Key"]} for value in response.get("Contents", [])]
        if objects:
            result = s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": objects, "Quiet": True},
            )
            if result.get("Errors"):
                raise RuntimeError("S3 reported media janitor delete errors")
            deleted_count += len(objects)
        if not response.get("IsTruncated"):
            return deleted_count
        continuation = str(response["NextContinuationToken"])


def handler(_event: dict[str, Any], context: Any) -> dict[str, int]:
    table = os.environ["JOBS_TABLE"]
    bucket = os.environ["JOB_BUCKET"]
    now = int(time.time())
    invocation = str(getattr(context, "aws_request_id", "") or uuid.uuid4().hex)
    cleaned = 0
    objects = 0
    cursor: dict[str, Any] | None = None

    while cleaned < _MAX_ROWS:
        arguments: dict[str, Any] = {
            "TableName": table,
            "Limit": min(100, _MAX_ROWS - cleaned),
            "ConsistentRead": True,
        }
        if cursor:
            arguments["ExclusiveStartKey"] = cursor
        response = ddb.scan(**arguments)
        for item in response.get("Items", []):
            if not _eligible(item, now):
                continue
            job_id = item.get("job_id", {}).get("S", "")
            prefix = item.get("output_prefix", {}).get("S", "")
            if not _JOB_ID.fullmatch(job_id) or prefix != f"media-jobs/{job_id}/":
                raise RuntimeError("media janitor row scope is invalid")
            owner = f"{invocation}:{job_id}"
            claimed_version = _claim(table, item, owner, now)
            if claimed_version is None:
                continue
            removed = _delete_prefix(bucket, prefix)
            ddb.delete_item(
                TableName=table,
                Key={"job_id": {"S": job_id}},
                ConditionExpression="cleanup_owner = :owner AND #version = :version",
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={
                    ":owner": {"S": owner},
                    ":version": {"N": str(claimed_version)},
                },
            )
            print(
                json.dumps(
                    {
                        "event": "media_job_cleaned",
                        "job_id": job_id,
                        "previous_status": item.get("status", {}).get("S", ""),
                        "objects": removed,
                    },
                    sort_keys=True,
                )
            )
            cleaned += 1
            objects += removed
            if cleaned >= _MAX_ROWS:
                break
        cursor = response.get("LastEvaluatedKey")
        if not cursor:
            break
    return {"cleaned_jobs": cleaned, "deleted_objects": objects}
