from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from teamagent.media.contracts import (
    MediaJobRequest,
    MediaJobResult,
    S3ObjectRef,
    ThumbnailOperation,
    make_job_request,
)
from teamagent.media.operations import (
    MediaOperationError,
    OperationOutput,
    ProducedArtifact,
)
from teamagent.media.worker import AwsWorkerBackend, WorkerClaim, WorkerLease, run_job


def _request() -> MediaJobRequest:
    job_id = "mj_0123456789abcdef01234567"
    source = S3ObjectRef(
        bucket="teamagent-media-test",
        key=f"media-jobs/{job_id}/input/source.bin",
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
        job_id=job_id,
    )


class _Backend:
    def __init__(
        self,
        *,
        status: str = "queued",
        version: int = 0,
        lease_owner: str = "",
        lease_expires_at: int = 0,
        result: MediaJobResult | None = None,
    ) -> None:
        self.status = status
        self.version = version
        self.lease_owner = lease_owner
        self.lease_expires_at = lease_expires_at
        self.result = result
        self.claims = 0
        self.loads = 0
        self.uploads = 0
        self.stores = 0
        self.cleanups = 0
        self.fail_cleanup = False

    def assert_request_scope(self, request: MediaJobRequest) -> None:
        assert request.output_bucket == "teamagent-media-test"

    def claim(
        self,
        request: MediaJobRequest,
        *,
        owner: str,
        now_epoch_s: int,
    ) -> WorkerClaim:
        del request
        self.claims += 1
        if self.status in {"done", "failed"}:
            assert self.result is not None
            return WorkerClaim(existing_result=self.result)
        if self.status == "running" and self.lease_expires_at >= now_epoch_s:
            return WorkerClaim()
        assert self.status in {"queued", "running"}
        self.status = "running"
        self.version += 1
        self.lease_owner = owner
        self.lease_expires_at = now_epoch_s + 60
        return WorkerClaim(lease=WorkerLease(owner=owner, version=self.version))

    def _assert_lease(self, lease: WorkerLease) -> None:
        assert self.status == "running"
        assert lease.owner == self.lease_owner
        assert lease.version == self.version

    def load_object(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        ref: S3ObjectRef,
        destination: Path,
    ) -> Path:
        del request, ref
        self._assert_lease(lease)
        self.loads += 1
        destination.write_bytes(b"source")
        return destination

    def upload_artifact(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        artifact: ProducedArtifact,
    ) -> S3ObjectRef:
        self._assert_lease(lease)
        self.uploads += 1
        body = artifact.path.read_bytes()
        return S3ObjectRef(
            bucket=request.output_bucket,
            key=(f"{request.output_prefix}attempts/{lease.version}/output/{artifact.name}"),
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            content_type=artifact.content_type,
        )

    def store_result(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        result: MediaJobResult,
    ) -> None:
        del request
        self._assert_lease(lease)
        self.stores += 1
        self.result = result
        self.status = result.status
        self.version += 1

    def cleanup_attempt(self, request: MediaJobRequest, lease: WorkerLease) -> None:
        del request
        self._assert_lease(lease)
        self.cleanups += 1
        if self.fail_cleanup:
            raise RuntimeError("S3 cleanup unavailable")


def _successful_operation(*_args: object, **kwargs: object) -> OperationOutput:
    workdir = kwargs["workdir"]
    assert isinstance(workdir, Path)
    output = workdir / "thumbnail.jpg"
    output.write_bytes(b"jpeg")
    return OperationOutput(
        (ProducedArtifact("thumbnail", output, "image/jpeg"),),
        {"network_requests_allowed": 0},
    )


class _PointerDynamo:
    def __init__(self, request: MediaJobRequest) -> None:
        self.item: dict[str, object] = {
            "job_id": {"S": request.job_id},
            "idempotency_key": {"S": request.idempotency_key},
            "payload_sha256": {"S": request.payload_sha256},
            "request_json": {"S": request.to_json_bytes().decode()},
        }

    def get_item(self, **_kwargs: object) -> dict[str, object]:
        return {"Item": self.item}


def _pointer_backend(request: MediaJobRequest) -> tuple[AwsWorkerBackend, _PointerDynamo]:
    ddb = _PointerDynamo(request)
    backend = object.__new__(AwsWorkerBackend)
    backend._table = "jobs"
    backend._ddb = ddb
    return backend, ddb


def test_worker_loads_exact_envelope_from_bounded_job_pointer() -> None:
    request = _request()
    backend, _ddb = _pointer_backend(request)

    assert backend.load_request(request.job_id, request.payload_sha256) == request


@pytest.mark.parametrize("mutation", ["payload", "body", "idempotency"])
def test_worker_rejects_mutated_job_pointer(mutation: str) -> None:
    request = _request()
    backend, ddb = _pointer_backend(request)
    if mutation == "payload":
        ddb.item["payload_sha256"] = {"S": "f" * 64}
    elif mutation == "idempotency":
        ddb.item["idempotency_key"] = {"S": "f" * 64}
    else:
        mutated = request.to_json_bytes().decode().replace('"width":480', '"width":481')
        ddb.item["request_json"] = {"S": mutated}

    with pytest.raises(Exception, match=r"payload_sha256 mismatch|pointer does not match"):
        backend.load_request(request.job_id, request.payload_sha256)


def test_worker_repeated_attempts_leave_request_temp_root_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("teamagent.media.worker.execute_operation", _successful_operation)

    for _ in range(3):
        backend = _Backend()
        result = run_job(
            _request(),
            backend,
            temp_root=tmp_path,
            now_epoch_s=101,
            owner="worker-a",
        )
        assert result.status == "done"
        assert backend.stores == 1
        assert backend.cleanups == 0
        assert list(tmp_path.iterdir()) == []


def test_concurrent_duplicate_does_not_execute_store_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend(
        status="running",
        version=3,
        lease_owner="worker-a",
        lease_expires_at=300,
    )

    def must_not_execute(*_args: object, **_kwargs: object) -> OperationOutput:
        raise AssertionError("duplicate delivery executed media operation")

    monkeypatch.setattr("teamagent.media.worker.execute_operation", must_not_execute)
    result = run_job(
        _request(),
        backend,
        temp_root=tmp_path,
        now_epoch_s=101,
        owner="worker-b",
    )

    assert result.status == "running"
    assert result.metadata == {"duplicate_delivery": True}
    assert backend.version == 3
    assert backend.stores == 0
    assert backend.cleanups == 0
    assert list(tmp_path.iterdir()) == []


def test_terminal_duplicate_after_deadline_returns_exact_result_without_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    artifact = S3ObjectRef(
        bucket=request.output_bucket,
        key=f"{request.output_prefix}attempts/1/output/thumbnail",
        sha256=hashlib.sha256(b"jpeg").hexdigest(),
        size=4,
        content_type="image/jpeg",
    )
    terminal = MediaJobResult(
        job_id=request.job_id,
        status="done",
        artifacts=(
            {
                "name": "thumbnail",
                "object": artifact,
            },
        ),
        metadata={"attempt": 1},
    )
    backend = _Backend(status="done", version=2, result=terminal)

    def must_not_execute(*_args: object, **_kwargs: object) -> OperationOutput:
        raise AssertionError("terminal duplicate executed media operation")

    monkeypatch.setattr("teamagent.media.worker.execute_operation", must_not_execute)
    result = run_job(
        request,
        backend,
        temp_root=tmp_path,
        now_epoch_s=request.deadline_epoch_s + 100,
        owner="worker-b",
    )

    assert result == terminal
    assert backend.version == 2
    assert backend.stores == 0
    assert backend.cleanups == 0


def test_expired_worker_lease_is_taken_over_with_new_attempt_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend(
        status="running",
        version=4,
        lease_owner="dead-worker",
        lease_expires_at=100,
    )
    monkeypatch.setattr("teamagent.media.worker.execute_operation", _successful_operation)

    result = run_job(
        _request(),
        backend,
        temp_root=tmp_path,
        now_epoch_s=101,
        owner="replacement-worker",
    )

    assert result.status == "done"
    assert result.artifacts[0].object.key.endswith("/attempts/5/output/thumbnail")
    assert backend.version == 6
    assert backend.stores == 1


def test_newly_claimed_expired_request_is_failed_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    backend = _Backend()

    def must_not_execute(*_args: object, **_kwargs: object) -> OperationOutput:
        raise AssertionError("expired request executed media operation")

    monkeypatch.setattr("teamagent.media.worker.execute_operation", must_not_execute)
    result = run_job(
        request,
        backend,
        temp_root=tmp_path,
        now_epoch_s=request.deadline_epoch_s + 1,
        owner="worker-a",
    )

    assert result.status == "failed"
    assert result.error_code == "MEDIA_JOB_DEADLINE_EXCEEDED"
    assert backend.stores == 1
    assert backend.cleanups == 0


def test_worker_failure_cleans_only_owned_attempt_and_local_request_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()

    def fail(*_args: object, **_kwargs: object) -> OperationOutput:
        raise MediaOperationError("MEDIA_TEST_FAILED", "test")

    monkeypatch.setattr("teamagent.media.worker.execute_operation", fail)
    result = run_job(
        _request(),
        backend,
        temp_root=tmp_path,
        now_epoch_s=101,
        owner="worker-a",
    )

    assert result.status == "failed"
    assert result.error_code == "MEDIA_TEST_FAILED"
    assert backend.cleanups == 1
    assert backend.stores == 1
    assert list(tmp_path.iterdir()) == []


def test_worker_cleanup_error_is_not_suppressed_or_recorded_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()
    backend.fail_cleanup = True

    def fail_operation(*_args: object, **_kwargs: object) -> OperationOutput:
        raise MediaOperationError("MEDIA_TEST_FAILED", "test")

    monkeypatch.setattr("teamagent.media.worker.execute_operation", fail_operation)

    with pytest.raises(RuntimeError, match="S3 cleanup unavailable"):
        run_job(
            _request(),
            backend,
            temp_root=tmp_path,
            now_epoch_s=101,
            owner="worker-a",
        )

    assert backend.status == "running"
    assert backend.stores == 0
    assert backend.cleanups == 1
    assert list(tmp_path.iterdir()) == []


def test_worker_fails_terminally_when_budget_expires_after_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()
    clock = [101.0]

    def finish_after_deadline(*args: object, **kwargs: object) -> OperationOutput:
        output = _successful_operation(*args, **kwargs)
        clock[0] = 401.0
        return output

    monkeypatch.setattr("teamagent.media.worker.execute_operation", finish_after_deadline)
    result = run_job(
        _request(),
        backend,
        temp_root=tmp_path,
        now_epoch_s=101,
        owner="worker-a",
        clock=lambda: clock[0],
    )

    assert result.status == "failed"
    assert result.error_code == "MEDIA_JOB_DEADLINE_EXCEEDED"
    assert backend.uploads == 0
    assert backend.cleanups == 1
    assert backend.stores == 1
