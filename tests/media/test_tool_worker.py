from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from teamagent.media import tool_worker
from teamagent.media.contracts import (
    AcquireOperation,
    S3ObjectRef,
    make_job_request,
)
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import (
    MediaOperationError,
    OperationOutput,
    ProducedArtifact,
)
from teamagent.media.tool_contracts import (
    InputCapability,
    OutputCapability,
    PresignedPost,
    ToolControl,
)

_ATTEMPT_ID = "01234567-89ab-4cde-8fab-0123456789ab"
_SECRET = "a" * 64


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = {name.lower(): value for name, value in (headers or {}).items()}
        self.body = io.BytesIO(body)
        self.closed = False

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def read(self, amount: int = -1) -> bytes:
        return self.body.read(amount)

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.method = ""
        self.target = ""
        self.headers: dict[str, str] = {}
        self.sent = bytearray()
        self.closed = False

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, target, headers))

    def putrequest(
        self,
        method: str,
        target: str,
        *,
        skip_accept_encoding: bool,
    ) -> None:
        assert skip_accept_encoding is True
        self.method = method
        self.target = target

    def putheader(self, name: str, value: str) -> None:
        self.headers[name.lower()] = value

    def endheaders(self) -> None:
        return

    def send(self, value: bytes) -> None:
        self.sent.extend(value)

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.closed = True


def _budget() -> DeadlineBudget:
    return DeadlineBudget(2_000, clock=lambda: 1_000)


def _source(body: bytes = b"source") -> S3ObjectRef:
    job_id = "mj_0123456789abcdef01234567"
    return S3ObjectRef(
        bucket="teamagent-media-test",
        key=f"media-jobs/{job_id}/input/source.mp4",
        version_id="version-1",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type="video/mp4",
    )


def _input_response(
    ref: S3ObjectRef,
    body: bytes,
    *,
    overrides: dict[str, str] | None = None,
    status: int = 200,
) -> _Response:
    headers = {
        "Content-Length": str(ref.size),
        "Content-Type": ref.content_type,
        "x-amz-version-id": ref.version_id,
        "x-amz-server-side-encryption": "AES256",
        "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(ref.sha256)).decode(),
    }
    headers.update(overrides or {})
    return _Response(status=status, headers=headers, body=body)


def _input_capability(ref: S3ObjectRef) -> InputCapability:
    return InputCapability(
        ref=ref,
        get_url=(
            f"https://{ref.bucket}.s3.amazonaws.com/{ref.key}?versionId=version-1&signature=test"
        ),
    )


def _output_capability(
    *,
    name: str = "media",
    key: str = (f"media-jobs/mj_0123456789abcdef01234567/attempts/1/{_ATTEMPT_ID}/output/media"),
    maximum: int = 1024,
) -> OutputCapability:
    return OutputCapability(
        name=name,
        key=key,
        max_bytes=maximum,
        post=PresignedPost(
            url="https://teamagent-media-test.s3.amazonaws.com/",
            fields=(("key", key), ("policy", "signed-policy")),
        ),
    )


def test_download_streams_one_exact_checksummed_immutable_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = b"source"
    ref = _source(body)
    response = _input_response(ref, body)
    connection = _Connection(response)
    monkeypatch.setattr(
        tool_worker,
        "_connection",
        lambda *_args, **_kwargs: (connection, f"/{ref.key}?signed=1"),
    )

    destination = tool_worker._download_exact(
        _input_capability(ref),
        tmp_path / "input" / "source.mp4",
        workdir=tmp_path,
        budget=_budget(),
    )

    assert destination.read_bytes() == body
    assert connection.requests == [
        (
            "GET",
            f"/{ref.key}?signed=1",
            {
                "Accept-Encoding": "identity",
                "Connection": "close",
                "x-amz-checksum-mode": "ENABLED",
            },
        )
    ]
    assert response.closed is True
    assert connection.closed is True


@pytest.mark.parametrize(
    ("overrides", "body", "status"),
    [
        ({"Content-Length": "7"}, b"source", 200),
        ({"Content-Type": "text/plain"}, b"source", 200),
        ({"x-amz-version-id": "other"}, b"source", 200),
        ({"x-amz-server-side-encryption": ""}, b"source", 200),
        ({"x-amz-checksum-sha256": "wrong"}, b"source", 200),
        ({"Content-Encoding": "gzip"}, b"source", 200),
        ({}, b"tamper", 200),
        ({}, b"source!", 200),
        ({}, b"", 302),
    ],
)
def test_download_rejects_header_body_redirect_and_size_mutations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overrides: dict[str, str],
    body: bytes,
    status: int,
) -> None:
    ref = _source()
    response = _input_response(ref, body, overrides=overrides, status=status)
    connection = _Connection(response)
    monkeypatch.setattr(
        tool_worker,
        "_connection",
        lambda *_args, **_kwargs: (connection, "/signed"),
    )
    destination = tmp_path / "source.mp4"

    with pytest.raises(tool_worker.ToolTransportError):
        tool_worker._download_exact(
            _input_capability(ref),
            destination,
            workdir=tmp_path,
            budget=_budget(),
        )

    assert not destination.exists()


def test_download_rejects_existing_destination_and_symlink_parent(
    tmp_path: Path,
) -> None:
    ref = _source()
    destination = tmp_path / "source.mp4"
    destination.write_bytes(b"owned")
    with pytest.raises(tool_worker.ToolTransportError, match="fresh"):
        tool_worker._download_exact(
            _input_capability(ref),
            destination,
            workdir=tmp_path,
            budget=_budget(),
        )

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "escape"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(tool_worker.ToolTransportError, match="escapes"):
        tool_worker._download_exact(
            _input_capability(ref),
            link / "source.mp4",
            workdir=tmp_path,
            budget=_budget(),
        )


def test_upload_streams_checksum_bound_multipart_and_returns_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"artifact")
    response = _Response(
        status=204,
        headers={"x-amz-version-id": "version-1"},
    )
    connection = _Connection(response)
    monkeypatch.setattr(
        tool_worker,
        "_connection",
        lambda *_args, **_kwargs: (connection, "/"),
    )
    capability = _output_capability()

    ref = tool_worker._upload_path(
        capability,
        path=artifact,
        content_type="video/mp4",
        bucket="teamagent-media-test",
        budget=_budget(),
    )

    assert ref.version_id == "version-1"
    assert ref.sha256 == hashlib.sha256(b"artifact").hexdigest()
    assert connection.method == "POST"
    assert int(connection.headers["content-length"]) == len(connection.sent)
    assert b'name="x-amz-checksum-sha256"' in connection.sent
    assert base64.b64encode(hashlib.sha256(b"artifact").digest()) in connection.sent
    assert b"Content-Type: video/mp4" in connection.sent
    assert b"artifact" in connection.sent


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (302, {}, b""),
        (204, {}, b""),
        (204, {"x-amz-version-id": "null"}, b""),
        (204, {"x-amz-version-id": "version-1"}, b"unexpected"),
    ],
)
def test_upload_rejects_redirect_mutable_version_and_response_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
    body: bytes,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"artifact")
    connection = _Connection(_Response(status=status, headers=headers, body=body))
    monkeypatch.setattr(
        tool_worker,
        "_connection",
        lambda *_args, **_kwargs: (connection, "/"),
    )

    with pytest.raises(tool_worker.ToolTransportError):
        tool_worker._upload_path(
            _output_capability(),
            path=artifact,
            content_type="video/mp4",
            bucket="teamagent-media-test",
            budget=_budget(),
        )


def test_upload_rejects_hardlinks_and_midstream_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"artifact")
    hardlink = tmp_path / "hardlink.bin"
    os.link(artifact, hardlink)
    with pytest.raises(tool_worker.ToolTransportError, match="regular-file slot"):
        tool_worker._upload_path(
            _output_capability(),
            path=artifact,
            content_type="video/mp4",
            bucket="teamagent-media-test",
            budget=_budget(),
        )
    hardlink.unlink()

    connection = _Connection(_Response(status=204, headers={"x-amz-version-id": "version-1"}))
    monkeypatch.setattr(
        tool_worker,
        "_connection",
        lambda *_args, **_kwargs: (connection, "/"),
    )
    original_send_fd = tool_worker._send_fd

    def mutate_after_send(
        target: Any,
        fd: int,
        budget: DeadlineBudget,
    ) -> None:
        original_send_fd(target, fd, budget)
        artifact.write_bytes(b"changed!")

    monkeypatch.setattr(tool_worker, "_send_fd", mutate_after_send)
    with pytest.raises(tool_worker.ToolTransportError, match="changed during upload"):
        tool_worker._upload_path(
            _output_capability(),
            path=artifact,
            content_type="video/mp4",
            bucket="teamagent-media-test",
            budget=_budget(),
        )


def _control() -> ToolControl:
    request = make_job_request(
        operation=AcquireOperation(
            kind="acquire",
            url="https://www.youtube.com/watch?v=BaW_jenozKc",
            max_bytes=1024,
        ),
        output_bucket="teamagent-media-test",
        request_fingerprint="roleless-tool-run",
        now_epoch_s=1_000,
        timeout_s=300,
        job_id="mj_0123456789abcdef01234567",
    )
    completion_key = f"{request.output_prefix}attempts/1/{_ATTEMPT_ID}/_COMPLETION.json"
    return ToolControl(
        request=request,
        attempt_id=_ATTEMPT_ID,
        attempt_version=1,
        capability_secret=_SECRET,
        inputs=(),
        outputs=(_output_capability(),),
        completion=_output_capability(
            name="_completion",
            key=completion_key,
            maximum=128 * 1024,
        ),
    )


def _identity_environment(control: ToolControl) -> dict[str, str]:
    return {
        "MEDIA_JOB_ID": control.request.job_id,
        "MEDIA_JOB_PAYLOAD_SHA256": control.request.payload_sha256,
        "MEDIA_JOB_DEADLINE_EPOCH_S": str(control.request.deadline_epoch_s),
        "MEDIA_ATTEMPT_ID": control.attempt_id,
        "MEDIA_ATTEMPT_VERSION": str(control.attempt_version),
        "MEDIA_CAPABILITY_SHA256": hashlib.sha256(control.capability_secret.encode()).hexdigest(),
    }


@contextmanager
def _no_signal(_deadline: float) -> Iterator[None]:
    yield


def test_run_tool_uploads_artifact_then_canonical_secret_bound_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    control = _control()
    monkeypatch.setattr(tool_worker, "_signal_scope", _no_signal)

    def execute(*_args: Any, **kwargs: Any) -> OperationOutput:
        workdir = kwargs["workdir"]
        assert isinstance(workdir, Path)
        artifact = workdir / "media"
        artifact.write_bytes(b"video")
        return OperationOutput(
            artifacts=(ProducedArtifact("media", artifact, "video/mp4"),),
            metadata={"bounded": True},
        )

    completion: dict[str, Any] = {}

    def upload(
        capability: OutputCapability,
        *,
        path: Path,
        content_type: str,
        bucket: str,
        budget: DeadlineBudget,
    ) -> S3ObjectRef:
        del budget
        body = path.read_bytes()
        if capability.name == "_completion":
            completion.update(json.loads(body))
        return S3ObjectRef(
            bucket=bucket,
            key=capability.key,
            version_id=f"version-{capability.name}",
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            content_type=content_type,
        )

    monkeypatch.setattr(tool_worker, "execute_operation", execute)
    monkeypatch.setattr(tool_worker, "_upload_path", upload)

    result = tool_worker.run_tool(
        control,
        environ=_identity_environment(control),
        temp_root=tmp_path,
        clock=lambda: 1_001,
    )

    assert result.status == "done"
    assert result.artifacts[0].name == "media"
    assert completion["capability_secret"] == _SECRET
    assert completion["attempt_id"] == _ATTEMPT_ID
    assert completion["result"] == result.model_dump(mode="json")


def test_run_tool_converts_operation_and_slot_failures_to_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    control = _control()
    monkeypatch.setattr(tool_worker, "_signal_scope", _no_signal)
    completions: list[dict[str, Any]] = []

    def upload(
        capability: OutputCapability,
        *,
        path: Path,
        content_type: str,
        bucket: str,
        budget: DeadlineBudget,
    ) -> S3ObjectRef:
        del budget
        body = path.read_bytes()
        if capability.name == "_completion":
            completions.append(json.loads(body))
        return S3ObjectRef(
            bucket=bucket,
            key=capability.key,
            version_id="version-1",
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            content_type=content_type,
        )

    monkeypatch.setattr(tool_worker, "_upload_path", upload)
    monkeypatch.setattr(
        tool_worker,
        "execute_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MediaOperationError("MEDIA_SAFE_FAILURE", "expected")
        ),
    )

    result = tool_worker.run_tool(
        control,
        environ=_identity_environment(control),
        temp_root=tmp_path,
        clock=lambda: 1_001,
    )

    assert result.status == "failed"
    assert result.error_code == "MEDIA_SAFE_FAILURE"
    assert completions[-1]["result"]["error_code"] == "MEDIA_SAFE_FAILURE"


@pytest.mark.parametrize(
    "forbidden",
    [
        "AWS_ACCESS_KEY_ID",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "MEDIA_JOBS_TABLE",
    ],
)
def test_run_tool_rejects_any_legacy_or_aws_authority_before_execution(
    forbidden: str,
) -> None:
    control = _control()
    environment = _identity_environment(control)
    environment[forbidden] = "authority"

    with pytest.raises(ValueError, match="forbidden AWS authority"):
        tool_worker.run_tool(
            control,
            environ=environment,
            clock=lambda: 1_001,
        )
