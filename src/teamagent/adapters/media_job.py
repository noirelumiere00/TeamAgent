"""Core側の bounded media job submit/poll/download/cleanup adapter。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import structlog

from teamagent.media.contracts import (
    MAX_INPUT_BYTES,
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
    make_job_request,
)

logger = structlog.get_logger(__name__)

_SYNC_TIMEOUT_DEFAULT_S = 180
_SYNC_TIMEOUT_MAX_S = 15 * 60
_ARTIFACT_TTL_DEFAULT_S = 3600
_JOB_ID_RE = re.compile(r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$")


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
        self._session_override = session

    @classmethod
    def is_configured(cls) -> bool:
        return all(
            os.environ.get(name)
            for name in ("MEDIA_TASK_QUEUE", "MEDIA_JOBS_TABLE", "MEDIA_JOB_BUCKET")
        )

    def _session(self) -> Any:
        if self._session_override is not None:
            return self._session_override
        import boto3

        return boto3.session.Session()

    def _clients(self) -> tuple[Any, Any, Any]:
        session = self._session()
        return (
            session.client("sqs", region_name=self._region),
            session.client("dynamodb", region_name=self._region),
            session.client("s3", region_name=self._region),
        )

    def _assert_configured(self) -> None:
        if not self._queue_url or not self._table or not self._bucket:
            raise MediaJobError("MEDIA_JOB_NOT_CONFIGURED")

    def _sse_args(self) -> dict[str, str]:
        if self._kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self._kms_key_id,
            }
        return {"ServerSideEncryption": "AES256"}

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
        ttl_s: int = _ARTIFACT_TTL_DEFAULT_S,
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
        if ttl_s < 60 or ttl_s > _ARTIFACT_TTL_DEFAULT_S:
            raise MediaJobError("MEDIA_INPUT_TTL_INVALID")
        _sqs, _ddb, s3 = self._clients()
        digest = hashlib.sha256(body).hexdigest()
        key = f"media-jobs/{job_id}/input/{name}"
        expires = datetime.fromtimestamp(int(time.time()) + ttl_s, tz=UTC)
        s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
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
        """Dynamo queuedを条件付き作成後、bounded canonical JSONをSQSへ送る。"""

        self._assert_configured()
        if request.output_bucket != self._bucket:
            raise MediaJobError("MEDIA_JOB_BUCKET_MISMATCH")
        sqs, ddb, _s3 = self._clients()
        body = request.to_json_bytes().decode("utf-8")
        queued = MediaJobResult(job_id=request.job_id, status="queued")
        try:
            ddb.put_item(
                TableName=self._table,
                Item={
                    "job_id": {"S": request.job_id},
                    "idempotency_key": {"S": request.idempotency_key},
                    "payload_sha256": {"S": request.payload_sha256},
                    "status": {"S": "queued"},
                    "created_at": {"N": str(request.created_at_epoch_s)},
                    "ttl": {"N": str(request.created_at_epoch_s + request.artifact_ttl_s)},
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
        except Exception as exc:
            if not _is_conditional_conflict(exc):
                raise MediaJobError("MEDIA_JOB_STATE_CREATE_FAILED") from exc
            existing = ddb.get_item(
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
                ConsistentRead=True,
            ).get("Item", {})
            if existing.get("idempotency_key", {}).get("S") != request.idempotency_key:
                raise MediaJobError("MEDIA_JOB_IDEMPOTENCY_CONFLICT") from exc
            return request.job_id

        arguments: dict[str, Any] = {
            "QueueUrl": self._queue_url,
            "MessageBody": body,
            "MessageAttributes": {
                "schema_version": {"DataType": "String", "StringValue": "1"},
                "payload_sha256": {
                    "DataType": "String",
                    "StringValue": request.payload_sha256,
                },
            },
        }
        if self._queue_url.endswith(".fifo"):
            arguments["MessageDeduplicationId"] = request.idempotency_key
            arguments["MessageGroupId"] = "teamagent-media"
        try:
            sqs.send_message(**arguments)
        except Exception as exc:
            ddb.delete_item(
                TableName=self._table,
                Key={"job_id": {"S": request.job_id}},
            )
            raise MediaJobError("MEDIA_JOB_SUBMIT_FAILED") from exc
        return request.job_id

    def get_result(self, job_id: str) -> MediaJobResult | None:
        self._assert_configured()
        _sqs, ddb, _s3 = self._clients()
        response = ddb.get_item(
            TableName=self._table,
            Key={"job_id": {"S": job_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        detail = item.get("detail", {}).get("S", "")
        try:
            result = MediaJobResult.model_validate_json(detail)
            persisted_status = item.get("status", {}).get("S")
            if persisted_status in ("queued", "running") and result.status != persisted_status:
                result = result.model_copy(update={"status": persisted_status})
            return result
        except Exception as exc:
            raise MediaJobError("MEDIA_JOB_RESULT_INVALID") from exc

    def wait(
        self,
        job_id: str,
        *,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
        poll_interval_s: float = 1.0,
    ) -> MediaJobResult:
        if timeout_s < 1 or timeout_s > _SYNC_TIMEOUT_MAX_S:
            raise MediaJobError("MEDIA_JOB_TIMEOUT_INVALID")
        deadline = self._monotonic() + timeout_s
        while self._monotonic() <= deadline:
            result = self.get_result(job_id)
            if result is not None and result.status in ("done", "failed"):
                return result
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleeper(min(max(poll_interval_s, 0.1), remaining, 5.0))
        raise MediaJobError("MEDIA_JOB_TIMEOUT")

    def download(self, ref: S3ObjectRef) -> bytes:
        self._assert_configured()
        if ref.bucket != self._bucket:
            raise MediaJobError("MEDIA_ARTIFACT_BUCKET_MISMATCH")
        _sqs, _ddb, s3 = self._clients()
        response = s3.get_object(Bucket=ref.bucket, Key=ref.key)
        if response.get("ServerSideEncryption") not in ("AES256", "aws:kms"):
            raise MediaJobError("MEDIA_ARTIFACT_NOT_ENCRYPTED")
        body = bytes(response["Body"].read(ref.size + 1))
        if len(body) != ref.size or hashlib.sha256(body).hexdigest() != ref.sha256:
            raise MediaJobError("MEDIA_ARTIFACT_INTEGRITY_FAILED")
        return body

    def cleanup(self, request: MediaJobRequest) -> None:
        """job prefixとstatus rowをbest-effortで完全削除する。"""

        self._cleanup_scope(request.job_id, request.output_prefix)

    def cleanup_job(self, job_id: str) -> None:
        """request構築前の失敗も含め、job prefixとstatus rowをbest-effort削除する。"""

        prefix = (
            f"tiktok-acquire/{job_id}/" if job_id.startswith("tk_") else f"media-jobs/{job_id}/"
        )
        self._cleanup_scope(job_id, prefix)

    def _cleanup_scope(self, job_id: str, prefix: str) -> None:
        """検証済みjob固有prefixだけを削除する。"""

        if not _JOB_ID_RE.fullmatch(job_id):
            logger.warning("media_job_cleanup_scope_rejected", job_id=job_id)
            return
        approved = {f"media-jobs/{job_id}/", f"tiktok-acquire/{job_id}/"}
        if prefix not in approved:
            logger.warning("media_job_cleanup_scope_rejected", job_id=job_id)
            return
        try:
            _sqs, ddb, s3 = self._clients()
            continuation: str | None = None
            while True:
                arguments: dict[str, Any] = {
                    "Bucket": self._bucket,
                    "Prefix": prefix,
                    "MaxKeys": 1000,
                }
                if continuation:
                    arguments["ContinuationToken"] = continuation
                response = s3.list_objects_v2(**arguments)
                keys = [{"Key": item["Key"]} for item in response.get("Contents", [])]
                if keys:
                    s3.delete_objects(
                        Bucket=self._bucket,
                        Delete={"Objects": keys, "Quiet": True},
                    )
                if not response.get("IsTruncated"):
                    break
                continuation = str(response["NextContinuationToken"])
            ddb.delete_item(
                TableName=self._table,
                Key={"job_id": {"S": job_id}},
            )
        except Exception as exc:
            logger.warning(
                "media_job_cleanup_failed",
                job_id=job_id,
                error=type(exc).__name__,
            )

    def _run_staged(
        self,
        *,
        job_id: str,
        request_fingerprint: str,
        timeout_s: int,
        operation_factory: Callable[[], MediaOperation],
    ) -> tuple[Mapping[str, bytes], Mapping[str, Any]]:
        """stage/operation/request途中の例外でも、作成済みinputを残さない。"""

        request: MediaJobRequest | None = None
        try:
            operation = operation_factory()
            request = self._request(
                operation,
                request_fingerprint,
                timeout_s,
                job_id=job_id,
            )
            return self.run_sync(request, timeout_s=timeout_s)
        finally:
            # run_sync に到達した場合は同メソッドの finally が cleanup する。
            if request is None:
                self.cleanup_job(job_id)

    def run_sync(
        self,
        request: MediaJobRequest,
        *,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> tuple[Mapping[str, bytes], Mapping[str, Any]]:
        """submit→bounded poll→hash付きdownload→finally cleanup。"""

        try:
            self.submit(request)
            result = self.wait(request.job_id, timeout_s=timeout_s)
            if result.status != "done":
                raise MediaJobError(result.error_code or "MEDIA_JOB_FAILED")
            artifacts = {
                artifact.name: self.download(artifact.object) for artifact in result.artifacts
            }
            return artifacts, result.metadata
        finally:
            self.cleanup(request)

    def acquire_video(
        self,
        url: str,
        *,
        request_fingerprint: str,
        max_bytes: int = 30 * 1024 * 1024,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> tuple[bytes, str]:
        operation = AcquireOperation(kind="acquire", url=url, max_bytes=max_bytes)
        request = self._request(operation, request_fingerprint, timeout_s)
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
        max_videos: int = 10,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> list[dict[str, Any]]:
        """既存の同期検索UXをgeneric TikTok media operationで維持する。"""

        operation = TikTokAcquireOperation(
            kind="tiktok_acquire",
            keywords=(query,),
            n_per_kw=max_videos,
            videos_per_kw=0,
            sort="display",
            client=TikTokClientConfig(),
        )
        request = self._request(operation, request_fingerprint, timeout_s)
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

        def operation_factory() -> MediaOperation:
            source = self.stage_bytes(
                job_id=job_id,
                name="source.bin",
                body=data,
                content_type=mime,
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

        def operation_factory() -> MediaOperation:
            source = self.stage_bytes(
                job_id=job_id,
                name="source.bin",
                body=data,
                content_type=mime,
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

        def operation_factory() -> MediaOperation:
            source = self.stage_bytes(
                job_id=job_id,
                name="source.bin",
                body=data,
                content_type=mime,
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
        request = self._request(operation, request_fingerprint, timeout_s)
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

        def operation_factory() -> MediaOperation:
            html_ref = self.stage_bytes(
                job_id=job_id,
                name="slides.html",
                body=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
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

        def operation_factory() -> MediaOperation:
            html_ref = self.stage_bytes(
                job_id=job_id,
                name="document.html",
                body=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
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

        def operation_factory() -> MediaOperation:
            template_ref = self.stage_bytes(
                job_id=job_id,
                name="template.pptx",
                body=template,
                content_type=(
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                ),
            )
            composer_ref = self.stage_bytes(
                job_id=job_id,
                name="composer.json",
                body=composer_json,
                content_type="application/json",
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
    ) -> MediaJobRequest:
        self._assert_configured()
        return make_job_request(
            operation=operation,
            output_bucket=self._bucket,
            request_fingerprint=request_fingerprint,
            timeout_s=timeout_s,
            artifact_ttl_s=_ARTIFACT_TTL_DEFAULT_S,
            job_id=job_id,
        )


def _is_conditional_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    return isinstance(error, dict) and error.get("Code") == "ConditionalCheckFailedException"


__all__ = ["MediaJobClient", "MediaJobError"]
