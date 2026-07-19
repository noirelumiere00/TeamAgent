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
_ATTEMPT_KEY = re.compile(
    r"^media-jobs/(?P<job_id>(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12}))/"
    r"attempts/(?P<lease_version>[1-9][0-9]*)/"
    r"(?P<attempt_id>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})/"
    r"(?P<relative_key>[^/].*)$"
)
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


def _orphan_sweep_eligible(item: dict[str, Any], now: int) -> bool:
    status = item.get("status", {}).get("S", "")
    if status == "queued":
        return True
    if status == "running":
        return _number(item, "lease_expires_at") < now
    if status in {"done", "failed"}:
        active = _number(item, "active_consumers")
        guard = _number(item, "consumer_guard_until")
        return active == 0 and (guard == 0 or guard <= now)
    return False


def _stale_nonterminal(item: dict[str, Any], now: int) -> bool:
    return (
        item.get("status", {}).get("S", "") in {"queued", "running"}
        and 0 < _number(item, "deadline") < now
    )


def _terminalize_stale(
    table: str,
    item: dict[str, Any],
    now: int,
) -> int | None:
    """Fence an expired queued/running row into a durable failed result."""

    job_id = item.get("job_id", {}).get("S", "")
    status = item.get("status", {}).get("S", "")
    version = _number(item, "version")
    deadline = _number(item, "deadline")
    detail = json.dumps(
        {
            "schema_version": "1",
            "job_id": job_id,
            "status": "failed",
            "artifacts": [],
            "metadata": {"reconciler": "stale-job"},
            "error_code": "MEDIA_JOB_STALE_TERMINALIZED",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        response = ddb.update_item(
            TableName=table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression=(
                "SET #status = :failed, detail = :detail, updated_at = :now "
                "REMOVE dispatch_owner, dispatch_lease_expires_at, "
                "lease_owner, lease_expires_at, attempt_id "
                "ADD #version :one"
            ),
            ConditionExpression=(
                "#version = :version AND #status = :status AND deadline = :deadline"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={
                ":failed": {"S": "failed"},
                ":detail": {"S": detail},
                ":now": {"N": str(now)},
                ":one": {"N": "1"},
                ":version": {"N": str(version)},
                ":status": {"S": status},
                ":deadline": {"N": str(deadline)},
            },
            ReturnValues="ALL_NEW",
        )
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
        if code == "ConditionalCheckFailedException":
            return None
        raise
    new_version = _number(response.get("Attributes", {}), "version", version + 1)
    item["status"] = {"S": "failed"}
    item["version"] = {"N": str(new_version)}
    print(
        json.dumps(
            {
                "event": "media_job_stale_terminalized",
                "job_id": job_id,
                "previous_status": status,
            },
            sort_keys=True,
        )
    )
    return new_version


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


def _claim_orphan_sweep(
    table: str,
    item: dict[str, Any],
    owner: str,
    now: int,
) -> int | None:
    job_id = item.get("job_id", {}).get("S", "")
    version = _number(item, "version")
    try:
        response = ddb.update_item(
            TableName=table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression=(
                "SET orphan_cleanup_owner = :owner, "
                "orphan_cleanup_lease_expires_at = :lease, updated_at = :now "
                "ADD #version :one"
            ),
            ConditionExpression=(
                "#version = :version AND "
                "(attribute_not_exists(orphan_cleanup_owner) OR "
                "orphan_cleanup_lease_expires_at < :now) AND "
                "(#status = :queued OR #status = :done OR #status = :failed OR "
                "(#status = :running AND lease_expires_at < :now))"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={
                ":owner": {"S": owner},
                ":lease": {"N": str(now + 240)},
                ":now": {"N": str(now)},
                ":queued": {"S": "queued"},
                ":running": {"S": "running"},
                ":done": {"S": "done"},
                ":failed": {"S": "failed"},
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


def _release_orphan_sweep(
    table: str,
    job_id: str,
    owner: str,
    version: int,
) -> None:
    ddb.update_item(
        TableName=table,
        Key={"job_id": {"S": job_id}},
        UpdateExpression=("REMOVE orphan_cleanup_owner, orphan_cleanup_lease_expires_at"),
        ConditionExpression="orphan_cleanup_owner = :owner AND #version = :version",
        ExpressionAttributeNames={"#version": "version"},
        ExpressionAttributeValues={
            ":owner": {"S": owner},
            ":version": {"N": str(version)},
        },
    )


def _delete_unfinalized_attempts(
    bucket: str,
    output_prefix: str,
    finalized_attempt_id: str,
) -> int:
    expected_job_id = output_prefix.removeprefix("media-jobs/").removesuffix("/")
    if not _JOB_ID.fullmatch(expected_job_id) or output_prefix != f"media-jobs/{expected_job_id}/":
        raise RuntimeError("media janitor output prefix is invalid")
    attempts: dict[str, list[tuple[str, re.Match[str]]]] = {}
    continuation: str | None = None
    while True:
        arguments: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": f"{output_prefix}attempts/",
            "MaxKeys": 1000,
        }
        if continuation:
            arguments["ContinuationToken"] = continuation
        response = s3.list_objects_v2(**arguments)
        for value in response.get("Contents", []):
            key = str(value["Key"])
            match = _ATTEMPT_KEY.match(key)
            if match is None or not key.startswith(output_prefix):
                raise RuntimeError("media janitor attempt key is invalid")
            if match.group("job_id") != expected_job_id:
                raise RuntimeError("media janitor attempt job metadata is invalid")
            attempt_id = match.group("attempt_id")
            if attempt_id != finalized_attempt_id:
                attempts.setdefault(attempt_id, []).append((key, match))
        if not response.get("IsTruncated"):
            break
        continuation = str(response["NextContinuationToken"])
    deleted = 0
    for values in attempts.values():
        keys: list[str] = []
        for key, match in values:
            metadata = s3.head_object(Bucket=bucket, Key=key).get("Metadata", {})
            tags = {
                str(tag.get("Key", "")): str(tag.get("Value", ""))
                for tag in s3.get_object_tagging(Bucket=bucket, Key=key).get(
                    "TagSet",
                    [],
                )
            }
            marker = match.group("relative_key") == "_FINALIZED.json"
            expected_finalized = "true" if marker else "false"
            if (
                metadata.get("job-id") != expected_job_id
                or metadata.get("attempt-id") != match.group("attempt_id")
                or metadata.get("lease-version") != match.group("lease_version")
                or metadata.get("finalized") != expected_finalized
                or tags.get("teamagent-attempt-id") != match.group("attempt_id")
                or tags.get("teamagent-finalized") != expected_finalized
            ):
                raise RuntimeError(
                    "media janitor refuses object without exact attempt metadata and tags"
                )
            keys.append(key)
        result = s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
        )
        if result.get("Errors"):
            raise RuntimeError("S3 reported media janitor delete errors")
        deleted += len(keys)
    return deleted


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
    reclaimed_attempts = 0
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
            job_id = item.get("job_id", {}).get("S", "")
            prefix = item.get("output_prefix", {}).get("S", "")
            if not _JOB_ID.fullmatch(job_id) or prefix != f"media-jobs/{job_id}/":
                raise RuntimeError("media janitor row scope is invalid")
            stale_terminalized = False
            if _stale_nonterminal(item, now):
                if _terminalize_stale(table, item, now) is None:
                    continue
                stale_terminalized = True
            if not _eligible(item, now):
                if stale_terminalized or _orphan_sweep_eligible(item, now):
                    owner = f"{invocation}:{job_id}:orphans"
                    claimed_version = _claim_orphan_sweep(table, item, owner, now)
                    if claimed_version is not None:
                        reclaimed_attempts += _delete_unfinalized_attempts(
                            bucket,
                            prefix,
                            item.get("finalized_attempt_id", {}).get("S", ""),
                        )
                        _release_orphan_sweep(
                            table,
                            job_id,
                            owner,
                            claimed_version,
                        )
                continue
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
    return {
        "cleaned_jobs": cleaned,
        "deleted_objects": objects,
        "reclaimed_attempt_objects": reclaimed_attempts,
    }
