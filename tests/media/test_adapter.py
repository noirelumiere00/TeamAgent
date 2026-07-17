from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
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
    make_job_request,
)

_BUCKET = "teamagent-media-test"


def _ref(job_id: str, body: bytes = b"artifact") -> S3ObjectRef:
    return S3ObjectRef(
        bucket=_BUCKET,
        key=f"media-jobs/{job_id}/output/artifact",
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
        super().__init__(queue_url="queue", table="jobs", bucket=_BUCKET)
        self.result = result
        self.submitted = 0
        self.cleaned = 0

    def submit(self, request: MediaJobRequest) -> str:
        self.submitted += 1
        return request.job_id

    def wait(
        self,
        job_id: str,
        *,
        timeout_s: int = 180,
        poll_interval_s: float = 1.0,
    ) -> MediaJobResult:
        del job_id, timeout_s, poll_interval_s
        return self.result

    def download(self, ref: S3ObjectRef) -> bytes:
        del ref
        return b"artifact"

    def cleanup(self, request: MediaJobRequest) -> None:
        del request
        self.cleaned += 1


def test_run_sync_cleans_after_success_and_failure() -> None:
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
    assert success.cleaned == 1

    failed = _LifecycleClient(
        MediaJobResult(
            job_id=request.job_id,
            status="failed",
            error_code="MEDIA_TEST_FAILED",
        )
    )
    with pytest.raises(MediaJobError, match="MEDIA_TEST_FAILED"):
        failed.run_sync(request)
    assert failed.cleaned == 1


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
        ttl_s: int = 3600,
    ) -> S3ObjectRef:
        del name, content_type, ttl_s
        self.staged += 1
        return S3ObjectRef(
            bucket=_BUCKET,
            key=f"media-jobs/{job_id}/input/staged-{self.staged}.bin",
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            content_type="application/octet-stream",
        )

    def cleanup_job(self, job_id: str) -> None:
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


def test_stage_rejects_scope_content_type_and_ttl_before_aws_write() -> None:
    client = MediaJobClient(queue_url="queue", table="jobs", bucket=_BUCKET)
    with pytest.raises(MediaJobError, match="MEDIA_JOB_ID_INVALID"):
        client.stage_bytes(
            job_id="../escape",
            name="source.bin",
            body=b"x",
            content_type="video/mp4",
        )
    with pytest.raises(MediaJobError, match="MEDIA_INPUT_CONTENT_TYPE_INVALID"):
        client.stage_bytes(
            job_id="mj_0123456789abcdef01234567",
            name="source.bin",
            body=b"x",
            content_type="video/mp4\nX-Evil: yes",
        )
    with pytest.raises(MediaJobError, match="MEDIA_INPUT_TTL_INVALID"):
        client.stage_bytes(
            job_id="mj_0123456789abcdef01234567",
            name="source.bin",
            body=b"x",
            content_type="video/mp4",
            ttl_s=59,
        )
