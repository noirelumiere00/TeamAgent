from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.media.contracts import (
    AcquireOperation,
    MediaArtifact,
    MediaJobRequest,
    MediaJobResult,
    S3ObjectRef,
    artifact_manifest_sha256,
    make_job_request,
)

_BUCKET = "teamagent-media-test"


class _ConditionalFailureError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("conditional failure")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


def _ref(job_id: str, body: bytes = b"artifact") -> S3ObjectRef:
    return S3ObjectRef(
        bucket=_BUCKET,
        key=f"media-jobs/{job_id}/output/artifact",
        version_id="version-1",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type="application/octet-stream",
    )


def _request() -> MediaJobRequest:
    return make_job_request(
        operation=AcquireOperation(
            kind="acquire",
            url="https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        output_bucket=_BUCKET,
        request_fingerprint="adapter-lifecycle",
        now_epoch_s=100,
        timeout_s=300,
    )


class _LifecycleClient(MediaJobClient):
    def __init__(self, result: MediaJobResult) -> None:
        super().__init__(
            queue_url="queue",
            table="jobs",
            bucket=_BUCKET,
            clock=lambda: 101.0,
        )
        self.result = result
        self.submitted = 0
        self.consumers = 0
        self.max_consumers = 0
        self.wait_deadline: int | None = None
        self.download_deadline: int | None = None

    def submit(self, request: MediaJobRequest) -> str:
        self.submitted += 1
        return request.job_id

    def wait(
        self,
        job_id: str,
        *,
        timeout_s: int = 180,
        poll_interval_s: float = 1.0,
        deadline_epoch_s: int | None = None,
        expected_audit_principal_hash: str | None = None,
    ) -> MediaJobResult:
        del job_id, timeout_s, poll_interval_s, expected_audit_principal_hash
        self.wait_deadline = deadline_epoch_s
        return self.result

    def download(self, ref: S3ObjectRef, *, deadline_epoch_s: int) -> bytes:
        del ref
        self.download_deadline = deadline_epoch_s
        return b"artifact"

    def _acquire_consumer(self, request: MediaJobRequest, *, timeout_s: int) -> None:
        del request, timeout_s
        self.consumers += 1
        self.max_consumers = max(self.max_consumers, self.consumers)

    def _release_consumer(self, request: MediaJobRequest) -> None:
        del request
        self.consumers -= 1


def test_run_sync_fences_consumers_without_deleting_shared_state() -> None:
    request = _request()
    done = MediaJobResult(
        job_id=request.job_id,
        status="done",
        artifacts=(MediaArtifact(name="media", object=_ref(request.job_id)),),
    )
    success = _LifecycleClient(done)
    artifacts, _metadata = success.run_sync(request)
    assert artifacts == {"media": b"artifact"}
    assert success.submitted == 1
    assert success.consumers == 0
    assert success.max_consumers == 1
    assert success.wait_deadline == request.deadline_epoch_s - 15
    assert success.download_deadline == request.deadline_epoch_s - 15

    failed = _LifecycleClient(
        MediaJobResult(
            job_id=request.job_id,
            status="failed",
            error_code="MEDIA_TEST_FAILED",
        )
    )
    with pytest.raises(MediaJobError, match="MEDIA_TEST_FAILED"):
        failed.run_sync(request)
    assert failed.consumers == 0
    assert failed.max_consumers == 1


class _Queue:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)


class _SubmitDynamo:
    def __init__(self, request: MediaJobRequest) -> None:
        self.request = request
        self.item: dict[str, Any] = {
            "job_id": {"S": request.job_id},
            "idempotency_key": {"S": request.idempotency_key},
            "payload_sha256": {"S": request.payload_sha256},
            "request_json": {"S": request.to_json_bytes().decode()},
            "status": {"S": "queued"},
            "submit_owner": {"S": "other-caller"},
            "submit_lease_expires_at": {"N": "130"},
        }
        self.claim_calls = 0

    def put_item(self, **_kwargs: Any) -> None:
        raise _ConditionalFailureError

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Item": self.item}

    def update_item(self, **kwargs: Any) -> None:
        expression = kwargs["UpdateExpression"]
        if expression.startswith("SET submit_owner"):
            self.claim_calls += 1
            raise _ConditionalFailureError
        raise AssertionError(f"unexpected update: {expression}")


class _SubmitClient(MediaJobClient):
    def __init__(
        self,
        *,
        queue: _Queue,
        ddb: _SubmitDynamo,
        sleeper: Any,
        monotonic: Any,
    ) -> None:
        super().__init__(
            session=_Session(queue=queue, ddb=ddb, s3=object()),
            queue_url="queue",
            table="jobs",
            bucket=_BUCKET,
            sleeper=sleeper,
            monotonic=monotonic,
            clock=lambda: 101.0,
        )
        self.queue = queue
        self.ddb = ddb


class _Session:
    def __init__(
        self,
        *,
        queue: Any,
        ddb: Any,
        s3: Any,
        credential_expiry_epoch_s: int = 1_000_000,
    ) -> None:
        self.clients = {"sqs": queue, "dynamodb": ddb, "s3": s3}
        self.credentials = type(
            "Credentials",
            (),
            {
                "_expiry_time": datetime.fromtimestamp(credential_expiry_epoch_s, tz=UTC),
                "get_frozen_credentials": lambda self: type(
                    "Frozen",
                    (),
                    {
                        "access_key": "AKIATEST",
                        "secret_key": "secret",
                        "token": "token",
                    },
                )(),
            },
        )()

    def client(self, service: str, **_kwargs: Any) -> Any:
        return self.clients[service]

    def get_credentials(self) -> Any:
        return self.credentials


class _ResultDynamo:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Item": self.item}


def _done_item(
    job_id: str,
    *,
    audit_principal_hash: str,
    artifact_key: str | None = None,
) -> tuple[dict[str, Any], MediaJobResult]:
    ref = S3ObjectRef(
        bucket=_BUCKET,
        key=(artifact_key or f"media-jobs/{job_id}/attempts/1/attempt-1/output/artifact"),
        version_id="version-1",
        sha256=hashlib.sha256(b"artifact").hexdigest(),
        size=len(b"artifact"),
        content_type="application/octet-stream",
    )
    result = MediaJobResult(
        job_id=job_id,
        status="done",
        artifacts=(MediaArtifact(name="artifact", object=ref),),
    )
    return (
        {
            "status": {"S": "done"},
            "audit_principal_hash": {"S": audit_principal_hash},
            "detail": {
                "S": json.dumps(
                    result.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
            "artifact_manifest_sha256": {"S": artifact_manifest_sha256(result.artifacts)},
        },
        result,
    )


def test_done_result_requires_exact_audit_owner_and_independent_artifact_manifest() -> None:
    job_id = "mj_0123456789abcdef01234567"
    owner = "a" * 64
    item, expected = _done_item(job_id, audit_principal_hash=owner)
    ddb = _ResultDynamo(item)
    client = MediaJobClient(
        session=_Session(queue=object(), ddb=ddb, s3=object()),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 100.0,
    )

    assert (
        client.get_result(
            job_id,
            deadline_epoch_s=130,
            expected_audit_principal_hash=owner,
        )
        == expected
    )
    with pytest.raises(MediaJobError, match="MEDIA_JOB_AUDIT_PRINCIPAL_MISMATCH"):
        client.get_result(
            job_id,
            deadline_epoch_s=130,
            expected_audit_principal_hash="b" * 64,
        )
    with pytest.raises(MediaJobError, match="MEDIA_JOB_AUDIT_PRINCIPAL_MISMATCH"):
        client.get_result(job_id, deadline_epoch_s=130)

    item["artifact_manifest_sha256"] = {"S": "f" * 64}
    with pytest.raises(MediaJobError, match="MEDIA_ARTIFACT_MANIFEST_INTEGRITY_FAILED"):
        client.get_result(
            job_id,
            deadline_epoch_s=130,
            expected_audit_principal_hash=owner,
        )


def test_done_result_job_id_must_match_the_requested_row_key() -> None:
    job_id = "mj_0123456789abcdef01234567"
    owner = "a" * 64
    item, result = _done_item(job_id, audit_principal_hash=owner)
    mismatched = result.model_copy(update={"job_id": "mj_aaaaaaaaaaaaaaaaaaaaaaaa"})
    item["detail"] = {
        "S": json.dumps(
            mismatched.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
    }
    client = MediaJobClient(
        session=_Session(
            queue=object(),
            ddb=_ResultDynamo(item),
            s3=object(),
        ),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 100.0,
    )

    with pytest.raises(MediaJobError, match="MEDIA_JOB_RESULT_SCOPE_INVALID"):
        client.get_result(
            job_id,
            deadline_epoch_s=130,
            expected_audit_principal_hash=owner,
        )


def test_done_result_rejects_content_addressed_artifact_outside_job_attempt_prefix() -> None:
    job_id = "mj_0123456789abcdef01234567"
    owner = "a" * 64
    item, _result = _done_item(
        job_id,
        audit_principal_hash=owner,
        artifact_key="media-jobs/another-job/attempts/1/attempt-1/output/artifact",
    )
    client = MediaJobClient(
        session=_Session(
            queue=object(),
            ddb=_ResultDynamo(item),
            s3=object(),
        ),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 100.0,
    )

    with pytest.raises(MediaJobError, match="MEDIA_ARTIFACT_MANIFEST_SCOPE_INVALID"):
        client.get_result(
            job_id,
            deadline_epoch_s=130,
            expected_audit_principal_hash=owner,
        )


def test_submit_sends_canonical_intent_without_mutating_authoritative_ledger() -> None:
    request = _request()
    queue = _Queue()
    ddb = _SubmitDynamo(request)
    clock = [0.0]

    def finish_other_submit(delay: float) -> None:
        clock[0] += delay
        ddb.item["message_sent_at"] = {"N": "101"}

    client = _SubmitClient(
        queue=queue,
        ddb=ddb,
        sleeper=finish_other_submit,
        monotonic=lambda: clock[0],
    )

    assert client.submit(request) == request.job_id
    assert ddb.claim_calls == 0
    assert len(queue.messages) == 1
    assert queue.messages[0]["MessageBody"] == request.to_json_bytes().decode()


class _RecoverableDynamo:
    def __init__(self, request: MediaJobRequest) -> None:
        self.item: dict[str, Any] = {
            "job_id": {"S": request.job_id},
            "idempotency_key": {"S": request.idempotency_key},
            "payload_sha256": {"S": request.payload_sha256},
            "request_json": {"S": request.to_json_bytes().decode()},
            "status": {"S": "queued"},
        }
        self.claimed = False

    def put_item(self, **_kwargs: Any) -> None:
        raise _ConditionalFailureError

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Item": self.item}

    def update_item(self, **kwargs: Any) -> None:
        expression = kwargs["UpdateExpression"]
        if expression.startswith("SET submit_owner"):
            self.claimed = True
            return
        if expression.startswith("SET message_sent_at"):
            assert self.claimed
            self.item["message_sent_at"] = {"N": "161"}
            return
        raise AssertionError(f"unexpected update: {expression}")


def test_delayed_semantic_retry_reuses_stable_queue_deduplication_identity() -> None:
    operation = AcquireOperation(
        kind="acquire",
        url="https://www.youtube.com/watch?v=BaW_jenozKc",
    )
    original = make_job_request(
        operation=operation,
        output_bucket=_BUCKET,
        request_fingerprint="delayed-retry",
        now_epoch_s=100,
        timeout_s=300,
    )
    delayed = make_job_request(
        operation=operation,
        output_bucket=_BUCKET,
        request_fingerprint="delayed-retry",
        now_epoch_s=160,
        timeout_s=300,
    )
    assert original.idempotency_key == delayed.idempotency_key
    assert original.payload_sha256 != delayed.payload_sha256

    queue = _Queue()
    ddb = _RecoverableDynamo(original)
    client = MediaJobClient(
        session=_Session(queue=queue, ddb=ddb, s3=object()),
        queue_url="https://sqs.example.invalid/media.fifo",
        table="jobs",
        bucket=_BUCKET,
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0.0,
        clock=lambda: 161.0,
    )
    assert client.submit(delayed) == original.job_id
    assert len(queue.messages) == 1
    sent = queue.messages[0]
    assert sent["MessageBody"] == delayed.to_json_bytes().decode()
    assert sent["MessageDeduplicationId"] == original.idempotency_key
    assert sent["MessageAttributes"]["payload_sha256"]["StringValue"] == delayed.payload_sha256


class _GuardClient(MediaJobClient):
    def __init__(self) -> None:
        super().__init__(queue_url="queue", table="jobs", bucket=_BUCKET)
        self.staged = 0
        self.cleaned: list[str] = []

    def stage_bytes(
        self,
        *,
        job_id: str,
        name: str,
        body: bytes,
        content_type: str,
        deadline_epoch_s: int,
        ttl_s: int = 3600,
    ) -> S3ObjectRef:
        del name, content_type, deadline_epoch_s, ttl_s
        self.staged += 1
        return S3ObjectRef(
            bucket=_BUCKET,
            key=f"media-jobs/{job_id}/input/staged-{self.staged}.bin",
            version_id=f"version-{self.staged}",
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            content_type="application/octet-stream",
        )

    def cleanup_job(self, job_id: str, *, deadline_epoch_s: int) -> None:
        del deadline_epoch_s
        self.cleaned.append(job_id)


def test_staged_inputs_are_cleaned_when_operation_validation_fails() -> None:
    client = _GuardClient()
    expected_job_id = f"mj_{hashlib.sha256(b'invalid-frame').hexdigest()[:24]}"

    with pytest.raises(ValidationError):
        client.extract_frames(
            b"video",
            "video/mp4",
            [0.0],
            request_fingerprint="invalid-frame",
            width=1,
        )

    assert client.staged == 1
    assert client.cleaned == [expected_job_id]


def test_multi_stage_proposal_failure_cleans_everything_by_job_prefix() -> None:
    client = _GuardClient()
    expected_job_id = f"mj_{hashlib.sha256(b'invalid-proposal').hexdigest()[:24]}"

    with pytest.raises(ValidationError):
        client.render_proposal_pptx(
            b"template",
            b'{"placeholders":{}}',
            request_fingerprint="invalid-proposal",
            evidence_images=[(0, 1, b"image", "image/jpeg")],
        )

    assert client.staged == 3
    assert client.cleaned == [expected_job_id]


class _SearchClient(MediaJobClient):
    def __init__(self) -> None:
        super().__init__(queue_url="queue", table="jobs", bucket=_BUCKET)
        self.request: MediaJobRequest | None = None

    def run_sync(
        self,
        request: MediaJobRequest,
        *,
        timeout_s: int = 180,
    ) -> tuple[Mapping[str, bytes], Mapping[str, Any]]:
        del timeout_s
        self.request = request
        payload = {
            "posts": [
                {
                    "pid": "p01001",
                    "url": "https://www.tiktok.com/@u/video/1",
                    "title": "title",
                }
            ]
        }
        return {"posts.json": json.dumps(payload).encode()}, {}


def test_sync_tiktok_search_uses_generic_bounded_operation() -> None:
    client = _SearchClient()
    posts = client.search_tiktok(
        "coffee",
        request_fingerprint="req:search",
        max_videos=3,
    )

    assert posts[0]["pid"] == "p01001"
    assert client.request is not None
    operation = client.request.operation
    assert operation.kind == "tiktok_acquire"
    assert operation.keywords == ("coffee",)
    assert operation.n_per_kw == 3
    assert operation.videos_per_kw == 0
    assert operation.artifact_mode == "metadata_only"


def test_stage_rejects_scope_content_type_and_ttl_before_aws_write() -> None:
    client = MediaJobClient(
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 100.0,
    )
    with pytest.raises(MediaJobError, match="MEDIA_JOB_ID_INVALID"):
        client.stage_bytes(
            job_id="../escape",
            name="source.bin",
            body=b"x",
            content_type="video/mp4",
            deadline_epoch_s=400,
        )
    with pytest.raises(MediaJobError, match="MEDIA_INPUT_CONTENT_TYPE_INVALID"):
        client.stage_bytes(
            job_id="mj_0123456789abcdef01234567",
            name="source.bin",
            body=b"x",
            content_type="video/mp4\nX-Evil: yes",
            deadline_epoch_s=400,
        )
    with pytest.raises(MediaJobError, match="MEDIA_INPUT_TTL_INVALID"):
        client.stage_bytes(
            job_id="mj_0123456789abcdef01234567",
            name="source.bin",
            body=b"x",
            content_type="video/mp4",
            deadline_epoch_s=400,
            ttl_s=299,
        )


class _PresignS3:
    def __init__(self, url: str = "https://s3.example.invalid/signed") -> None:
        self.url = url
        self.checksum = base64.b64encode(hashlib.sha256(b"artifact").digest()).decode("ascii")
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.head_calls: list[dict[str, Any]] = []

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_calls.append(kwargs)
        return {
            "ContentLength": 8,
            "ContentType": "application/octet-stream",
            "ChecksumSHA256": self.checksum,
            "ServerSideEncryption": "AES256",
            "VersionId": "version-1",
            "Metadata": {
                "sha256": hashlib.sha256(b"artifact").hexdigest(),
            },
        }

    def generate_presigned_url(
        self,
        operation: str,
        **kwargs: Any,
    ) -> str:
        self.calls.append((operation, kwargs["Params"], kwargs["ExpiresIn"]))
        return self.url


def test_presign_get_is_integrity_checked_short_lived_and_deadline_bounded() -> None:
    s3 = _PresignS3()
    client = MediaJobClient(
        session=_Session(queue=object(), ddb=object(), s3=s3),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 100.0,
    )
    ref = _ref("mj_0123456789abcdef01234567")

    assert (
        client.presign_get(ref, deadline_epoch_s=130, expires_s=300)
        == "https://s3.example.invalid/signed"
    )
    assert s3.calls == [
        (
            "get_object",
            {"Bucket": _BUCKET, "Key": ref.key, "VersionId": ref.version_id},
            300,
        )
    ]
    assert s3.head_calls == [
        {
            "Bucket": _BUCKET,
            "Key": ref.key,
            "VersionId": ref.version_id,
            "ChecksumMode": "ENABLED",
        }
    ]

    with pytest.raises(MediaJobError, match="MEDIA_ARTIFACT_PRESIGN_EXPIRY_INVALID"):
        client.presign_get(ref, deadline_epoch_s=130, expires_s=901)

    s3.checksum = base64.b64encode(hashlib.sha256(b"tampered").digest()).decode("ascii")
    with pytest.raises(MediaJobError, match="MEDIA_ARTIFACT_INTEGRITY_FAILED"):
        client.presign_get(ref, deadline_epoch_s=130, expires_s=900)

    expired = MediaJobClient(
        session=_Session(queue=object(), ddb=object(), s3=s3),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 130.0,
    )
    with pytest.raises(MediaJobError, match="MEDIA_JOB_DEADLINE_EXCEEDED"):
        expired.presign_get(ref, deadline_epoch_s=130, expires_s=300)
    assert len(s3.calls) == 1


def test_presign_get_is_clamped_before_ecs_credential_expiry() -> None:
    s3 = _PresignS3()
    client = MediaJobClient(
        session=_Session(
            queue=object(),
            ddb=object(),
            s3=s3,
            credential_expiry_epoch_s=250,
        ),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 100.0,
    )
    ref = _ref("mj_0123456789abcdef01234567")

    client.presign_get(ref, deadline_epoch_s=130, expires_s=300)

    assert s3.calls[-1][2] == 90


def test_artifact_ttl_contract_is_bounded_and_environment_driven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEDIA_ARTIFACT_TTL_SECONDS", "600")
    assert MediaJobClient.artifact_ttl_seconds() == 600
    monkeypatch.setenv("MEDIA_ARTIFACT_TTL_SECONDS", "2592001")
    with pytest.raises(MediaJobError, match="MEDIA_ARTIFACT_TTL_INVALID"):
        MediaJobClient.artifact_ttl_seconds()


class _FlakyDynamo:
    """1回目だけ実物の botocore ConnectTimeoutError を投げる DynamoDB フェイク。

    本番の失敗モードそのもの (worker 成功後の取得段で接続層が瞬断し、
    リトライ 0 回設計のため即ジョブ全体が落ちた) を再現する。
    """

    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.calls = 0

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            from botocore.exceptions import ConnectTimeoutError

            raise ConnectTimeoutError(endpoint_url="https://dynamodb.example")
        return {"Item": self.item}


def test_wait_survives_transient_connect_timeout_and_returns_done() -> None:
    job_id = "mj_0123456789abcdef01234567"
    owner = "a" * 64
    item, expected = _done_item(job_id, audit_principal_hash=owner)
    ddb = _FlakyDynamo(item)
    client = MediaJobClient(
        session=_Session(queue=object(), ddb=ddb, s3=object()),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        sleeper=lambda _s: None,
        clock=lambda: 100.0,
    )

    result = client.wait(
        job_id,
        timeout_s=30,
        deadline_epoch_s=130,
        expected_audit_principal_hash=owner,
    )
    assert result == expected
    assert ddb.calls == 2


def test_wait_still_raises_on_non_transient_errors() -> None:
    job_id = "mj_0123456789abcdef01234567"

    class _BrokenDynamo:
        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            raise ValueError("not a network error")

    client = MediaJobClient(
        session=_Session(queue=object(), ddb=_BrokenDynamo(), s3=object()),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        sleeper=lambda _s: None,
        clock=lambda: 100.0,
    )
    with pytest.raises(ValueError, match="not a network error"):
        client.wait(job_id, timeout_s=30, deadline_epoch_s=130)


class _FlakyDownloadClient(_LifecycleClient):
    def __init__(self, result: MediaJobResult, *, failures: int) -> None:
        super().__init__(result)
        self.failures = failures
        self.download_attempts = 0

    def download(self, ref: S3ObjectRef, *, deadline_epoch_s: int) -> bytes:
        self.download_attempts += 1
        if self.download_attempts <= self.failures:
            from botocore.exceptions import ConnectTimeoutError

            raise ConnectTimeoutError(endpoint_url="https://s3.example")
        return super().download(ref, deadline_epoch_s=deadline_epoch_s)


def test_run_sync_retries_transient_download_then_succeeds() -> None:
    request = _request()
    done = MediaJobResult(
        job_id=request.job_id,
        status="done",
        artifacts=(MediaArtifact(name="media", object=_ref(request.job_id)),),
    )
    client = _FlakyDownloadClient(done, failures=2)
    artifacts, _metadata = client.run_sync(request)
    assert artifacts == {"media": b"artifact"}
    assert client.download_attempts == 3

    exhausted = _FlakyDownloadClient(done, failures=3)
    from botocore.exceptions import ConnectTimeoutError

    with pytest.raises(ConnectTimeoutError):
        exhausted.run_sync(request)
    assert exhausted.download_attempts == 3
    assert exhausted.consumers == 0
