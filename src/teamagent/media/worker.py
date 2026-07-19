"""1 process / 1 request の teamagent-media-worker entrypoint。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import signal
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import teamagent.media.operations as media_operations
from teamagent.media.contracts import (
    ARTIFACT_RETENTION_SECONDS,
    DDB_RETENTION_GRACE_SECONDS,
    MAX_INPUT_BYTES,
    MAX_JOB_BUDGET_SECONDS,
    MAX_OUTPUT_BYTES,
    MediaArtifact,
    MediaJobRequest,
    MediaJobResult,
    S3ObjectRef,
    TikTokAcquireOperation,
    artifact_manifest_sha256,
    parse_job_request,
)
from teamagent.media.deadline import DeadlineBudget, MediaDeadlineExceededError, botocore_config
from teamagent.media.operations import (
    MediaOperationError,
    ProducedArtifact,
    execute_operation,
)

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
_TERMINAL_RESERVE_SECONDS = 15.0
_PROCESS_TERMINATION_GRACE_SECONDS = 0.5
_PROCESS_REAP_POLL_SECONDS = 0.01
_PROCESS_TRACKER_LOCK_TIMEOUT_SECONDS = 0.05


def _checksum_sha256_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


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
    attempt_id: str


@dataclass(frozen=True, slots=True)
class WorkerClaim:
    lease: WorkerLease | None = None
    existing_result: MediaJobResult | None = None


class _WorkerTerminatedError(RuntimeError):
    pass


class _TerminalWriteState(Enum):
    COMMITTED = "committed"
    OWNED_RUNNING = "owned_running"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class _TerminalResultWriteError(RuntimeError):
    """A done transition that must not be downgraded or destructively cleaned."""

    def __init__(self, state: _TerminalWriteState) -> None:
        super().__init__(f"terminal result write was not committed: {state.value}")
        self.state = state


def _tracked_process_groups() -> tuple[int, ...]:
    """Snapshot operation-owned process groups without deadlocking a signal handler."""

    lock = media_operations._ACTIVE_PROCESS_GROUPS_LOCK
    acquired = lock.acquire(timeout=_PROCESS_TRACKER_LOCK_TIMEOUT_SECONDS)
    try:
        return tuple(media_operations._ACTIVE_PROCESS_GROUPS)
    finally:
        if acquired:
            lock.release()


def _discard_tracked_process_groups(process_groups: tuple[int, ...]) -> None:
    lock = media_operations._ACTIVE_PROCESS_GROUPS_LOCK
    acquired = lock.acquire(timeout=_PROCESS_TRACKER_LOCK_TIMEOUT_SECONDS)
    try:
        media_operations._ACTIVE_PROCESS_GROUPS.difference_update(process_groups)
    finally:
        if acquired:
            lock.release()


def _reap_process_group_leader(process_group: int, *, block: bool) -> bool:
    options = 0 if block else os.WNOHANG
    while True:
        try:
            waited, _status = os.waitpid(process_group, options)
        except InterruptedError:
            continue
        except ChildProcessError:
            return True
        return waited == process_group


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_and_reap_active_process_groups(
    *,
    grace_s: float = _PROCESS_TERMINATION_GRACE_SECONDS,
) -> None:
    """TERM, then KILL and reap every process group active at signal delivery."""

    process_groups = _tracked_process_groups()
    if not process_groups:
        return
    for process_group in process_groups:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass

    reaped: set[int] = set()
    grace_deadline = time.monotonic() + grace_s
    live_groups = set(process_groups)
    while live_groups:
        for process_group in tuple(live_groups):
            if process_group not in reaped and _reap_process_group_leader(
                process_group,
                block=False,
            ):
                reaped.add(process_group)
            if not _process_group_exists(process_group):
                live_groups.discard(process_group)
        remaining = grace_deadline - time.monotonic()
        if not live_groups or remaining <= 0:
            break
        time.sleep(min(_PROCESS_REAP_POLL_SECONDS, remaining))

    for process_group in live_groups:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for process_group in process_groups:
        if process_group not in reaped:
            _reap_process_group_leader(process_group, block=True)
    _discard_tracked_process_groups(process_groups)


@contextmanager
def _worker_signal_scope(deadline_epoch_s: float) -> Any:
    """Install one absolute execution watchdog and process-group-aware SIGTERM handler."""

    previous_alarm = signal.getsignal(signal.SIGALRM)
    previous_term = signal.getsignal(signal.SIGTERM)
    terminating = False

    def terminate_for_signal(error: BaseException) -> None:
        nonlocal terminating
        if terminating:
            return
        terminating = True
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            _terminate_and_reap_active_process_groups()
        except Exception:
            logger.exception("failed to terminate all active media process groups")
        raise error

    def deadline_handler(_signum: int, _frame: Any) -> None:
        terminate_for_signal(MediaDeadlineExceededError("media job deadline exceeded"))

    def term_handler(_signum: int, _frame: Any) -> None:
        terminate_for_signal(_WorkerTerminatedError("media worker termination requested"))

    remaining = deadline_epoch_s - time.time()
    if remaining <= 0:
        raise MediaDeadlineExceededError("media job deadline exceeded")
    signal.signal(signal.SIGALRM, deadline_handler)
    signal.signal(signal.SIGTERM, term_handler)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)
        signal.signal(signal.SIGTERM, previous_term)


def _conditional_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code == "ConditionalCheckFailedException"


class AwsWorkerBackend:
    """S3/DynamoDBだけを使うworker backend。RDS/Slack/OAuth/MCP権限は不要。"""

    def __init__(
        self,
        *,
        deadline_epoch_s: int,
        session: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._region = os.environ.get("AWS_REGION", "ap-northeast-1")
        self._bucket = os.environ.get("MEDIA_JOB_BUCKET", "")
        self._table = os.environ.get("MEDIA_JOBS_TABLE", "")
        self._kms_key_id = os.environ.get("MEDIA_JOB_KMS_KEY_ID", "")
        if not self._bucket or not self._table:
            raise RuntimeError("MEDIA_JOB_BUCKET and MEDIA_JOBS_TABLE are required")
        try:
            self._artifact_ttl_cap_s = int(
                os.environ.get(
                    "MEDIA_ARTIFACT_TTL_SECONDS",
                    str(ARTIFACT_RETENTION_SECONDS),
                )
            )
        except ValueError as exc:
            raise RuntimeError("MEDIA_ARTIFACT_TTL_SECONDS is invalid") from exc
        if not 300 <= self._artifact_ttl_cap_s <= ARTIFACT_RETENTION_SECONDS:
            raise RuntimeError("MEDIA_ARTIFACT_TTL_SECONDS is invalid")
        now_epoch_s = int(clock())
        if (
            deadline_epoch_s <= now_epoch_s
            or deadline_epoch_s - now_epoch_s > MAX_JOB_BUDGET_SECONDS
        ):
            raise RuntimeError("MEDIA_JOB_DEADLINE_EPOCH_S is invalid")
        if session is None:
            import boto3

            session = boto3.session.Session()
        self._session = session
        self._deadline_epoch_s = deadline_epoch_s
        self._clock = clock

    def _client(self, service: str) -> Any:
        override_name = {"dynamodb": "_ddb", "s3": "_s3"}.get(service, f"_{service}")
        override = getattr(self, override_name, None)
        if override is not None:
            return override
        budget = DeadlineBudget(self._deadline_epoch_s, clock=self._clock)
        return self._session.client(
            service,
            region_name=self._region,
            config=botocore_config(budget),
        )

    def _call(self, service: str, operation: str, **kwargs: Any) -> Any:
        return getattr(self._client(service), operation)(**kwargs)

    def _sse_args(self) -> dict[str, str]:
        if self._kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self._kms_key_id,
            }
        return {"ServerSideEncryption": "AES256"}

    def load_request(self, job_id: str, payload_sha256: str) -> MediaJobRequest:
        """Load the exact canonical envelope behind a bounded ECS override."""

        response = self._call(
            "dynamodb",
            "get_item",
            TableName=self._table,
            Key={"job_id": {"S": job_id}},
            ConsistentRead=True,
        )
        item = response.get("Item", {})
        raw = item.get("request_json", {}).get("S", "")
        if not raw:
            raise ValueError("media job request pointer is missing")
        request = parse_job_request(raw)
        persisted_audit_hash = item.get("audit_principal_hash", {}).get("S", "")
        request_audit_hash = request.audit_principal_hash or ""
        if (
            request.job_id != job_id
            or request.payload_sha256 != payload_sha256
            or item.get("payload_sha256", {}).get("S") != payload_sha256
            or item.get("idempotency_key", {}).get("S") != request.idempotency_key
            or request.deadline_epoch_s != self._deadline_epoch_s
            or not hmac.compare_digest(persisted_audit_hash, request_audit_hash)
        ):
            raise ValueError("media job request pointer does not match persisted envelope")
        return request

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
        attempt_id = str(uuid.uuid4())
        audit_condition = (
            "audit_principal_hash = :audit"
            if request.audit_principal_hash
            else "attribute_not_exists(audit_principal_hash)"
        )
        values: dict[str, Any] = {
            ":queued": {"S": "queued"},
            ":running": {"S": "running"},
            ":owner": {"S": owner},
            ":attempt": {"S": attempt_id},
            ":lease": {"N": str(lease_expires)},
            ":now": {"N": str(now_epoch_s)},
            ":one": {"N": "1"},
            ":idempotency": {"S": request.idempotency_key},
            ":payload": {"S": request.payload_sha256},
        }
        if request.audit_principal_hash:
            values[":audit"] = {"S": request.audit_principal_hash}
        try:
            response = self._call(
                "dynamodb",
                "update_item",
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                UpdateExpression=(
                    "SET #status = :running, lease_owner = :owner, "
                    "lease_expires_at = :lease, attempt_id = :attempt, "
                    "updated_at = :now ADD #version :one"
                ),
                ConditionExpression=(
                    "attribute_exists(job_id) AND idempotency_key = :idempotency AND "
                    f"payload_sha256 = :payload AND {audit_condition} AND "
                    "(attribute_not_exists(orphan_cleanup_owner) OR "
                    "orphan_cleanup_lease_expires_at < :now) AND "
                    "(#status = :queued OR "
                    "(#status = :running AND lease_expires_at < :now))"
                ),
                ExpressionAttributeNames={"#status": "status", "#version": "version"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if not _conditional_conflict(exc):
                raise
            item = self._call(
                "dynamodb",
                "get_item",
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                ConsistentRead=True,
            ).get("Item", {})
            if (
                item.get("idempotency_key", {}).get("S") != request.idempotency_key
                or item.get("payload_sha256", {}).get("S") != request.payload_sha256
                or not hmac.compare_digest(
                    item.get("audit_principal_hash", {}).get("S", ""),
                    request.audit_principal_hash or "",
                )
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
        return WorkerClaim(lease=WorkerLease(owner=owner, version=version, attempt_id=attempt_id))

    def _renew_lease(self, request: MediaJobRequest, lease: WorkerLease) -> None:
        now = int(self._clock())
        self._call(
            "dynamodb",
            "update_item",
            TableName=self._table,
            Key={"job_id": {"S": request.job_id}},
            UpdateExpression="SET lease_expires_at = :lease, updated_at = :now",
            ConditionExpression=(
                "#status = :running AND lease_owner = :owner AND #version = :version "
                "AND attempt_id = :attempt AND "
                "attribute_not_exists(orphan_cleanup_owner)"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={
                ":running": {"S": "running"},
                ":owner": {"S": lease.owner},
                ":attempt": {"S": lease.attempt_id},
                ":version": {"N": str(lease.version)},
                ":lease": {"N": str(max(request.deadline_epoch_s + 60, now + 60))},
                ":now": {"N": str(now)},
            },
        )

    def _assert_lease_fence(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
    ) -> None:
        """Conditionally prove that this exact attempt still owns the lease."""

        now = int(self._clock())
        self._call(
            "dynamodb",
            "update_item",
            TableName=self._table,
            Key={"job_id": {"S": request.job_id}},
            UpdateExpression="SET fence_checked_at = :now",
            ConditionExpression=(
                "#status = :running AND lease_owner = :owner AND #version = :version "
                "AND attempt_id = :attempt AND "
                "attribute_not_exists(orphan_cleanup_owner)"
            ),
            ExpressionAttributeNames={"#status": "status", "#version": "version"},
            ExpressionAttributeValues={
                ":running": {"S": "running"},
                ":owner": {"S": lease.owner},
                ":attempt": {"S": lease.attempt_id},
                ":version": {"N": str(lease.version)},
                ":now": {"N": str(now)},
            },
        )

    @staticmethod
    def _attempt_prefix(request: MediaJobRequest, lease: WorkerLease) -> str:
        return f"{request.output_prefix}attempts/{lease.version}/{lease.attempt_id}/"

    def _delete_exact_attempt_key(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        key: str,
    ) -> None:
        """Delete only an object demonstrably written by this attempt UUID."""

        prefix = self._attempt_prefix(request, lease)
        if not key.startswith(prefix):
            raise ValueError("refusing to delete object outside exact attempt prefix")
        try:
            response = self._call("s3", "head_object", Bucket=self._bucket, Key=key)
        except Exception as exc:
            error = getattr(exc, "response", {})
            code = error.get("Error", {}).get("Code") if isinstance(error, dict) else None
            if code in {"404", "NoSuchKey", "NotFound"}:
                return
            raise
        metadata = response.get("Metadata", {})
        if (
            metadata.get("job-id") != request.job_id
            or metadata.get("attempt-id") != lease.attempt_id
            or metadata.get("lease-version") != str(lease.version)
        ):
            raise RuntimeError("refusing to delete object without exact attempt metadata")
        self._call("s3", "delete_object", Bucket=self._bucket, Key=key)

    def _delete_attempt_without_fence(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
    ) -> None:
        """Reclaim this attempt's objects without reacquiring a lost lease."""

        prefix = self._attempt_prefix(request, lease)
        continuation: str | None = None
        while True:
            arguments: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if continuation:
                arguments["ContinuationToken"] = continuation
            response = self._call("s3", "list_objects_v2", **arguments)
            for item in response.get("Contents", []):
                self._delete_exact_attempt_key(request, lease, str(item["Key"]))
            if not response.get("IsTruncated"):
                return
            continuation = str(response["NextContinuationToken"])

    def _put_finalize_marker(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        result: MediaJobResult,
    ) -> str:
        prefix = self._attempt_prefix(request, lease)
        marker_key = f"{prefix}_FINALIZED.json"
        marker = json.dumps(
            {
                "schema_version": "1",
                "job_id": request.job_id,
                "attempt_id": lease.attempt_id,
                "lease_version": lease.version,
                "artifact_manifest_sha256": artifact_manifest_sha256(result.artifacts),
                "artifacts": [
                    {
                        "name": artifact.name,
                        "key": artifact.object.key,
                        "sha256": artifact.object.sha256,
                        "size": artifact.object.size,
                        "content_type": artifact.object.content_type,
                    }
                    for artifact in result.artifacts
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        marker_digest = hashlib.sha256(marker).hexdigest()
        for artifact in result.artifacts:
            if not artifact.object.key.startswith(prefix):
                raise ValueError("result artifact is outside exact attempt prefix")
        expires_at = datetime.fromtimestamp(
            int(self._clock()) + request.artifact_ttl_s,
            tz=UTC,
        )
        self._call(
            "s3",
            "put_object",
            Bucket=self._bucket,
            Key=marker_key,
            Body=marker,
            ContentType="application/json",
            ChecksumSHA256=_checksum_sha256_b64(marker_digest),
            Expires=expires_at,
            Metadata={
                "sha256": marker_digest,
                "schema-version": request.schema_version,
                "job-id": request.job_id,
                "attempt-id": lease.attempt_id,
                "lease-version": str(lease.version),
                "finalized": "true",
            },
            Tagging=(
                f"teamagent-ttl-epoch={int(expires_at.timestamp())}"
                f"&teamagent-attempt-id={lease.attempt_id}"
                "&teamagent-finalized=true"
            ),
            **self._sse_args(),
        )
        try:
            self._assert_lease_fence(request, lease)
        except Exception:
            self._delete_attempt_without_fence(request, lease)
            raise
        return marker_key

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
        response = self._call("s3", "get_object", Bucket=ref.bucket, Key=ref.key)
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
        key = f"{self._attempt_prefix(request, lease)}{relative_key}"
        expires_at = datetime.fromtimestamp(
            int(self._clock()) + request.artifact_ttl_s,
            tz=UTC,
        )
        self._call(
            "s3",
            "put_object",
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=artifact.content_type,
            ChecksumSHA256=_checksum_sha256_b64(digest),
            Expires=expires_at,
            Metadata={
                "sha256": digest,
                "schema-version": request.schema_version,
                "job-id": request.job_id,
                "attempt-id": lease.attempt_id,
                "lease-version": str(lease.version),
                "finalized": "false",
            },
            Tagging=(
                f"teamagent-ttl-epoch={int(expires_at.timestamp())}"
                f"&teamagent-attempt-id={lease.attempt_id}"
                "&teamagent-finalized=false"
            ),
            **self._sse_args(),
        )
        try:
            self._assert_lease_fence(request, lease)
        except Exception:
            self._delete_exact_attempt_key(request, lease, key)
            raise
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
        now = int(self._clock())
        marker_key: str | None = None
        manifest_expression = ""
        manifest_values: dict[str, Any] = {}
        if result.status == "done":
            marker_key = self._put_finalize_marker(request, lease, result)
            manifest_expression = ", artifact_manifest_sha256 = :artifact_manifest"
            manifest_values[":artifact_manifest"] = {
                "S": artifact_manifest_sha256(result.artifacts)
            }
        finalized_expression = ", finalized_attempt_id = :attempt" if marker_key is not None else ""
        try:
            self._call(
                "dynamodb",
                "update_item",
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                UpdateExpression=(
                    "SET #status = :status, detail = :detail, updated_at = :now, "
                    "cleanup_at = :cleanup, cleanup_status = :pending, ttl = :ttl"
                    f"{finalized_expression}{manifest_expression} "
                    "REMOVE lease_expires_at ADD #version :one"
                ),
                ConditionExpression=(
                    "#status = :running AND lease_owner = :owner AND #version = :version "
                    "AND attempt_id = :attempt AND "
                    "attribute_not_exists(orphan_cleanup_owner)"
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
                    ":attempt": {"S": lease.attempt_id},
                    ":version": {"N": str(lease.version)},
                    ":one": {"N": "1"},
                    ":now": {"N": str(now)},
                    ":cleanup": {"N": str(request.created_at_epoch_s + request.artifact_ttl_s)},
                    ":pending": {"S": "pending"},
                    ":ttl": {
                        "N": str(
                            request.created_at_epoch_s
                            + request.artifact_ttl_s
                            + DDB_RETENTION_GRACE_SECONDS
                        )
                    },
                    **manifest_values,
                },
            )
        except Exception as exc:
            state = _TerminalWriteState.UNKNOWN
            try:
                state = self._terminal_write_state(request, lease, result)
            except Exception:
                logger.exception(
                    "terminal result reconciliation read failed: job_id=%s",
                    request.job_id,
                )
            if state is _TerminalWriteState.COMMITTED:
                logger.warning(
                    "terminal result reconciled after ambiguous write: job_id=%s",
                    request.job_id,
                )
                return
            if marker_key is not None and state is _TerminalWriteState.SUPERSEDED:
                try:
                    self._delete_attempt_without_fence(request, lease)
                except Exception:
                    logger.exception(
                        "superseded done-attempt cleanup deferred to janitor: job_id=%s",
                        request.job_id,
                    )
            if marker_key is not None:
                if state is not _TerminalWriteState.SUPERSEDED:
                    logger.error(
                        "preserving unconfirmed done artifacts: job_id=%s state=%s",
                        request.job_id,
                        state.value,
                    )
                raise _TerminalResultWriteError(state) from exc
            raise

    def _terminal_write_state(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        result: MediaJobResult,
    ) -> _TerminalWriteState:
        """Strongly read the row and classify an ambiguous terminal UpdateItem."""

        item = self._call(
            "dynamodb",
            "get_item",
            TableName=self._table,
            Key={"job_id": {"S": request.job_id}},
            ConsistentRead=True,
        ).get("Item", {})
        if (
            item.get("idempotency_key", {}).get("S") != request.idempotency_key
            or item.get("payload_sha256", {}).get("S") != request.payload_sha256
        ):
            return _TerminalWriteState.UNKNOWN
        try:
            version = int(item.get("version", {}).get("N", "0"))
        except (TypeError, ValueError):
            return _TerminalWriteState.UNKNOWN
        status = item.get("status", {}).get("S", "")
        attempt_id = item.get("attempt_id", {}).get("S", "")
        lease_owner = item.get("lease_owner", {}).get("S", "")

        if status in {"done", "failed"}:
            persisted = self._result_from_item(item)
            finalized_attempt = item.get("finalized_attempt_id", {}).get("S", "")
            finalized_matches = result.status != "done" or finalized_attempt == lease.attempt_id
            if (
                status == result.status
                and persisted == result
                and attempt_id == lease.attempt_id
                and lease_owner == lease.owner
                and version >= lease.version + 1
                and finalized_matches
            ):
                return _TerminalWriteState.COMMITTED
            return _TerminalWriteState.SUPERSEDED

        if (
            status == "running"
            and version == lease.version
            and attempt_id == lease.attempt_id
            and lease_owner == lease.owner
        ):
            return _TerminalWriteState.OWNED_RUNNING
        if status == "running" and (
            version > lease.version or attempt_id != lease.attempt_id or lease_owner != lease.owner
        ):
            return _TerminalWriteState.SUPERSEDED
        return _TerminalWriteState.UNKNOWN

    def cleanup_attempt(self, request: MediaJobRequest, lease: WorkerLease) -> None:
        self._delete_attempt_without_fence(request, lease)

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
            response = self._call("s3", "list_objects_v2", **arguments)
            keys = [{"Key": item["Key"]} for item in response.get("Contents", [])]
            if keys:
                deleted = self._call(
                    "s3",
                    "delete_objects",
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


def _store_failed_and_cleanup(
    request: MediaJobRequest,
    backend: WorkerBackend,
    lease: WorkerLease,
    error_code: str,
) -> MediaJobResult:
    """Fence the terminal result first; orphan cleanup may safely be retried."""

    result = _failed_result(request, error_code)
    try:
        backend.store_result(request, lease, result)
    except Exception:
        # A lost lease must not authorize a terminal transition, but this exact
        # attempt UUID can still be reclaimed without reacquiring that lease.
        try:
            backend.cleanup_attempt(request, lease)
        except Exception:
            logger.exception(
                "failed-result cleanup deferred to janitor: job_id=%s",
                request.job_id,
            )
        raise
    try:
        backend.cleanup_attempt(request, lease)
    except Exception:
        logger.exception(
            "terminal attempt cleanup deferred to janitor: job_id=%s",
            request.job_id,
        )
    return result


def run_job(
    request: MediaJobRequest,
    backend: WorkerBackend,
    *,
    temp_root: Path | None = None,
    now_epoch_s: int | None = None,
    owner: str | None = None,
    clock: Callable[[], float] | None = None,
) -> MediaJobResult:
    """Execute one owner/version-fenced attempt in a bounded request directory."""

    now = int(time.time()) if now_epoch_s is None else now_epoch_s
    if now >= request.deadline_epoch_s:
        raise MediaDeadlineExceededError("media job deadline exceeded before claim")
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
    resolved_clock = clock or (time.time if now_epoch_s is None else lambda: float(now))
    hard_budget = DeadlineBudget(request.deadline_epoch_s, clock=resolved_clock)
    execution_deadline_epoch_s = float(request.deadline_epoch_s) - _TERMINAL_RESERVE_SECONDS
    execution_budget = DeadlineBudget(
        execution_deadline_epoch_s,
        clock=resolved_clock,
    )
    if float(now) >= execution_deadline_epoch_s:
        hard_budget.checkpoint()
        return _store_failed_and_cleanup(
            request,
            backend,
            lease,
            "MEDIA_JOB_DEADLINE_EXCEEDED",
        )
    install_watchdog = now_epoch_s is None and clock is None
    try:
        watchdog = (
            _worker_signal_scope(execution_deadline_epoch_s) if install_watchdog else nullcontext()
        )
        with watchdog:
            execution_budget.checkpoint()
            with tempfile.TemporaryDirectory(
                prefix=f"{request.job_id}-",
                dir=root,
            ) as raw_workdir:
                workdir = Path(raw_workdir)

                def load(ref: S3ObjectRef, destination: Path) -> Path:
                    execution_budget.checkpoint()
                    loaded = backend.load_object(request, lease, ref, destination)
                    execution_budget.checkpoint()
                    return loaded

                output = execute_operation(
                    request.operation,
                    workdir=workdir,
                    load_object=load,
                    budget=execution_budget,
                )
                artifacts_list: list[MediaArtifact] = []
                for artifact in output.artifacts:
                    execution_budget.checkpoint()
                    uploaded = backend.upload_artifact(request, lease, artifact)
                    execution_budget.checkpoint()
                    artifacts_list.append(
                        MediaArtifact(
                            name=artifact.name,
                            object=uploaded,
                        )
                    )
                artifacts = tuple(artifacts_list)
                metadata = dict(output.metadata)
                if isinstance(request.operation, TikTokAcquireOperation):
                    metadata["s3_prefix"] = (
                        f"{request.output_prefix}attempts/{lease.version}/{lease.attempt_id}/"
                    )
                result = MediaJobResult(
                    job_id=request.job_id,
                    status="done",
                    artifacts=artifacts,
                    metadata=metadata,
                )
                execution_budget.checkpoint()
        # The watchdog is deliberately disarmed before the terminal write.
        # That write consumes only the immutable hard-deadline reserve.
        hard_budget.checkpoint()
        backend.store_result(request, lease, result)
        return result
    except _TerminalResultWriteError:
        logger.exception(
            "done result write was not safely committed: job_id=%s",
            request.job_id,
        )
        raise
    except MediaDeadlineExceededError:
        logger.warning("media job deadline exhausted: job_id=%s", request.job_id)
        hard_budget.checkpoint()
        return _store_failed_and_cleanup(
            request,
            backend,
            lease,
            "MEDIA_JOB_DEADLINE_EXCEEDED",
        )
    except MediaOperationError as exc:
        logger.warning("media job failed: job_id=%s code=%s", request.job_id, exc.code)
        hard_budget.checkpoint()
        return _store_failed_and_cleanup(request, backend, lease, exc.code)
    except _WorkerTerminatedError:
        logger.warning("media worker termination requested: job_id=%s", request.job_id)
        hard_budget.checkpoint()
        return _store_failed_and_cleanup(
            request,
            backend,
            lease,
            "MEDIA_WORKER_TERMINATED",
        )
    except Exception:
        logger.exception("media worker failed: job_id=%s", request.job_id)
        hard_budget.checkpoint()
        return _store_failed_and_cleanup(
            request,
            backend,
            lease,
            "MEDIA_WORKER_FAILED",
        )


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    # Fargate task-scoped /tmp, owned by the non-root runtime user.
    runtime_root = Path("/tmp/teamagent")  # nosec B108
    for name in _RUNTIME_DIRECTORIES:
        (runtime_root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    job_id = os.environ.get("MEDIA_JOB_ID", "")
    payload_sha256 = os.environ.get("MEDIA_JOB_PAYLOAD_SHA256", "")
    if not job_id or not payload_sha256:
        logger.error("MEDIA_JOB_ID and MEDIA_JOB_PAYLOAD_SHA256 are required")
        return 2
    try:
        raw_deadline = os.environ.get("MEDIA_JOB_DEADLINE_EPOCH_S", "")
        if not raw_deadline or not raw_deadline.isdigit():
            raise ValueError("MEDIA_JOB_DEADLINE_EPOCH_S is required")
        deadline_epoch_s = int(raw_deadline)
        execution_deadline_epoch_s = deadline_epoch_s - _TERMINAL_RESERVE_SECONDS
        with _worker_signal_scope(execution_deadline_epoch_s):
            backend = AwsWorkerBackend(deadline_epoch_s=deadline_epoch_s)
            request = backend.load_request(job_id, payload_sha256)
        result = run_job(
            request,
            backend,
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
