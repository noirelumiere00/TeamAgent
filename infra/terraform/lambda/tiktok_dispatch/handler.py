"""Dispatch one strict generic media envelope to one Fargate task.

The Lambda validates the canonical SQS envelope but passes only its bounded
DynamoDB pointer to ECS.  A short DynamoDB dispatch lease prevents duplicate
SQS deliveries from silently launching unbounded tasks; worker-side lease
fencing remains authoritative for the actual job transition.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import boto3

ecs = boto3.client("ecs")
ddb = boto3.client("dynamodb")

_MAX_BODY_BYTES = 128 * 1024
_MAX_ECS_OVERRIDE_CHARACTERS = 8192
_JOB_ID = re.compile(r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_S3_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_S3_KEY = re.compile(r"^[A-Za-z0-9!_.*'()/+=:@-]+$")
_SIMPLE_SELECTOR = re.compile(r"[.#][A-Za-z][A-Za-z0-9_-]{0,78}")
_ACQUIRE_HOST_SUFFIXES = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "instagr.am",
)
_MAX_INPUT_BYTES = 128 * 1024 * 1024
_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_OPERATIONS = {
    "acquire",
    "tiktok_acquire",
    "proxy",
    "frame",
    "thumbnail",
    "slides",
    "proposal_pptx",
    "pdf",
}
_REQUIRED_KEYS = {
    "schema_version",
    "job_id",
    "idempotency_key",
    "created_at_epoch_s",
    "deadline_epoch_s",
    "artifact_ttl_s",
    "audit_principal_hash",
    "output_bucket",
    "output_prefix",
    "operation",
    "payload_sha256",
}


def _exact_keys(value: Any, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} keys are invalid")
    return value


def _bounded_int(value: Any, *, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} is invalid")
    return value


def _bounded_string(value: Any, *, minimum: int, maximum: int, name: str) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} is invalid")
    return value


def _validate_s3_ref(value: Any) -> dict[str, Any]:
    ref = _exact_keys(
        value,
        {"bucket", "key", "sha256", "size", "content_type"},
        "S3 reference",
    )
    bucket = _bounded_string(ref["bucket"], minimum=3, maximum=63, name="S3 bucket")
    key = _bounded_string(ref["key"], minimum=1, maximum=1024, name="S3 key")
    content_type = _bounded_string(
        ref["content_type"],
        minimum=1,
        maximum=128,
        name="S3 content type",
    )
    if not _S3_BUCKET.fullmatch(bucket):
        raise ValueError("S3 bucket is invalid")
    if (
        not _S3_KEY.fullmatch(key)
        or "\\" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise ValueError("S3 key is invalid")
    if not _SHA256.fullmatch(str(ref["sha256"])):
        raise ValueError("S3 digest is invalid")
    _bounded_int(ref["size"], minimum=0, maximum=_MAX_INPUT_BYTES, name="S3 size")
    if "\r" in content_type or "\n" in content_type:
        raise ValueError("S3 content type is invalid")
    return ref


def _validate_https_url(value: Any, *, allowlisted: bool) -> str:
    url = _bounded_string(value, minimum=8, maximum=2048, name="media URL")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("media URL port is invalid") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise ValueError("media URL must be canonical HTTPS")
    if allowlisted and not any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _ACQUIRE_HOST_SUFFIXES
    ):
        raise ValueError("media URL host is not allowlisted")
    if allowlisted and parsed.fragment:
        raise ValueError("media URL fragments are not allowed")
    return url


def _validate_tiktok_client(value: Any) -> None:
    client = _exact_keys(
        value,
        {"client", "client_short", "competitors", "industry"},
        "TikTok client",
    )
    for name in ("client", "client_short", "industry"):
        item = client[name]
        if item is not None:
            _bounded_string(item, minimum=0, maximum=200, name=f"TikTok {name}")
    competitors = client["competitors"]
    if not isinstance(competitors, list) or len(competitors) > 20:
        raise ValueError("TikTok competitors are invalid")
    for competitor in competitors:
        _bounded_string(competitor, minimum=0, maximum=200, name="TikTok competitor")


def _validate_operation(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("media operation is invalid")
    kind = value.get("kind")
    if kind == "acquire":
        operation = _exact_keys(value, {"kind", "url", "max_bytes"}, "acquire operation")
        _validate_https_url(operation["url"], allowlisted=True)
        _bounded_int(
            operation["max_bytes"],
            minimum=1,
            maximum=_MAX_OUTPUT_BYTES,
            name="acquire max_bytes",
        )
    elif kind == "tiktok_acquire":
        operation = _exact_keys(
            value,
            {
                "kind",
                "keywords",
                "n_per_kw",
                "videos_per_kw",
                "sort",
                "max_video_bytes",
                "client",
            },
            "TikTok operation",
        )
        keywords = operation["keywords"]
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 10:
            raise ValueError("TikTok keywords are invalid")
        normalized: list[str] = []
        for keyword in keywords:
            normalized.append(
                _bounded_string(keyword, minimum=1, maximum=100, name="TikTok keyword").strip()
            )
        if any(not keyword for keyword in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("TikTok keywords are invalid")
        _bounded_int(operation["n_per_kw"], minimum=1, maximum=30, name="TikTok n_per_kw")
        _bounded_int(
            operation["videos_per_kw"],
            minimum=0,
            maximum=10,
            name="TikTok videos_per_kw",
        )
        if operation["sort"] not in {"display", "save_rate", "recent"}:
            raise ValueError("TikTok sort is invalid")
        _bounded_int(
            operation["max_video_bytes"],
            minimum=1,
            maximum=_MAX_OUTPUT_BYTES,
            name="TikTok max_video_bytes",
        )
        _validate_tiktok_client(operation["client"])
    elif kind == "proxy":
        operation = _exact_keys(
            value,
            {"kind", "source", "limit_bytes", "long_edge", "preview"},
            "proxy operation",
        )
        _validate_s3_ref(operation["source"])
        _bounded_int(
            operation["limit_bytes"],
            minimum=1024,
            maximum=_MAX_OUTPUT_BYTES,
            name="proxy limit_bytes",
        )
        _bounded_int(operation["long_edge"], minimum=240, maximum=2160, name="proxy long_edge")
        if type(operation["preview"]) is not bool:
            raise ValueError("proxy preview is invalid")
    elif kind == "frame":
        operation = _exact_keys(
            value,
            {"kind", "source", "timecodes", "width"},
            "frame operation",
        )
        _validate_s3_ref(operation["source"])
        timecodes = operation["timecodes"]
        if not isinstance(timecodes, list) or not 1 <= len(timecodes) <= 12:
            raise ValueError("frame timecodes are invalid")
        if any(type(value) is not float or value < 0 or value > 6 * 60 * 60 for value in timecodes):
            raise ValueError("frame timecodes are invalid")
        if sorted(set(timecodes)) != timecodes:
            raise ValueError("frame timecodes are invalid")
        _bounded_int(operation["width"], minimum=64, maximum=1920, name="frame width")
    elif kind == "thumbnail":
        operation = _exact_keys(
            value,
            {"kind", "source", "url", "width"},
            "thumbnail operation",
        )
        source = operation["source"]
        url = operation["url"]
        if (source is None) == (url is None):
            raise ValueError("thumbnail source is invalid")
        if source is not None:
            _validate_s3_ref(source)
        if url is not None:
            _validate_https_url(url, allowlisted=False)
        _bounded_int(operation["width"], minimum=64, maximum=1920, name="thumbnail width")
    elif kind == "slides":
        operation = _exact_keys(
            value,
            {"kind", "html", "selector", "width", "height", "device_scale_factor"},
            "slides operation",
        )
        _validate_s3_ref(operation["html"])
        selector = _bounded_string(
            operation["selector"],
            minimum=1,
            maximum=80,
            name="slides selector",
        )
        if not _SIMPLE_SELECTOR.fullmatch(selector):
            raise ValueError("slides selector is invalid")
        _bounded_int(operation["width"], minimum=320, maximum=1920, name="slides width")
        _bounded_int(operation["height"], minimum=180, maximum=1080, name="slides height")
        _bounded_int(
            operation["device_scale_factor"],
            minimum=1,
            maximum=2,
            name="slides device_scale_factor",
        )
    elif kind == "proposal_pptx":
        operation = _exact_keys(
            value,
            {"kind", "template", "composer_json", "evidence", "fail_if_missing"},
            "proposal operation",
        )
        _validate_s3_ref(operation["template"])
        _validate_s3_ref(operation["composer_json"])
        evidence = operation["evidence"]
        if not isinstance(evidence, list) or len(evidence) > 20:
            raise ValueError("proposal evidence is invalid")
        for item in evidence:
            entry = _exact_keys(
                item,
                {"placeholder_id", "rank", "source"},
                "proposal evidence",
            )
            _bounded_int(
                entry["placeholder_id"],
                minimum=1,
                maximum=103,
                name="proposal placeholder_id",
            )
            _bounded_int(entry["rank"], minimum=1, maximum=20, name="proposal rank")
            _validate_s3_ref(entry["source"])
        if type(operation["fail_if_missing"]) is not bool:
            raise ValueError("proposal fail_if_missing is invalid")
    elif kind == "pdf":
        operation = _exact_keys(value, {"kind", "html"}, "PDF operation")
        _validate_s3_ref(operation["html"])
    else:
        raise ValueError("media operation is invalid")


def _operation_s3_refs(operation: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for name in ("source", "html", "template", "composer_json"):
        value = operation.get(name)
        if isinstance(value, dict):
            refs.append(value)
    evidence = operation.get("evidence", [])
    if isinstance(evidence, list):
        refs.extend(
            item["source"]
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("source"), dict)
        )
    return refs


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code == "ConditionalCheckFailedException"


def _validate_envelope(
    body: str,
    *,
    expected_bucket: str,
    now: int,
    max_artifact_ttl_s: int = 21600,
) -> dict[str, Any]:
    encoded = body.encode("utf-8")
    if not encoded or len(encoded) > _MAX_BODY_BYTES:
        raise ValueError("media envelope size is invalid")
    spec = json.loads(encoded)
    if not isinstance(spec, dict) or set(spec) != _REQUIRED_KEYS:
        raise ValueError("media envelope keys are invalid")
    if spec["schema_version"] != "1" or not _JOB_ID.fullmatch(str(spec["job_id"])):
        raise ValueError("media envelope identity is invalid")
    if not _SHA256.fullmatch(str(spec["idempotency_key"])) or not _SHA256.fullmatch(
        str(spec["payload_sha256"])
    ):
        raise ValueError("media envelope digest is invalid")
    operation = spec["operation"]
    if not isinstance(operation, dict) or operation.get("kind") not in _OPERATIONS:
        raise ValueError("media operation is invalid")
    _validate_operation(operation)
    created = spec["created_at_epoch_s"]
    deadline = spec["deadline_epoch_s"]
    ttl = spec["artifact_ttl_s"]
    if (
        type(created) is not int
        or type(deadline) is not int
        or type(ttl) is not int
        or not 1 <= deadline - created <= 900
        or not 300 <= ttl <= max_artifact_ttl_s <= 21600
    ):
        raise ValueError("media envelope timing is invalid")
    if deadline < now:
        raise TimeoutError("media envelope deadline exceeded")
    if spec["output_bucket"] != expected_bucket:
        raise ValueError("media output bucket is outside dispatcher scope")
    if not _S3_BUCKET.fullmatch(str(spec["output_bucket"])):
        raise ValueError("media output bucket is invalid")
    audit_hash = spec["audit_principal_hash"]
    if audit_hash is not None and not _SHA256.fullmatch(str(audit_hash)):
        raise ValueError("media audit principal hash is invalid")
    expected_prefix = f"media-jobs/{spec['job_id']}/"
    if spec["output_prefix"] != expected_prefix:
        raise ValueError("media output prefix is outside dispatcher scope")
    for ref in _operation_s3_refs(operation):
        if ref["bucket"] != expected_bucket or not ref["key"].startswith(
            f"{expected_prefix}input/"
        ):
            raise ValueError("media input object is outside dispatcher scope")
    without_hash = dict(spec)
    without_hash.pop("payload_sha256")
    if hashlib.sha256(_canonical(without_hash)).hexdigest() != spec["payload_sha256"]:
        raise ValueError("media envelope payload digest mismatch")
    if _canonical(spec) != encoded:
        raise ValueError("media envelope is not canonical JSON")
    return spec


def _mark_failed(table: str, job_id: str, code: str, now: int) -> None:
    detail = _canonical(
        {
            "schema_version": "1",
            "job_id": job_id,
            "status": "failed",
            "artifacts": [],
            "metadata": {"dispatcher": True},
            "error_code": code,
        }
    ).decode("utf-8")
    try:
        ddb.update_item(
            TableName=table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression=(
                "SET #status = :failed, detail = :detail, updated_at = :now, "
                "cleanup_at = :now ADD #version :one"
            ),
            ConditionExpression="#status = :queued",
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={
                ":queued": {"S": "queued"},
                ":failed": {"S": "failed"},
                ":detail": {"S": detail},
                ":now": {"N": str(now)},
                ":one": {"N": "1"},
            },
        )
    except Exception as exc:
        if not _conditional_failure(exc):
            raise


def _claim_dispatch(table: str, spec: dict[str, Any], owner: str, now: int) -> bool:
    try:
        ddb.update_item(
            TableName=table,
            Key={"job_id": {"S": spec["job_id"]}},
            UpdateExpression=(
                "SET dispatch_owner = :owner, dispatch_lease_expires_at = :lease, "
                "dispatch_started_at = :now"
            ),
            ConditionExpression=(
                "#status = :queued AND payload_sha256 = :payload AND "
                "(attribute_not_exists(dispatched_task_arn)) AND "
                "(attribute_not_exists(dispatch_owner) OR dispatch_lease_expires_at < :now)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":queued": {"S": "queued"},
                ":payload": {"S": spec["payload_sha256"]},
                ":owner": {"S": owner},
                ":now": {"N": str(now)},
                ":lease": {"N": str(now + 120)},
            },
        )
        return True
    except Exception as exc:
        if not _conditional_failure(exc):
            raise
        item = ddb.get_item(
            TableName=table,
            Key={"job_id": {"S": spec["job_id"]}},
            ConsistentRead=True,
        ).get("Item", {})
        if item.get("dispatched_task_arn", {}).get("S"):
            return False
        status = item.get("status", {}).get("S")
        if status in {"running", "done", "failed"}:
            return False
        raise RuntimeError("media dispatch lease is already held") from exc


def _release_dispatch(table: str, job_id: str, owner: str) -> None:
    ddb.update_item(
        TableName=table,
        Key={"job_id": {"S": job_id}},
        UpdateExpression="REMOVE dispatch_owner, dispatch_lease_expires_at",
        ConditionExpression="dispatch_owner = :owner",
        ExpressionAttributeValues={":owner": {"S": owner}},
    )


def _task_overrides(container: str, spec: dict[str, Any]) -> dict[str, Any]:
    overrides = {
        "containerOverrides": [
            {
                "name": container,
                "environment": [
                    {"name": "MEDIA_JOB_ID", "value": spec["job_id"]},
                    {
                        "name": "MEDIA_JOB_PAYLOAD_SHA256",
                        "value": spec["payload_sha256"],
                    },
                ],
            }
        ]
    }
    if len(_canonical(overrides).decode("utf-8")) > _MAX_ECS_OVERRIDE_CHARACTERS:
        raise ValueError("ECS task override exceeds the 8192-character service limit")
    return overrides


def handler(event: dict[str, Any], context: Any) -> dict[str, list[str | None]]:
    cluster = os.environ["CLUSTER_ARN"]
    taskdef = os.environ["TASKDEF_ARN"]
    table = os.environ["JOBS_TABLE"]
    bucket = os.environ["JOB_BUCKET"]
    subnets = [subnet for subnet in os.environ["SUBNETS"].split(",") if subnet]
    security_group = os.environ["SG_ID"]
    container = os.environ.get("CONTAINER", "media-worker")
    try:
        max_artifact_ttl_s = int(os.environ["MEDIA_ARTIFACT_TTL_SECONDS"])
    except (KeyError, ValueError) as exc:
        raise RuntimeError("MEDIA_ARTIFACT_TTL_SECONDS is invalid") from exc
    if not 300 <= max_artifact_ttl_s <= 21600:
        raise RuntimeError("MEDIA_ARTIFACT_TTL_SECONDS is invalid")

    started: list[str | None] = []
    for record in event.get("Records", []):
        body = record.get("body", "")
        now = int(time.time())
        raw_job_id = ""
        try:
            untrusted = json.loads(body)
            if isinstance(untrusted, dict):
                raw_job_id = str(untrusted.get("job_id", ""))
        except (TypeError, json.JSONDecodeError):
            pass
        try:
            spec = _validate_envelope(
                body,
                expected_bucket=bucket,
                now=now,
                max_artifact_ttl_s=max_artifact_ttl_s,
            )
        except TimeoutError:
            if _JOB_ID.fullmatch(raw_job_id):
                _mark_failed(table, raw_job_id, "MEDIA_JOB_DEADLINE_EXCEEDED", now)
            raise
        except Exception:
            if _JOB_ID.fullmatch(raw_job_id):
                _mark_failed(table, raw_job_id, "MEDIA_DISPATCH_ENVELOPE_INVALID", now)
            raise

        owner = str(record.get("messageId") or getattr(context, "aws_request_id", "dispatch"))
        if not _claim_dispatch(table, spec, owner, now):
            continue
        try:
            if int(time.time()) >= spec["deadline_epoch_s"]:
                _mark_failed(
                    table,
                    spec["job_id"],
                    "MEDIA_JOB_DEADLINE_EXCEEDED",
                    int(time.time()),
                )
                raise TimeoutError("media envelope deadline exceeded before task launch")
            response = ecs.run_task(
                cluster=cluster,
                taskDefinition=taskdef,
                launchType="FARGATE",
                clientToken=spec["idempotency_key"],
                count=1,
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnets,
                        "securityGroups": [security_group],
                        "assignPublicIp": "ENABLED",
                    }
                },
                overrides=_task_overrides(container, spec),
            )
            failures = response.get("failures", [])
            tasks = response.get("tasks", [])
            if failures or len(tasks) != 1:
                raise RuntimeError(f"run_task did not start exactly one task: {failures}")
            task_arn = tasks[0]["taskArn"]
            ddb.update_item(
                TableName=table,
                Key={"job_id": {"S": spec["job_id"]}},
                UpdateExpression=(
                    "SET dispatched_task_arn = :task, dispatched_at = :now "
                    "REMOVE dispatch_owner, dispatch_lease_expires_at"
                ),
                ConditionExpression="dispatch_owner = :owner",
                ExpressionAttributeValues={
                    ":task": {"S": task_arn},
                    ":now": {"N": str(now)},
                    ":owner": {"S": owner},
                },
            )
            started.append(task_arn)
        except Exception:
            _release_dispatch(table, spec["job_id"], owner)
            raise
    return {"started": started}
