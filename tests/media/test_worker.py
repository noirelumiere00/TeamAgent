from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from teamagent.media.contracts import (
    MediaJobRequest,
    S3ObjectRef,
    ThumbnailOperation,
    make_job_request,
)
from teamagent.media.operations import (
    MediaOperationError,
    OperationOutput,
    ProducedArtifact,
)
from teamagent.media.worker import run_job


def _request() -> MediaJobRequest:
    source = S3ObjectRef(
        bucket="teamagent-media-test",
        key="media-jobs/mj_0123456789abcdef01234567/input/source.bin",
        sha256=hashlib.sha256(b"source").hexdigest(),
        size=6,
        content_type="video/mp4",
    )
    return make_job_request(
        operation=ThumbnailOperation(kind="thumbnail", source=source),
        output_bucket="teamagent-media-test",
        request_fingerprint="worker-cleanup",
        now_epoch_s=100,
        timeout_s=300,
        job_id="mj_0123456789abcdef01234567",
    )


class _Backend:
    def __init__(self) -> None:
        self.running = 0
        self.results: list[str] = []
        self.inputs_cleaned = 0
        self.all_cleaned = 0

    def assert_request_scope(self, request: MediaJobRequest) -> None:
        assert request.output_bucket == "teamagent-media-test"

    def mark_running(self, request: MediaJobRequest) -> None:
        self.running += 1

    def load_object(self, request: MediaJobRequest, ref: S3ObjectRef, destination: Path) -> Path:
        del request, ref
        destination.write_bytes(b"source")
        return destination

    def upload_artifact(self, request: MediaJobRequest, artifact: ProducedArtifact) -> S3ObjectRef:
        body = artifact.path.read_bytes()
        return S3ObjectRef(
            bucket=request.output_bucket,
            key=f"{request.output_prefix}output/{artifact.name}",
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            content_type=artifact.content_type,
        )

    def store_result(self, request: MediaJobRequest, result: Any) -> None:
        del request
        self.results.append(result.status)

    def cleanup_inputs(self, request: MediaJobRequest) -> None:
        del request
        self.inputs_cleaned += 1

    def cleanup_all_artifacts(self, request: MediaJobRequest) -> None:
        del request
        self.all_cleaned += 1


def test_worker_repeated_requests_leave_request_temp_root_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()

    def fake_execute(*_args: object, **kwargs: object) -> OperationOutput:
        workdir = kwargs["workdir"]
        assert isinstance(workdir, Path)
        output = workdir / "thumbnail.jpg"
        output.write_bytes(b"jpeg")
        return OperationOutput(
            (ProducedArtifact("thumbnail", output, "image/jpeg"),),
            {"network_requests_allowed": 0},
        )

    monkeypatch.setattr("teamagent.media.worker.execute_operation", fake_execute)
    for _ in range(3):
        result = run_job(_request(), backend, temp_root=tmp_path, now_epoch_s=101)
        assert result.status == "done"
        assert list(tmp_path.iterdir()) == []

    assert backend.running == 3
    assert backend.inputs_cleaned == 3
    assert backend.all_cleaned == 0


def test_worker_failure_cleans_remote_artifacts_and_local_request_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()

    def fail(*_args: object, **_kwargs: object) -> OperationOutput:
        raise MediaOperationError("MEDIA_TEST_FAILED", "test")

    monkeypatch.setattr("teamagent.media.worker.execute_operation", fail)
    result = run_job(_request(), backend, temp_root=tmp_path, now_epoch_s=101)

    assert result.status == "failed"
    assert result.error_code == "MEDIA_TEST_FAILED"
    assert backend.all_cleaned == 1
    assert backend.inputs_cleaned == 1
    assert list(tmp_path.iterdir()) == []
