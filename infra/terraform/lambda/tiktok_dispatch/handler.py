"""Dispatch one strict generic media envelope to one Fargate task.

The Lambda validates the canonical SQS envelope but passes only its bounded
DynamoDB pointer to ECS.  A short DynamoDB dispatch lease prevents duplicate
SQS deliveries from silently launching unbounded tasks; worker-side lease
fencing remains authoritative for the actual job transition.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

_MAX_BODY_BYTES = 128 * 1024
_MAX_ECS_OVERRIDE_CHARACTERS = 8192
_MAX_JOB_BUDGET_SECONDS = 15 * 60
_MAX_CLOCK_SKEW_SECONDS = 30
_TIKTOK_OPERATION_EXECUTION_LIMIT_SECONDS = _MAX_JOB_BUDGET_SECONDS - 30
_TIKTOK_SEARCH_WORST_CASE_SECONDS = 120
_TIKTOK_THUMBNAIL_WORST_CASE_SECONDS = 50
_TIKTOK_VIDEO_WORST_CASE_SECONDS = 120
_MAX_ARTIFACT_RETENTION_SECONDS = 30 * 24 * 60 * 60
_TASK_START_MINIMUM_BUDGET_SECONDS = 30.0
_TERMINAL_WRITE_RESERVE_SECONDS = 15.0
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
        {"bucket", "key", "version_id", "sha256", "size", "content_type"},
        "S3 reference",
    )
    bucket = _bounded_string(ref["bucket"], minimum=3, maximum=63, name="S3 bucket")
    key = _bounded_string(ref["key"], minimum=1, maximum=1024, name="S3 key")
    version_id = _bounded_string(
        ref["version_id"],
        minimum=1,
        maximum=1024,
        name="S3 version ID",
    )
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
    if version_id == "null" or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", version_id):
        raise ValueError("S3 version ID is invalid")
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
                "search_type",
                "keywords",
                "n_per_kw",
                "videos_per_kw",
                "sort",
                "artifact_mode",
                "max_video_bytes",
                "client",
            },
            "TikTok operation",
        )
        keywords = operation["keywords"]
        if operation["search_type"] not in {"keyword", "hashtag"}:
            raise ValueError("TikTok search type is invalid")
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
        artifact_mode = operation["artifact_mode"]
        if artifact_mode not in {"metadata_only", "full"}:
            raise ValueError("TikTok artifact mode is invalid")
        if artifact_mode == "metadata_only" and operation["videos_per_kw"] != 0:
            raise ValueError("metadata-only TikTok operation cannot download videos")
        estimated_seconds = len(keywords) * _TIKTOK_SEARCH_WORST_CASE_SECONDS
        if artifact_mode == "full":
            estimated_seconds += len(keywords) * (
                operation["n_per_kw"] * _TIKTOK_THUMBNAIL_WORST_CASE_SECONDS
                + operation["videos_per_kw"] * _TIKTOK_VIDEO_WORST_CASE_SECONDS
            )
        if estimated_seconds > _TIKTOK_OPERATION_EXECUTION_LIMIT_SECONDS:
            raise ValueError("TikTok operation exceeds immutable job deadline")
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


def _remaining(deadline_epoch_s: int) -> float:
    remaining = deadline_epoch_s - time.time()
    if remaining <= 0:
        raise TimeoutError("media envelope deadline exceeded")
    return max(0.001, remaining)


def _client(
    service: str,
    deadline_epoch_s: int,
    *,
    reserve_seconds: float = 0.0,
) -> Any:
    if reserve_seconds < 0:
        raise ValueError("deadline reserve cannot be negative")
    remaining = _remaining(deadline_epoch_s) - reserve_seconds
    if remaining <= 0:
        raise TimeoutError("media envelope terminal budget reserve reached")
    phase_timeout = max(0.001, min(10.0, remaining / 2.0))
    return boto3.client(
        service,
        config=Config(
            connect_timeout=phase_timeout,
            read_timeout=phase_timeout,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )


def _call(
    service: str,
    operation: str,
    deadline_epoch_s: int,
    *,
    reserve_seconds: float = 0.0,
    **kwargs: Any,
) -> Any:
    return getattr(
        _client(
            service,
            deadline_epoch_s,
            reserve_seconds=reserve_seconds,
        ),
        operation,
    )(**kwargs)


def _failure_target(body: str, now: int) -> dict[str, Any] | None:
    """Return only a complete, digest-proven persisted-envelope identity."""

    try:
        value = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != _REQUIRED_KEYS
        or _canonical(value) != body.encode("utf-8")
    ):
        return None
    job_id = value.get("job_id")
    created = value.get("created_at_epoch_s")
    deadline = value.get("deadline_epoch_s")
    idempotency_key = value.get("idempotency_key")
    payload_sha256 = value.get("payload_sha256")
    audit_hash = value.get("audit_principal_hash")
    if (
        not isinstance(job_id, str)
        or not _JOB_ID.fullmatch(job_id)
        or type(created) is not int
        or type(deadline) is not int
        or not 1 <= deadline - created <= _MAX_JOB_BUDGET_SECONDS
        or created > now + _MAX_CLOCK_SKEW_SECONDS
        or not now < deadline <= now + _MAX_JOB_BUDGET_SECONDS
        or not isinstance(idempotency_key, str)
        or not _SHA256.fullmatch(idempotency_key)
        or not isinstance(payload_sha256, str)
        or not _SHA256.fullmatch(payload_sha256)
        or (audit_hash is not None and not _SHA256.fullmatch(str(audit_hash)))
    ):
        return None
    without_hash = dict(value)
    without_hash.pop("payload_sha256")
    if not hmac.compare_digest(
        hashlib.sha256(_canonical(without_hash)).hexdigest(),
        payload_sha256,
    ):
        return None
    return {
        "job_id": job_id,
        "deadline_epoch_s": deadline,
        "idempotency_key": idempotency_key,
        "payload_sha256": payload_sha256,
        "audit_principal_hash": audit_hash,
        "request_json": body,
    }


def _validate_envelope(
    body: str,
    *,
    expected_bucket: str,
    now: int,
    max_artifact_ttl_s: int = _MAX_ARTIFACT_RETENTION_SECONDS,
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
        or not 1 <= deadline - created <= _MAX_JOB_BUDGET_SECONDS
        or created > now + _MAX_CLOCK_SKEW_SECONDS
        or deadline - now > _MAX_JOB_BUDGET_SECONDS
        or not 300 <= ttl <= max_artifact_ttl_s <= _MAX_ARTIFACT_RETENTION_SECONDS
    ):
        raise ValueError("media envelope timing is invalid")
    if deadline <= now:
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


def _mark_failed(
    table: str,
    target: dict[str, Any],
    code: str,
    now: int,
) -> None:
    job_id = str(target["job_id"])
    deadline_epoch_s = int(target["deadline_epoch_s"])
    audit_hash = target["audit_principal_hash"]
    audit_condition = (
        "audit_principal_hash = :audit"
        if audit_hash is not None
        else "attribute_not_exists(audit_principal_hash)"
    )
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
    values: dict[str, Any] = {
        ":queued": {"S": "queued"},
        ":failed": {"S": "failed"},
        ":detail": {"S": detail},
        ":now": {"N": str(now)},
        ":cleanup": {"N": str(now + _MAX_ARTIFACT_RETENTION_SECONDS)},
        ":one": {"N": "1"},
        ":request_json": {"S": target["request_json"]},
        ":payload": {"S": target["payload_sha256"]},
        ":idempotency": {"S": target["idempotency_key"]},
    }
    if audit_hash is not None:
        values[":audit"] = {"S": audit_hash}
    try:
        _call(
            "dynamodb",
            "update_item",
            deadline_epoch_s,
            TableName=table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression=(
                "SET #status = :failed, detail = :detail, updated_at = :now, "
                "cleanup_at = if_not_exists(hard_cleanup_at, :cleanup) "
                "ADD #version :one"
            ),
            ConditionExpression=(
                "#status = :queued AND request_json = :request_json AND "
                "payload_sha256 = :payload AND idempotency_key = :idempotency AND "
                f"{audit_condition}"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if not _conditional_failure(exc):
            raise


def _claim_dispatch(table: str, spec: dict[str, Any], owner: str, now: int) -> bool:
    deadline_epoch_s = int(spec["deadline_epoch_s"])
    audit_hash = spec["audit_principal_hash"]
    audit_condition = (
        "audit_principal_hash = :audit"
        if audit_hash is not None
        else "attribute_not_exists(audit_principal_hash)"
    )
    values: dict[str, Any] = {
        ":queued": {"S": "queued"},
        ":payload": {"S": spec["payload_sha256"]},
        ":owner": {"S": owner},
        ":now": {"N": str(now)},
        ":lease": {"N": str(now + 120)},
    }
    if audit_hash is not None:
        values[":audit"] = {"S": audit_hash}
    try:
        _call(
            "dynamodb",
            "update_item",
            deadline_epoch_s,
            TableName=table,
            Key={"job_id": {"S": spec["job_id"]}},
            UpdateExpression=(
                "SET dispatch_owner = :owner, dispatch_lease_expires_at = :lease, "
                "dispatch_started_at = :now"
            ),
            ConditionExpression=(
                f"#status = :queued AND payload_sha256 = :payload AND {audit_condition} AND "
                "(attribute_not_exists(dispatched_task_arn)) AND "
                "(attribute_not_exists(dispatch_owner) OR dispatch_lease_expires_at < :now)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )
        return True
    except Exception as exc:
        if not _conditional_failure(exc):
            raise
        item = _call(
            "dynamodb",
            "get_item",
            deadline_epoch_s,
            TableName=table,
            Key={"job_id": {"S": spec["job_id"]}},
            ConsistentRead=True,
        ).get("Item", {})
        if item.get("payload_sha256", {}).get("S", "") != spec[
            "payload_sha256"
        ] or not hmac.compare_digest(
            item.get("audit_principal_hash", {}).get("S", ""),
            str(audit_hash or ""),
        ):
            raise RuntimeError("media dispatch row ownership mismatch") from exc
        if item.get("dispatched_task_arn", {}).get("S"):
            return False
        status = item.get("status", {}).get("S")
        if status in {"running", "done", "failed"}:
            return False
        raise RuntimeError("media dispatch lease is already held") from exc


def _release_dispatch(
    table: str,
    job_id: str,
    owner: str,
    deadline_epoch_s: int,
) -> None:
    _call(
        "dynamodb",
        "update_item",
        deadline_epoch_s,
        TableName=table,
        Key={"job_id": {"S": job_id}},
        UpdateExpression="REMOVE dispatch_owner, dispatch_lease_expires_at",
        ConditionExpression="dispatch_owner = :owner",
        ExpressionAttributeValues={":owner": {"S": owner}},
    )


def _task_overrides(container: str, spec: dict[str, Any]) -> dict[str, Any]:
    environment = [
        {"name": "MEDIA_JOB_ID", "value": spec["job_id"]},
        {
            "name": "MEDIA_JOB_PAYLOAD_SHA256",
            "value": spec["payload_sha256"],
        },
        {
            "name": "MEDIA_JOB_DEADLINE_EPOCH_S",
            "value": str(spec["deadline_epoch_s"]),
        },
    ]
    if spec["audit_principal_hash"] is not None:
        environment.append(
            {
                "name": "MEDIA_JOB_AUDIT_PRINCIPAL_HASH",
                "value": spec["audit_principal_hash"],
            }
        )
    overrides = {
        "containerOverrides": [
            {
                "name": container,
                "environment": environment,
            }
        ]
    }
    if len(_canonical(overrides).decode("utf-8")) > _MAX_ECS_OVERRIDE_CHARACTERS:
        raise ValueError("ECS task override exceeds the 8192-character service limit")
    return overrides


def _task_definition_family_arn(value: Any) -> str:
    task_definition = _bounded_string(
        value,
        minimum=20,
        maximum=512,
        name="ECS task definition ARN",
    )
    family, separator, revision = task_definition.rpartition(":")
    if (
        not separator
        or not revision.isdigit()
        or ":task-definition/" not in family
        or not family.startswith("arn:")
    ):
        raise ValueError("ECS task definition ARN is invalid")
    return family


def _event_deadline(context: Any) -> int:
    remaining_ms = getattr(context, "get_remaining_time_in_millis", lambda: 30_000)()
    if type(remaining_ms) is not int or remaining_ms < 2_000:
        raise TimeoutError("ECS STOPPED reconciler has no write budget")
    return int(time.time()) + max(1, min(25, (remaining_ms - 1_000) // 1_000))


def _tag_values(detail: dict[str, Any]) -> dict[str, str]:
    tags = detail.get("tags")
    # Task state-change events preserve RunTask overrides, while tags are not
    # part of the documented event shape. If present, use tags as an extra
    # identity cross-check only.
    if tags is None:
        return {}
    if not isinstance(tags, list) or len(tags) > 50:
        raise ValueError("ECS STOPPED task tags are invalid")
    values: dict[str, str] = {}
    for raw in tags:
        if not isinstance(raw, dict):
            raise ValueError("ECS STOPPED task tags are invalid")
        key = raw.get("key")
        value = raw.get("value")
        if not isinstance(key, str) or not isinstance(value, str) or key in values:
            raise ValueError("ECS STOPPED task tags are invalid")
        values[key] = value
    return values


def _override_values(detail: dict[str, Any], expected_container: str) -> dict[str, str]:
    overrides = detail.get("overrides")
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError("ECS STOPPED overrides are invalid")
    containers = overrides.get("containerOverrides")
    if not isinstance(containers, list):
        raise ValueError("ECS STOPPED overrides are invalid")
    matches = [
        value
        for value in containers
        if isinstance(value, dict) and value.get("name") == expected_container
    ]
    if len(matches) != 1:
        raise ValueError("ECS STOPPED container override is invalid")
    environment = matches[0].get("environment")
    if not isinstance(environment, list):
        raise ValueError("ECS STOPPED container environment is invalid")
    values: dict[str, str] = {}
    for raw in environment:
        if not isinstance(raw, dict):
            raise ValueError("ECS STOPPED container environment is invalid")
        name = raw.get("name")
        value = raw.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or name in values:
            raise ValueError("ECS STOPPED container environment is invalid")
        values[name] = value
    return values


def _stopped_identity(
    detail: dict[str, Any],
    *,
    expected_container: str,
) -> tuple[str, str, str | None]:
    tags = _tag_values(detail)
    overrides = _override_values(detail, expected_container)
    tagged_job_id = tags.get("teamagent-job-id")
    tagged_payload = tags.get("teamagent-payload-sha256")
    overridden_job_id = overrides.get("MEDIA_JOB_ID")
    overridden_payload = overrides.get("MEDIA_JOB_PAYLOAD_SHA256")
    overridden_audit = overrides.get("MEDIA_JOB_AUDIT_PRINCIPAL_HASH")
    if tagged_job_id is not None and overridden_job_id != tagged_job_id:
        raise ValueError("ECS STOPPED job identity disagrees with task tags")
    if tagged_payload is not None and overridden_payload != tagged_payload:
        raise ValueError("ECS STOPPED payload identity disagrees with task tags")
    if (
        overridden_job_id is None
        or not _JOB_ID.fullmatch(overridden_job_id)
        or overridden_payload is None
        or not _SHA256.fullmatch(overridden_payload)
        or detail.get("startedBy") != overridden_job_id
    ):
        raise ValueError("ECS STOPPED task identity is invalid")
    tagged_audit = tags.get("teamagent-audit-principal-hash")
    if tagged_audit is not None and tagged_audit != overridden_audit:
        raise ValueError("ECS STOPPED audit identity disagrees with task tags")
    if overridden_audit is not None and not _SHA256.fullmatch(overridden_audit):
        raise ValueError("ECS STOPPED audit identity is invalid")
    return overridden_job_id, overridden_payload, overridden_audit


def _stopped_diagnostics(detail: dict[str, Any]) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for source, target, maximum in (
        ("stopCode", "stop_code", 100),
        ("stoppedReason", "stopped_reason", 512),
    ):
        value = detail.get(source)
        if isinstance(value, str):
            diagnostics[target] = value[:maximum]
    containers: list[dict[str, Any]] = []
    raw_containers = detail.get("containers")
    if isinstance(raw_containers, list):
        for raw in raw_containers[:20]:
            if not isinstance(raw, dict):
                continue
            item: dict[str, Any] = {}
            if isinstance(raw.get("name"), str):
                item["name"] = raw["name"][:100]
            if type(raw.get("exitCode")) is int:
                item["exit_code"] = raw["exitCode"]
            if isinstance(raw.get("reason"), str):
                item["reason"] = raw["reason"][:512]
            if item:
                containers.append(item)
    if containers:
        diagnostics["containers"] = containers
    return diagnostics


def _reconcile_stopped(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("source") != "aws.ecs" or event.get("detail-type") != "ECS Task State Change":
        raise ValueError("ECS STOPPED event identity is invalid")
    detail = event.get("detail")
    if not isinstance(detail, dict) or detail.get("lastStatus") != "STOPPED":
        raise ValueError("ECS STOPPED event status is invalid")
    cluster = os.environ["CLUSTER_ARN"]
    taskdef = os.environ["TASKDEF_ARN"]
    table = os.environ["JOBS_TABLE"]
    container = os.environ.get("CONTAINER", "media-worker")
    if detail.get("clusterArn") != cluster:
        raise ValueError("ECS STOPPED cluster is outside reconciler scope")
    if _task_definition_family_arn(detail.get("taskDefinitionArn")) != (
        _task_definition_family_arn(taskdef)
    ):
        raise ValueError("ECS STOPPED task family is outside reconciler scope")
    task_arn = _bounded_string(
        detail.get("taskArn"),
        minimum=20,
        maximum=512,
        name="ECS task ARN",
    )
    if not task_arn.startswith("arn:") or ":task/" not in task_arn:
        raise ValueError("ECS task ARN is invalid")
    job_id, payload_hash, audit_hash = _stopped_identity(
        detail,
        expected_container=container,
    )
    now = int(time.time())
    deadline = _event_deadline(context)
    audit_condition = (
        "audit_principal_hash = :audit"
        if audit_hash is not None
        else "attribute_not_exists(audit_principal_hash)"
    )
    diagnostics = _stopped_diagnostics(detail)
    result_detail = _canonical(
        {
            "schema_version": "1",
            "job_id": job_id,
            "status": "failed",
            "artifacts": [],
            "metadata": {
                "reconciler": "ecs-stopped",
                "ecs": diagnostics,
            },
            "error_code": "MEDIA_ECS_TASK_STOPPED",
        }
    ).decode("utf-8")
    values: dict[str, Any] = {
        ":queued": {"S": "queued"},
        ":running": {"S": "running"},
        ":failed": {"S": "failed"},
        ":task": {"S": task_arn},
        ":payload": {"S": payload_hash},
        ":detail": {"S": result_detail},
        ":now": {"N": str(now)},
        ":cleanup": {"N": str(now + _MAX_ARTIFACT_RETENTION_SECONDS)},
        ":one": {"N": "1"},
    }
    if audit_hash is not None:
        values[":audit"] = {"S": audit_hash}
    try:
        _call(
            "dynamodb",
            "update_item",
            deadline,
            TableName=table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression=(
                "SET #status = :failed, detail = :detail, updated_at = :now, "
                "cleanup_at = if_not_exists(hard_cleanup_at, :cleanup) "
                "REMOVE dispatch_owner, dispatch_lease_expires_at "
                "ADD #version :one"
            ),
            ConditionExpression=(
                "(#status = :queued OR #status = :running) AND "
                "dispatched_task_arn = :task AND payload_sha256 = :payload AND "
                f"{audit_condition}"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if not _conditional_failure(exc):
            raise
        item = _call(
            "dynamodb",
            "get_item",
            deadline,
            TableName=table,
            Key={"job_id": {"S": job_id}},
            ConsistentRead=True,
        ).get("Item", {})
        status = item.get("status", {}).get("S")
        if status in {"done", "failed"}:
            return {"reconciled": False, "job_id": job_id, "status": status}
        if (
            item.get("dispatched_task_arn", {}).get("S") != task_arn
            or item.get("payload_sha256", {}).get("S") != payload_hash
            or not hmac.compare_digest(
                item.get("audit_principal_hash", {}).get("S", ""),
                audit_hash or "",
            )
        ):
            raise RuntimeError("ECS STOPPED row ownership mismatch") from exc
        # The dispatch confirmation may race the STOPPED event. Raising keeps
        # the EventBridge delivery retryable until that write is observable.
        raise RuntimeError("ECS STOPPED transition raced job state") from exc
    return {"reconciled": True, "job_id": job_id, "status": "failed"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("source") == "aws.ecs":
        return _reconcile_stopped(event, context)

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
    if not 300 <= max_artifact_ttl_s <= _MAX_ARTIFACT_RETENTION_SECONDS:
        raise RuntimeError("MEDIA_ARTIFACT_TTL_SECONDS is invalid")

    started: list[str | None] = []
    for record in event.get("Records", []):
        body = record.get("body", "")
        now = int(time.time())
        failure_target = _failure_target(body, now)
        try:
            spec = _validate_envelope(
                body,
                expected_bucket=bucket,
                now=now,
                max_artifact_ttl_s=max_artifact_ttl_s,
            )
        except TimeoutError:
            # The immutable deadline is already exhausted. No synthetic
            # deadline and no post-deadline network call are permitted.
            raise
        except Exception:
            if failure_target is not None:
                _mark_failed(
                    table,
                    failure_target,
                    "MEDIA_DISPATCH_ENVELOPE_INVALID",
                    now,
                )
            raise

        deadline_epoch_s = int(spec["deadline_epoch_s"])
        if _remaining(deadline_epoch_s) <= _TASK_START_MINIMUM_BUDGET_SECONDS:
            if failure_target is None:
                raise RuntimeError("validated media envelope lost failure identity")
            _mark_failed(
                table,
                failure_target,
                "MEDIA_JOB_DEADLINE_EXCEEDED",
                int(time.time()),
            )
            raise TimeoutError("media envelope deadline exceeded before dispatch claim")
        owner = str(record.get("messageId") or getattr(context, "aws_request_id", "dispatch"))
        if not _claim_dispatch(table, spec, owner, now):
            continue
        try:
            if _remaining(deadline_epoch_s) <= _TASK_START_MINIMUM_BUDGET_SECONDS:
                if failure_target is None:
                    raise RuntimeError("validated media envelope lost failure identity")
                _mark_failed(
                    table,
                    failure_target,
                    "MEDIA_JOB_DEADLINE_EXCEEDED",
                    int(time.time()),
                )
                raise TimeoutError("media envelope deadline exceeded before task launch")
            response = _call(
                "ecs",
                "run_task",
                deadline_epoch_s,
                reserve_seconds=_TERMINAL_WRITE_RESERVE_SECONDS,
                cluster=cluster,
                taskDefinition=taskdef,
                launchType="FARGATE",
                clientToken=spec["idempotency_key"],
                startedBy=spec["job_id"],
                count=1,
                tags=[
                    {"key": "teamagent-job-id", "value": spec["job_id"]},
                    {
                        "key": "teamagent-payload-sha256",
                        "value": spec["payload_sha256"],
                    },
                    *(
                        [
                            {
                                "key": "teamagent-audit-principal-hash",
                                "value": spec["audit_principal_hash"],
                            }
                        ]
                        if spec["audit_principal_hash"] is not None
                        else []
                    ),
                ],
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
            confirmation_values: dict[str, Any] = {
                ":task": {"S": task_arn},
                ":now": {"N": str(now)},
                ":owner": {"S": owner},
                ":payload": {"S": spec["payload_sha256"]},
            }
            if spec["audit_principal_hash"] is not None:
                confirmation_values[":audit"] = {"S": spec["audit_principal_hash"]}
                confirmation_audit_condition = "audit_principal_hash = :audit"
            else:
                confirmation_audit_condition = "attribute_not_exists(audit_principal_hash)"
            _call(
                "dynamodb",
                "update_item",
                deadline_epoch_s,
                TableName=table,
                Key={"job_id": {"S": spec["job_id"]}},
                UpdateExpression=(
                    "SET dispatched_task_arn = :task, dispatched_at = :now "
                    "REMOVE dispatch_owner, dispatch_lease_expires_at"
                ),
                ConditionExpression=(
                    "dispatch_owner = :owner AND payload_sha256 = :payload AND "
                    f"{confirmation_audit_condition}"
                ),
                ExpressionAttributeValues=confirmation_values,
            )
            started.append(task_arn)
        except Exception:
            try:
                _release_dispatch(table, spec["job_id"], owner, deadline_epoch_s)
            except TimeoutError:
                # No network call is permitted after the immutable deadline.
                pass
            raise
    return {"started": started, "batchItemFailures": []}
