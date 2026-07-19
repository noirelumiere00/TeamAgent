from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from teamagent.media.contracts import (
    MediaArtifact,
    MediaJobRequest,
    MediaJobResult,
    S3ObjectRef,
    ThumbnailOperation,
    artifact_manifest_sha256,
    make_job_request,
)
from teamagent.media.operations import (
    MediaOperationError,
    OperationOutput,
    ProducedArtifact,
)
from teamagent.media.worker import (
    AwsWorkerBackend,
    WorkerClaim,
    WorkerLease,
    _TerminalResultWriteError,
    _TerminalWriteState,
    run_job,
)


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
        self.attempt_id = ""
        self.lease_expires_at = lease_expires_at
        self.result = result
        self.claims = 0
        self.loads = 0
        self.uploads = 0
        self.stores = 0
        self.cleanups = 0
        self.fail_cleanup = False
        self.events: list[str] = []

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
        self.attempt_id = f"attempt-{self.version}"
        self.lease_expires_at = now_epoch_s + 60
        return WorkerClaim(
            lease=WorkerLease(
                owner=owner,
                version=self.version,
                attempt_id=self.attempt_id,
            )
        )

    def _assert_lease(self, lease: WorkerLease) -> None:
        assert self.status == "running"
        assert lease.owner == self.lease_owner
        assert lease.version == self.version
        assert lease.attempt_id == self.attempt_id

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
            key=(
                f"{request.output_prefix}attempts/{lease.version}/"
                f"{lease.attempt_id}/output/{artifact.name}"
            ),
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
        self.events.append("store")
        self.result = result
        self.status = result.status
        self.version += 1

    def cleanup_attempt(self, request: MediaJobRequest, lease: WorkerLease) -> None:
        del request
        assert lease.owner == self.lease_owner
        assert lease.attempt_id == self.attempt_id
        self.cleanups += 1
        self.events.append("cleanup")
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
        if request.audit_principal_hash is not None:
            self.item["audit_principal_hash"] = {"S": request.audit_principal_hash}

    def get_item(self, **_kwargs: object) -> dict[str, object]:
        return {"Item": self.item}


def _pointer_backend(request: MediaJobRequest) -> tuple[AwsWorkerBackend, _PointerDynamo]:
    ddb = _PointerDynamo(request)
    backend = object.__new__(AwsWorkerBackend)
    backend._table = "jobs"
    backend._ddb = ddb
    backend._deadline_epoch_s = request.deadline_epoch_s
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


def test_worker_rejects_pointer_owned_by_a_different_audit_principal() -> None:
    base = _request()
    request = make_job_request(
        operation=base.operation,
        output_bucket=base.output_bucket,
        request_fingerprint="worker-audit-owner",
        now_epoch_s=100,
        timeout_s=300,
        job_id=base.job_id,
        audit_principal_hash="a" * 64,
    )
    backend, ddb = _pointer_backend(request)
    ddb.item["audit_principal_hash"] = {"S": "b" * 64}

    with pytest.raises(ValueError, match="pointer does not match"):
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


def test_worker_never_calls_backend_after_absolute_deadline(
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
    with pytest.raises(TimeoutError, match="before claim"):
        run_job(
            request,
            backend,
            temp_root=tmp_path,
            now_epoch_s=request.deadline_epoch_s + 100,
            owner="worker-b",
        )

    assert backend.version == 2
    assert backend.claims == 0
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
    assert result.artifacts[0].object.key.endswith("/attempts/5/attempt-5/output/thumbnail")
    assert backend.version == 6
    assert backend.stores == 1


def test_request_inside_terminal_reserve_is_failed_without_execution(
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
        now_epoch_s=request.deadline_epoch_s - 1,
        owner="worker-a",
    )

    assert result.status == "failed"
    assert result.error_code == "MEDIA_JOB_DEADLINE_EXCEEDED"
    assert backend.stores == 1
    assert backend.cleanups == 1
    assert backend.events == ["store", "cleanup"]


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
    assert backend.events == ["store", "cleanup"]
    assert list(tmp_path.iterdir()) == []


class _LeaseLostError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("lease lost")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _LeaseLossDynamo:
    def __init__(self) -> None:
        self.updates = 0

    def update_item(self, **_kwargs: object) -> dict[str, object]:
        self.updates += 1
        if self.updates == 2:
            raise _LeaseLostError
        return {}


class _AttemptS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.puts: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.puts.append(kwargs)
        key = str(kwargs["Key"])
        self.objects[key] = {
            "Body": kwargs["Body"],
            "Metadata": kwargs["Metadata"],
        }
        return {}

    def head_object(self, **kwargs: object) -> dict[str, object]:
        return {"Metadata": self.objects[str(kwargs["Key"])]["Metadata"]}

    def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.objects.pop(str(kwargs["Key"]), None)
        return {}


def test_lease_loss_after_s3_put_removes_only_exact_attempt_orphan(
    tmp_path: Path,
) -> None:
    request = _request()
    artifact_path = tmp_path / "thumbnail.jpg"
    artifact_path.write_bytes(b"jpeg")
    backend = object.__new__(AwsWorkerBackend)
    backend._bucket = request.output_bucket
    backend._table = "jobs"
    backend._kms_key_id = ""
    backend._clock = lambda: 101.0
    backend._deadline_epoch_s = request.deadline_epoch_s
    backend._ddb = _LeaseLossDynamo()
    backend._s3 = _AttemptS3()
    lease = WorkerLease(owner="worker-a", version=7, attempt_id="attempt-exact")

    with pytest.raises(_LeaseLostError):
        backend.upload_artifact(
            request,
            lease,
            ProducedArtifact("thumbnail", artifact_path, "image/jpeg"),
        )

    assert backend._s3.objects == {}
    assert backend._s3.puts[0]["ChecksumSHA256"] == base64.b64encode(
        hashlib.sha256(b"jpeg").digest()
    ).decode("ascii")


class _StoreDynamo:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def update_item(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {}


def test_done_write_persists_independent_manifest_and_thirty_day_retention() -> None:
    request = _request()
    lease = WorkerLease(owner="worker-a", version=7, attempt_id="attempt-exact")
    ref = S3ObjectRef(
        bucket=request.output_bucket,
        key=(
            f"{request.output_prefix}attempts/{lease.version}/{lease.attempt_id}/output/thumbnail"
        ),
        sha256=hashlib.sha256(b"jpeg").hexdigest(),
        size=4,
        content_type="image/jpeg",
    )
    result = MediaJobResult(
        job_id=request.job_id,
        status="done",
        artifacts=(MediaArtifact(name="thumbnail", object=ref),),
    )
    ddb = _StoreDynamo()
    s3 = _AttemptS3()
    backend = object.__new__(AwsWorkerBackend)
    backend._bucket = request.output_bucket
    backend._table = "jobs"
    backend._kms_key_id = ""
    backend._clock = lambda: 101.0
    backend._deadline_epoch_s = request.deadline_epoch_s
    backend._ddb = ddb
    backend._s3 = s3

    backend.store_result(request, lease, result)

    terminal = ddb.calls[-1]["ExpressionAttributeValues"]
    assert isinstance(terminal, dict)
    assert terminal[":artifact_manifest"] == {"S": artifact_manifest_sha256(result.artifacts)}
    assert terminal[":cleanup"] == {"N": str(request.created_at_epoch_s + request.artifact_ttl_s)}
    assert terminal[":ttl"] == {
        "N": str(request.created_at_epoch_s + request.artifact_ttl_s + 86400)
    }
    assert s3.puts[0]["ChecksumSHA256"] == base64.b64encode(
        hashlib.sha256(s3.puts[0]["Body"]).digest()
    ).decode("ascii")


class _TerminalTimeoutDynamo:
    def __init__(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        *,
        commit_before_timeout: bool,
    ) -> None:
        self.commit_before_timeout = commit_before_timeout
        self.reads: list[bool] = []
        self.item: dict[str, object] = {
            "job_id": {"S": request.job_id},
            "idempotency_key": {"S": request.idempotency_key},
            "payload_sha256": {"S": request.payload_sha256},
            "status": {"S": "running"},
            "version": {"N": str(lease.version)},
            "lease_owner": {"S": lease.owner},
            "lease_expires_at": {"N": "460"},
            "attempt_id": {"S": lease.attempt_id},
        }

    def update_item(self, **kwargs: object) -> dict[str, object]:
        expression = str(kwargs["UpdateExpression"])
        if expression.startswith("SET fence_checked_at"):
            return {}
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(values, dict)
        if self.commit_before_timeout:
            self.item.update(
                {
                    "status": values[":status"],
                    "detail": values[":detail"],
                    "updated_at": values[":now"],
                    "cleanup_at": values[":cleanup"],
                    "cleanup_status": values[":pending"],
                    "ttl": values[":ttl"],
                    "finalized_attempt_id": values[":attempt"],
                    "version": {"N": str(int(self.item["version"]["N"]) + 1)},
                }
            )
            self.item.pop("lease_expires_at", None)
        raise TimeoutError("response timed out after UpdateItem")

    def get_item(self, **kwargs: object) -> dict[str, object]:
        self.reads.append(kwargs.get("ConsistentRead") is True)
        return {"Item": self.item}


class _FinalizeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.deleted: list[str] = []
        self.list_calls = 0

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.objects[str(kwargs["Key"])] = {
            "Body": kwargs["Body"],
            "Metadata": kwargs["Metadata"],
        }
        return {}

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self.list_calls += 1
        prefix = str(kwargs["Prefix"])
        return {
            "Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(prefix)]
        }

    def head_object(self, **kwargs: object) -> dict[str, object]:
        return {"Metadata": self.objects[str(kwargs["Key"])]["Metadata"]}

    def delete_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        self.deleted.append(key)
        self.objects.pop(key, None)
        return {}


def _terminal_timeout_backend(
    request: MediaJobRequest,
    lease: WorkerLease,
    *,
    commit_before_timeout: bool,
) -> tuple[AwsWorkerBackend, _TerminalTimeoutDynamo, _FinalizeS3, MediaJobResult]:
    artifact = S3ObjectRef(
        bucket=request.output_bucket,
        key=(
            f"{request.output_prefix}attempts/{lease.version}/{lease.attempt_id}/output/thumbnail"
        ),
        sha256=hashlib.sha256(b"jpeg").hexdigest(),
        size=4,
        content_type="image/jpeg",
    )
    result = MediaJobResult(
        job_id=request.job_id,
        status="done",
        artifacts=({"name": "thumbnail", "object": artifact},),
    )
    ddb = _TerminalTimeoutDynamo(
        request,
        lease,
        commit_before_timeout=commit_before_timeout,
    )
    s3 = _FinalizeS3()
    s3.objects[artifact.key] = {
        "Body": b"jpeg",
        "Metadata": {
            "job-id": request.job_id,
            "attempt-id": lease.attempt_id,
            "lease-version": str(lease.version),
            "finalized": "false",
        },
    }
    backend = object.__new__(AwsWorkerBackend)
    backend._bucket = request.output_bucket
    backend._table = "jobs"
    backend._kms_key_id = ""
    backend._clock = lambda: 101.0
    backend._deadline_epoch_s = request.deadline_epoch_s
    backend._ddb = ddb
    backend._s3 = s3
    return backend, ddb, s3, result


def test_done_update_timeout_reconciles_committed_row_without_deleting_artifacts() -> None:
    request = _request()
    lease = WorkerLease(owner="worker-a", version=7, attempt_id="attempt-exact")
    backend, ddb, s3, result = _terminal_timeout_backend(
        request,
        lease,
        commit_before_timeout=True,
    )

    backend.store_result(request, lease, result)

    assert ddb.reads == [True]
    assert ddb.item["status"] == {"S": "done"}
    assert ddb.item["finalized_attempt_id"] == {"S": lease.attempt_id}
    assert s3.list_calls == 0
    assert s3.deleted == []
    assert set(s3.objects) == {
        result.artifacts[0].object.key,
        f"{request.output_prefix}attempts/{lease.version}/{lease.attempt_id}/_FINALIZED.json",
    }


def test_done_update_timeout_preserves_artifacts_while_commit_is_unconfirmed() -> None:
    request = _request()
    lease = WorkerLease(owner="worker-a", version=7, attempt_id="attempt-exact")
    backend, ddb, s3, result = _terminal_timeout_backend(
        request,
        lease,
        commit_before_timeout=False,
    )

    with pytest.raises(_TerminalResultWriteError) as caught:
        backend.store_result(request, lease, result)

    assert caught.value.state is _TerminalWriteState.OWNED_RUNNING
    assert ddb.reads == [True]
    assert ddb.item["status"] == {"S": "running"}
    assert s3.list_calls == 0
    assert s3.deleted == []
    assert len(s3.objects) == 2


class _UnconfirmedDoneBackend(_Backend):
    def store_result(
        self,
        request: MediaJobRequest,
        lease: WorkerLease,
        result: MediaJobResult,
    ) -> None:
        del request, result
        self._assert_lease(lease)
        self.stores += 1
        self.events.append("store")
        raise _TerminalResultWriteError(_TerminalWriteState.OWNED_RUNNING)


def test_unconfirmed_done_write_is_not_downgraded_to_failed_or_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _UnconfirmedDoneBackend()
    monkeypatch.setattr("teamagent.media.worker.execute_operation", _successful_operation)

    with pytest.raises(_TerminalResultWriteError):
        run_job(
            _request(),
            backend,
            temp_root=tmp_path,
            now_epoch_s=101,
            owner="worker-a",
        )

    assert backend.status == "running"
    assert backend.stores == 1
    assert backend.cleanups == 0
    assert backend.events == ["store"]


def test_worker_cleanup_error_preserves_terminal_failure_for_janitor_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()
    backend.fail_cleanup = True

    def fail_operation(*_args: object, **_kwargs: object) -> OperationOutput:
        raise MediaOperationError("MEDIA_TEST_FAILED", "test")

    monkeypatch.setattr("teamagent.media.worker.execute_operation", fail_operation)

    result = run_job(
        _request(),
        backend,
        temp_root=tmp_path,
        now_epoch_s=101,
        owner="worker-a",
    )

    assert result.status == "failed"
    assert result.error_code == "MEDIA_TEST_FAILED"
    assert backend.status == "failed"
    assert backend.stores == 1
    assert backend.cleanups == 1
    assert backend.events == ["store", "cleanup"]
    assert list(tmp_path.iterdir()) == []


def test_worker_fails_terminally_when_budget_expires_after_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()
    clock = [101.0]

    def finish_after_deadline(*args: object, **kwargs: object) -> OperationOutput:
        output = _successful_operation(*args, **kwargs)
        clock[0] = 386.0
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
    assert backend.events == ["store", "cleanup"]


def test_worker_reserves_terminal_budget_before_starting_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    backend = _Backend()

    def must_not_execute(*_args: object, **_kwargs: object) -> OperationOutput:
        raise AssertionError("operation started inside terminal reserve")

    monkeypatch.setattr("teamagent.media.worker.execute_operation", must_not_execute)
    result = run_job(
        request,
        backend,
        temp_root=tmp_path,
        now_epoch_s=request.deadline_epoch_s - 14,
        owner="worker-a",
    )

    assert result.status == "failed"
    assert result.error_code == "MEDIA_JOB_DEADLINE_EXCEEDED"
    assert backend.events == ["store", "cleanup"]
