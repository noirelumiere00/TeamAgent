"""Roleless one-shot media tool driven only by bounded HTTP capabilities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import signal
import ssl
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from urllib.parse import SplitResult, urlsplit

import teamagent.media.operations as media_operations
from teamagent.media.contracts import (
    MediaArtifact,
    MediaJobRequest,
    MediaJobResult,
    S3ObjectRef,
)
from teamagent.media.deadline import DeadlineBudget, MediaDeadlineExceededError
from teamagent.media.operations import MediaOperationError, ProducedArtifact, execute_operation
from teamagent.media.tool_contracts import (
    InputCapability,
    OutputCapability,
    PresignedPost,
    ToolControl,
    parse_control_from_env,
)

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 1024 * 1024
_MAX_HTTP_ERROR_BYTES = 64 * 1024
_TERMINAL_RESERVE_SECONDS = 15
_HTTP_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_FORBIDDEN_ENVIRONMENT = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "MEDIA_JOB_BUCKET",
    "MEDIA_JOBS_TABLE",
}


class ToolTransportError(RuntimeError):
    """A bounded presigned HTTP transfer failed its exact contract."""


class _ToolTerminatedError(RuntimeError):
    pass


def _checksum_sha256_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def _https_target(url: str) -> tuple[str, int, str]:
    if not url.isascii() or any(ord(character) < 32 for character in url):
        raise ToolTransportError("capability URL must be printable ASCII")
    try:
        parsed: SplitResult = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ToolTransportError("capability URL is malformed") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ToolTransportError("capability URL host must be ASCII") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise ToolTransportError("capability URL must be canonical HTTPS")
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return host, port or 443, target


def _connection(url: str, budget: DeadlineBudget) -> tuple[http.client.HTTPSConnection, str]:
    host, port, target = _https_target(url)
    timeout = min(30.0, budget.remaining())
    context = ssl.create_default_context()
    return (
        http.client.HTTPSConnection(
            host,
            port=port,
            timeout=max(0.001, timeout),
            context=context,
        ),
        target,
    )


def _safe_destination(workdir: Path, destination: Path) -> Path:
    root = workdir.resolve(strict=True)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = destination.parent.resolve(strict=True)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ToolTransportError("input destination escapes the job directory") from exc
    if destination.exists() or destination.is_symlink():
        raise ToolTransportError("input destination must be fresh")
    return destination


def _open_fresh(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, 0o600)


def _read_error_body(response: http.client.HTTPResponse) -> bytes:
    body = response.read(_MAX_HTTP_ERROR_BYTES + 1)
    if len(body) > _MAX_HTTP_ERROR_BYTES:
        raise ToolTransportError("HTTP error response exceeds bounded size")
    return body


def _download_exact(
    capability: InputCapability,
    destination: Path,
    *,
    workdir: Path,
    budget: DeadlineBudget,
) -> Path:
    destination = _safe_destination(workdir, destination)
    connection, target = _connection(capability.get_url, budget)
    fd = -1
    response: http.client.HTTPResponse | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Accept-Encoding": "identity",
                "Connection": "close",
                "x-amz-checksum-mode": "ENABLED",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            _read_error_body(response)
            raise ToolTransportError(f"presigned GET returned HTTP {response.status}")
        ref = capability.ref
        if (
            response.getheader("Content-Encoding") not in {None, "identity"}
            or response.getheader("Content-Length") != str(ref.size)
            or response.getheader("Content-Type") != ref.content_type
            or response.getheader("x-amz-version-id") != ref.version_id
            or response.getheader("x-amz-server-side-encryption") not in {"AES256", "aws:kms"}
            or not hmac.compare_digest(
                response.getheader("x-amz-checksum-sha256") or "",
                _checksum_sha256_b64(ref.sha256),
            )
        ):
            raise ToolTransportError("presigned GET headers do not match immutable input")
        fd = _open_fresh(destination)
        while True:
            budget.checkpoint()
            chunk = response.read(min(_CHUNK_BYTES, ref.size + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > ref.size:
                raise ToolTransportError("presigned GET exceeded declared input size")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise ToolTransportError("input file write failed")
                view = view[written:]
        os.fsync(fd)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        if response is not None:
            response.close()
        connection.close()
    if total != capability.ref.size or not hmac.compare_digest(
        digest.hexdigest(),
        capability.ref.sha256,
    ):
        destination.unlink(missing_ok=True)
        raise ToolTransportError("presigned GET content digest or size is invalid")
    info = destination.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != total:
        destination.unlink(missing_ok=True)
        raise ToolTransportError("downloaded input is not one exact regular file")
    return destination


def _form_field(name: str, value: str, boundary: str) -> bytes:
    if (
        not _HTTP_FIELD_NAME.fullmatch(name)
        or not isinstance(value, str)
        or len(value.encode("utf-8")) > 128 * 1024
        or "\r" in value
        or "\n" in value
        or "\x00" in value
    ):
        raise ToolTransportError("presigned POST form field is invalid")
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode()


def _multipart_parts(
    post: PresignedPost,
    *,
    content_type: str,
    checksum_sha256: str,
    boundary: str,
) -> tuple[tuple[bytes, ...], bytes, bytes]:
    fields = dict(post.fields)
    if "Content-Type" in fields or "x-amz-checksum-sha256" in fields:
        raise ToolTransportError("dispatcher must leave dynamic POST fields unset")
    fields["Content-Type"] = content_type
    fields["x-amz-checksum-sha256"] = _checksum_sha256_b64(checksum_sha256)
    preamble = tuple(_form_field(name, fields[name], boundary) for name in sorted(fields))
    file_header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="artifact"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("ascii")
    footer = f"\r\n--{boundary}--\r\n".encode("ascii")
    return preamble, file_header, footer


def _regular_file(path: Path, maximum: int) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= maximum:
        os.close(fd)
        raise ToolTransportError("artifact is outside its bounded regular-file slot")
    return fd, info


def _hash_fd(fd: int, budget: DeadlineBudget) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        budget.checkpoint()
        chunk = os.read(fd, _CHUNK_BYTES)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _send_fd(
    connection: http.client.HTTPSConnection,
    fd: int,
    budget: DeadlineBudget,
) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        budget.checkpoint()
        chunk = os.read(fd, _CHUNK_BYTES)
        if not chunk:
            return
        connection.send(chunk)


def _upload_path(
    capability: OutputCapability,
    *,
    path: Path,
    content_type: str,
    bucket: str,
    budget: DeadlineBudget,
) -> S3ObjectRef:
    if (
        not content_type
        or len(content_type) > 128
        or not content_type.isascii()
        or any(ord(character) < 32 for character in content_type)
    ):
        raise ToolTransportError("artifact content type is invalid")
    fd, before = _regular_file(path, capability.max_bytes)
    connection: http.client.HTTPSConnection | None = None
    response: http.client.HTTPResponse | None = None
    try:
        digest = _hash_fd(fd, budget)
        boundary = f"teamagent-{secrets.token_hex(24)}"
        preamble, file_header, footer = _multipart_parts(
            capability.post,
            content_type=content_type,
            checksum_sha256=digest,
            boundary=boundary,
        )
        content_length = (
            sum(len(part) for part in preamble) + len(file_header) + before.st_size + len(footer)
        )
        if content_length - before.st_size > 64 * 1024:
            raise ToolTransportError("presigned POST multipart overhead exceeds its policy")
        connection, target = _connection(capability.post.url, budget)
        connection.putrequest("POST", target, skip_accept_encoding=True)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.putheader("Connection", "close")
        connection.endheaders()
        for part in preamble:
            connection.send(part)
        connection.send(file_header)
        _send_fd(connection, fd, budget)
        connection.send(footer)
        response = connection.getresponse()
        if response.status not in {201, 204}:
            _read_error_body(response)
            raise ToolTransportError(f"presigned POST returned HTTP {response.status}")
        if response.read(_MAX_HTTP_ERROR_BYTES + 1):
            raise ToolTransportError("presigned POST returned an unexpected response body")
        version_id = response.getheader("x-amz-version-id") or ""
        if (
            not 1 <= len(version_id) <= 1024
            or version_id == "null"
            or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", version_id)
        ):
            raise ToolTransportError("presigned POST did not return an immutable version")
        after = os.fstat(fd)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or after.st_nlink != 1
        ):
            raise ToolTransportError("artifact changed during upload")
        return S3ObjectRef(
            bucket=bucket,
            key=capability.key,
            version_id=version_id,
            sha256=digest,
            size=before.st_size,
            content_type=content_type,
        )
    finally:
        os.close(fd)
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()


def _ref_identity(ref: S3ObjectRef) -> tuple[str, str, str, str, int, str]:
    return (
        ref.bucket,
        ref.key,
        ref.version_id,
        ref.sha256,
        ref.size,
        ref.content_type,
    )


def _assert_identity_environment(control: ToolControl, environ: Mapping[str, str]) -> None:
    request = control.request
    expected = {
        "MEDIA_JOB_ID": request.job_id,
        "MEDIA_JOB_PAYLOAD_SHA256": request.payload_sha256,
        "MEDIA_JOB_DEADLINE_EPOCH_S": str(request.deadline_epoch_s),
        "MEDIA_ATTEMPT_ID": control.attempt_id,
        "MEDIA_ATTEMPT_VERSION": str(control.attempt_version),
        "MEDIA_CAPABILITY_SHA256": hashlib.sha256(
            control.capability_secret.encode("ascii")
        ).hexdigest(),
    }
    if request.audit_principal_hash is not None:
        expected["MEDIA_JOB_AUDIT_PRINCIPAL_HASH"] = request.audit_principal_hash
    if any(
        not hmac.compare_digest(environ.get(name, ""), value) for name, value in expected.items()
    ):
        raise ValueError("media task identity does not match its capability control")
    if any(name in environ for name in _FORBIDDEN_ENVIRONMENT):
        raise ValueError("roleless media task received forbidden AWS authority")


def _active_process_groups() -> tuple[int, ...]:
    with media_operations._ACTIVE_PROCESS_GROUPS_LOCK:
        return tuple(media_operations._ACTIVE_PROCESS_GROUPS)


def _terminate_process_groups() -> None:
    groups = _active_process_groups()
    for process_group in groups:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    limit = time.monotonic() + 0.5
    while time.monotonic() < limit:
        live = []
        for process_group in groups:
            try:
                os.killpg(process_group, 0)
                live.append(process_group)
            except ProcessLookupError:
                pass
        if not live:
            return
        time.sleep(0.01)
    for process_group in groups:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass


@contextmanager
def _signal_scope(deadline_epoch_s: float) -> Iterator[None]:
    previous_alarm = signal.getsignal(signal.SIGALRM)
    previous_term = signal.getsignal(signal.SIGTERM)

    def handle_alarm(_signum: int, _frame: FrameType | None) -> None:
        _terminate_process_groups()
        raise MediaDeadlineExceededError("media tool execution deadline exceeded")

    def handle_term(_signum: int, _frame: FrameType | None) -> None:
        _terminate_process_groups()
        raise _ToolTerminatedError("media tool termination requested")

    remaining = deadline_epoch_s - time.time()
    if remaining <= 0:
        raise MediaDeadlineExceededError("media tool deadline exceeded")
    signal.signal(signal.SIGALRM, handle_alarm)
    signal.signal(signal.SIGTERM, handle_term)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)
        signal.signal(signal.SIGTERM, previous_term)


def _failed_result(request: MediaJobRequest, error_code: str) -> MediaJobResult:
    return MediaJobResult(
        job_id=request.job_id,
        status="failed",
        error_code=error_code,
    )


def _completion_bytes(control: ToolControl, result: MediaJobResult) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "job_id": control.request.job_id,
            "payload_sha256": control.request.payload_sha256,
            "attempt_id": control.attempt_id,
            "attempt_version": control.attempt_version,
            "capability_secret": control.capability_secret,
            "result": result.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _upload_completion(
    control: ToolControl,
    result: MediaJobResult,
    *,
    workdir: Path,
    budget: DeadlineBudget,
) -> None:
    encoded = _completion_bytes(control, result)
    if not 1 <= len(encoded) <= control.completion.max_bytes:
        raise ToolTransportError("completion exceeds its bounded slot")
    path = workdir / "_COMPLETION.json"
    fd = _open_fresh(path)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ToolTransportError("completion write failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _upload_path(
        control.completion,
        path=path,
        content_type="application/json",
        bucket=control.request.output_bucket,
        budget=budget,
    )


def run_tool(
    control: ToolControl,
    *,
    environ: Mapping[str, str] = os.environ,
    temp_root: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> MediaJobResult:
    _assert_identity_environment(control, environ)
    request = control.request
    request.assert_dispatchable(now_epoch_s=int(clock()))
    execution_deadline = request.deadline_epoch_s - _TERMINAL_RESERVE_SECONDS
    execution_budget = DeadlineBudget(execution_deadline, clock=clock)
    hard_budget = DeadlineBudget(request.deadline_epoch_s, clock=clock)
    root = temp_root or Path("/tmp/teamagent/jobs")  # nosec B108
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    inputs = {_ref_identity(item.ref): item for item in control.inputs}
    output_slots = {item.name: item for item in control.outputs}
    result: MediaJobResult
    try:
        with _signal_scope(execution_deadline):
            with tempfile.TemporaryDirectory(
                prefix=f"{request.job_id}-",
                dir=root,
            ) as raw_workdir:
                workdir = Path(raw_workdir)

                def load(ref: S3ObjectRef, destination: Path) -> Path:
                    capability = inputs.get(_ref_identity(ref))
                    if capability is None:
                        raise ToolTransportError("operation requested an ungranted input")
                    return _download_exact(
                        capability,
                        destination,
                        workdir=workdir,
                        budget=execution_budget,
                    )

                output = execute_operation(
                    request.operation,
                    workdir=workdir,
                    load_object=load,
                    budget=execution_budget,
                )
                artifacts: list[MediaArtifact] = []
                names: set[str] = set()
                for produced in output.artifacts:
                    if not isinstance(produced, ProducedArtifact):
                        raise ToolTransportError("operation returned an invalid artifact")
                    if produced.name in names:
                        raise ToolTransportError("operation returned duplicate artifacts")
                    slot = output_slots.get(produced.name)
                    if slot is None:
                        raise ToolTransportError("operation exceeded its granted output slots")
                    ref = _upload_path(
                        slot,
                        path=produced.path,
                        content_type=produced.content_type,
                        bucket=request.output_bucket,
                        budget=execution_budget,
                    )
                    artifacts.append(MediaArtifact(name=produced.name, object=ref))
                    names.add(produced.name)
                result = MediaJobResult(
                    job_id=request.job_id,
                    status="done",
                    artifacts=tuple(artifacts),
                    metadata=dict(output.metadata),
                )
    except MediaDeadlineExceededError:
        result = _failed_result(request, "MEDIA_JOB_DEADLINE_EXCEEDED")
    except MediaOperationError as exc:
        result = _failed_result(request, exc.code)
    except _ToolTerminatedError:
        result = _failed_result(request, "MEDIA_WORKER_TERMINATED")
    except Exception:
        logger.exception("roleless media tool execution failed")
        result = _failed_result(request, "MEDIA_WORKER_FAILED")
    hard_budget.checkpoint()
    with tempfile.TemporaryDirectory(
        prefix=f"{request.job_id}-completion-",
        dir=root,
    ) as completion_dir:
        _upload_completion(
            control,
            result,
            workdir=Path(completion_dir),
            budget=hard_budget,
        )
    return result


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        control = parse_control_from_env(os.environ)
        result = run_tool(control)
    except Exception:
        logger.exception("roleless media tool rejected its capability envelope")
        return 2
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ToolTransportError",
    "main",
    "run_tool",
]
