"""Core側の bounded media job submit/poll/download/cleanup adapter。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

import structlog

from teamagent.media.contracts import (
    ARTIFACT_RETENTION_SECONDS,
    DDB_RETENTION_GRACE_SECONDS,
    MAX_DEADLINE_SECONDS,
    MAX_INPUT_BYTES,
    MAX_PRESIGNED_URL_SECONDS,
    AcquireOperation,
    FrameOperation,
    MediaJobRequest,
    MediaJobResult,
    MediaOperation,
    PdfOperation,
    ProposalEvidence,
    ProposalPptxOperation,
    ProxyOperation,
    S3ObjectRef,
    SlidesOperation,
    ThumbnailOperation,
    TikTokAcquireOperation,
    TikTokClientConfig,
    artifact_manifest_sha256,
    make_job_request,
    parse_job_request,
)
from teamagent.media.deadline import (
    DeadlineBudget,
    MediaDeadlineExceededError,
    botocore_config,
)

logger = structlog.get_logger(__name__)

_SYNC_TIMEOUT_DEFAULT_S = 180
_SYNC_TIMEOUT_MAX_S = 15 * 60
_ARTIFACT_TTL_DEFAULT_S = ARTIFACT_RETENTION_SECONDS
_CONSUMER_RELEASE_RESERVE_SECONDS = 15
_JOB_ID_RE = re.compile(r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$")


def _checksum_sha256_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


class MediaJobError(RuntimeError):
    """media job境界のsubmit/status/integrity失敗。"""


class MediaJobClient:
    """Coreが持つSQS/DynamoDB/S3権限だけで同期UXを維持する。"""

    def __init__(
        self,
        *,
        session: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
        queue_url: str | None = None,
        table: str | None = None,
        bucket: str | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        self._region = os.environ.get("AWS_REGION", "ap-northeast-1")
        self._queue_url = os.environ.get("MEDIA_TASK_QUEUE", "") if queue_url is None else queue_url
        self._table = os.environ.get("MEDIA_JOBS_TABLE", "") if table is None else table
        self._bucket = os.environ.get("MEDIA_JOB_BUCKET", "") if bucket is None else bucket
        self._kms_key_id = (
            os.environ.get("MEDIA_JOB_KMS_KEY_ID", "") if kms_key_id is None else kms_key_id
        )
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._clock = clock
        self._session_override = session

    @classmethod
    def is_configured(cls) -> bool:
        return all(
            os.environ.get(name)
            for name in ("MEDIA_TASK_QUEUE", "MEDIA_JOBS_TABLE", "MEDIA_JOB_BUCKET")
        )

    @classmethod
    def local_runtime_enabled(cls) -> bool:
        """Allow heavyweight in-process media only after an explicit local opt-in."""

        return os.environ.get("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    @classmethod
    def require_configured(cls) -> None:
        if not cls.is_configured():
            raise MediaJobError("MEDIA_JOB_NOT_CONFIGURED")

    @classmethod
    def artifact_ttl_seconds(cls) -> int:
        raw = os.environ.get("MEDIA_ARTIFACT_TTL_SECONDS", str(_ARTIFACT_TTL_DEFAULT_S))
        try:
            value = int(raw)
        except ValueError as exc:
            raise MediaJobError("MEDIA_ARTIFACT_TTL_INVALID") from exc
        if value < 300 or value > ARTIFACT_RETENTION_SECONDS:
            raise MediaJobError("MEDIA_ARTIFACT_TTL_INVALID")
        return value

    def _session(self) -> Any:
        if self._session_override is not None:
            return self._session_override
        import boto3

        return boto3.session.Session()

    def _client(self, service: str, deadline_epoch_s: float) -> Any:
        budget = DeadlineBudget(deadline_epoch_s, clock=self._clock)
        try:
            config = botocore_config(budget)
        except MediaDeadlineExceededError as exc:
            raise MediaJobError("MEDIA_JOB_DEADLINE_EXCEEDED") from exc
        return self._session().client(service, region_name=self._region, config=config)

    def _call(
        self,
        service: str,
        deadline_epoch_s: float,
        operation: str,
        **kwargs: Any,
    ) -> Any:
        client = self._client(service, deadline_epoch_s)
        return getattr(client, operation)(**kwargs)

    def _assert_configured(self) -> None:
        if not self._queue_url or not self._table or not self._bucket:
            raise MediaJobError("MEDIA_JOB_NOT_CONFIGURED")

    def _remaining(self, deadline_epoch_s: float, *, cap_s: float | None = None) -> float:
        try:
            return DeadlineBudget(deadline_epoch_s, clock=self._clock).remaining(cap_s=cap_s)
        except MediaDeadlineExceededError as exc:
            raise MediaJobError("MEDIA_JOB_DEADLINE_EXCEEDED") from exc

    def _absolute_deadline(self, timeout_s: int) -> int:
        if timeout_s < 1 or timeout_s > MAX_DEADLINE_SECONDS:
            raise MediaJobError("MEDIA_JOB_TIMEOUT_INVALID")
        return int(self._clock()) + timeout_s

    def _sse_args(self) -> dict[str, str]:
        if self._kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self._kms_key_id,
            }
        return {"ServerSideEncryption": "AES256"}

    @staticmethod
    def _persisted_request(
        item: dict[str, Any],
        submitted: MediaJobRequest,
    ) -> MediaJobRequest:
        """Recover the original timestamp envelope for a semantic retry."""

        if item.get("idempotency_key", {}).get("S") != submitted.idempotency_key:
            raise MediaJobError("MEDIA_JOB_IDEMPOTENCY_CONFLICT")
        raw = item.get("request_json", {}).get("S", "")
        if not raw:
            if item.get("payload_sha256", {}).get("S") == submitted.payload_sha256:
                return submitted
            raise MediaJobError("MEDIA_JOB_ENVELOPE_MISSING")
        try:
            persisted = parse_job_request(raw)
        except MediaJobError:
            raise
        except Exception as exc:
            raise MediaJobError("MEDIA_JOB_ENVELOPE_INVALID") from exc
        if (
            persisted.job_id != submitted.job_id
            or persisted.idempotency_key != submitted.idempotency_key
            or persisted.output_bucket != submitted.output_bucket
            or persisted.output_prefix != submitted.output_prefix
            or item.get("payload_sha256", {}).get("S") != persisted.payload_sha256
        ):
            raise MediaJobError("MEDIA_JOB_IDEMPOTENCY_CONFLICT")
        return persisted

    @staticmethod
    def _assert_audit_owner(
        item: dict[str, Any],
        expected_audit_principal_hash: str | None,
    ) -> None:
        persisted = item.get("audit_principal_hash", {}).get("S", "")
        if not hmac.compare_digest(persisted, expected_audit_principal_hash or ""):
            raise MediaJobError("MEDIA_JOB_AUDIT_PRINCIPAL_MISMATCH")

    @staticmethod
    def _job_id(request_fingerprint: str) -> str:
        digest = hashlib.sha256(request_fingerprint.encode("utf-8")).hexdigest()
        return f"mj_{digest[:24]}"

    def stage_bytes(
        self,
        *,
        job_id: str,
        name: str,
        body: bytes,
        content_type: str,
        deadline_epoch_s: int,
        ttl_s: int | None = None,
    ) -> S3ObjectRef:
        self._assert_configured()
        if not _JOB_ID_RE.fullmatch(job_id):
            raise MediaJobError("MEDIA_JOB_ID_INVALID")
        if not body or len(body) > MAX_INPUT_BYTES:
            raise MediaJobError("MEDIA_INPUT_SIZE_INVALID")
        if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-._" for char in name):
            raise MediaJobError("MEDIA_INPUT_NAME_INVALID")
        if not 1 <= len(content_type) <= 128 or "\r" in content_type or "\n" in content_type:
            raise MediaJobError("MEDIA_INPUT_CONTENT_TYPE_INVALID")
        resolved_ttl_s = self.artifact_ttl_seconds() if ttl_s is None else ttl_s
        if resolved_ttl_s < 300 or resolved_ttl_s > self.artifact_ttl_seconds():
            raise MediaJobError("MEDIA_INPUT_TTL_INVALID")
        self._remaining(deadline_epoch_s)
        digest = hashlib.sha256(body).hexdigest()
        key = f"media-jobs/{job_id}/input/{name}"
        expires = datetime.fromtimestamp(int(self._clock()) + resolved_ttl_s, tz=UTC)
        self._call(
            "s3",
            deadline_epoch_s,
            "put_object",
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            ChecksumSHA256=_checksum_sha256_b64(digest),
            Expires=expires,
            Metadata={"sha256": digest, "job-id": job_id, "schema-version": "1"},
            Tagging=f"teamagent-ttl-epoch={int(expires.timestamp())}",
            **self._sse_args(),
        )
        return S3ObjectRef(
            bucket=self._bucket,
            key=key,
            sha256=digest,
            size=len(body),
            content_type=content_type,
        )

    def submit(self, request: MediaJobRequest) -> str:
        """Create/reuse a semantic row and enqueue its original timestamp envelope."""

        self._assert_configured()
        self._remaining(request.deadline_epoch_s)
        if request.output_bucket != self._bucket:
            raise MediaJobError("MEDIA_JOB_BUCKET_MISMATCH")
        queued_request = request
        body = queued_request.to_json_bytes().decode("utf-8")
        queued = MediaJobResult(job_id=request.job_id, status="queued")
        try:
            self._remaining(request.deadline_epoch_s)
            self._call(
                "dynamodb",
                request.deadline_epoch_s,
                "put_item",
                TableName=self._table,
                Item={
                    "job_id": {"S": request.job_id},
                    "idempotency_key": {"S": request.idempotency_key},
                    "payload_sha256": {"S": request.payload_sha256},
                    "request_json": {"S": body},
                    "status": {"S": "queued"},
                    "version": {"N": "0"},
                    "active_consumers": {"N": "0"},
                    "created_at": {"N": str(request.created_at_epoch_s)},
                    "deadline": {"N": str(request.deadline_epoch_s)},
                    "output_prefix": {"S": request.output_prefix},
                    "cleanup_at": {"N": str(request.created_at_epoch_s + request.artifact_ttl_s)},
                    "hard_cleanup_at": {
                        "N": str(request.created_at_epoch_s + request.artifact_ttl_s)
                    },
                    "cleanup_status": {"S": "pending"},
                    "ttl": {
                        "N": str(
                            request.created_at_epoch_s
                            + request.artifact_ttl_s
                            + DDB_RETENTION_GRACE_SECONDS
                        )
                    },
                    "detail": {
                        "S": json.dumps(
                            queued.model_dump(mode="json"),
                            separators=(",", ":"),
                        )
                    },
                    **(
                        {"audit_principal_hash": {"S": request.audit_principal_hash}}
                        if request.audit_principal_hash
                        else {}
                    ),
                },
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except MediaJobError:
            raise
        except Exception as exc:
            if not _is_conditional_conflict(exc):
                raise MediaJobError("MEDIA_JOB_STATE_CREATE_FAILED") from exc
            existing = self._call(
                "dynamodb",
                request.deadline_epoch_s,
                "get_item",
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                ConsistentRead=True,
            ).get("Item", {})
            if existing.get("idempotency_key", {}).get("S") != request.idempotency_key:
                raise MediaJobError("MEDIA_JOB_IDEMPOTENCY_CONFLICT") from exc
            self._assert_audit_owner(existing, request.audit_principal_hash)
            queued_request = self._persisted_request(existing, request)
            body = queued_request.to_json_bytes().decode("utf-8")
            if existing.get("message_sent_at", {}).get("N"):
                return request.job_id
            if existing.get("status", {}).get("S") in {"running", "done", "failed"}:
                return request.job_id

        submit_owner = f"submit-{uuid.uuid4().hex}"
        claim_deadline = self._monotonic() + min(
            35.0,
            self._remaining(queued_request.deadline_epoch_s),
        )
        while True:
            self._remaining(queued_request.deadline_epoch_s)
            now = int(self._clock())
            try:
                self._call(
                    "dynamodb",
                    queued_request.deadline_epoch_s,
                    "update_item",
                    TableName=self._table,
                    Key={"job_id": {"S": request.job_id}},
                    UpdateExpression="SET submit_owner = :owner, submit_lease_expires_at = :lease",
                    ConditionExpression=(
                        "#status = :queued AND idempotency_key = :idempotency AND "
                        "payload_sha256 = :payload AND attribute_not_exists(message_sent_at) AND "
                        "(attribute_not_exists(submit_owner) OR submit_lease_expires_at < :now)"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":queued": {"S": "queued"},
                        ":idempotency": {"S": queued_request.idempotency_key},
                        ":payload": {"S": queued_request.payload_sha256},
                        ":owner": {"S": submit_owner},
                        ":now": {"N": str(now)},
                        ":lease": {"N": str(now + 30)},
                    },
                )
                break
            except Exception as exc:
                if not _is_conditional_conflict(exc):
                    raise MediaJobError("MEDIA_JOB_ENQUEUE_CLAIM_FAILED") from exc
                existing = self._call(
                    "dynamodb",
                    queued_request.deadline_epoch_s,
                    "get_item",
                    TableName=self._table,
                    Key={"job_id": {"S": request.job_id}},
                    ConsistentRead=True,
                ).get("Item", {})
                if existing.get("idempotency_key", {}).get("S") != queued_request.idempotency_key:
                    raise MediaJobError("MEDIA_JOB_IDEMPOTENCY_CONFLICT") from exc
                self._assert_audit_owner(existing, queued_request.audit_principal_hash)
                if existing.get("message_sent_at", {}).get("N") or existing.get("status", {}).get(
                    "S"
                ) in {"running", "done", "failed"}:
                    return request.job_id
                queued_request = self._persisted_request(existing, queued_request)
                body = queued_request.to_json_bytes().decode("utf-8")
                if self._monotonic() >= claim_deadline:
                    raise MediaJobError("MEDIA_JOB_ENQUEUE_CLAIM_TIMEOUT") from exc
                lease_expires_at = int(
                    existing.get("submit_lease_expires_at", {}).get("N", str(now + 1))
                )
                self._sleeper(
                    min(
                        1.0,
                        max(0.1, lease_expires_at - now),
                        self._remaining(queued_request.deadline_epoch_s),
                    )
                )

        arguments: dict[str, Any] = {
            "QueueUrl": self._queue_url,
            "MessageBody": body,
            "MessageAttributes": {
                "schema_version": {"DataType": "String", "StringValue": "1"},
                "payload_sha256": {
                    "DataType": "String",
                    "StringValue": queued_request.payload_sha256,
                },
            },
        }
        if self._queue_url.endswith(".fifo"):
            arguments["MessageDeduplicationId"] = queued_request.idempotency_key
            arguments["MessageGroupId"] = "teamagent-media"
        try:
            self._remaining(queued_request.deadline_epoch_s)
            self._call(
                "sqs",
                queued_request.deadline_epoch_s,
                "send_message",
                **arguments,
            )
        except MediaJobError:
            try:
                self._call(
                    "dynamodb",
                    queued_request.deadline_epoch_s,
                    "update_item",
                    TableName=self._table,
                    Key={"job_id": {"S": request.job_id}},
                    UpdateExpression="REMOVE submit_owner, submit_lease_expires_at",
                    ConditionExpression="submit_owner = :owner",
                    ExpressionAttributeValues={":owner": {"S": submit_owner}},
                )
            except Exception:
                logger.exception("failed to release expired media submit lease")
            raise
        except Exception as exc:
            try:
                self._call(
                    "dynamodb",
                    queued_request.deadline_epoch_s,
                    "update_item",
                    TableName=self._table,
                    Key={"job_id": {"S": request.job_id}},
                    UpdateExpression="REMOVE submit_owner, submit_lease_expires_at",
                    ConditionExpression="submit_owner = :owner",
                    ExpressionAttributeValues={":owner": {"S": submit_owner}},
                )
            except Exception as release_exc:
                raise MediaJobError("MEDIA_JOB_SUBMIT_AND_RELEASE_FAILED") from release_exc
            raise MediaJobError("MEDIA_JOB_SUBMIT_FAILED") from exc
        try:
            self._call(
                "dynamodb",
                queued_request.deadline_epoch_s,
                "update_item",
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                UpdateExpression=(
                    "SET message_sent_at = :now REMOVE submit_owner, submit_lease_expires_at"
                ),
                ConditionExpression="submit_owner = :owner",
                ExpressionAttributeValues={
                    ":owner": {"S": submit_owner},
                    ":now": {"N": str(int(self._clock()))},
                },
            )
        except MediaJobError:
            raise
        except Exception as exc:
            raise MediaJobError("MEDIA_JOB_ENQUEUE_CONFIRM_FAILED") from exc
        self._remaining(queued_request.deadline_epoch_s)
        return request.job_id

    def get_result(
        self,
        job_id: str,
        *,
        deadline_epoch_s: int,
        expected_audit_principal_hash: str | None = None,
    ) -> MediaJobResult | None:
        self._assert_configured()
        response = self._call(
            "dynamodb",
            deadline_epoch_s,
            "get_item",
            TableName=self._table,
            Key={"job_id": {"S": job_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        self._assert_audit_owner(item, expected_audit_principal_hash)
        detail = item.get("detail", {}).get("S", "")
        try:
            result = MediaJobResult.model_validate_json(detail)
            if result.job_id != job_id:
                raise MediaJobError("MEDIA_JOB_RESULT_SCOPE_INVALID")
            persisted_status = item.get("status", {}).get("S")
            if persisted_status in ("queued", "running") and result.status != persisted_status:
                result = result.model_copy(update={"status": persisted_status})
            if result.status == "done":
                expected_prefix = f"media-jobs/{job_id}/attempts/"
                if any(
                    artifact.object.bucket != self._bucket
                    or not artifact.object.key.startswith(expected_prefix)
                    or artifact.object.size <= 0
                    for artifact in result.artifacts
                ):
                    raise MediaJobError("MEDIA_ARTIFACT_MANIFEST_SCOPE_INVALID")
                persisted_manifest = item.get("artifact_manifest_sha256", {}).get("S", "")
                if not persisted_manifest or not hmac.compare_digest(
                    persisted_manifest,
                    artifact_manifest_sha256(result.artifacts),
                ):
                    raise MediaJobError("MEDIA_ARTIFACT_MANIFEST_INTEGRITY_FAILED")
            return result
        except MediaJobError:
            raise
        except Exception as exc:
            raise MediaJobError("MEDIA_JOB_RESULT_INVALID") from exc

    def wait(
        self,
        job_id: str,
        *,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
        poll_interval_s: float = 1.0,
        deadline_epoch_s: int | None = None,
        expected_audit_principal_hash: str | None = None,
    ) -> MediaJobResult:
        if timeout_s < 1 or timeout_s > _SYNC_TIMEOUT_MAX_S:
            raise MediaJobError("MEDIA_JOB_TIMEOUT_INVALID")
        monotonic_deadline = self._monotonic() + timeout_s
        now = self._clock()
        absolute_deadline = min(
            now + timeout_s,
            float(deadline_epoch_s) if deadline_epoch_s is not None else now + timeout_s,
        )
        while self._monotonic() <= monotonic_deadline:
            remaining_absolute = self._remaining(absolute_deadline)
            result = self.get_result(
                job_id,
                deadline_epoch_s=int(absolute_deadline),
                expected_audit_principal_hash=expected_audit_principal_hash,
            )
            if result is not None and result.status in ("done", "failed"):
                return result
            remaining = monotonic_deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleeper(
                min(
                    max(poll_interval_s, 0.1),
                    remaining,
                    remaining_absolute,
                    5.0,
                )
            )
        raise MediaJobError("MEDIA_JOB_TIMEOUT")

    def download(self, ref: S3ObjectRef, *, deadline_epoch_s: int) -> bytes:
        self._assert_configured()
        if ref.bucket != self._bucket:
            raise MediaJobError("MEDIA_ARTIFACT_BUCKET_MISMATCH")
        response = self._call(
            "s3",
            deadline_epoch_s,
            "get_object",
            Bucket=ref.bucket,
            Key=ref.key,
        )
        if response.get("ServerSideEncryption") not in ("AES256", "aws:kms"):
            raise MediaJobError("MEDIA_ARTIFACT_NOT_ENCRYPTED")
        body = bytes(response["Body"].read(ref.size + 1))
        if len(body) != ref.size or hashlib.sha256(body).hexdigest() != ref.sha256:
            raise MediaJobError("MEDIA_ARTIFACT_INTEGRITY_FAILED")
        return body

    def _verify_artifact_ref(self, ref: S3ObjectRef, *, deadline_epoch_s: int) -> None:
        if ref.bucket != self._bucket:
            raise MediaJobError("MEDIA_ARTIFACT_BUCKET_MISMATCH")
        try:
            response = self._call(
                "s3",
                deadline_epoch_s,
                "head_object",
                Bucket=ref.bucket,
                Key=ref.key,
                ChecksumMode="ENABLED",
            )
        except MediaJobError:
            raise
        except Exception as exc:
            raise MediaJobError("MEDIA_ARTIFACT_HEAD_FAILED") from exc
        metadata = response.get("Metadata")
        if (
            response.get("ServerSideEncryption") not in ("AES256", "aws:kms")
            or response.get("ContentLength") != ref.size
            or not hmac.compare_digest(
                str(response.get("ChecksumSHA256") or ""),
                _checksum_sha256_b64(ref.sha256),
            )
            or not isinstance(metadata, dict)
            or not hmac.compare_digest(str(metadata.get("sha256") or ""), ref.sha256)
        ):
            raise MediaJobError("MEDIA_ARTIFACT_INTEGRITY_FAILED")

    def presign_get(
        self,
        ref: S3ObjectRef,
        *,
        deadline_epoch_s: int,
        expires_s: int,
    ) -> str:
        """Create a verified URL compatible with the seven-day SigV4 ceiling."""

        self._assert_configured()
        if ref.bucket != self._bucket:
            raise MediaJobError("MEDIA_ARTIFACT_BUCKET_MISMATCH")
        if expires_s < 1 or expires_s > MAX_PRESIGNED_URL_SECONDS:
            raise MediaJobError("MEDIA_ARTIFACT_PRESIGN_EXPIRY_INVALID")
        self._verify_artifact_ref(ref, deadline_epoch_s=deadline_epoch_s)
        self._remaining(deadline_epoch_s)
        try:
            url = self._client("s3", deadline_epoch_s).generate_presigned_url(
                "get_object",
                Params={"Bucket": ref.bucket, "Key": ref.key},
                ExpiresIn=expires_s,
            )
        except MediaJobError:
            raise
        except Exception as exc:
            raise MediaJobError("MEDIA_ARTIFACT_PRESIGN_FAILED") from exc
        self._remaining(deadline_epoch_s)
        if not isinstance(url, str) or not url.startswith("https://"):
            raise MediaJobError("MEDIA_ARTIFACT_PRESIGN_FAILED")
        return url

    def cleanup(self, request: MediaJobRequest) -> None:
        """Keep shared terminal state until the fenced janitor window.

        Synchronous callers must never delete a deterministic idempotency key
        that another caller may be polling or downloading.  This method now
        validates scope only; deletion is owned by the scheduled janitor.
        """

        approved = f"media-jobs/{request.job_id}/"
        if request.output_prefix != approved:
            raise MediaJobError("MEDIA_JOB_CLEANUP_SCOPE_INVALID")

    def cleanup_job(self, job_id: str, *, deadline_epoch_s: int) -> None:
        """Register pre-submit staged bytes for deterministic janitor cleanup."""

        if not _JOB_ID_RE.fullmatch(job_id):
            raise MediaJobError("MEDIA_JOB_CLEANUP_SCOPE_INVALID")
        now = int(self._clock())
        result = MediaJobResult(
            job_id=job_id,
            status="failed",
            error_code="MEDIA_REQUEST_BUILD_FAILED",
        )
        try:
            self._call(
                "dynamodb",
                deadline_epoch_s,
                "put_item",
                TableName=self._table,
                Item={
                    "job_id": {"S": job_id},
                    "status": {"S": "failed"},
                    "version": {"N": "0"},
                    "active_consumers": {"N": "0"},
                    "output_prefix": {"S": f"media-jobs/{job_id}/"},
                    "cleanup_at": {"N": str(now)},
                    "hard_cleanup_at": {"N": str(now)},
                    "cleanup_status": {"S": "pending"},
                    "ttl": {"N": str(now + DDB_RETENTION_GRACE_SECONDS)},
                    "detail": {
                        "S": json.dumps(
                            result.model_dump(mode="json"),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    },
                },
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except Exception as exc:
            if not _is_conditional_conflict(exc):
                raise MediaJobError("MEDIA_JOB_CLEANUP_REGISTRATION_FAILED") from exc

    def _acquire_consumer(self, request: MediaJobRequest, *, timeout_s: int) -> None:
        now = int(self._clock())
        execution_deadline_epoch_s = request.deadline_epoch_s - _CONSUMER_RELEASE_RESERVE_SECONDS
        try:
            self._call(
                "dynamodb",
                execution_deadline_epoch_s,
                "update_item",
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                UpdateExpression=("SET consumer_guard_until = :guard ADD active_consumers :one"),
                ConditionExpression=(
                    "idempotency_key = :idempotency AND attribute_not_exists(cleanup_owner)"
                ),
                ExpressionAttributeValues={
                    ":idempotency": {"S": request.idempotency_key},
                    ":guard": {"N": str(now + timeout_s + 60)},
                    ":one": {"N": "1"},
                },
            )
        except Exception as exc:
            raise MediaJobError("MEDIA_JOB_CONSUMER_ACQUIRE_FAILED") from exc

    def _release_consumer(self, request: MediaJobRequest) -> None:
        try:
            self._call(
                "dynamodb",
                request.deadline_epoch_s,
                "update_item",
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                UpdateExpression="ADD active_consumers :minus_one",
                ConditionExpression="active_consumers >= :one",
                ExpressionAttributeValues={
                    ":minus_one": {"N": "-1"},
                    ":one": {"N": "1"},
                },
            )
        except Exception as exc:
            raise MediaJobError("MEDIA_JOB_CONSUMER_RELEASE_FAILED") from exc

    def _run_staged(
        self,
        *,
        job_id: str,
        request_fingerprint: str,
        timeout_s: int,
        operation_factory: Callable[[int], MediaOperation],
    ) -> tuple[Mapping[str, bytes], Mapping[str, Any]]:
        """stage/operation/request途中の例外でも、作成済みinputを残さない。"""

        request: MediaJobRequest | None = None
        deadline_epoch_s = self._absolute_deadline(timeout_s)
        try:
            operation = operation_factory(deadline_epoch_s)
            request = self._request(
                operation,
                request_fingerprint,
                timeout_s,
                job_id=job_id,
                deadline_epoch_s=deadline_epoch_s,
            )
            return self.run_sync(request, timeout_s=timeout_s)
        finally:
            # run_sync に到達した場合は同メソッドの finally が cleanup する。
            if request is None:
                self.cleanup_job(job_id, deadline_epoch_s=deadline_epoch_s)

    def run_sync(
        self,
        request: MediaJobRequest,
        *,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> tuple[Mapping[str, bytes], Mapping[str, Any]]:
        """submit→consumer lease→bounded poll→integrity-checked download."""

        execution_deadline_epoch_s = request.deadline_epoch_s - _CONSUMER_RELEASE_RESERVE_SECONDS
        remaining = self._remaining(
            execution_deadline_epoch_s,
            cap_s=float(timeout_s),
        )
        self.submit(request)
        remaining = self._remaining(execution_deadline_epoch_s, cap_s=remaining)
        bounded_timeout = max(1, math.ceil(remaining))
        self._acquire_consumer(request, timeout_s=bounded_timeout)
        try:
            result = self.wait(
                request.job_id,
                timeout_s=bounded_timeout,
                deadline_epoch_s=execution_deadline_epoch_s,
                expected_audit_principal_hash=request.audit_principal_hash,
            )
            if result.status != "done":
                raise MediaJobError(result.error_code or "MEDIA_JOB_FAILED")
            artifacts: dict[str, bytes] = {}
            for artifact in result.artifacts:
                self._remaining(execution_deadline_epoch_s)
                artifacts[artifact.name] = self.download(
                    artifact.object,
                    deadline_epoch_s=execution_deadline_epoch_s,
                )
                self._remaining(execution_deadline_epoch_s)
            return artifacts, result.metadata
        finally:
            self._release_consumer(request)

    def acquire_video(
        self,
        url: str,
        *,
        request_fingerprint: str,
        max_bytes: int = 30 * 1024 * 1024,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> tuple[bytes, str]:
        operation = AcquireOperation(kind="acquire", url=url, max_bytes=max_bytes)
        deadline_epoch_s = self._absolute_deadline(timeout_s)
        request = self._request(
            operation,
            request_fingerprint,
            timeout_s,
            deadline_epoch_s=deadline_epoch_s,
        )
        artifacts, _metadata = self.run_sync(request, timeout_s=timeout_s)
        body = artifacts.get("media")
        if body is None:
            raise MediaJobError("MEDIA_ACQUIRE_ARTIFACT_MISSING")
        if body.startswith(b"\x1aE\xdf\xa3"):
            mime = "video/webm"
        elif body[4:12].startswith(b"ftypqt"):
            mime = "video/quicktime"
        else:
            mime = "video/mp4"
        return body, mime

    def search_tiktok(
        self,
        query: str,
        *,
        request_fingerprint: str,
        search_type: str = "keyword",
        max_videos: int = 10,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> list[dict[str, Any]]:
        """既存の同期検索UXをgeneric TikTok media operationで維持する。"""

        operation = TikTokAcquireOperation(
            kind="tiktok_acquire",
            search_type=cast(Literal["keyword", "hashtag"], search_type),
            keywords=(query,),
            n_per_kw=max_videos,
            videos_per_kw=0,
            sort="display",
            artifact_mode="metadata_only",
            client=TikTokClientConfig(),
        )
        deadline_epoch_s = self._absolute_deadline(timeout_s)
        request = self._request(
            operation,
            request_fingerprint,
            timeout_s,
            deadline_epoch_s=deadline_epoch_s,
        )
        artifacts, _metadata = self.run_sync(request, timeout_s=timeout_s)
        body = artifacts.get("posts.json")
        if body is None:
            raise MediaJobError("MEDIA_TIKTOK_POSTS_MISSING")
        try:
            payload = json.loads(body)
            posts = payload["posts"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise MediaJobError("MEDIA_TIKTOK_POSTS_INVALID") from exc
        if not isinstance(posts, list) or not all(isinstance(item, dict) for item in posts):
            raise MediaJobError("MEDIA_TIKTOK_POSTS_INVALID")
        return posts

    def proxy_video(
        self,
        data: bytes,
        mime: str,
        *,
        request_fingerprint: str,
        limit_bytes: int = 18 * 1024 * 1024,
        preview: bool = False,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> tuple[bytes, str]:
        job_id = self._job_id(request_fingerprint)

        def operation_factory(deadline_epoch_s: int) -> MediaOperation:
            source = self.stage_bytes(
                job_id=job_id,
                name="source.bin",
                body=data,
                content_type=mime,
                deadline_epoch_s=deadline_epoch_s,
            )
            return ProxyOperation(
                kind="proxy",
                source=source,
                limit_bytes=limit_bytes,
                preview=preview,
            )

        artifacts, _metadata = self._run_staged(
            job_id=job_id,
            request_fingerprint=request_fingerprint,
            timeout_s=timeout_s,
            operation_factory=operation_factory,
        )
        body = artifacts.get("proxy")
        if body is None:
            raise MediaJobError("MEDIA_PROXY_ARTIFACT_MISSING")
        return body, "video/mp4" if body[:8].endswith(b"ftyp") else mime

    def extract_frames(
        self,
        data: bytes,
        mime: str,
        timecodes: list[float],
        *,
        request_fingerprint: str,
        width: int = 320,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> list[tuple[float, bytes]]:
        job_id = self._job_id(request_fingerprint)

        def operation_factory(deadline_epoch_s: int) -> MediaOperation:
            source = self.stage_bytes(
                job_id=job_id,
                name="source.bin",
                body=data,
                content_type=mime,
                deadline_epoch_s=deadline_epoch_s,
            )
            return FrameOperation(
                kind="frame",
                source=source,
                timecodes=tuple(sorted(set(timecodes))),
                width=width,
            )

        artifacts, _metadata = self._run_staged(
            job_id=job_id,
            request_fingerprint=request_fingerprint,
            timeout_s=timeout_s,
            operation_factory=operation_factory,
        )
        normalized_timecodes = tuple(sorted(set(timecodes)))
        return [
            (second, artifacts[f"frame-{index:02d}"])
            for index, second in enumerate(normalized_timecodes)
            if f"frame-{index:02d}" in artifacts
        ]

    def make_thumbnail(
        self,
        data: bytes,
        mime: str,
        *,
        request_fingerprint: str,
        width: int = 480,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> tuple[bytes, dict[str, Any]]:
        job_id = self._job_id(request_fingerprint)

        def operation_factory(deadline_epoch_s: int) -> MediaOperation:
            source = self.stage_bytes(
                job_id=job_id,
                name="source.bin",
                body=data,
                content_type=mime,
                deadline_epoch_s=deadline_epoch_s,
            )
            return ThumbnailOperation(kind="thumbnail", source=source, width=width)

        artifacts, metadata = self._run_staged(
            job_id=job_id,
            request_fingerprint=request_fingerprint,
            timeout_s=timeout_s,
            operation_factory=operation_factory,
        )
        image = artifacts.get("thumbnail")
        if image is None:
            raise MediaJobError("MEDIA_THUMBNAIL_ARTIFACT_MISSING")
        return image, dict(metadata)

    def make_thumbnail_from_url(
        self,
        url: str,
        *,
        request_fingerprint: str,
        width: int = 480,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> tuple[bytes, dict[str, Any]]:
        operation = ThumbnailOperation(kind="thumbnail", url=url, width=width)
        deadline_epoch_s = self._absolute_deadline(timeout_s)
        request = self._request(
            operation,
            request_fingerprint,
            timeout_s,
            deadline_epoch_s=deadline_epoch_s,
        )
        artifacts, metadata = self.run_sync(request, timeout_s=timeout_s)
        image = artifacts.get("thumbnail")
        if image is None:
            raise MediaJobError("MEDIA_THUMBNAIL_ARTIFACT_MISSING")
        return image, dict(metadata)

    def slides_to_pptx(
        self,
        html: str,
        *,
        request_fingerprint: str,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> bytes:
        job_id = self._job_id(request_fingerprint)

        def operation_factory(deadline_epoch_s: int) -> MediaOperation:
            html_ref = self.stage_bytes(
                job_id=job_id,
                name="slides.html",
                body=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                deadline_epoch_s=deadline_epoch_s,
            )
            return SlidesOperation(kind="slides", html=html_ref)

        artifacts, _metadata = self._run_staged(
            job_id=job_id,
            request_fingerprint=request_fingerprint,
            timeout_s=timeout_s,
            operation_factory=operation_factory,
        )
        body = artifacts.get("slides.pptx")
        if body is None:
            raise MediaJobError("MEDIA_SLIDES_ARTIFACT_MISSING")
        return body

    def html_to_pdf(
        self,
        html: str,
        *,
        request_fingerprint: str,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> bytes:
        job_id = self._job_id(request_fingerprint)

        def operation_factory(deadline_epoch_s: int) -> MediaOperation:
            html_ref = self.stage_bytes(
                job_id=job_id,
                name="document.html",
                body=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                deadline_epoch_s=deadline_epoch_s,
            )
            return PdfOperation(kind="pdf", html=html_ref)

        artifacts, _metadata = self._run_staged(
            job_id=job_id,
            request_fingerprint=request_fingerprint,
            timeout_s=timeout_s,
            operation_factory=operation_factory,
        )
        body = artifacts.get("document.pdf")
        if body is None:
            raise MediaJobError("MEDIA_PDF_ARTIFACT_MISSING")
        return body

    def render_proposal_pptx(
        self,
        template: bytes,
        composer_json: bytes,
        *,
        request_fingerprint: str,
        evidence_images: list[tuple[int, int, bytes, str]] | None = None,
        fail_if_missing: bool = True,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> bytes:
        job_id = self._job_id(request_fingerprint)

        def operation_factory(deadline_epoch_s: int) -> MediaOperation:
            template_ref = self.stage_bytes(
                job_id=job_id,
                name="template.pptx",
                body=template,
                content_type=(
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                ),
                deadline_epoch_s=deadline_epoch_s,
            )
            composer_ref = self.stage_bytes(
                job_id=job_id,
                name="composer.json",
                body=composer_json,
                content_type="application/json",
                deadline_epoch_s=deadline_epoch_s,
            )
            evidence: list[ProposalEvidence] = []
            for index, (placeholder_id, rank, image, content_type) in enumerate(
                evidence_images or []
            ):
                evidence.append(
                    ProposalEvidence(
                        placeholder_id=placeholder_id,
                        rank=rank,
                        source=self.stage_bytes(
                            job_id=job_id,
                            name=f"evidence-{index:02d}.bin",
                            body=image,
                            content_type=content_type,
                            deadline_epoch_s=deadline_epoch_s,
                        ),
                    )
                )
            return ProposalPptxOperation(
                kind="proposal_pptx",
                template=template_ref,
                composer_json=composer_ref,
                evidence=tuple(evidence),
                fail_if_missing=fail_if_missing,
            )

        artifacts, _metadata = self._run_staged(
            job_id=job_id,
            request_fingerprint=request_fingerprint,
            timeout_s=timeout_s,
            operation_factory=operation_factory,
        )
        body = artifacts.get("proposal.pptx")
        if body is None:
            raise MediaJobError("MEDIA_PROPOSAL_ARTIFACT_MISSING")
        return body

    def _request(
        self,
        operation: MediaOperation,
        request_fingerprint: str,
        timeout_s: int,
        *,
        job_id: str | None = None,
        deadline_epoch_s: int | None = None,
    ) -> MediaJobRequest:
        self._assert_configured()
        return make_job_request(
            operation=operation,
            output_bucket=self._bucket,
            request_fingerprint=request_fingerprint,
            now_epoch_s=int(self._clock()),
            timeout_s=timeout_s,
            deadline_epoch_s=deadline_epoch_s,
            artifact_ttl_s=self.artifact_ttl_seconds(),
            job_id=job_id,
        )


def _is_conditional_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    return isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException"


__all__ = ["MediaJobClient", "MediaJobError"]
