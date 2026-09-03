"""Core側の bounded media job submit/poll/download/cleanup adapter。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

import structlog

from teamagent.media.contracts import (
    ARTIFACT_RETENTION_SECONDS,
    MAX_DEADLINE_SECONDS,
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_PRESIGNED_URL_SECONDS,
    MAX_PROPOSAL_PPTX_BYTES,
    TIKTOK_N_PER_KW_MAX,
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
_CREDENTIAL_EXPIRY_SAFETY_SECONDS = 60
_JOB_ID_RE = re.compile(r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$")

# ── boto3 session/client のプロセス内キャッシュ ────────────────────────────────
# ``MediaJobClient()`` は 20 箇所以上で「呼ぶたびに新規生成」されるため、インスタンス
# 変数に持たせても意味がない（proposal_job_store.py の client キャッシュはストアが
# 長命なので成立している）。同じ流儀のまま置き場所だけモジュールへ引き上げる。
#
# 実測（本リポの依存・arm64・20 回平均）:
#   新規 Session + client 生成 = 39.99 ms / 共有 Session から client = 8.09 ms(s3)
#   ・1.72 ms(dynamodb) / キャッシュ済み client の再利用 = 0.00001 ms
# ``wait()`` は 1 秒間隔で ``get_result()`` を回すので、1 ジョブあたりの無駄が
# core 秒オーダーで積み上がっていた。
#
# 排他: botocore の client は呼び出しについてはスレッド安全だが、Session からの
# client 生成はそうではない。生成だけをロックで囲う（proposal_job_store と同じ）。
_BOTO_SESSION: Any | None = None
_BOTO_SESSION_LOCK = threading.Lock()
# key = (service, region, phase_timeout)。phase_timeout は deadline 由来で
# ``min(30.0, remaining/2)`` なので、実運用の残予算（既定 180 秒〜）では常に 30.0 に
# 飽和し、実質 service 数ぶんしか増えない。締切間際だけ端数キーになるので上限を置き、
# 上限超過時はキャッシュせずその場限りの client を返す（無制限増殖を作らない）。
_BOTO_CLIENTS: dict[tuple[str, str, float], Any] = {}
_BOTO_CLIENT_CACHE_MAX = 16


def _shared_boto_session() -> Any:
    """プロセスで 1 つだけの boto3 Session を返す（credential/loader を共有する）。"""

    global _BOTO_SESSION
    if _BOTO_SESSION is None:
        with _BOTO_SESSION_LOCK:
            if _BOTO_SESSION is None:
                import boto3

                _BOTO_SESSION = boto3.session.Session()
    return _BOTO_SESSION


def reset_boto_cache() -> None:
    """キャッシュした Session/client を捨てる（テスト用・本番経路からは呼ばない）。"""

    global _BOTO_SESSION
    with _BOTO_SESSION_LOCK:
        _BOTO_SESSION = None
        _BOTO_CLIENTS.clear()


def _checksum_sha256_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def _artifact_identity_matches(ref: S3ObjectRef, metadata: Mapping[str, Any]) -> bool:
    """メタデータが参照先オブジェクトの出自と一致するか（書き手ごとに検査を選ぶ）。

    - core 自身が put するオブジェクト (入力等) は内容依存の ``sha256``
      メタデータを持つ。従来どおりそれを照合する。
    - worker の出力 (``media-jobs/<job>/attempts/<ver>/<attempt>/output/<name>``)
      は dispatcher の presigned POST 経由で put される。POST 条件は固定
      メタデータの完全一致なので内容依存値は原理的に載らない (実測: 実
      オブジェクトは job-id/attempt-id/attempt-version/capability-sha256 のみ)。
      この経路では dispatcher が焼き込む識別子とキー経路の一致を検査し、
      別ジョブ・別試行の成果物を掴まないことを保証する。
    どちらの経路でも内容の完全性は S3 の ChecksumSHA256 と download() の
    本文 sha256 照合が別途担保する。
    """
    if hmac.compare_digest(str(metadata.get("sha256") or ""), ref.sha256):
        return True
    parts = ref.key.split("/")
    if len(parts) < 6 or parts[0] != "media-jobs" or parts[2] != "attempts":
        return False
    job_id, attempt_version, attempt_id = parts[1], parts[3], parts[4]
    return (
        hmac.compare_digest(str(metadata.get("job-id") or ""), job_id)
        and hmac.compare_digest(str(metadata.get("attempt-id") or ""), attempt_id)
        and hmac.compare_digest(str(metadata.get("attempt-version") or ""), attempt_version)
    )


def _is_transient_network_error(exc: BaseException) -> bool:
    """botocore の接続層エラーか（deadline 予算内で再試行してよい失敗か）。

    deadline.py は「1 呼び出し＝リトライ 0 回」を意図的に貫くため、過渡的な
    ConnectTimeoutError 1 発がジョブ全体を落とす（実測: worker 成功・S3 に
    結果ありでも取得段の瞬断で TIKTOK_MEDIA_JOB_FAILED）。再試行の責務は
    残り予算を再計算できる呼び出し側ループにあり、その判定にのみ使う。
    """
    try:
        from botocore.exceptions import ConnectionError as _BotoConnectionError
        from botocore.exceptions import HTTPClientError as _BotoHTTPClientError
    except ImportError:  # pragma: no cover - botocore はランタイム必須依存
        return False
    return isinstance(exc, (_BotoConnectionError, _BotoHTTPClientError))


class MediaJobError(RuntimeError):
    """media job境界のsubmit/status/integrity失敗。"""


class MediaJobClient:
    """SQS送信・DynamoDB参照・限定S3権限だけで同期UXを維持する。"""

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
        return _shared_boto_session()

    def _client(self, service: str, deadline_epoch_s: float) -> Any:
        return self._client_from_session(self._session(), service, deadline_epoch_s)

    def _client_from_session(
        self,
        session: Any,
        service: str,
        deadline_epoch_s: float,
    ) -> Any:
        budget = DeadlineBudget(deadline_epoch_s, clock=self._clock)
        try:
            config = botocore_config(budget)
        except MediaDeadlineExceededError as exc:
            raise MediaJobError("MEDIA_JOB_DEADLINE_EXCEEDED") from exc
        if self._session_override is not None:
            # 注入された Session（テスト/呼び出し側の実体）は共有キャッシュへ混ぜない。
            return session.client(service, region_name=self._region, config=config)
        # config は deadline 由来なのでキーに含める＝残予算が変われば別 client になり、
        # 「短い締切なのに長い timeout の client を掴む」取り違えが起きない。
        key = (service, self._region, float(config.connect_timeout))
        cached = _BOTO_CLIENTS.get(key)
        if cached is not None:
            return cached
        with _BOTO_SESSION_LOCK:
            cached = _BOTO_CLIENTS.get(key)
            if cached is not None:
                return cached
            client = session.client(service, region_name=self._region, config=config)
            if len(_BOTO_CLIENTS) < _BOTO_CLIENT_CACHE_MAX:
                _BOTO_CLIENTS[key] = client
        return client

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
        max_bytes: int = MAX_INPUT_BYTES,
    ) -> S3ObjectRef:
        self._assert_configured()
        if not _JOB_ID_RE.fullmatch(job_id):
            raise MediaJobError("MEDIA_JOB_ID_INVALID")
        if max_bytes < 1 or max_bytes > MAX_PROPOSAL_PPTX_BYTES:
            raise MediaJobError("MEDIA_INPUT_BOUND_INVALID")
        if not body or len(body) > max_bytes:
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
        put_response = self._call(
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
        ref = S3ObjectRef(
            bucket=self._bucket,
            key=key,
            version_id=str(put_response.get("VersionId") or ""),
            sha256=digest,
            size=len(body),
            content_type=content_type,
        )
        self._verify_artifact_ref(ref, deadline_epoch_s=deadline_epoch_s)
        return ref

    def submit(self, request: MediaJobRequest) -> str:
        """Send one canonical intent; the trusted dispatcher owns all ledger writes."""

        self._assert_configured()
        self._remaining(request.deadline_epoch_s)
        if request.output_bucket != self._bucket:
            raise MediaJobError("MEDIA_JOB_BUCKET_MISMATCH")
        body = request.to_json_bytes().decode("utf-8")
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
            self._remaining(request.deadline_epoch_s)
            self._call(
                "sqs",
                request.deadline_epoch_s,
                "send_message",
                **arguments,
            )
        except MediaJobError:
            raise
        except Exception as exc:
            raise MediaJobError("MEDIA_JOB_SUBMIT_FAILED") from exc
        self._remaining(request.deadline_epoch_s)
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
            try:
                result = self.get_result(
                    job_id,
                    deadline_epoch_s=int(absolute_deadline),
                    expected_audit_principal_hash=expected_audit_principal_hash,
                )
            except MediaJobError:
                raise
            except Exception as exc:
                if not _is_transient_network_error(exc):
                    raise
                # 瞬断はこのポーリング 1 回の空振りとして扱う（全体は
                # monotonic/absolute の両 deadline が引き続き制限する）
                result = None
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
            VersionId=ref.version_id,
            ChecksumMode="ENABLED",
        )
        self._assert_exact_artifact_response(ref, response)
        body = bytes(response["Body"].read(ref.size + 1))
        if len(body) != ref.size or hashlib.sha256(body).hexdigest() != ref.sha256:
            raise MediaJobError("MEDIA_ARTIFACT_INTEGRITY_FAILED")
        return body

    @staticmethod
    def _assert_exact_artifact_response(
        ref: S3ObjectRef,
        response: dict[str, Any],
    ) -> None:
        metadata = response.get("Metadata")
        if (
            response.get("VersionId") != ref.version_id
            or response.get("ServerSideEncryption") not in ("AES256", "aws:kms")
            or response.get("ContentLength") != ref.size
            or response.get("ContentType") != ref.content_type
            or not hmac.compare_digest(
                str(response.get("ChecksumSHA256") or ""),
                _checksum_sha256_b64(ref.sha256),
            )
            or not isinstance(metadata, dict)
            # dispatcher の presigned POST は「固定メタデータの完全一致」条件で
            # 発行されるため、producer は内容依存値 (x-amz-meta-sha256) を原理的に
            # 付けられない＝設計時から不通過の検査だった (実測: 実オブジェクトの
            # metadata は job-id/attempt-id/attempt-version/capability-sha256 のみ)。
            # 内容の完全性は S3 が実バイトから計算した ChecksumSHA256 の一致
            # (上) と download() の本文 sha256 照合が担保する。ここでは代わりに
            # 固定メタデータが参照先の job/attempt と一致することを検査し、
            # 別ジョブ・別試行の成果物を掴まないことを保証する。
            or not _artifact_identity_matches(ref, metadata)
        ):
            raise MediaJobError("MEDIA_ARTIFACT_INTEGRITY_FAILED")

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
                VersionId=ref.version_id,
                ChecksumMode="ENABLED",
            )
        except MediaJobError:
            raise
        except Exception as exc:
            raise MediaJobError("MEDIA_ARTIFACT_HEAD_FAILED") from exc
        self._assert_exact_artifact_response(ref, response)

    def presign_get(
        self,
        ref: S3ObjectRef,
        *,
        deadline_epoch_s: int,
        expires_s: int,
    ) -> str:
        """Create a short URL bounded by the signing credential's lifetime."""

        self._assert_configured()
        if ref.bucket != self._bucket:
            raise MediaJobError("MEDIA_ARTIFACT_BUCKET_MISMATCH")
        if expires_s < 1 or expires_s > MAX_PRESIGNED_URL_SECONDS:
            raise MediaJobError("MEDIA_ARTIFACT_PRESIGN_EXPIRY_INVALID")
        self._verify_artifact_ref(ref, deadline_epoch_s=deadline_epoch_s)
        self._remaining(deadline_epoch_s)
        try:
            session = self._session()
            credentials = session.get_credentials()
            if credentials is None:
                raise MediaJobError("MEDIA_ARTIFACT_SIGNING_CREDENTIALS_MISSING")
            frozen = credentials.get_frozen_credentials()
            if not frozen.access_key or not frozen.secret_key:
                raise MediaJobError("MEDIA_ARTIFACT_SIGNING_CREDENTIALS_MISSING")
            effective_expires_s = expires_s
            credential_expiry = getattr(credentials, "_expiry_time", None)
            if credential_expiry is not None:
                remaining_credentials_s = int(
                    credential_expiry.timestamp()
                    - self._clock()
                    - _CREDENTIAL_EXPIRY_SAFETY_SECONDS
                )
                if remaining_credentials_s < 1:
                    raise MediaJobError("MEDIA_ARTIFACT_SIGNING_CREDENTIALS_EXPIRING")
                effective_expires_s = min(effective_expires_s, remaining_credentials_s)
            url = self._client_from_session(
                session,
                "s3",
                deadline_epoch_s,
            ).generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": ref.bucket,
                    "Key": ref.key,
                    "VersionId": ref.version_id,
                },
                ExpiresIn=effective_expires_s,
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
        """Validate abandoned input scope; the bucket lifecycle owns final cleanup."""

        if not _JOB_ID_RE.fullmatch(job_id):
            raise MediaJobError("MEDIA_JOB_CLEANUP_SCOPE_INVALID")
        self._remaining(deadline_epoch_s)

    def _acquire_consumer(self, request: MediaJobRequest, *, timeout_s: int) -> None:
        del timeout_s
        if request.output_prefix != f"media-jobs/{request.job_id}/":
            raise MediaJobError("MEDIA_JOB_CONSUMER_SCOPE_INVALID")

    def _release_consumer(self, request: MediaJobRequest) -> None:
        if request.output_prefix != f"media-jobs/{request.job_id}/":
            raise MediaJobError("MEDIA_JOB_CONSUMER_SCOPE_INVALID")

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
        """submit→bounded poll→integrity-checked download."""

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
                artifact_bound = (
                    MAX_PROPOSAL_PPTX_BYTES
                    if isinstance(request.operation, ProposalPptxOperation)
                    and artifact.name == "proposal.pptx"
                    else MAX_OUTPUT_BYTES
                )
                if artifact.object.size > artifact_bound:
                    raise MediaJobError("MEDIA_ARTIFACT_SIZE_INVALID")
                for attempt in range(3):
                    self._remaining(execution_deadline_epoch_s)
                    try:
                        artifacts[artifact.name] = self.download(
                            artifact.object,
                            deadline_epoch_s=execution_deadline_epoch_s,
                        )
                        break
                    except MediaJobError:
                        raise
                    except Exception as exc:
                        if not _is_transient_network_error(exc) or attempt == 2:
                            raise
                        self._sleeper(1.0)
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
        """既存の同期検索UXをgeneric TikTok media operationで維持する。

        ``max_videos`` は dispatcher Lambda の n_per_kw 上限（TIKTOK_N_PER_KW_MAX）を
        超えると SQS へ乗せる前に ValueError で落とす。上限超えを送ると Lambda 側で
        「TikTok n_per_kw is invalid」として全ジョブが失敗する（2026-09-02 本番事故）
        ので、往復させずに要求値と上限をログ・例外文へ残す。
        """

        if not 1 <= max_videos <= TIKTOK_N_PER_KW_MAX:
            logger.error(
                "media_tiktok_n_per_kw_out_of_range",
                n_per_kw=max_videos,
                n_per_kw_max=TIKTOK_N_PER_KW_MAX,
                search_type=search_type,
            )
            raise ValueError(
                f"TikTok n_per_kw={max_videos} is outside the dispatcher limit "
                f"(1..{TIKTOK_N_PER_KW_MAX})"
            )
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
        width: int = 1280,
        height: int = 720,
        device_scale_factor: int = 1,
        timeout_s: int = _SYNC_TIMEOUT_DEFAULT_S,
    ) -> bytes:
        """HTML の `.slide` 群をスクショして PPTX 化する（media worker slides operation）。

        契約 ``SlidesOperation`` の既定 device_scale_factor は 2 だが、ここでは 1 を
        既定にする（1920×1080 を明示せず呼ぶと 3840×2160×枚数へ肥大する罠の回避。
        spec_README 必要改修2）。高解像度が要る呼び出し側は明示的に 2 を渡す。
        """
        job_id = self._job_id(request_fingerprint)

        def operation_factory(deadline_epoch_s: int) -> MediaOperation:
            html_ref = self.stage_bytes(
                job_id=job_id,
                name="slides.html",
                body=html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                deadline_epoch_s=deadline_epoch_s,
            )
            return SlidesOperation(
                kind="slides",
                html=html_ref,
                width=width,
                height=height,
                device_scale_factor=device_scale_factor,
            )

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
                max_bytes=MAX_PROPOSAL_PPTX_BYTES,
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


__all__ = ["MediaJobClient", "MediaJobError", "reset_boto_cache"]
