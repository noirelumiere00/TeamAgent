"""Dispatch and finalize one strict roleless Fargate media attempt.

The trusted Lambda owns DynamoDB and S3 authority.  It validates the canonical
SQS envelope, emits exact VersionId-bound GET and checksum-enforcing POST
capabilities, then starts a task with no task role.  On ECS STOPPED it verifies
the exact attempt, completion, object versions, checksums, sizes, types, and
metadata before one fenced terminal DynamoDB transition.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
import zlib
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
_TASK_START_MINIMUM_BUDGET_SECONDS = 90.0
_TERMINAL_WRITE_RESERVE_SECONDS = 15.0
_PRESIGN_SAFETY_SECONDS = 10
_MAX_PRESIGN_SECONDS = 15 * 60
_MAX_CONTROL_BYTES = 768 * 1024
_MAX_COMPLETION_BYTES = 128 * 1024
_S3_STREAM_CHUNK_BYTES = 1024 * 1024
_JOB_ID = re.compile(r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_ARTIFACT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
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
_DEFINITIVE_RUN_TASK_REJECTION_CODES = {
    "AccessDeniedException",
    "ClientException",
    "ClusterNotFoundException",
    "InvalidParameterException",
    "PlatformTaskDefinitionIncompatibilityException",
    "UnsupportedFeatureException",
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


def _definitive_run_task_rejection(exc: Exception) -> bool:
    """Return true only when ECS explicitly says no task was accepted.

    Transport errors, throttling, service errors, and idempotency conflicts are
    deliberately ambiguous: ECS may have accepted the request before the
    caller lost the response.  Those attempts must retain the running fence so
    an SQS retry can reuse the persisted ``clientToken``.
    """

    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code in _DEFINITIVE_RUN_TASK_REJECTION_CODES


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
    config_arguments: dict[str, Any] = {
        "connect_timeout": phase_timeout,
        "read_timeout": phase_timeout,
        "retries": {"mode": "standard", "total_max_attempts": 1},
    }
    if service == "s3":
        config_arguments["signature_version"] = "s3v4"
    return boto3.client(
        service,
        config=Config(**config_arguments),
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


def _checksum_sha256_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def _ddb_string(item: dict[str, Any], name: str) -> str:
    value = item.get(name, {})
    return value.get("S", "") if isinstance(value, dict) else ""


def _ddb_int(item: dict[str, Any], name: str) -> int:
    value = item.get(name, {})
    raw = value.get("N", "") if isinstance(value, dict) else ""
    if not isinstance(raw, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)", raw):
        raise ValueError(f"media job {name} is invalid")
    return int(raw)


def _audit_condition(audit_hash: str | None) -> str:
    return (
        "audit_principal_hash = :audit"
        if audit_hash is not None
        else "attribute_not_exists(audit_principal_hash)"
    )


def _assert_owned_row(
    item: dict[str, Any],
    spec: dict[str, Any],
    *,
    require_request: bool = True,
) -> None:
    if (
        _ddb_string(item, "idempotency_key") != spec["idempotency_key"]
        or _ddb_string(item, "payload_sha256") != spec["payload_sha256"]
        or not hmac.compare_digest(
            _ddb_string(item, "audit_principal_hash"),
            str(spec["audit_principal_hash"] or ""),
        )
        or (require_request and _ddb_string(item, "request_json") != _canonical(spec).decode())
    ):
        raise RuntimeError("media dispatch row ownership mismatch")


def _assert_exact_input_response(ref: dict[str, Any], response: dict[str, Any]) -> None:
    metadata = response.get("Metadata")
    if (
        response.get("VersionId") != ref["version_id"]
        or response.get("ServerSideEncryption") not in {"AES256", "aws:kms"}
        or response.get("ContentLength") != ref["size"]
        or response.get("ContentType") != ref["content_type"]
        or not isinstance(metadata, dict)
        or not hmac.compare_digest(str(metadata.get("sha256") or ""), ref["sha256"])
        or not hmac.compare_digest(
            str(response.get("ChecksumSHA256") or ""),
            _checksum_sha256_b64(ref["sha256"]),
        )
    ):
        raise ValueError("media input object does not match immutable reference")


def _presign_expiry(deadline_epoch_s: int) -> int:
    remaining = int(_remaining(deadline_epoch_s)) - _PRESIGN_SAFETY_SECONDS
    if remaining < 1:
        raise TimeoutError("media presigned capability budget exhausted")
    return min(_MAX_PRESIGN_SECONDS, remaining)


def _operation_output_slots(spec: dict[str, Any], attempt: dict[str, Any]) -> list[dict[str, Any]]:
    operation = spec["operation"]
    kind = operation["kind"]
    prefix = (
        f"{spec['output_prefix']}attempts/{attempt['attempt_version']}/"
        f"{attempt['attempt_id']}/output/"
    )
    slots: list[tuple[str, str, int]] = []
    if kind == "acquire":
        slots.append(("media", "media", int(operation["max_bytes"])))
    elif kind == "tiktok_acquire":
        slots.append(("posts.json", "posts.normalized.json", 16 * 1024 * 1024))
        if operation["artifact_mode"] == "full":
            slots.extend(
                (
                    ("config.json", "config.json", 256 * 1024),
                    ("manifest.json", "videos/manifest.json", 16 * 1024 * 1024),
                )
            )
            for keyword_index in range(len(operation["keywords"])):
                for rank in range(1, int(operation["n_per_kw"]) + 1):
                    pid = f"p{keyword_index + 1:02d}{rank:03d}"
                    slots.append((f"thumb-{pid}", f"thumbs/{pid}.jpg", 8 * 1024 * 1024))
                    slots.append(
                        (
                            f"video-{pid}",
                            f"videos/{pid}.mp4",
                            int(operation["max_video_bytes"]),
                        )
                    )
    elif kind == "proxy":
        slots.append(("proxy", "proxy", int(operation["limit_bytes"])))
    elif kind == "frame":
        for index in range(len(operation["timecodes"])):
            slots.append((f"frame-{index:02d}", f"frame-{index:02d}.jpg", 8 * 1024 * 1024))
        slots.append(("frames.json", "frames.json", 1024 * 1024))
    elif kind == "thumbnail":
        slots.extend(
            (
                ("thumbnail", "thumbnail.jpg", 8 * 1024 * 1024),
                ("thumbnail.json", "thumbnail.json", 256 * 1024),
            )
        )
    elif kind == "slides":
        slots.append(("slides.pptx", "slides.pptx", _MAX_OUTPUT_BYTES))
    elif kind == "proposal_pptx":
        slots.append(("proposal.pptx", "proposal.pptx", _MAX_OUTPUT_BYTES))
    elif kind == "pdf":
        slots.append(("document.pdf", "document.pdf", _MAX_OUTPUT_BYTES))
    else:
        raise ValueError("media operation is invalid")
    if len(slots) > 512 or len({name for name, _key, _size in slots}) != len(slots):
        raise ValueError("media output slot set is invalid")
    return [
        {"name": name, "key": f"{prefix}{relative}", "max_bytes": maximum}
        for name, relative, maximum in slots
    ]


def _required_output_names(spec: dict[str, Any]) -> set[str]:
    kind = spec["operation"]["kind"]
    if kind == "tiktok_acquire":
        names = {"posts.json"}
        if spec["operation"]["artifact_mode"] == "full":
            names.update({"config.json", "manifest.json"})
        return names
    return {
        slot["name"]
        for slot in _operation_output_slots(
            spec,
            {"attempt_version": 1, "attempt_id": "00000000-0000-4000-8000-000000000000"},
        )
    }


def _allowed_content_types(spec: dict[str, Any], name: str) -> set[str]:
    operation = spec["operation"]
    kind = operation["kind"]
    if kind == "acquire" and name == "media":
        return {
            "video/mp4",
            "video/webm",
            "video/quicktime",
            "video/x-matroska",
            "application/octet-stream",
        }
    if kind == "proxy" and name == "proxy":
        return {operation["source"]["content_type"], "video/mp4"}
    if name.startswith("frame-") or name.startswith("thumb-") or name == "thumbnail":
        return {"image/jpeg"}
    if name.endswith(".json"):
        return {"application/json"}
    if name.endswith(".pptx"):
        return {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    if name == "document.pdf":
        return {"application/pdf"}
    raise ValueError("media artifact content type has no approved contract")


def _fixed_object_metadata(spec: dict[str, Any], attempt: dict[str, Any]) -> dict[str, str]:
    return {
        "job-id": spec["job_id"],
        "attempt-id": attempt["attempt_id"],
        "attempt-version": str(attempt["attempt_version"]),
        "capability-sha256": attempt["capability_sha256"],
    }


def _presigned_post(
    s3: Any,
    *,
    bucket: str,
    key: str,
    metadata: dict[str, str],
    expires_s: int,
    max_bytes: int,
) -> dict[str, Any]:
    fields = {
        "x-amz-server-side-encryption": "AES256",
        "x-amz-checksum-algorithm": "SHA256",
        **{f"x-amz-meta-{name}": value for name, value in metadata.items()},
    }
    conditions: list[Any] = [
        {"x-amz-server-side-encryption": "AES256"},
        {"x-amz-checksum-algorithm": "SHA256"},
        *({f"x-amz-meta-{name}": value} for name, value in metadata.items()),
        ["starts-with", "$x-amz-checksum-sha256", ""],
        ["starts-with", "$Content-Type", ""],
        ["content-length-range", 1, max_bytes + 64 * 1024],
    ]
    post = s3.generate_presigned_post(
        Bucket=bucket,
        Key=key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=expires_s,
    )
    if (
        not isinstance(post, dict)
        or set(post) != {"url", "fields"}
        or not isinstance(post["url"], str)
        or not post["url"].startswith("https://")
        or not isinstance(post["fields"], dict)
    ):
        raise RuntimeError("media output presign failed")
    return post


def _build_control(
    spec: dict[str, Any],
    attempt: dict[str, Any],
    *,
    deadline_epoch_s: int,
) -> tuple[bytes, list[dict[str, Any]], str, str]:
    s3 = _client("s3", deadline_epoch_s, reserve_seconds=_TERMINAL_WRITE_RESERVE_SECONDS)
    expires_s = _presign_expiry(deadline_epoch_s)
    inputs: list[dict[str, Any]] = []
    for ref in _operation_s3_refs(spec["operation"]):
        response = s3.head_object(
            Bucket=ref["bucket"],
            Key=ref["key"],
            VersionId=ref["version_id"],
            ChecksumMode="ENABLED",
        )
        _assert_exact_input_response(ref, response)
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": ref["bucket"],
                "Key": ref["key"],
                "VersionId": ref["version_id"],
            },
            ExpiresIn=expires_s,
        )
        if not isinstance(url, str) or not url.startswith("https://"):
            raise RuntimeError("media input presign failed")
        inputs.append({"ref": ref, "get_url": url})
    slots = _operation_output_slots(spec, attempt)
    metadata = _fixed_object_metadata(spec, attempt)
    outputs = [
        {
            **slot,
            "post": _presigned_post(
                s3,
                bucket=spec["output_bucket"],
                key=slot["key"],
                metadata=metadata,
                expires_s=expires_s,
                max_bytes=slot["max_bytes"],
            ),
        }
        for slot in slots
    ]
    completion_key = (
        f"{spec['output_prefix']}attempts/{attempt['attempt_version']}/"
        f"{attempt['attempt_id']}/_COMPLETION.json"
    )
    control = {
        "schema_version": "1",
        "request": spec,
        "attempt_id": attempt["attempt_id"],
        "attempt_version": attempt["attempt_version"],
        "capability_secret": attempt["capability_secret"],
        "inputs": inputs,
        "outputs": outputs,
        "completion": {
            "key": completion_key,
            "max_bytes": _MAX_COMPLETION_BYTES,
            "post": _presigned_post(
                s3,
                bucket=spec["output_bucket"],
                key=completion_key,
                metadata=metadata,
                expires_s=expires_s,
                max_bytes=_MAX_COMPLETION_BYTES,
            ),
        },
    }
    encoded = _canonical(control)
    if len(encoded) > _MAX_CONTROL_BYTES:
        raise ValueError("media capability control exceeds bounded size")
    control_sha256 = hashlib.sha256(encoded).hexdigest()
    compressed = base64.b64encode(zlib.compress(encoded, level=9))
    environment_file = b"MEDIA_CONTROL_ZLIB_B64=" + compressed + b"\n"
    if len(environment_file) > _MAX_CONTROL_BYTES:
        raise ValueError("media capability environment exceeds bounded size")
    return environment_file, slots, completion_key, control_sha256


def _delete_exact_version(
    *,
    bucket: str,
    key: str,
    version_id: str,
    deadline_epoch_s: int,
) -> None:
    _call(
        "s3",
        "delete_object",
        deadline_epoch_s,
        Bucket=bucket,
        Key=key,
        VersionId=version_id,
    )


def _resume_attempt(item: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "attempt_id": _ddb_string(item, "attempt_id"),
        "attempt_version": _ddb_int(item, "version"),
        "capability_sha256": _ddb_string(item, "capability_sha256"),
        "control_key": _ddb_string(item, "control_key"),
        "control_version_id": _ddb_string(item, "control_version_id"),
        "control_sha256": _ddb_string(item, "control_sha256"),
        "completion_key": _ddb_string(item, "completion_key"),
        "dispatch_client_token": _ddb_string(item, "dispatch_client_token"),
    }
    if (
        not _ATTEMPT_ID.fullmatch(attempt["attempt_id"])
        or attempt["attempt_version"] < 1
        or not _SHA256.fullmatch(attempt["capability_sha256"])
        or not _SHA256.fullmatch(attempt["control_sha256"])
        or not _SHA256.fullmatch(attempt["dispatch_client_token"])
        or not attempt["control_version_id"]
        or attempt["control_version_id"] == "null"
        or attempt["control_key"] != f"{spec['output_prefix']}control/{attempt['attempt_id']}.env"
        or attempt["completion_key"]
        != (
            f"{spec['output_prefix']}attempts/{attempt['attempt_version']}/"
            f"{attempt['attempt_id']}/_COMPLETION.json"
        )
    ):
        raise RuntimeError("media persisted dispatch capability is invalid")
    return attempt


def _prepare_attempt(
    table: str,
    bucket: str,
    spec: dict[str, Any],
    owner: str,
    now: int,
) -> dict[str, Any] | None:
    deadline_epoch_s = int(spec["deadline_epoch_s"])
    item = _call(
        "dynamodb",
        "get_item",
        deadline_epoch_s,
        TableName=table,
        Key={"job_id": {"S": spec["job_id"]}},
        ConsistentRead=True,
    ).get("Item", {})
    _assert_owned_row(item, spec)
    status = _ddb_string(item, "status")
    if status in {"done", "failed"}:
        return None
    if status == "running":
        if _ddb_string(item, "dispatched_task_arn"):
            return None
        return _resume_attempt(item, spec)
    if status != "queued":
        raise RuntimeError("media job row has invalid status")
    previous_version = _ddb_int(item, "version")
    attempt_id = str(uuid.uuid4())
    attempt_version = previous_version + 1
    capability_secret = os.urandom(32).hex()
    capability_sha256 = hashlib.sha256(capability_secret.encode("ascii")).hexdigest()
    client_token = hashlib.sha256(
        f"{spec['job_id']}:{attempt_version}:{attempt_id}".encode("ascii")
    ).hexdigest()
    attempt: dict[str, Any] = {
        "attempt_id": attempt_id,
        "attempt_version": attempt_version,
        "capability_secret": capability_secret,
        "capability_sha256": capability_sha256,
        "dispatch_client_token": client_token,
    }
    environment_file, _slots, completion_key, control_sha256 = _build_control(
        spec,
        attempt,
        deadline_epoch_s=deadline_epoch_s,
    )
    control_object_sha256 = hashlib.sha256(environment_file).hexdigest()
    control_key = f"{spec['output_prefix']}control/{attempt_id}.env"
    put_response = _call(
        "s3",
        "put_object",
        deadline_epoch_s,
        reserve_seconds=_TERMINAL_WRITE_RESERVE_SECONDS,
        Bucket=bucket,
        Key=control_key,
        Body=environment_file,
        ContentType="text/plain",
        ServerSideEncryption="AES256",
        ChecksumSHA256=_checksum_sha256_b64(control_object_sha256),
        Metadata={
            "job-id": spec["job_id"],
            "attempt-id": attempt_id,
            "control-sha256": control_sha256,
        },
        IfNoneMatch="*",
    )
    control_version_id = str(put_response.get("VersionId") or "")
    if not control_version_id or control_version_id == "null":
        raise RuntimeError("media control object is not version-bound")
    try:
        control_head = _call(
            "s3",
            "head_object",
            deadline_epoch_s,
            reserve_seconds=_TERMINAL_WRITE_RESERVE_SECONDS,
            Bucket=bucket,
            Key=control_key,
            VersionId=control_version_id,
            ChecksumMode="ENABLED",
        )
        if (
            control_head.get("VersionId") != control_version_id
            or control_head.get("ServerSideEncryption") != "AES256"
            or control_head.get("ContentLength") != len(environment_file)
            or control_head.get("ContentType") != "text/plain"
            or not hmac.compare_digest(
                str(control_head.get("ChecksumSHA256") or ""),
                _checksum_sha256_b64(control_object_sha256),
            )
            or not isinstance(control_head.get("Metadata"), dict)
            or control_head["Metadata"].get("job-id") != spec["job_id"]
            or control_head["Metadata"].get("attempt-id") != attempt_id
            or control_head["Metadata"].get("control-sha256") != control_sha256
        ):
            raise RuntimeError("media control object integrity verification failed")
    except Exception:
        try:
            _delete_exact_version(
                bucket=bucket,
                key=control_key,
                version_id=control_version_id,
                deadline_epoch_s=deadline_epoch_s,
            )
        except Exception:
            pass
        raise
    attempt.update(
        {
            "control_key": control_key,
            "control_version_id": control_version_id,
            "control_sha256": control_sha256,
            "completion_key": completion_key,
        }
    )
    audit_hash = spec["audit_principal_hash"]
    values: dict[str, Any] = {
        ":queued": {"S": "queued"},
        ":running": {"S": "running"},
        ":payload": {"S": spec["payload_sha256"]},
        ":previous": {"N": str(previous_version)},
        ":version": {"N": str(attempt_version)},
        ":attempt": {"S": attempt_id},
        ":capability": {"S": capability_sha256},
        ":owner": {"S": owner},
        ":lease": {"N": str(spec["deadline_epoch_s"] + 60)},
        ":dispatch_lease": {"N": str(now + 120)},
        ":now": {"N": str(now)},
        ":control_key": {"S": control_key},
        ":control_version": {"S": control_version_id},
        ":control_sha": {"S": control_sha256},
        ":completion_key": {"S": completion_key},
        ":client_token": {"S": client_token},
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
                "SET #status = :running, #version = :version, attempt_id = :attempt, "
                "capability_sha256 = :capability, lease_owner = :owner, "
                "lease_expires_at = :lease, dispatch_owner = :owner, "
                "dispatch_lease_expires_at = :dispatch_lease, dispatch_started_at = :now, "
                "updated_at = :now, control_key = :control_key, "
                "control_version_id = :control_version, control_sha256 = :control_sha, "
                "completion_key = :completion_key, dispatch_client_token = :client_token "
                "REMOVE dispatched_task_arn, dispatched_at"
            ),
            ConditionExpression=(
                "#status = :queued AND #version = :previous AND "
                "payload_sha256 = :payload AND "
                f"{_audit_condition(audit_hash)} AND "
                "attribute_not_exists(dispatched_task_arn)"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues=values,
        )
    except Exception:
        try:
            _delete_exact_version(
                bucket=bucket,
                key=control_key,
                version_id=control_version_id,
                deadline_epoch_s=deadline_epoch_s,
            )
        except Exception:
            pass
        raise
    attempt.pop("capability_secret")
    return attempt


def _task_identity_environment(
    spec: dict[str, Any],
    attempt: dict[str, Any],
) -> list[dict[str, str]]:
    environment = [
        {"name": "MEDIA_JOB_ID", "value": spec["job_id"]},
        {"name": "MEDIA_JOB_PAYLOAD_SHA256", "value": spec["payload_sha256"]},
        {"name": "MEDIA_JOB_DEADLINE_EPOCH_S", "value": str(spec["deadline_epoch_s"])},
        {"name": "MEDIA_ATTEMPT_ID", "value": attempt["attempt_id"]},
        {"name": "MEDIA_ATTEMPT_VERSION", "value": str(attempt["attempt_version"])},
        {"name": "MEDIA_CAPABILITY_SHA256", "value": attempt["capability_sha256"]},
        {"name": "MEDIA_CONTROL_SHA256", "value": attempt["control_sha256"]},
    ]
    if spec["audit_principal_hash"] is not None:
        environment.append(
            {
                "name": "MEDIA_JOB_AUDIT_PRINCIPAL_HASH",
                "value": spec["audit_principal_hash"],
            }
        )
    return environment


def _task_overrides(
    container: str,
    spec: dict[str, Any],
    attempt: dict[str, Any],
    *,
    bucket: str,
) -> dict[str, Any]:
    overrides = {
        "containerOverrides": [
            {
                "name": container,
                "environment": _task_identity_environment(spec, attempt),
                "environmentFiles": [
                    {
                        "type": "s3",
                        "value": f"arn:aws:s3:::{bucket}/{attempt['control_key']}",
                    }
                ],
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


def _override_environment_file(
    detail: dict[str, Any],
    expected_container: str,
) -> str | None:
    overrides = detail.get("overrides")
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
    files = matches[0].get("environmentFiles")
    # ECS accepts environmentFiles in RunTask overrides, but the documented
    # EventBridge task-state shape does not promise that field.  When AWS
    # includes it, bind it exactly; otherwise the DDB attempt and the seven
    # explicit identity hashes remain authoritative.
    if files is None:
        return None
    if (
        not isinstance(files, list)
        or len(files) != 1
        or not isinstance(files[0], dict)
        or set(files[0]) != {"type", "value"}
        or files[0].get("type") != "s3"
        or not isinstance(files[0].get("value"), str)
    ):
        raise ValueError("ECS STOPPED environment file is invalid")
    value = files[0]["value"]
    if not isinstance(value, str):
        raise ValueError("ECS STOPPED environment file is invalid")
    return value


def _stopped_identity(
    detail: dict[str, Any],
    *,
    expected_container: str,
) -> dict[str, Any]:
    tags = _tag_values(detail)
    overrides = _override_values(detail, expected_container)
    values: dict[str, Any] = {
        "job_id": overrides.get("MEDIA_JOB_ID"),
        "payload_sha256": overrides.get("MEDIA_JOB_PAYLOAD_SHA256"),
        "deadline_epoch_s": overrides.get("MEDIA_JOB_DEADLINE_EPOCH_S"),
        "attempt_id": overrides.get("MEDIA_ATTEMPT_ID"),
        "attempt_version": overrides.get("MEDIA_ATTEMPT_VERSION"),
        "capability_sha256": overrides.get("MEDIA_CAPABILITY_SHA256"),
        "control_sha256": overrides.get("MEDIA_CONTROL_SHA256"),
        "audit_principal_hash": overrides.get("MEDIA_JOB_AUDIT_PRINCIPAL_HASH"),
        "control_arn": _override_environment_file(detail, expected_container),
    }
    if (
        not isinstance(values["job_id"], str)
        or not _JOB_ID.fullmatch(values["job_id"])
        or not isinstance(values["payload_sha256"], str)
        or not _SHA256.fullmatch(values["payload_sha256"])
        or not isinstance(values["attempt_id"], str)
        or not _ATTEMPT_ID.fullmatch(values["attempt_id"])
        or not isinstance(values["capability_sha256"], str)
        or not _SHA256.fullmatch(values["capability_sha256"])
        or not isinstance(values["control_sha256"], str)
        or not _SHA256.fullmatch(values["control_sha256"])
        or not isinstance(values["deadline_epoch_s"], str)
        or not re.fullmatch(r"[1-9][0-9]{0,11}", values["deadline_epoch_s"])
        or not isinstance(values["attempt_version"], str)
        or not re.fullmatch(r"[1-9][0-9]{0,11}", values["attempt_version"])
        or detail.get("startedBy") != values["job_id"]
    ):
        raise ValueError("ECS STOPPED task identity is invalid")
    values["deadline_epoch_s"] = int(values["deadline_epoch_s"])
    values["attempt_version"] = int(values["attempt_version"])
    audit_hash = values["audit_principal_hash"]
    if audit_hash is not None and (
        not isinstance(audit_hash, str) or not _SHA256.fullmatch(audit_hash)
    ):
        raise ValueError("ECS STOPPED audit identity is invalid")
    tag_contract = {
        "teamagent-job-id": values["job_id"],
        "teamagent-payload-sha256": values["payload_sha256"],
        "teamagent-attempt-id": values["attempt_id"],
        "teamagent-attempt-version": str(values["attempt_version"]),
        "teamagent-capability-sha256": values["capability_sha256"],
        "teamagent-control-sha256": values["control_sha256"],
        **({"teamagent-audit-principal-hash": audit_hash} if audit_hash is not None else {}),
    }
    for key, expected in tag_contract.items():
        if key in tags and tags[key] != expected:
            raise ValueError("ECS STOPPED identity disagrees with task tags")
    return values


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


def _task_exit_code(detail: dict[str, Any], expected_container: str) -> int | None:
    containers = detail.get("containers")
    if not isinstance(containers, list):
        return None
    matches = [
        value
        for value in containers
        if isinstance(value, dict) and value.get("name") == expected_container
    ]
    if len(matches) != 1:
        return None
    exit_code = matches[0].get("exitCode")
    if type(exit_code) is not int:
        return None
    return exit_code


def _single_object_version(
    s3: Any,
    *,
    bucket: str,
    key: str,
) -> str | None:
    response = s3.list_object_versions(Bucket=bucket, Prefix=key, MaxKeys=1000)
    if response.get("IsTruncated"):
        raise ValueError("media output key has too many versions")
    versions = [
        value
        for value in response.get("Versions", [])
        if isinstance(value, dict) and value.get("Key") == key
    ]
    markers = [
        value
        for value in response.get("DeleteMarkers", [])
        if isinstance(value, dict) and value.get("Key") == key
    ]
    if not versions and not markers:
        return None
    if len(versions) != 1 or markers:
        raise ValueError("media output key is not a single immutable version")
    version_id = str(versions[0].get("VersionId") or "")
    if not version_id or version_id == "null":
        raise ValueError("media output version is invalid")
    return version_id


def _read_bounded_body(response: dict[str, Any], maximum: int) -> bytes:
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise ValueError("media S3 response body is invalid")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = body.read(min(_S3_STREAM_CHUNK_BYTES, maximum + 1 - total))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ValueError("media S3 response body is invalid")
            total += len(chunk)
            if total > maximum:
                raise ValueError("media S3 response exceeds bounded size")
            chunks.append(chunk)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return b"".join(chunks)


def _assert_attempt_metadata(
    metadata: Any,
    spec: dict[str, Any],
    attempt: dict[str, Any],
) -> None:
    expected = _fixed_object_metadata(spec, attempt)
    if not isinstance(metadata, dict) or any(
        not hmac.compare_digest(str(metadata.get(key) or ""), value)
        for key, value in expected.items()
    ):
        raise ValueError("media output attempt metadata is invalid")


def _load_completion(
    s3: Any,
    *,
    bucket: str,
    spec: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any] | None:
    key = attempt["completion_key"]
    version_id = _single_object_version(s3, bucket=bucket, key=key)
    if version_id is None:
        return None
    response = s3.get_object(
        Bucket=bucket,
        Key=key,
        VersionId=version_id,
        ChecksumMode="ENABLED",
    )
    length = response.get("ContentLength")
    checksum = str(response.get("ChecksumSHA256") or "")
    if (
        response.get("VersionId") != version_id
        or response.get("ServerSideEncryption") != "AES256"
        or response.get("ContentType") != "application/json"
        or type(length) is not int
        or not 1 <= length <= _MAX_COMPLETION_BYTES
        or not checksum
    ):
        raise ValueError("media completion object headers are invalid")
    _assert_attempt_metadata(response.get("Metadata"), spec, attempt)
    encoded = _read_bounded_body(response, _MAX_COMPLETION_BYTES)
    if len(encoded) != length or not hmac.compare_digest(
        _checksum_sha256_b64(hashlib.sha256(encoded).hexdigest()),
        checksum,
    ):
        raise ValueError("media completion object checksum is invalid")
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("media completion JSON is invalid") from exc
    if not isinstance(value, dict) or _canonical(value) != encoded:
        raise ValueError("media completion JSON is not canonical")
    value["_completion_version_id"] = version_id
    return value


def _artifact_manifest_sha256(artifacts: list[dict[str, Any]]) -> str:
    rows = sorted(
        (
            {
                "name": artifact["name"],
                "bucket": artifact["object"]["bucket"],
                "key": artifact["object"]["key"],
                "version_id": artifact["object"]["version_id"],
                "sha256": artifact["object"]["sha256"],
                "size": artifact["object"]["size"],
                "content_type": artifact["object"]["content_type"],
            }
            for artifact in artifacts
        ),
        key=lambda row: (row["name"], row["key"]),
    )
    return hashlib.sha256(_canonical(rows)).hexdigest()


def _validate_completion_result(
    completion: dict[str, Any],
    *,
    s3: Any,
    bucket: str,
    spec: dict[str, Any],
    attempt: dict[str, Any],
    exit_code: int | None,
) -> tuple[dict[str, Any], str | None]:
    completion_version = completion.pop("_completion_version_id", None)
    if (
        set(completion)
        != {
            "schema_version",
            "job_id",
            "payload_sha256",
            "attempt_id",
            "attempt_version",
            "capability_secret",
            "result",
        }
        or completion["schema_version"] != "1"
        or completion["job_id"] != spec["job_id"]
        or completion["payload_sha256"] != spec["payload_sha256"]
        or completion["attempt_id"] != attempt["attempt_id"]
        or completion["attempt_version"] != attempt["attempt_version"]
        or not isinstance(completion["capability_secret"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", completion["capability_secret"])
        or not hmac.compare_digest(
            hashlib.sha256(completion["capability_secret"].encode("ascii")).hexdigest(),
            attempt["capability_sha256"],
        )
        or not isinstance(completion_version, str)
    ):
        raise ValueError("media completion identity is invalid")
    result = _exact_keys(
        completion["result"],
        {"schema_version", "job_id", "status", "artifacts", "metadata", "error_code"},
        "media result",
    )
    if result["schema_version"] != "1" or result["job_id"] != spec["job_id"]:
        raise ValueError("media result identity is invalid")
    status = result["status"]
    artifacts = result["artifacts"]
    metadata = result["metadata"]
    error_code = result["error_code"]
    if (
        status not in {"done", "failed"}
        or not isinstance(artifacts, list)
        or len(artifacts) > 512
        or not isinstance(metadata, dict)
        or len(_canonical(metadata)) > 32 * 1024
    ):
        raise ValueError("media result shape is invalid")
    if status == "failed":
        if (
            artifacts
            or not isinstance(error_code, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error_code)
        ):
            raise ValueError("media failed result is invalid")
        return result, None
    if exit_code != 0 or error_code is not None or not artifacts:
        raise ValueError("media done result disagrees with task exit")
    slots = {slot["name"]: slot for slot in _operation_output_slots(spec, attempt)}
    names: set[str] = set()
    keys: set[str] = set()
    for raw in artifacts:
        artifact = _exact_keys(raw, {"name", "object"}, "media artifact")
        name = artifact["name"]
        if (
            not isinstance(name, str)
            or not _ARTIFACT_NAME.fullmatch(name)
            or name in names
            or name not in slots
        ):
            raise ValueError("media artifact name is invalid")
        ref = _validate_s3_ref(artifact["object"])
        slot = slots[name]
        if (
            ref["bucket"] != bucket
            or ref["key"] != slot["key"]
            or ref["key"] in keys
            or not 1 <= ref["size"] <= slot["max_bytes"]
            or ref["content_type"] not in _allowed_content_types(spec, name)
        ):
            raise ValueError("media artifact is outside its exact output slot")
        version_id = _single_object_version(s3, bucket=bucket, key=ref["key"])
        if version_id != ref["version_id"]:
            raise ValueError("media artifact version is not exact")
        response = s3.head_object(
            Bucket=bucket,
            Key=ref["key"],
            VersionId=ref["version_id"],
            ChecksumMode="ENABLED",
        )
        if (
            response.get("VersionId") != ref["version_id"]
            or response.get("ServerSideEncryption") != "AES256"
            or response.get("ContentLength") != ref["size"]
            or response.get("ContentType") != ref["content_type"]
            or not hmac.compare_digest(
                str(response.get("ChecksumSHA256") or ""),
                _checksum_sha256_b64(ref["sha256"]),
            )
        ):
            raise ValueError("media artifact headers do not match completion")
        _assert_attempt_metadata(response.get("Metadata"), spec, attempt)
        names.add(name)
        keys.add(ref["key"])
    if not _required_output_names(spec).issubset(names):
        raise ValueError("media result is missing required output slots")
    return result, _artifact_manifest_sha256(artifacts)


def _failure_result(
    spec: dict[str, Any],
    *,
    code: str,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "job_id": spec["job_id"],
        "status": "failed",
        "artifacts": [],
        "metadata": {"reconciler": "ecs-stopped", "ecs": diagnostics},
        "error_code": code,
    }


def _commit_terminal(
    *,
    table: str,
    spec: dict[str, Any],
    attempt: dict[str, Any],
    result: dict[str, Any],
    manifest_sha256: str | None,
    task_arn: str | None,
    now: int,
    deadline_epoch_s: int,
) -> bool:
    status = result["status"]
    audit_hash = spec["audit_principal_hash"]
    values: dict[str, Any] = {
        ":running": {"S": "running"},
        ":status": {"S": status},
        ":payload": {"S": spec["payload_sha256"]},
        ":attempt": {"S": attempt["attempt_id"]},
        ":version": {"N": str(attempt["attempt_version"])},
        ":capability": {"S": attempt["capability_sha256"]},
        ":control_sha": {"S": attempt["control_sha256"]},
        ":detail": {"S": _canonical(result).decode("utf-8")},
        ":now": {"N": str(now)},
        ":cleanup": {"N": str(now + _MAX_ARTIFACT_RETENTION_SECONDS)},
        ":one": {"N": "1"},
    }
    if audit_hash is not None:
        values[":audit"] = {"S": audit_hash}
    task_condition: str
    task_set = ""
    if task_arn is None:
        task_condition = "attribute_not_exists(dispatched_task_arn)"
    else:
        values[":task"] = {"S": task_arn}
        task_condition = (
            "(attribute_not_exists(dispatched_task_arn) OR dispatched_task_arn = :task)"
        )
        task_set = ", dispatched_task_arn = :task"
    manifest_set = ""
    manifest_remove = ", artifact_manifest_sha256"
    if manifest_sha256 is not None:
        values[":manifest"] = {"S": manifest_sha256}
        manifest_set = ", artifact_manifest_sha256 = :manifest"
        manifest_remove = ""
    try:
        _call(
            "dynamodb",
            "update_item",
            deadline_epoch_s,
            TableName=table,
            Key={"job_id": {"S": spec["job_id"]}},
            UpdateExpression=(
                "SET #status = :status, detail = :detail, updated_at = :now, "
                "cleanup_at = if_not_exists(hard_cleanup_at, :cleanup)"
                f"{task_set}{manifest_set} "
                "REMOVE dispatch_owner, dispatch_lease_expires_at, lease_owner, "
                f"lease_expires_at{manifest_remove} ADD #version :one"
            ),
            ConditionExpression=(
                "#status = :running AND #version = :version AND "
                "payload_sha256 = :payload AND attempt_id = :attempt AND "
                "capability_sha256 = :capability AND control_sha256 = :control_sha AND "
                f"{task_condition} AND {_audit_condition(audit_hash)}"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
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
        _assert_owned_row(item, spec)
        if _ddb_string(item, "status") in {"done", "failed"}:
            return False
        raise RuntimeError("media terminal transition lost exact attempt fence") from exc


def _load_persisted_spec(
    item: dict[str, Any],
    *,
    bucket: str,
    max_artifact_ttl_s: int,
) -> dict[str, Any]:
    raw = _ddb_string(item, "request_json")
    try:
        candidate = json.loads(raw)
        created = int(candidate["created_at_epoch_s"])
        deadline = int(candidate["deadline_epoch_s"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("persisted media request is invalid") from exc
    validation_now = max(created, deadline - 1)
    return _validate_envelope(
        raw,
        expected_bucket=bucket,
        now=validation_now,
        max_artifact_ttl_s=max_artifact_ttl_s,
    )


def _reconcile_stopped(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("source") != "aws.ecs" or event.get("detail-type") != "ECS Task State Change":
        raise ValueError("ECS STOPPED event identity is invalid")
    detail = event.get("detail")
    if not isinstance(detail, dict) or detail.get("lastStatus") != "STOPPED":
        raise ValueError("ECS STOPPED event status is invalid")
    cluster = os.environ["CLUSTER_ARN"]
    taskdef = os.environ["TASKDEF_ARN"]
    table = os.environ["JOBS_TABLE"]
    bucket = os.environ["JOB_BUCKET"]
    container = os.environ.get("CONTAINER", "media-worker")
    max_artifact_ttl_s = int(os.environ["MEDIA_ARTIFACT_TTL_SECONDS"])
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
    identity = _stopped_identity(detail, expected_container=container)
    deadline = _event_deadline(context)
    item = _call(
        "dynamodb",
        "get_item",
        deadline,
        TableName=table,
        Key={"job_id": {"S": identity["job_id"]}},
        ConsistentRead=True,
    ).get("Item", {})
    status = _ddb_string(item, "status")
    if status in {"done", "failed"}:
        return {
            "reconciled": False,
            "job_id": identity["job_id"],
            "status": status,
        }
    spec = _load_persisted_spec(
        item,
        bucket=bucket,
        max_artifact_ttl_s=max_artifact_ttl_s,
    )
    _assert_owned_row(item, spec)
    attempt = _resume_attempt(item, spec)
    if (
        status != "running"
        or identity["payload_sha256"] != spec["payload_sha256"]
        or identity["deadline_epoch_s"] != spec["deadline_epoch_s"]
        or identity["attempt_id"] != attempt["attempt_id"]
        or identity["attempt_version"] != attempt["attempt_version"]
        or identity["capability_sha256"] != attempt["capability_sha256"]
        or identity["control_sha256"] != attempt["control_sha256"]
        or not hmac.compare_digest(
            str(identity["audit_principal_hash"] or ""),
            str(spec["audit_principal_hash"] or ""),
        )
        or (
            identity["control_arn"] is not None
            and identity["control_arn"] != f"arn:aws:s3:::{bucket}/{attempt['control_key']}"
        )
        or _ddb_string(item, "dispatched_task_arn") not in {"", task_arn}
    ):
        raise RuntimeError("ECS STOPPED row ownership mismatch")
    s3 = _client("s3", deadline)
    exit_code = _task_exit_code(detail, container)
    diagnostics = _stopped_diagnostics(detail)
    completion: dict[str, Any] | None = None
    completion_version_id: str | None = None
    result: dict[str, Any]
    manifest_sha256: str | None = None
    try:
        completion = _load_completion(
            s3,
            bucket=bucket,
            spec=spec,
            attempt=attempt,
        )
        if completion is None:
            result = _failure_result(
                spec,
                code="MEDIA_ECS_TASK_STOPPED",
                diagnostics=diagnostics,
            )
        else:
            raw_completion_version = completion.get("_completion_version_id")
            if isinstance(raw_completion_version, str):
                completion_version_id = raw_completion_version
            result, manifest_sha256 = _validate_completion_result(
                completion,
                s3=s3,
                bucket=bucket,
                spec=spec,
                attempt=attempt,
                exit_code=exit_code,
            )
            if result["status"] == "failed":
                manifest_sha256 = None
    except ValueError:
        result = _failure_result(
            spec,
            code="MEDIA_COMPLETION_INVALID",
            diagnostics=diagnostics,
        )
        manifest_sha256 = None
    now = int(time.time())
    changed = _commit_terminal(
        table=table,
        spec=spec,
        attempt=attempt,
        result=result,
        manifest_sha256=manifest_sha256,
        task_arn=task_arn,
        now=now,
        deadline_epoch_s=deadline,
    )
    if changed:
        if completion_version_id is not None:
            try:
                _delete_exact_version(
                    bucket=bucket,
                    key=attempt["completion_key"],
                    version_id=completion_version_id,
                    deadline_epoch_s=deadline,
                )
            except Exception:
                pass
        try:
            _delete_exact_version(
                bucket=bucket,
                key=attempt["control_key"],
                version_id=attempt["control_version_id"],
                deadline_epoch_s=deadline,
            )
        except Exception:
            # The secret is bounded by the presigned expiry and the lifecycle
            # janitor; terminal state must not be rolled back by cleanup failure.
            pass
    return {
        "reconciled": changed,
        "job_id": spec["job_id"],
        "status": result["status"],
    }


def _attempt_tags(spec: dict[str, Any], attempt: dict[str, Any]) -> list[dict[str, str]]:
    tags = [
        {"key": "teamagent-job-id", "value": spec["job_id"]},
        {"key": "teamagent-payload-sha256", "value": spec["payload_sha256"]},
        {"key": "teamagent-attempt-id", "value": attempt["attempt_id"]},
        {"key": "teamagent-attempt-version", "value": str(attempt["attempt_version"])},
        {"key": "teamagent-capability-sha256", "value": attempt["capability_sha256"]},
        {"key": "teamagent-control-sha256", "value": attempt["control_sha256"]},
    ]
    if spec["audit_principal_hash"] is not None:
        tags.append(
            {
                "key": "teamagent-audit-principal-hash",
                "value": spec["audit_principal_hash"],
            }
        )
    return tags


def _confirm_task(
    *,
    table: str,
    spec: dict[str, Any],
    attempt: dict[str, Any],
    task_arn: str,
    now: int,
) -> None:
    values: dict[str, Any] = {
        ":running": {"S": "running"},
        ":task": {"S": task_arn},
        ":now": {"N": str(now)},
        ":attempt": {"S": attempt["attempt_id"]},
        ":version": {"N": str(attempt["attempt_version"])},
        ":payload": {"S": spec["payload_sha256"]},
        ":capability": {"S": attempt["capability_sha256"]},
    }
    if spec["audit_principal_hash"] is not None:
        values[":audit"] = {"S": spec["audit_principal_hash"]}
    try:
        _call(
            "dynamodb",
            "update_item",
            int(spec["deadline_epoch_s"]),
            TableName=table,
            Key={"job_id": {"S": spec["job_id"]}},
            UpdateExpression=(
                "SET dispatched_task_arn = :task, dispatched_at = :now "
                "REMOVE dispatch_owner, dispatch_lease_expires_at"
            ),
            ConditionExpression=(
                "#status = :running AND #version = :version AND "
                "attempt_id = :attempt AND payload_sha256 = :payload AND "
                "capability_sha256 = :capability AND "
                "(attribute_not_exists(dispatched_task_arn) OR "
                "dispatched_task_arn = :task) AND "
                f"{_audit_condition(spec['audit_principal_hash'])}"
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
            int(spec["deadline_epoch_s"]),
            TableName=table,
            Key={"job_id": {"S": spec["job_id"]}},
            ConsistentRead=True,
        ).get("Item", {})
        if _ddb_string(item, "status") in {"done", "failed"} or (
            _ddb_string(item, "dispatched_task_arn") == task_arn
            and _ddb_string(item, "attempt_id") == attempt["attempt_id"]
        ):
            return
        raise RuntimeError("media task confirmation lost exact attempt fence") from exc


def _commit_unlaunched_failure(
    *,
    table: str,
    spec: dict[str, Any],
    attempt: dict[str, Any],
    deadline_epoch_s: int,
) -> None:
    """Best-effort terminal write after a definitive pre-launch rejection."""

    try:
        failure = _failure_result(
            spec,
            code="MEDIA_DISPATCH_START_FAILED",
            diagnostics={},
        )
        _commit_terminal(
            table=table,
            spec=spec,
            attempt=attempt,
            result=failure,
            manifest_sha256=None,
            task_arn=None,
            now=int(time.time()),
            deadline_epoch_s=deadline_epoch_s,
        )
    except Exception:
        # Preserve the original dispatch exception.  A later retry/finalizer
        # still has the exact attempt fence and can reconcile it.
        pass


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

    started: list[str] = []
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
        try:
            attempt = _prepare_attempt(table, bucket, spec, owner, now)
        except TimeoutError as exc:
            if failure_target is not None and _remaining(deadline_epoch_s) > 0:
                _mark_failed(
                    table,
                    failure_target,
                    "MEDIA_JOB_DEADLINE_EXCEEDED",
                    int(time.time()),
                )
            raise TimeoutError("media envelope deadline exceeded before task launch") from exc
        if attempt is None:
            continue
        if _remaining(deadline_epoch_s) <= _TASK_START_MINIMUM_BUDGET_SECONDS:
            _commit_unlaunched_failure(
                table=table,
                spec=spec,
                attempt=attempt,
                deadline_epoch_s=deadline_epoch_s,
            )
            raise TimeoutError("media envelope deadline exceeded before task launch")
        try:
            response = _call(
                "ecs",
                "run_task",
                deadline_epoch_s,
                reserve_seconds=_TERMINAL_WRITE_RESERVE_SECONDS,
                cluster=cluster,
                taskDefinition=taskdef,
                launchType="FARGATE",
                platformVersion="1.4.0",
                clientToken=attempt["dispatch_client_token"],
                startedBy=spec["job_id"],
                count=1,
                enableExecuteCommand=False,
                propagateTags="TASK_DEFINITION",
                tags=_attempt_tags(spec, attempt),
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnets,
                        "securityGroups": [security_group],
                        "assignPublicIp": "ENABLED",
                    }
                },
                overrides=_task_overrides(
                    container,
                    spec,
                    attempt,
                    bucket=bucket,
                ),
            )
        except Exception as exc:
            if _definitive_run_task_rejection(exc):
                _commit_unlaunched_failure(
                    table=table,
                    spec=spec,
                    attempt=attempt,
                    deadline_epoch_s=deadline_epoch_s,
                )
            # An unclassified SDK/transport error is ambiguous.  Do not mark
            # the row failed: SQS must retry the same persisted client token.
            raise
        failures = response.get("failures", [])
        tasks = response.get("tasks", [])
        if failures or len(tasks) != 1:
            if not tasks:
                _commit_unlaunched_failure(
                    table=table,
                    spec=spec,
                    attempt=attempt,
                    deadline_epoch_s=deadline_epoch_s,
                )
            raise RuntimeError(f"run_task did not start exactly one task: {failures}")
        task_arn = str(tasks[0].get("taskArn") or "")
        if not task_arn.startswith("arn:") or ":task/" not in task_arn:
            # A task object exists, so malformed confirmation is still
            # ambiguous.  Leave the fence running for STOPPED/retry recovery.
            raise RuntimeError("run_task returned an invalid task ARN")
        _confirm_task(
            table=table,
            spec=spec,
            attempt=attempt,
            task_arn=task_arn,
            now=int(time.time()),
        )
        started.append(task_arn)
    return {"started": started, "batchItemFailures": []}
