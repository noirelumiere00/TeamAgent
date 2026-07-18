"""1 process / 1 request の teamagent-media-worker entrypoint。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from teamagent.media.contracts import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MediaArtifact,
    MediaJobRequest,
    MediaJobResult,
    S3ObjectRef,
    TikTokAcquireOperation,
    parse_job_request,
)
from teamagent.media.operations import MediaOperationError, ProducedArtifact, execute_operation

logger = logging.getLogger(__name__)

_RUNTIME_DIRECTORIES = (
    "home",
    "tmp",
    "cache",
    "config",
    "data",
    "state",
    "jobs",
)


class WorkerBackend(Protocol):
    """AWS実装とunit-test fakeが共有する最小backend。"""

    def assert_request_scope(self, request: MediaJobRequest) -> None: ...

    def claim(
        self,
        request: MediaJobRequest,
        *,
        owner: str,
        now_epoch_s: int,
    ) -> WorkerClaim: ...

    def load_object(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        ref: S3ObjectRef,
        destination: Path,
    ) -> Path: ...

    def upload_artifact(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        artifact: ProducedArtifact,
    ) -> S3ObjectRef: ...

    def store_result(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        result: MediaJobResult,
    ) -> None: ...

    def cleanup_attempt(self, request: MediaJobRequest, lease: WorkerLease) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerLease:
    owner: str
    version: int


@dataclass(frozen=True, slots=True)
class WorkerClaim:
    lease: WorkerLease | None = None
    existing_result: MediaJobResult | None = None


def _conditional_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code == "ConditionalCheckFailedException"


class AwsWorkerBackend:
    """S3/DynamoDBだけを使うworker backend。RDS/Slack/OAuth/MCP権限は不要。"""

    def __init__(self) -> None:
        self._region = os.environ.get("AWS_REGION", "ap-northeast-1")
        self._bucket = os.environ.get("MEDIA_JOB_BUCKET", "")
        self._table = os.environ.get("MEDIA_JOBS_TABLE", "")
        self._kms_key_id = os.environ.get("MEDIA_JOB_KMS_KEY_ID", "")
        if not self._bucket or not self._table:
            raise RuntimeError("MEDIA_JOB_BUCKET and MEDIA_JOBS_TABLE are required")
        try:
            self._artifact_ttl_cap_s = int(os.environ.get("MEDIA_ARTIFACT_TTL_SECONDS", "21600"))
        except ValueError as exc:
            raise RuntimeError("MEDIA_ARTIFACT_TTL_SECONDS is invalid") from exc
        if not 300 <= self._artifact_ttl_cap_s <= 21600:
            raise RuntimeError("MEDIA_ARTIFACT_TTL_SECONDS is invalid")
        import boto3

        session = boto3.session.Session()
        self._s3 = session.client("s3", region_name=self._region)
        self._ddb = session.client("dynamodb", region_name=self._region)

    def _sse_args(self) -> dict[str, str]:
        if self._kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self._kms_key_id,
            }
        return {"ServerSideEncryption": "AES256"}

    def assert_request_scope(self, request: MediaJobRequest) -> None:
        if request.output_bucket != self._bucket:
            raise ValueError("request bucket is outside worker scope")
        if request.artifact_ttl_s > self._artifact_ttl_cap_s:
            raise ValueError("request artifact TTL exceeds worker scope")
        for ref in _operation_refs(request):
            if ref.bucket != self._bucket or not ref.key.startswith(
                f"{request.output_prefix}input/"
            ):
                raise ValueError("input object is outside job scope")

    @staticmethod
    def _result_from_item(item: dict[str, Any]) -> MediaJobResult | None:
        detail = item.get("detail", {}).get("S", "")
        if not detail:
            return None
        return MediaJobResult.model_validate_json(detail)

    def claim(
        self,
        request: MediaJobRequest,
        *,
        owner: str,
        now_epoch_s: int,
    ) -> WorkerClaim:
        lease_expires = max(request.deadline_epoch_s + 60, now_epoch_s + 60)
        try:
            response = self._ddb.update_item(
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                UpdateExpression=(
                    "SET #status = :running, lease_owner = :owner, "
                    "lease_expires_at = :lease, updated_at = :now ADD #version :one"
                ),
                ConditionExpression=(
                    "attribute_exists(job_id) AND idempotency_key = :idempotency AND "
                    "payload_sha256 = :payload AND "
                    "(#status = :queued OR "
                    "(#status = :running AND lease_expires_at < :now))"
                ),
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues={
                    ":queued": {"S": "queued"},
                    ":running": {"S": "running"},
                    ":owner": {"S": owner},
                    ":lease": {"N": str(lease_expires)},
                    ":now": {"N": str(now_epoch_s)},
                    ":one": {"N": "1"},
                    ":idempotency": {"S": request.idempotency_key},
                    ":payload": {"S": request.payload_sha256},
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if not _conditional_conflict(exc):
                raise
            item = self._ddb.get_item(
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                ConsistentRead=True,
            ).get("Item", {})
            if (
                item.get("idempotency_key", {}).get("S") != request.idempotency_key
                or item.get("payload_sha256", {}).get("S") != request.payload_sha256
            ):
                raise ValueError("job row does not match request envelope") from exc
            status = item.get("status", {}).get("S")
            if status in {"done", "failed"}:
                result = self._result_from_item(item)
                if result is None or result.status != status:
                    raise ValueError("terminal job row has invalid result") from exc
                return WorkerClaim(existing_result=result)
            if status in {"queued", "running"}:
                return WorkerClaim()
            raise ValueError("job row has invalid status") from exc
        attributes = response.get("Attributes", {})
        version = int(attributes.get("version", {}).get("N", "0"))
        if version < 1:
            raise ValueError("worker lease version is invalid")
        return WorkerClaim(lease=WorkerLease(owner=owner, version=version))

    def _renew_lease(self, request: MediaJobRequest, lease: WorkerLease) -> None:
        now = int(time.time())
        self._ddb.update_item(
            TableName=self._table,
            Key={"job_id": {"S": request.job_id}},
            UpdateExpression="SET lease_expires_at = :lease, updated_at = :now",
            ConditionExpression=(
                "#status = :running AND lease_owner = :owner AND #version = :version"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={
                ":running": {"S": "running"},
                ":owner": {"S": lease.owner},
                ":version": {"N": str(lease.version)},
                ":lease": {"N": str(max(request.deadline_epoch_s + 60, now + 60))},
                ":now": {"N": str(now)},
            },
        )

    def load_object(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        ref: S3ObjectRef,
        destination: Path,
    ) -> Path:
        self._renew_lease(request, lease)
        self.assert_request_scope(request)
        if ref.size > MAX_INPUT_BYTES:
            raise ValueError("input size exceeds worker bound")
        response = self._s3.get_object(Bucket=ref.bucket, Key=ref.key)
        if response.get("ServerSideEncryption") not in ("AES256", "aws:kms"):
            raise ValueError("input object is not server-side encrypted")
        body = response["Body"].read(ref.size + 1)
        if len(body) != ref.size or hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ValueError("input object size/hash mismatch")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(body)
        destination.chmod(0o600)
        return destination

    def upload_artifact(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        artifact: ProducedArtifact,
    ) -> S3ObjectRef:
        self._renew_lease(request, lease)
        body = artifact.path.read_bytes()
        if not body or len(body) > MAX_OUTPUT_BYTES:
            raise ValueError("output artifact size is invalid")
        digest = hashlib.sha256(body).hexdigest()
        relative_key = artifact.relative_key or f"output/{artifact.name}"
        if relative_key.startswith("/") or ".." in relative_key.split("/") or "\\" in relative_key:
            raise ValueError("artifact relative key is unsafe")
        key = f"{request.output_prefix}attempts/{lease.version}/{relative_key}"
        expires_at = datetime.fromtimestamp(
            int(time.time()) + request.artifact_ttl_s,
            tz=UTC,
        )
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=artifact.content_type,
            Expires=expires_at,
            Metadata={
                "sha256": digest,
                "schema-version": request.schema_version,
                "job-id": request.job_id,
            },
            Tagging=f"teamagent-ttl-epoch={int(expires_at.timestamp())}",
            **self._sse_args(),
        )
        return S3ObjectRef(
            bucket=self._bucket,
            key=key,
            sha256=digest,
            size=len(body),
            content_type=artifact.content_type,
        )

    def store_result(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        result: MediaJobResult,
    ) -> None:
        now = int(time.time())
        self._ddb.update_item(
            TableName=self._table,
            Key={"job_id": {"S": request.job_id}},
            UpdateExpression=(
                "SET #status = :status, detail = :detail, updated_at = :now, "
                "cleanup_at = :cleanup, cleanup_status = :pending, ttl = :ttl "
                "REMOVE lease_expires_at ADD #version :one"
            ),
            ConditionExpression=(
                "#status = :running AND lease_owner = :owner AND #version = :version"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={
                ":status": {"S": result.status},
                ":running": {"S": "running"},
                ":detail": {
                    "S": json.dumps(
                        result.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                },
                ":owner": {"S": lease.owner},
                ":version": {"N": str(lease.version)},
                ":one": {"N": "1"},
                ":now": {"N": str(now)},
                ":cleanup": {
                    "N": str(min(now + request.artifact_ttl_s, request.created_at_epoch_s + 21600))
                },
                ":pending": {"S": "pending"},
                ":ttl": {"N": str(request.created_at_epoch_s + 86400)},
            },
        )

    def cleanup_attempt(self, request: MediaJobRequest, lease: WorkerLease) -> None:
        self._renew_lease(request, lease)
        self._delete_prefix(f"{request.output_prefix}attempts/{lease.version}/")

    def _delete_prefix(self, prefix: str) -> None:
        continuation: str | None = None
        while True:
            arguments: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if continuation:
                arguments["ContinuationToken"] = continuation
            response = self._s3.list_objects_v2(**arguments)
            keys = [{"Key": item["Key"]} for item in response.get("Contents", [])]
            if keys:
                deleted = self._s3.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": keys, "Quiet": True},
                )
                if deleted.get("Errors"):
                    raise RuntimeError("S3 reported media cleanup errors")
            if not response.get("IsTruncated"):
                return
            continuation = str(response["NextContinuationToken"])


def _operation_refs(request: MediaJobRequest) -> tuple[S3ObjectRef, ...]:
    operation = request.operation
    refs: list[S3ObjectRef] = []
    for name in ("source", "html", "template", "composer_json"):
        value = getattr(operation, name, None)
        if isinstance(value, S3ObjectRef):
            refs.append(value)
    evidence = getattr(operation, "evidence", ())
    refs.extend(item.source for item in evidence)
    return tuple(refs)


def _failed_result(request: MediaJobRequest, error_code: str) -> MediaJobResult:
    return MediaJobResult(
        job_id=request.job_id,
        status="failed",
        error_code=error_code,
    )


def run_job(
    request: MediaJobRequest,
    backend: WorkerBackend,
    *,
    temp_root: Path | None = None,
    now_epoch_s: int | None = None,
    owner: str | None = None,
) -> MediaJobResult:
    """Execute one owner/version-fenced attempt in a bounded request directory."""

    now = int(time.time()) if now_epoch_s is None else now_epoch_s
    root = temp_root or Path(
        os.environ.get(
            "MEDIA_JOB_TMP_ROOT",
            # Fargate task-scoped /tmp; each job still uses TemporaryDirectory.
            "/tmp/teamagent/jobs",  # nosec B108
        )
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backend.assert_request_scope(request)
    claim = backend.claim(
        request,
        owner=owner or f"local-{uuid.uuid4().hex}",
        now_epoch_s=now,
    )
    if claim.existing_result is not None:
        return claim.existing_result
    if claim.lease is None:
        return MediaJobResult(
            job_id=request.job_id,
            status="running",
            metadata={"duplicate_delivery": True},
        )
    lease = claim.lease
    if now > request.deadline_epoch_s:
        result = _failed_result(request, "MEDIA_JOB_DEADLINE_EXCEEDED")
        backend.store_result(request, lease, result)
        return result
    try:
        with tempfile.TemporaryDirectory(prefix=f"{request.job_id}-", dir=root) as raw_workdir:
            workdir = Path(raw_workdir)

            def load(ref: S3ObjectRef, destination: Path) -> Path:
                return backend.load_object(request, lease, ref, destination)

            output = execute_operation(
                request.operation,
                workdir=workdir,
                load_object=load,
            )
            artifacts = tuple(
                MediaArtifact(
                    name=artifact.name,
                    object=backend.upload_artifact(request, lease, artifact),
                )
                for artifact in output.artifacts
            )
            metadata = dict(output.metadata)
            if isinstance(request.operation, TikTokAcquireOperation):
                metadata["s3_prefix"] = f"{request.output_prefix}attempts/{lease.version}/"
            result = MediaJobResult(
                job_id=request.job_id,
                status="done",
                artifacts=artifacts,
                metadata=metadata,
            )
            backend.store_result(request, lease, result)
            return result
    except MediaOperationError as exc:
        logger.warning("media job failed: job_id=%s code=%s", request.job_id, exc.code)
        result = _failed_result(request, exc.code)
        backend.cleanup_attempt(request, lease)
        backend.store_result(request, lease, result)
        return result
    except Exception:
        logger.exception("media worker failed: job_id=%s", request.job_id)
        result = _failed_result(request, "MEDIA_WORKER_FAILED")
        backend.cleanup_attempt(request, lease)
        backend.store_result(request, lease, result)
        return result


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    # Fargate task-scoped /tmp, owned by the non-root runtime user.
    runtime_root = Path("/tmp/teamagent")  # nosec B108
    for name in _RUNTIME_DIRECTORIES:
        (runtime_root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    body = os.environ.get("MEDIA_JOB_JSON") or os.environ.get("TIKTOK_JOB_JSON", "")
    if not body:
        logger.error("MEDIA_JOB_JSON is required")
        return 2
    try:
        request = parse_job_request(body)
        result = run_job(
            request,
            AwsWorkerBackend(),
            owner=os.environ.get("ECS_TASK_ARN") or f"task-{uuid.uuid4().hex}",
        )
    except Exception:
        logger.exception("media worker envelope rejected")
        return 2
    if result.status == "running" and result.metadata.get("duplicate_delivery") is True:
        return 0
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AwsWorkerBackend",
    "WorkerBackend",
    "WorkerClaim",
    "WorkerLease",
    "main",
    "run_job",
]
