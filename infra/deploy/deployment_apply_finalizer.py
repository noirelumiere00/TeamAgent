#!/usr/bin/env python3
"""Atomically finalize a guarded Terraform apply and its durable receipt.

The runtime guard deliberately leaves the EventBridge and ECS rollback sagas
in their APPLYING states until every post-apply probe has passed.  This helper
then performs one DynamoDB transaction that:

* marks both rollback sagas terminal,
* marks the exact deployment intent APPLIED,
* releases the exact shared deployment lock, and
* persists the complete canonical apply receipt in bounded immutable chunks.

Consequently, a process crash after the transaction cannot leave a successful
production change without a recoverable apply receipt.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

_REGION = "ap-northeast-1"
_ACCOUNT_ID = "718959508629"
_IMAGE_LEDGER_TABLE = "teamagent-dev-image-deployment-intents"
_LOCK_RECORD_ID = "lock#teamagent/terraform.tfstate"
_ECS_ACTIVE_RECORD_ID = "ecs-service-apply#active#teamagent-dev-mcp-connect-web"
_ECS_ACTIVE_RECORD_TYPE = "teamagent.ecs-service-apply-saga-active"
_ECS_ACTIVE_SCOPE_ID = "teamagent-dev-mcp-connect-web"
_ECS_ATTEMPT_RECORD_TYPE = "teamagent.ecs-service-apply-saga"
_EVENTBRIDGE_ACTIVE_RECORD_TYPE = "teamagent.eventbridge-apply-saga-active"
_EVENTBRIDGE_RECORD_PREFIX = "ecs-service-apply#eventbridge#active#"
_FINALIZATION_PREFIX = "apply-finalization#"
_FINALIZATION_CHUNK_PREFIX = "apply-finalization-chunk#"
_FINALIZATION_KIND = "teamagent.deployment-apply-finalization"
_FINALIZATION_RECEIPT_KIND = "teamagent-deployment-apply-finalization-receipt"
_FINALIZATION_CHUNK_KIND = "teamagent.deployment-apply-finalization-chunk"
_SCHEMA_VERSION = 1
_APPLY_RECEIPT_SCHEMA_VERSION = 7
_CHUNK_BYTES = 160 * 1024
_MAX_RECEIPT_BYTES = 2_000_000
_MAX_CHUNKS = 80
_MAX_TRANSACTION_ITEMS = 100
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO8601_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_ENDPOINT = f"https://dynamodb.{_REGION}.amazonaws.com"
_MANIFEST_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "apply_receipt_draft_sha256",
        "apply_receipt_sha256",
        "apply_receipt_size",
        "applied_at_epoch",
        "audit_expires_at",
        "chunk_count",
        "ecs_verification_receipt_sha256",
        "eventbridge_verification_receipt_sha256",
        "finalization_receipt_sha256",
        "intent_id",
        "plan_sha256",
        "record_id",
        "record_type",
        "schema_version",
        "state",
    }
)
_CHUNK_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "audit_expires_at",
        "chunk_count",
        "chunk_index",
        "finalization_record_id",
        "intent_id",
        "payload",
        "payload_sha256",
        "plan_sha256",
        "record_id",
        "record_type",
        "schema_version",
    }
)


class FinalizationError(RuntimeError):
    """The composite deployment finalization cannot be proven exact."""


class FinalizationNotFoundError(FinalizationError):
    """No committed finalization exists for the exact intent and plan."""


class LedgerClient(Protocol):
    """Minimal exact DynamoDB surface used by the finalizer."""

    def get_item(
        self,
        *,
        table_name: str,
        key: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Any] | None:
        """Read one item with strong consistency."""

    def transact_write(
        self,
        *,
        items: Sequence[Mapping[str, Any]],
        client_request_token: str,
    ) -> None:
        """Execute one idempotent TransactWriteItems request."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"{label} is unreadable") from exc
    if type(value) is not dict:
        raise FinalizationError(f"{label} must be a JSON object")
    return value


def _canonical_value(value: object) -> object:
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise FinalizationError("JSON object contains a non-string key")
        return {str(key): _canonical_value(item) for key, item in sorted(value.items())}
    if type(value) is list:
        return [_canonical_value(item) for item in value]
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise FinalizationError("JSON value is outside the canonical allowlist")


def _canonical_bytes(value: object, *, newline: bool = False) -> bytes:
    encoded = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _client_request_token(apply_attempt_id: str) -> str:
    suffix = hashlib.sha256(
        f"teamagent-deployment-finalization:{apply_attempt_id}".encode()
    ).hexdigest()[:27]
    return f"finalize-{suffix}"


def _string(value: object, *, label: str) -> str:
    if type(value) is not str or not value:
        raise FinalizationError(f"{label} is invalid")
    return value


def _sha256(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if _SHA256_RE.fullmatch(text) is None:
        raise FinalizationError(f"{label} is invalid")
    return text


def _uuid4(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if _UUID_RE.fullmatch(text) is None:
        raise FinalizationError(f"{label} is invalid")
    return text


def _integer(value: object, *, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise FinalizationError(f"{label} is invalid")
    return value


def _ddb_string(value: str) -> dict[str, str]:
    return {"S": value}


def _ddb_number(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _ddb_binary(value: bytes) -> dict[str, str]:
    return {"B": base64.b64encode(value).decode("ascii")}


def _ddb_item(values: Mapping[str, object]) -> dict[str, dict[str, str]]:
    item: dict[str, dict[str, str]] = {}
    for name, value in sorted(values.items()):
        if type(value) is str:
            item[name] = _ddb_string(value)
        elif type(value) is int:
            item[name] = _ddb_number(value)
        elif type(value) is bytes:
            item[name] = _ddb_binary(value)
        else:
            raise FinalizationError("unsupported DynamoDB item value")
    return item


def _ddb_read_string(item: Mapping[str, Any], name: str) -> str:
    raw = item.get(name)
    value = raw.get("S") if type(raw) is dict and frozenset(raw) == {"S"} else None
    if type(value) is not str:
        raise FinalizationError("durable finalization item is invalid")
    return value


def _ddb_read_number(item: Mapping[str, Any], name: str) -> int:
    raw = item.get(name)
    value = raw.get("N") if type(raw) is dict and frozenset(raw) == {"N"} else None
    if type(value) is not str or not value.isdecimal():
        raise FinalizationError("durable finalization item is invalid")
    return int(value)


def _ddb_read_binary(item: Mapping[str, Any], name: str) -> bytes:
    raw = item.get(name)
    value = raw.get("B") if type(raw) is dict and frozenset(raw) == {"B"} else None
    if type(value) is not str:
        raise FinalizationError("durable finalization chunk is invalid")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise FinalizationError("durable finalization chunk is invalid") from exc


def _trusted_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise FinalizationError("AWS CLI executable is unavailable") from exc
    if (
        not resolved.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or not metadata.st_mode & stat.S_IXUSR
    ):
        raise FinalizationError("AWS CLI executable is not trusted")
    return resolved


def _aws_environment() -> dict[str, str]:
    environment = os.environ.copy()
    rejected = {
        "ALL_PROXY",
        "AWS_CA_BUNDLE",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_DATA_PATH",
        "AWS_DEFAULT_PROFILE",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "BOTO_CONFIG",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    for name in tuple(environment):
        if name.startswith("AWS_ENDPOINT_URL") or name in rejected:
            environment.pop(name, None)
    environment.update(
        {
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_DEFAULT_REGION": _REGION,
            "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
            "AWS_PAGER": "",
            "AWS_REGION": _REGION,
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "LC_ALL": "C",
        }
    )
    return environment


class SubprocessLedgerClient:
    """AWS CLI v2 adapter with an exact regional endpoint."""

    def __init__(self, aws_bin: Path) -> None:
        self.aws_bin = _trusted_executable(aws_bin)

    def _execute(
        self,
        operation: str,
        arguments: Sequence[str],
        *,
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [
                    str(self.aws_bin),
                    "--region",
                    _REGION,
                    "--endpoint-url",
                    _ENDPOINT,
                    "--no-cli-pager",
                    "--no-paginate",
                    "dynamodb",
                    operation,
                    *arguments,
                    "--output",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=_aws_environment(),
                timeout=timeout_seconds,
            )
            value = json.loads(
                completed.stdout or "{}",
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise FinalizationError(
                "AWS finalization operation failed without exposing details"
            ) from exc
        if type(value) is not dict:
            raise FinalizationError("AWS finalization response is invalid")
        return value

    def get_item(
        self,
        *,
        table_name: str,
        key: Mapping[str, Mapping[str, str]],
    ) -> dict[str, Any] | None:
        response = self._execute(
            "get-item",
            [
                "--table-name",
                table_name,
                "--key",
                json.dumps(key, separators=(",", ":"), sort_keys=True),
                "--consistent-read",
            ],
        )
        if frozenset(response) - {"ConsumedCapacity", "Item"}:
            raise FinalizationError("DynamoDB GetItem response has unknown fields")
        item = response.get("Item")
        if item is None:
            return None
        if type(item) is not dict:
            raise FinalizationError("DynamoDB GetItem response is invalid")
        return item

    def transact_write(
        self,
        *,
        items: Sequence[Mapping[str, Any]],
        client_request_token: str,
    ) -> None:
        payload = json.dumps(
            list(items),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="teamagent-finalize-") as raw_dir:
            directory = Path(raw_dir)
            directory.chmod(0o700)
            request = directory / "transact-items.json"
            descriptor = os.open(
                request,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                before = request.stat()
                before_digest = _bytes_digest(request.read_bytes())
                self._execute(
                    "transact-write-items",
                    [
                        "--transact-items",
                        f"file://{request}",
                        "--client-request-token",
                        client_request_token,
                        "--return-consumed-capacity",
                        "NONE",
                    ],
                )
                after = request.stat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) or before_digest != _bytes_digest(request.read_bytes()):
                    raise FinalizationError("DynamoDB transaction request changed during execution")
            finally:
                try:
                    request.unlink()
                except FileNotFoundError:
                    pass


def _finalization_record_id(intent_id: str) -> str:
    return f"{_FINALIZATION_PREFIX}{intent_id}"


def _chunk_record_id(intent_id: str, index: int) -> str:
    return f"{_FINALIZATION_CHUNK_PREFIX}{intent_id}#{index:03d}"


def _image_key(record_id: str) -> dict[str, dict[str, str]]:
    return {"record_id": _ddb_string(record_id)}


def _split_chunks(payload: bytes) -> list[bytes]:
    if not payload or len(payload) > _MAX_RECEIPT_BYTES:
        raise FinalizationError("canonical apply receipt exceeds its durable bound")
    chunks = [
        payload[offset : offset + _CHUNK_BYTES] for offset in range(0, len(payload), _CHUNK_BYTES)
    ]
    if not chunks or len(chunks) > _MAX_CHUNKS:
        raise FinalizationError("canonical apply receipt chunk count is invalid")
    return chunks


def _write_atomic(path: Path, payload: bytes) -> None:
    try:
        parent = path.parent.resolve(strict=True)
        parent_metadata = parent.stat()
    except OSError as exc:
        raise FinalizationError("apply receipt output directory is unavailable") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent_metadata.st_mode & 0o077:
        raise FinalizationError("apply receipt output directory must be private")
    try:
        requested_parent = path.parent.absolute()
    except OSError as exc:
        raise FinalizationError("apply receipt output path is unavailable") from exc
    if requested_parent != parent or path.name in {"", ".", ".."}:
        raise FinalizationError("apply receipt output path must be canonical")
    path = parent / path.name
    try:
        if path.exists() or path.is_symlink():
            raise FinalizationError("apply receipt output already exists")
    except OSError as exc:
        raise FinalizationError("apply receipt output path is unavailable") from exc
    descriptor, raw_stage = tempfile.mkstemp(
        prefix=".teamagent-apply-receipt-",
        dir=parent,
    )
    stage = Path(raw_stage)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(stage, path, follow_symlinks=False)
        linked = path.stat()
        staged = stage.stat()
        if (
            linked.st_dev,
            linked.st_ino,
            linked.st_size,
            stat.S_IMODE(linked.st_mode),
        ) != (
            staged.st_dev,
            staged.st_ino,
            staged.st_size,
            0o600,
        ):
            raise FinalizationError("apply receipt atomic publication is invalid")
        directory_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except FileExistsError as exc:
        raise FinalizationError("apply receipt output already exists") from exc
    except (OSError, FinalizationError) as exc:
        try:
            path.unlink()
        except OSError:
            pass
        if isinstance(exc, FinalizationError):
            raise
        raise FinalizationError("apply receipt atomic publication failed") from exc
    finally:
        try:
            stage.unlink()
        except FileNotFoundError:
            pass


def _manifest_identity(
    *,
    intent_id: str,
    plan_sha256: str,
    apply_attempt_id: str | None,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "record_id": _finalization_record_id(intent_id),
        "record_type": _FINALIZATION_KIND,
        "schema_version": _SCHEMA_VERSION,
        "state": "APPLIED",
        "intent_id": intent_id,
        "plan_sha256": plan_sha256,
    }
    if apply_attempt_id is not None:
        identity["apply_attempt_id"] = apply_attempt_id
    return identity


def _read_durable_receipt(
    client: LedgerClient,
    *,
    intent_id: str,
    plan_sha256: str,
    apply_attempt_id: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    manifest = client.get_item(
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(_finalization_record_id(intent_id)),
    )
    if manifest is None:
        raise FinalizationNotFoundError("durable apply finalization does not exist")
    if frozenset(manifest) != _MANIFEST_FIELDS:
        raise FinalizationError("durable apply finalization fields differ")
    expected_identity = _manifest_identity(
        intent_id=intent_id,
        plan_sha256=plan_sha256,
        apply_attempt_id=apply_attempt_id,
    )
    for name, expected in expected_identity.items():
        if type(expected) is str:
            observed: object = _ddb_read_string(manifest, name)
        else:
            observed = _ddb_read_number(manifest, name)
        if observed != expected:
            raise FinalizationError("durable apply finalization identity differs")
    attempt = _uuid4(
        _ddb_read_string(manifest, "apply_attempt_id"),
        label="durable apply attempt ID",
    )
    if apply_attempt_id is not None and attempt != apply_attempt_id:
        raise FinalizationError("durable apply attempt ID differs")
    receipt_sha256 = _sha256(
        _ddb_read_string(manifest, "apply_receipt_sha256"),
        label="durable apply receipt SHA-256",
    )
    receipt_size = _ddb_read_number(manifest, "apply_receipt_size")
    chunk_count = _ddb_read_number(manifest, "chunk_count")
    if (
        receipt_size < 1
        or receipt_size > _MAX_RECEIPT_BYTES
        or chunk_count < 1
        or chunk_count > _MAX_CHUNKS
    ):
        raise FinalizationError("durable apply receipt bounds are invalid")
    chunks: list[bytes] = []
    for index in range(chunk_count):
        chunk = client.get_item(
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(_chunk_record_id(intent_id, index)),
        )
        if chunk is None:
            raise FinalizationError("durable apply receipt chunk is missing")
        if (
            frozenset(chunk) != _CHUNK_FIELDS
            or _ddb_read_string(chunk, "record_id") != _chunk_record_id(intent_id, index)
            or _ddb_read_string(chunk, "record_type") != _FINALIZATION_CHUNK_KIND
            or _ddb_read_number(chunk, "schema_version") != _SCHEMA_VERSION
            or _ddb_read_string(chunk, "finalization_record_id")
            != _finalization_record_id(intent_id)
            or _ddb_read_string(chunk, "intent_id") != intent_id
            or _ddb_read_string(chunk, "plan_sha256") != plan_sha256
            or _ddb_read_string(chunk, "apply_attempt_id") != attempt
            or _ddb_read_number(chunk, "chunk_index") != index
            or _ddb_read_number(chunk, "chunk_count") != chunk_count
        ):
            raise FinalizationError("durable apply receipt chunk identity differs")
        payload = _ddb_read_binary(chunk, "payload")
        if _bytes_digest(payload) != _sha256(
            _ddb_read_string(chunk, "payload_sha256"),
            label="durable chunk SHA-256",
        ):
            raise FinalizationError("durable apply receipt chunk digest differs")
        chunks.append(payload)
    receipt = b"".join(chunks)
    if len(receipt) != receipt_size or _bytes_digest(receipt) != receipt_sha256:
        raise FinalizationError("durable apply receipt digest differs")
    try:
        decoded = json.loads(receipt, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise FinalizationError("durable apply receipt JSON is invalid") from exc
    if (
        type(decoded) is not dict
        or decoded.get("kind") != "terraform-runtime-apply-receipt"
        or decoded.get("schema_version") != _APPLY_RECEIPT_SCHEMA_VERSION
        or decoded.get("status") != "applied"
        or decoded.get("provenance_outcome") != "applied"
        or decoded.get("image_deployment_intent_id") != intent_id
        or decoded.get("plan_sha256") != plan_sha256
        or decoded.get("apply_attempt_id") != attempt
        or _canonical_bytes(decoded, newline=True) != receipt
    ):
        raise FinalizationError("durable apply receipt contract is invalid")
    finalization = decoded.get("deployment_finalization_receipt")
    eventbridge_verification = decoded.get("eventbridge_apply_saga_verification_receipt")
    ecs_verification = decoded.get("ecs_service_saga_verification_receipt")
    if (
        type(finalization) is not dict
        or type(eventbridge_verification) is not dict
        or type(ecs_verification) is not dict
        or _ddb_read_string(manifest, "apply_receipt_draft_sha256")
        != finalization.get("apply_receipt_draft_sha256")
        or _ddb_read_string(manifest, "finalization_receipt_sha256")
        != decoded.get("deployment_finalization_receipt_sha256")
        or _ddb_read_string(
            manifest,
            "eventbridge_verification_receipt_sha256",
        )
        != eventbridge_verification.get("receipt_sha256")
        or _ddb_read_string(manifest, "ecs_verification_receipt_sha256")
        != ecs_verification.get("receipt_sha256")
        or _ddb_read_number(manifest, "applied_at_epoch") != decoded.get("applied_at_epoch")
    ):
        raise FinalizationError("durable apply receipt manifest binding differs")
    return receipt, decoded


_ECS_VERIFICATION_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "baseline_sha256",
        "kind",
        "ledger_item_sha256",
        "plan_sha256",
        "planned_sha256",
        "receipt_sha256",
        "record_id",
        "schema_version",
        "stage",
    }
)
_ECS_ATTEMPT_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "baseline_json",
        "baseline_sha256",
        "plan_sha256",
        "planned_json",
        "planned_sha256",
        "record_id",
        "record_type",
        "schema_version",
        "stage",
    }
)
_ECS_ACTIVE_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "attempt_record_id",
        "baseline_sha256",
        "plan_sha256",
        "planned_sha256",
        "record_id",
        "record_type",
        "schema_version",
        "scope_id",
        "stage",
    }
)


def _validate_ecs_verification(
    raw: Mapping[str, Any],
    *,
    plan_sha256: str,
    apply_attempt_id: str,
) -> dict[str, Any]:
    receipt = dict(raw)
    expected_record = f"ecs-service-apply#{apply_attempt_id}"
    if (
        frozenset(receipt) != _ECS_VERIFICATION_FIELDS
        or receipt.get("kind") != "teamagent-ecs-service-apply-saga-receipt"
        or receipt.get("schema_version") != 1
        or receipt.get("record_id") != expected_record
        or receipt.get("stage") != "VERIFIED_APPLIED"
        or receipt.get("plan_sha256") != plan_sha256
        or receipt.get("apply_attempt_id") != apply_attempt_id
    ):
        raise FinalizationError("ECS verification receipt identity differs")
    for name in ("baseline_sha256", "planned_sha256", "ledger_item_sha256"):
        _sha256(receipt.get(name), label=f"ECS verification {name}")
    claimed = _sha256(
        receipt.get("receipt_sha256"),
        label="ECS verification receipt SHA-256",
    )
    if (
        _digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
        != claimed
    ):
        raise FinalizationError("ECS verification receipt digest differs")
    return receipt


def _validate_ecs_attempt_item(
    item: Mapping[str, Any],
    *,
    verification: Mapping[str, Any],
) -> None:
    if (
        frozenset(item) != _ECS_ATTEMPT_FIELDS
        or _ddb_read_string(item, "record_id") != verification["record_id"]
        or _ddb_read_string(item, "record_type") != _ECS_ATTEMPT_RECORD_TYPE
        or _ddb_read_number(item, "schema_version") != 1
        or _ddb_read_string(item, "stage") != "APPLYING"
        or _ddb_read_string(item, "plan_sha256") != verification["plan_sha256"]
        or _ddb_read_string(item, "apply_attempt_id") != verification["apply_attempt_id"]
        or _ddb_read_string(item, "baseline_sha256") != verification["baseline_sha256"]
        or _ddb_read_string(item, "planned_sha256") != verification["planned_sha256"]
        or _digest(item) != verification["ledger_item_sha256"]
    ):
        raise FinalizationError("ECS APPLYING ledger differs from its verification")


def _validate_ecs_active_item(
    item: Mapping[str, Any],
    *,
    verification: Mapping[str, Any],
) -> None:
    if (
        frozenset(item) != _ECS_ACTIVE_FIELDS
        or _ddb_read_string(item, "record_id") != _ECS_ACTIVE_RECORD_ID
        or _ddb_read_string(item, "record_type") != _ECS_ACTIVE_RECORD_TYPE
        or _ddb_read_number(item, "schema_version") != 1
        or _ddb_read_string(item, "scope_id") != _ECS_ACTIVE_SCOPE_ID
        or _ddb_read_string(item, "stage") != "APPLYING"
        or _ddb_read_string(item, "attempt_record_id") != verification["record_id"]
        or _ddb_read_string(item, "plan_sha256") != verification["plan_sha256"]
        or _ddb_read_string(item, "apply_attempt_id") != verification["apply_attempt_id"]
        or _ddb_read_string(item, "baseline_sha256") != verification["baseline_sha256"]
        or _ddb_read_string(item, "planned_sha256") != verification["planned_sha256"]
    ):
        raise FinalizationError("ECS active APPLYING index differs from its verification")


def _terminal_ecs_receipt(
    verification: Mapping[str, Any],
    attempt_item: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    terminal_item = {
        name: dict(value) if type(value) is dict else value for name, value in attempt_item.items()
    }
    terminal_item["stage"] = _ddb_string("APPLIED")
    receipt = {
        **verification,
        "stage": "APPLIED",
        "ledger_item_sha256": _digest(terminal_item),
    }
    receipt.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt, _digest(terminal_item)


def _terminal_ecs_active_digest(active_item: Mapping[str, Any]) -> str:
    terminal_item = {
        name: dict(value) if type(value) is dict else value for name, value in active_item.items()
    }
    terminal_item["stage"] = _ddb_string("APPLIED")
    return _digest(terminal_item)


def _read_required_item(
    client: LedgerClient,
    *,
    table_name: str,
    key: Mapping[str, Mapping[str, str]],
    label: str,
) -> dict[str, Any]:
    item = client.get_item(table_name=table_name, key=key)
    if item is None:
        raise FinalizationError(f"{label} does not exist")
    return item


def _validate_intent_and_lock(
    client: LedgerClient,
    *,
    intent_id: str,
    plan_sha256: str,
    apply_attempt_id: str,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    intent = _read_required_item(
        client,
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(f"intent#{intent_id}"),
        label="deployment intent",
    )
    if (
        _ddb_read_string(intent, "record_id") != f"intent#{intent_id}"
        or _ddb_read_string(intent, "record_type") != "teamagent.image-deployment-intent"
        or _ddb_read_number(intent, "schema_version") != 1
        or _ddb_read_string(intent, "intent_id") != intent_id
        or _ddb_read_string(intent, "state") != "CONSUMED"
        or _ddb_read_string(intent, "plan_sha256") != plan_sha256
        or _ddb_read_string(intent, "apply_attempt_id") != apply_attempt_id
    ):
        raise FinalizationError("deployment intent is not the exact consumed attempt")
    audit_expires_at = _ddb_read_number(intent, "audit_expires_at")
    lock = _read_required_item(
        client,
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(_LOCK_RECORD_ID),
        label="shared deployment lock",
    )
    if (
        _ddb_read_string(lock, "record_id") != _LOCK_RECORD_ID
        or _ddb_read_string(lock, "record_type") != "teamagent.image-release-apply-lock"
        or _ddb_read_number(lock, "schema_version") != 1
        or _ddb_read_string(lock, "state") != "LOCKED"
        or _ddb_read_string(lock, "intent_id") != intent_id
        or _ddb_read_string(lock, "plan_sha256") != plan_sha256
        or _ddb_read_string(lock, "apply_attempt_id") != apply_attempt_id
    ):
        raise FinalizationError("shared deployment lock differs from the exact attempt")
    return intent, lock, audit_expires_at


def _iso8601_from_epoch(epoch: int) -> str:
    try:
        value = dt.datetime.fromtimestamp(epoch, tz=dt.UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise FinalizationError("trusted finalization epoch is invalid") from exc
    rendered = value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if _ISO8601_RE.fullmatch(rendered) is None:
        raise FinalizationError("trusted finalization timestamp is invalid")
    return rendered


def _update(
    *,
    table_name: str,
    key: Mapping[str, Mapping[str, str]],
    update_expression: str,
    condition_expression: str,
    names: Mapping[str, str],
    values: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "Update": {
            "TableName": table_name,
            "Key": dict(key),
            "UpdateExpression": update_expression,
            "ConditionExpression": condition_expression,
            "ExpressionAttributeNames": dict(names),
            "ExpressionAttributeValues": dict(values),
        }
    }


def _delete(
    *,
    table_name: str,
    key: Mapping[str, Mapping[str, str]],
    condition_expression: str,
    names: Mapping[str, str],
    values: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "Delete": {
            "TableName": table_name,
            "Key": dict(key),
            "ConditionExpression": condition_expression,
            "ExpressionAttributeNames": dict(names),
            "ExpressionAttributeValues": dict(values),
        }
    }


def _put(
    *,
    table_name: str,
    item: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": dict(item),
            "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": {"#pk": "record_id"},
        }
    }


def _ecs_transaction_items(
    *,
    verification: Mapping[str, Any],
) -> list[dict[str, Any]]:
    values = _ddb_item(
        {
            ":active": "APPLYING",
            ":applied": "APPLIED",
            ":attempt": str(verification["apply_attempt_id"]),
            ":attempt_record": str(verification["record_id"]),
            ":baseline": str(verification["baseline_sha256"]),
            ":plan": str(verification["plan_sha256"]),
            ":planned": str(verification["planned_sha256"]),
            ":scope": _ECS_ACTIVE_SCOPE_ID,
        }
    )
    attempt = _update(
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(str(verification["record_id"])),
        update_expression="SET #stage = :applied",
        condition_expression=(
            "#stage = :active AND plan_sha256 = :plan"
            " AND apply_attempt_id = :attempt"
            " AND baseline_sha256 = :baseline"
            " AND planned_sha256 = :planned"
        ),
        names={"#stage": "stage"},
        values={
            name: values[name]
            for name in (
                ":active",
                ":applied",
                ":attempt",
                ":baseline",
                ":plan",
                ":planned",
            )
        },
    )
    active = _update(
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(_ECS_ACTIVE_RECORD_ID),
        update_expression="SET #stage = :applied",
        condition_expression=(
            "#stage = :active AND scope_id = :scope"
            " AND attempt_record_id = :attempt_record"
            " AND plan_sha256 = :plan AND apply_attempt_id = :attempt"
            " AND baseline_sha256 = :baseline"
            " AND planned_sha256 = :planned"
        ),
        names={"#stage": "stage"},
        values=values,
    )
    return [attempt, active]


def _intent_and_lock_transaction_items(
    *,
    intent_id: str,
    plan_sha256: str,
    apply_attempt_id: str,
    outcome_recorded_at: str,
    verified_at_epoch: int,
) -> list[dict[str, Any]]:
    values = _ddb_item(
        {
            ":applied": "APPLIED",
            ":attempt": apply_attempt_id,
            ":consumed": "CONSUMED",
            ":intent": intent_id,
            ":locked": "LOCKED",
            ":plan": plan_sha256,
            ":recorded": outcome_recorded_at,
            ":verified": verified_at_epoch,
        }
    )
    intent = _update(
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(f"intent#{intent_id}"),
        update_expression=("SET #state = :applied, outcome_recorded_at = :recorded"),
        condition_expression=(
            "#state = :consumed AND intent_id = :intent"
            " AND apply_attempt_id = :attempt AND plan_sha256 = :plan"
        ),
        names={"#state": "state"},
        values={
            name: values[name]
            for name in (
                ":applied",
                ":attempt",
                ":consumed",
                ":intent",
                ":plan",
                ":recorded",
            )
        },
    )
    lock = _delete(
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(_LOCK_RECORD_ID),
        condition_expression=(
            "#state = :locked AND intent_id = :intent"
            " AND apply_attempt_id = :attempt AND plan_sha256 = :plan"
            " AND lease_expires_at >= :verified"
        ),
        names={"#state": "state"},
        values={
            name: values[name]
            for name in (
                ":attempt",
                ":intent",
                ":locked",
                ":plan",
                ":verified",
            )
        },
    )
    return [intent, lock]


_EVENTBRIDGE_VERIFICATION_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "baseline_sha256",
        "kind",
        "ledger_item_sha256",
        "plan_sha256",
        "planned_sha256",
        "receipt_sha256",
        "record_id",
        "rotation_epoch",
        "schema_version",
        "stage",
        "verified_at",
    }
)
_EVENTBRIDGE_APPLYING_FIELDS = frozenset(
    {
        "apply_attempt_id",
        "baseline_json",
        "baseline_sha256",
        "plan_sha256",
        "planned_json",
        "planned_sha256",
        "record_id",
        "record_type",
        "revision",
        "rotation_epoch",
        "schema_version",
        "stage",
        "started_at",
    }
)


def _validate_eventbridge_verification(
    raw: Mapping[str, Any],
    *,
    plan_sha256: str,
    apply_attempt_id: str,
) -> dict[str, Any]:
    receipt = dict(raw)
    record_id = receipt.get("record_id")
    rotation_epoch = receipt.get("rotation_epoch")
    if (
        frozenset(receipt) != _EVENTBRIDGE_VERIFICATION_FIELDS
        or receipt.get("kind") != "teamagent-eventbridge-apply-saga-receipt"
        or receipt.get("schema_version") != 2
        or receipt.get("stage") != "verified_applied"
        or type(rotation_epoch) is not str
        or not rotation_epoch
        or record_id != f"{_EVENTBRIDGE_RECORD_PREFIX}{rotation_epoch}"
        or receipt.get("plan_sha256") != plan_sha256
        or receipt.get("apply_attempt_id") != apply_attempt_id
    ):
        raise FinalizationError("EventBridge verification receipt identity differs")
    for name in ("baseline_sha256", "planned_sha256", "ledger_item_sha256"):
        _sha256(receipt.get(name), label=f"EventBridge verification {name}")
    _integer(
        receipt.get("verified_at"),
        label="EventBridge trusted verification epoch",
        minimum=1,
    )
    claimed = _sha256(
        receipt.get("receipt_sha256"),
        label="EventBridge verification receipt SHA-256",
    )
    if (
        _digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
        != claimed
    ):
        raise FinalizationError("EventBridge verification receipt digest differs")
    return receipt


def _validate_eventbridge_item(
    item: Mapping[str, Any],
    *,
    verification: Mapping[str, Any],
) -> None:
    if (
        frozenset(item) != _EVENTBRIDGE_APPLYING_FIELDS
        or _ddb_read_string(item, "record_id") != verification["record_id"]
        or _ddb_read_string(item, "record_type") != _EVENTBRIDGE_ACTIVE_RECORD_TYPE
        or _ddb_read_number(item, "schema_version") != 2
        or _ddb_read_string(item, "stage") != "applying"
        or _ddb_read_string(item, "rotation_epoch") != verification["rotation_epoch"]
        or _ddb_read_string(item, "plan_sha256") != verification["plan_sha256"]
        or _ddb_read_string(item, "apply_attempt_id") != verification["apply_attempt_id"]
        or _ddb_read_string(item, "baseline_sha256") != verification["baseline_sha256"]
        or _ddb_read_string(item, "planned_sha256") != verification["planned_sha256"]
        or _digest(item) != verification["ledger_item_sha256"]
    ):
        raise FinalizationError("EventBridge APPLYING ledger differs from its verification")
    _integer(
        _ddb_read_number(item, "revision"),
        label="EventBridge active revision",
        minimum=1,
    )


def _terminal_eventbridge_item(
    item: Mapping[str, Any],
    *,
    verified_at: int,
) -> dict[str, Any]:
    terminal = {name: dict(value) if type(value) is dict else value for name, value in item.items()}
    terminal["stage"] = _ddb_string("complete")
    terminal["revision"] = _ddb_number(_ddb_read_number(item, "revision") + 1)
    terminal["finished_at"] = _ddb_number(verified_at)
    return terminal


def _eventbridge_transaction_item(
    *,
    verification: Mapping[str, Any],
    active_item: Mapping[str, Any],
) -> dict[str, Any]:
    revision = _ddb_read_number(active_item, "revision")
    values = _ddb_item(
        {
            ":applying": "applying",
            ":attempt": str(verification["apply_attempt_id"]),
            ":baseline": str(verification["baseline_sha256"]),
            ":complete": "complete",
            ":finished": int(verification["verified_at"]),
            ":one": 1,
            ":plan": str(verification["plan_sha256"]),
            ":planned": str(verification["planned_sha256"]),
            ":revision": revision,
        }
    )
    return _update(
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(str(verification["record_id"])),
        update_expression=(
            "SET #stage = :complete, finished_at = :finished, revision = revision + :one"
        ),
        condition_expression=(
            "#stage = :applying AND revision = :revision"
            " AND plan_sha256 = :plan AND apply_attempt_id = :attempt"
            " AND baseline_sha256 = :baseline"
            " AND planned_sha256 = :planned"
        ),
        names={"#stage": "stage"},
        values=values,
    )


_DRAFT_FORBIDDEN_FIELDS = frozenset(
    {
        "deployment_finalization_receipt",
        "deployment_finalization_receipt_sha256",
        "ecs_service_saga_receipt",
        "ecs_service_saga_receipt_sha256",
        "ecs_service_saga_verification_receipt",
        "ecs_service_saga_verification_receipt_sha256",
        "eventbridge_apply_saga_verification_receipt",
        "eventbridge_apply_saga_verification_receipt_sha256",
        "provenance_outcome_receipt",
        "provenance_outcome_receipt_sha256",
    }
)


def _validate_draft(
    raw: Mapping[str, Any],
    *,
    intent_id: str,
    plan_sha256: str,
    apply_attempt_id: str,
) -> dict[str, Any]:
    draft = dict(raw)
    if (
        _DRAFT_FORBIDDEN_FIELDS & frozenset(draft)
        or draft.get("kind") != "terraform-runtime-apply-receipt-draft"
        or draft.get("schema_version") != _APPLY_RECEIPT_SCHEMA_VERSION
        or draft.get("status") != "verified_pending_finalization"
        or draft.get("provenance_outcome") != "pending"
        or draft.get("image_deployment_intent_id") != intent_id
        or draft.get("plan_sha256") != plan_sha256
        or draft.get("apply_attempt_id") != apply_attempt_id
    ):
        raise FinalizationError("apply receipt draft identity differs")
    # These are the upstream evidence bindings relied on by activation.  The
    # runtime guard performs the full semantic validation before this helper;
    # the finalizer additionally refuses malformed identities and hashes.
    _string(draft.get("guard_version"), label="guard version")
    _string(draft.get("git_commit"), label="guard Git commit")
    if re.fullmatch(r"[0-9a-f]{40}", str(draft["git_commit"])) is None:
        raise FinalizationError("guard Git commit is invalid")
    for name in (
        "source_receipt_sha256",
        "openclaw_rollout_result_sha256",
        "post_apply_service_probe_sha256",
        "post_state_contract_sha256",
        "post_live_fingerprint_sha256",
        "post_runtime_inventory_sha256",
        "shared_deployment_lock_receipt_sha256",
    ):
        _sha256(draft.get(name), label=f"apply receipt draft {name}")
    migration_contract = draft.get("migration_contract_sha256")
    reviewed_plan = draft.get("reviewed_plan_sha256")
    if not (
        (migration_contract == "" and reviewed_plan == "")
        or (
            _SHA256_RE.fullmatch(str(migration_contract)) is not None
            and _SHA256_RE.fullmatch(str(reviewed_plan)) is not None
        )
    ):
        raise FinalizationError("apply receipt draft migration binding is invalid")
    if draft.get("shared_deployment_lock_record_id") != _LOCK_RECORD_ID:
        raise FinalizationError("apply receipt draft lock binding differs")
    return draft


def _finalization_receipt(
    *,
    intent_id: str,
    plan_sha256: str,
    apply_attempt_id: str,
    applied_at_epoch: int,
    draft_sha256: str,
    eventbridge_verification: Mapping[str, Any],
    eventbridge_terminal_sha256: str,
    ecs_verification: Mapping[str, Any],
    ecs_attempt_terminal_sha256: str,
    ecs_active_terminal_sha256: str,
) -> dict[str, Any]:
    receipt = {
        "kind": _FINALIZATION_RECEIPT_KIND,
        "schema_version": _SCHEMA_VERSION,
        "record_id": _finalization_record_id(intent_id),
        "state": "APPLIED",
        "intent_id": intent_id,
        "plan_sha256": plan_sha256,
        "apply_attempt_id": apply_attempt_id,
        "apply_receipt_draft_sha256": draft_sha256,
        "eventbridge_record_id": eventbridge_verification["record_id"],
        "eventbridge_verification_receipt_sha256": eventbridge_verification["receipt_sha256"],
        "eventbridge_terminal_ledger_item_sha256": eventbridge_terminal_sha256,
        "ecs_attempt_record_id": ecs_verification["record_id"],
        "ecs_active_record_id": _ECS_ACTIVE_RECORD_ID,
        "ecs_verification_receipt_sha256": ecs_verification["receipt_sha256"],
        "ecs_attempt_terminal_ledger_item_sha256": ecs_attempt_terminal_sha256,
        "ecs_active_terminal_ledger_item_sha256": ecs_active_terminal_sha256,
        "applied_at_epoch": applied_at_epoch,
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def _build_final_receipt(
    *,
    draft: Mapping[str, Any],
    finalization_receipt: Mapping[str, Any],
    eventbridge_verification: Mapping[str, Any],
    ecs_verification: Mapping[str, Any],
    ecs_terminal_receipt: Mapping[str, Any],
    intent_id: str,
    plan_sha256: str,
    applied_at_epoch: int,
) -> tuple[dict[str, Any], bytes]:
    result = dict(draft)
    result.update(
        {
            "kind": "terraform-runtime-apply-receipt",
            "schema_version": _APPLY_RECEIPT_SCHEMA_VERSION,
            "status": "applied",
            "provenance_outcome": "applied",
            "applied_at_epoch": applied_at_epoch,
            "provenance_outcome_receipt": {
                "intent_id": intent_id,
                "plan_sha256": plan_sha256,
                "state": "APPLIED",
            },
            "ecs_service_saga_receipt": dict(ecs_terminal_receipt),
            "ecs_service_saga_verification_receipt": dict(ecs_verification),
            "eventbridge_apply_saga_verification_receipt": dict(eventbridge_verification),
            "deployment_finalization_receipt": dict(finalization_receipt),
        }
    )
    for name in (
        "provenance_outcome_receipt",
        "ecs_service_saga_receipt",
        "ecs_service_saga_verification_receipt",
        "eventbridge_apply_saga_verification_receipt",
        "deployment_finalization_receipt",
    ):
        result[f"{name}_sha256"] = _digest(result[name])
    payload = _canonical_bytes(result, newline=True)
    if len(payload) > _MAX_RECEIPT_BYTES:
        raise FinalizationError("final apply receipt exceeds its durable bound")
    return result, payload


def _manifest_and_chunks(
    *,
    payload: bytes,
    draft_sha256: str,
    finalization_receipt: Mapping[str, Any],
    eventbridge_verification: Mapping[str, Any],
    ecs_verification: Mapping[str, Any],
    intent_id: str,
    plan_sha256: str,
    apply_attempt_id: str,
    applied_at_epoch: int,
    audit_expires_at: int,
) -> tuple[dict[str, dict[str, str]], list[dict[str, dict[str, str]]]]:
    chunks = _split_chunks(payload)
    receipt_sha256 = _bytes_digest(payload)
    finalization_record_id = _finalization_record_id(intent_id)
    manifest = _ddb_item(
        {
            "record_id": finalization_record_id,
            "record_type": _FINALIZATION_KIND,
            "schema_version": _SCHEMA_VERSION,
            "state": "APPLIED",
            "intent_id": intent_id,
            "plan_sha256": plan_sha256,
            "apply_attempt_id": apply_attempt_id,
            "apply_receipt_draft_sha256": draft_sha256,
            "apply_receipt_sha256": receipt_sha256,
            "apply_receipt_size": len(payload),
            "chunk_count": len(chunks),
            "finalization_receipt_sha256": _digest(finalization_receipt),
            "eventbridge_verification_receipt_sha256": str(
                eventbridge_verification["receipt_sha256"]
            ),
            "ecs_verification_receipt_sha256": str(ecs_verification["receipt_sha256"]),
            "applied_at_epoch": applied_at_epoch,
            "audit_expires_at": audit_expires_at,
        }
    )
    chunk_items = [
        _ddb_item(
            {
                "record_id": _chunk_record_id(intent_id, index),
                "record_type": _FINALIZATION_CHUNK_KIND,
                "schema_version": _SCHEMA_VERSION,
                "finalization_record_id": finalization_record_id,
                "intent_id": intent_id,
                "plan_sha256": plan_sha256,
                "apply_attempt_id": apply_attempt_id,
                "chunk_index": index,
                "chunk_count": len(chunks),
                "payload": chunk,
                "payload_sha256": _bytes_digest(chunk),
                "audit_expires_at": audit_expires_at,
            }
        )
        for index, chunk in enumerate(chunks)
    ]
    return manifest, chunk_items


class ApplyFinalizer:
    """Crash-safe composite finalization for one exact deployment attempt."""

    def __init__(
        self,
        *,
        client: LedgerClient,
        intent_id: str,
        plan_sha256: str,
        apply_attempt_id: str,
    ) -> None:
        self.client = client
        self.intent_id = _uuid4(intent_id, label="deployment intent ID")
        self.plan_sha256 = _sha256(plan_sha256, label="saved plan SHA-256")
        self.apply_attempt_id = _uuid4(
            apply_attempt_id,
            label="apply attempt ID",
        )

    def _read_ecs_ledgers(
        self,
        verification: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        attempt = _read_required_item(
            self.client,
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(str(verification["record_id"])),
            label="ECS attempt ledger",
        )
        active = _read_required_item(
            self.client,
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(_ECS_ACTIVE_RECORD_ID),
            label="ECS active ledger",
        )
        _validate_ecs_attempt_item(attempt, verification=verification)
        _validate_ecs_active_item(active, verification=verification)
        return attempt, active

    def _read_eventbridge_ledger(
        self,
        verification: Mapping[str, Any],
    ) -> dict[str, Any]:
        item = _read_required_item(
            self.client,
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(str(verification["record_id"])),
            label="EventBridge active ledger",
        )
        _validate_eventbridge_item(item, verification=verification)
        return item

    def _confirm_terminal(
        self,
        *,
        expected_receipt_sha256: str | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        payload, receipt = _read_durable_receipt(
            self.client,
            intent_id=self.intent_id,
            plan_sha256=self.plan_sha256,
            apply_attempt_id=self.apply_attempt_id,
        )
        if (
            expected_receipt_sha256 is not None
            and _bytes_digest(payload) != expected_receipt_sha256
        ):
            raise FinalizationError("committed apply receipt differs")
        finalization = receipt.get("deployment_finalization_receipt")
        if type(finalization) is not dict:
            raise FinalizationError("deployment finalization receipt is missing")
        if (
            finalization.get("kind") != _FINALIZATION_RECEIPT_KIND
            or finalization.get("schema_version") != _SCHEMA_VERSION
            or finalization.get("record_id") != _finalization_record_id(self.intent_id)
            or finalization.get("state") != "APPLIED"
            or finalization.get("intent_id") != self.intent_id
            or finalization.get("plan_sha256") != self.plan_sha256
            or finalization.get("apply_attempt_id") != self.apply_attempt_id
        ):
            raise FinalizationError("deployment finalization receipt identity differs")
        finalization_claimed = _sha256(
            finalization.get("receipt_sha256"),
            label="deployment finalization receipt SHA-256",
        )
        if _digest(
            {key: value for key, value in finalization.items() if key != "receipt_sha256"}
        ) != finalization_claimed or receipt.get(
            "deployment_finalization_receipt_sha256"
        ) != _digest(finalization):
            raise FinalizationError("deployment finalization receipt digest differs")

        intent = _read_required_item(
            self.client,
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(f"intent#{self.intent_id}"),
            label="terminal deployment intent",
        )
        if (
            _ddb_read_string(intent, "state") != "APPLIED"
            or _ddb_read_string(intent, "intent_id") != self.intent_id
            or _ddb_read_string(intent, "plan_sha256") != self.plan_sha256
            or _ddb_read_string(intent, "apply_attempt_id") != self.apply_attempt_id
            or _ddb_read_string(intent, "outcome_recorded_at")
            != _iso8601_from_epoch(
                _integer(
                    finalization.get("applied_at_epoch"),
                    label="finalization applied epoch",
                    minimum=1,
                )
            )
        ):
            raise FinalizationError("terminal deployment intent differs")
        if (
            self.client.get_item(
                table_name=_IMAGE_LEDGER_TABLE,
                key=_image_key(_LOCK_RECORD_ID),
            )
            is not None
        ):
            raise FinalizationError("shared deployment lock was not released")

        ecs_attempt = _read_required_item(
            self.client,
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(str(finalization["ecs_attempt_record_id"])),
            label="terminal ECS attempt ledger",
        )
        ecs_active = _read_required_item(
            self.client,
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(_ECS_ACTIVE_RECORD_ID),
            label="terminal ECS active ledger",
        )
        if (
            _ddb_read_string(ecs_attempt, "stage") != "APPLIED"
            or _ddb_read_string(ecs_attempt, "plan_sha256") != self.plan_sha256
            or _ddb_read_string(ecs_attempt, "apply_attempt_id") != self.apply_attempt_id
            or _digest(ecs_attempt) != finalization.get("ecs_attempt_terminal_ledger_item_sha256")
            or _ddb_read_string(ecs_active, "stage") != "APPLIED"
            or _ddb_read_string(ecs_active, "attempt_record_id")
            != finalization["ecs_attempt_record_id"]
            or _ddb_read_string(ecs_active, "plan_sha256") != self.plan_sha256
            or _ddb_read_string(ecs_active, "apply_attempt_id") != self.apply_attempt_id
            or _digest(ecs_active) != finalization.get("ecs_active_terminal_ledger_item_sha256")
        ):
            raise FinalizationError("terminal ECS ledger differs")

        eventbridge = _read_required_item(
            self.client,
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(str(finalization["eventbridge_record_id"])),
            label="terminal EventBridge ledger",
        )
        if (
            _ddb_read_string(eventbridge, "stage") != "complete"
            or _ddb_read_string(eventbridge, "plan_sha256") != self.plan_sha256
            or _ddb_read_string(eventbridge, "apply_attempt_id") != self.apply_attempt_id
            or _digest(eventbridge) != finalization.get("eventbridge_terminal_ledger_item_sha256")
        ):
            raise FinalizationError("terminal EventBridge ledger differs")
        return payload, receipt

    def recover(self, *, output: Path) -> dict[str, Any]:
        payload, receipt = self._confirm_terminal()
        _write_atomic(output, payload)
        return {
            "ok": True,
            "state": "RECOVERED",
            "intent_id": self.intent_id,
            "plan_sha256": self.plan_sha256,
            "apply_attempt_id": self.apply_attempt_id,
            "apply_receipt_sha256": _bytes_digest(payload),
            "deployment_finalization_receipt_sha256": receipt[
                "deployment_finalization_receipt_sha256"
            ],
        }

    def commit(
        self,
        *,
        draft_raw: Mapping[str, Any],
        eventbridge_raw: Mapping[str, Any],
        ecs_raw: Mapping[str, Any],
        output: Path,
    ) -> dict[str, Any]:
        draft = _validate_draft(
            draft_raw,
            intent_id=self.intent_id,
            plan_sha256=self.plan_sha256,
            apply_attempt_id=self.apply_attempt_id,
        )
        draft_sha256 = _digest(draft)
        eventbridge = _validate_eventbridge_verification(
            eventbridge_raw,
            plan_sha256=self.plan_sha256,
            apply_attempt_id=self.apply_attempt_id,
        )
        ecs = _validate_ecs_verification(
            ecs_raw,
            plan_sha256=self.plan_sha256,
            apply_attempt_id=self.apply_attempt_id,
        )

        existing = self.client.get_item(
            table_name=_IMAGE_LEDGER_TABLE,
            key=_image_key(_finalization_record_id(self.intent_id)),
        )
        if existing is not None:
            if (
                _ddb_read_string(existing, "apply_receipt_draft_sha256") != draft_sha256
                or _ddb_read_string(
                    existing,
                    "eventbridge_verification_receipt_sha256",
                )
                != eventbridge["receipt_sha256"]
                or _ddb_read_string(existing, "ecs_verification_receipt_sha256")
                != ecs["receipt_sha256"]
            ):
                raise FinalizationError("durable finalization replay differs")
            payload, receipt = self._confirm_terminal()
            _write_atomic(output, payload)
            return {
                "ok": True,
                "state": "RECOVERED",
                "intent_id": self.intent_id,
                "plan_sha256": self.plan_sha256,
                "apply_attempt_id": self.apply_attempt_id,
                "apply_receipt_sha256": _bytes_digest(payload),
                "deployment_finalization_receipt_sha256": receipt[
                    "deployment_finalization_receipt_sha256"
                ],
            }

        _intent, lock, audit_expires_at = _validate_intent_and_lock(
            self.client,
            intent_id=self.intent_id,
            plan_sha256=self.plan_sha256,
            apply_attempt_id=self.apply_attempt_id,
        )
        applied_at_epoch = _integer(
            eventbridge["verified_at"],
            label="EventBridge trusted verification epoch",
            minimum=1,
        )
        if _ddb_read_number(lock, "lease_expires_at") < applied_at_epoch:
            raise FinalizationError("shared deployment lock expired before verification")
        ecs_attempt, ecs_active = self._read_ecs_ledgers(ecs)
        eventbridge_active = self._read_eventbridge_ledger(eventbridge)
        ecs_terminal_receipt, ecs_attempt_terminal_sha256 = _terminal_ecs_receipt(
            ecs,
            ecs_attempt,
        )
        ecs_active_terminal_sha256 = _terminal_ecs_active_digest(ecs_active)
        eventbridge_terminal = _terminal_eventbridge_item(
            eventbridge_active,
            verified_at=applied_at_epoch,
        )
        eventbridge_terminal_sha256 = _digest(eventbridge_terminal)
        finalization_receipt = _finalization_receipt(
            intent_id=self.intent_id,
            plan_sha256=self.plan_sha256,
            apply_attempt_id=self.apply_attempt_id,
            applied_at_epoch=applied_at_epoch,
            draft_sha256=draft_sha256,
            eventbridge_verification=eventbridge,
            eventbridge_terminal_sha256=eventbridge_terminal_sha256,
            ecs_verification=ecs,
            ecs_attempt_terminal_sha256=ecs_attempt_terminal_sha256,
            ecs_active_terminal_sha256=ecs_active_terminal_sha256,
        )
        _receipt, payload = _build_final_receipt(
            draft=draft,
            finalization_receipt=finalization_receipt,
            eventbridge_verification=eventbridge,
            ecs_verification=ecs,
            ecs_terminal_receipt=ecs_terminal_receipt,
            intent_id=self.intent_id,
            plan_sha256=self.plan_sha256,
            applied_at_epoch=applied_at_epoch,
        )
        manifest, chunks = _manifest_and_chunks(
            payload=payload,
            draft_sha256=draft_sha256,
            finalization_receipt=finalization_receipt,
            eventbridge_verification=eventbridge,
            ecs_verification=ecs,
            intent_id=self.intent_id,
            plan_sha256=self.plan_sha256,
            apply_attempt_id=self.apply_attempt_id,
            applied_at_epoch=applied_at_epoch,
            audit_expires_at=audit_expires_at,
        )
        items: list[dict[str, Any]] = [
            _eventbridge_transaction_item(
                verification=eventbridge,
                active_item=eventbridge_active,
            ),
            *_ecs_transaction_items(verification=ecs),
            *_intent_and_lock_transaction_items(
                intent_id=self.intent_id,
                plan_sha256=self.plan_sha256,
                apply_attempt_id=self.apply_attempt_id,
                outcome_recorded_at=_iso8601_from_epoch(applied_at_epoch),
                verified_at_epoch=applied_at_epoch,
            ),
            _put(table_name=_IMAGE_LEDGER_TABLE, item=manifest),
            *[_put(table_name=_IMAGE_LEDGER_TABLE, item=chunk) for chunk in chunks],
        ]
        if len(items) > _MAX_TRANSACTION_ITEMS:
            raise FinalizationError("deployment finalization transaction is too large")
        expected_receipt_sha256 = _bytes_digest(payload)
        try:
            self.client.transact_write(
                items=items,
                client_request_token=_client_request_token(self.apply_attempt_id),
            )
        except Exception:
            try:
                committed_payload, committed_receipt = self._confirm_terminal(
                    expected_receipt_sha256=expected_receipt_sha256,
                )
            except Exception as confirmation_exc:
                raise FinalizationError(
                    "deployment finalization transaction was not committed"
                ) from confirmation_exc
            _write_atomic(output, committed_payload)
            return {
                "ok": True,
                "state": "RECOVERED_AFTER_AMBIGUOUS_COMMIT",
                "intent_id": self.intent_id,
                "plan_sha256": self.plan_sha256,
                "apply_attempt_id": self.apply_attempt_id,
                "apply_receipt_sha256": _bytes_digest(committed_payload),
                "deployment_finalization_receipt_sha256": committed_receipt[
                    "deployment_finalization_receipt_sha256"
                ],
            }
        committed_payload, committed_receipt = self._confirm_terminal(
            expected_receipt_sha256=expected_receipt_sha256,
        )
        _write_atomic(output, committed_payload)
        return {
            "ok": True,
            "state": "COMMITTED",
            "intent_id": self.intent_id,
            "plan_sha256": self.plan_sha256,
            "apply_attempt_id": self.apply_attempt_id,
            "apply_receipt_sha256": _bytes_digest(committed_payload),
            "deployment_finalization_receipt_sha256": committed_receipt[
                "deployment_finalization_receipt_sha256"
            ],
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("commit", "recover"))
    parser.add_argument("--aws-bin", type=Path, required=True)
    parser.add_argument("--intent-id", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--apply-attempt-id")
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--eventbridge-verification", type=Path)
    parser.add_argument("--ecs-verification", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def _discover_attempt(
    client: LedgerClient,
    *,
    intent_id: str,
    plan_sha256: str,
) -> str:
    manifest = client.get_item(
        table_name=_IMAGE_LEDGER_TABLE,
        key=_image_key(_finalization_record_id(intent_id)),
    )
    if manifest is None:
        raise FinalizationNotFoundError("durable apply finalization does not exist")
    if (
        _ddb_read_string(manifest, "intent_id") != intent_id
        or _ddb_read_string(manifest, "plan_sha256") != plan_sha256
    ):
        raise FinalizationError("durable apply finalization identity differs")
    return _uuid4(
        _ddb_read_string(manifest, "apply_attempt_id"),
        label="durable apply attempt ID",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client: LedgerClient | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        intent_id = _uuid4(args.intent_id, label="deployment intent ID")
        plan_sha256 = _sha256(args.plan_sha256, label="saved plan SHA-256")
        ledger = client or SubprocessLedgerClient(args.aws_bin)
        if args.action == "commit":
            if (
                args.apply_attempt_id is None
                or args.draft is None
                or args.eventbridge_verification is None
                or args.ecs_verification is None
            ):
                raise FinalizationError("commit requires the attempt and all verification inputs")
            attempt = _uuid4(args.apply_attempt_id, label="apply attempt ID")
            finalizer = ApplyFinalizer(
                client=ledger,
                intent_id=intent_id,
                plan_sha256=plan_sha256,
                apply_attempt_id=attempt,
            )
            result = finalizer.commit(
                draft_raw=_load_object(args.draft, label="apply receipt draft"),
                eventbridge_raw=_load_object(
                    args.eventbridge_verification,
                    label="EventBridge verification receipt",
                ),
                ecs_raw=_load_object(
                    args.ecs_verification,
                    label="ECS verification receipt",
                ),
                output=args.out,
            )
        else:
            if any(
                path is not None
                for path in (
                    args.draft,
                    args.eventbridge_verification,
                    args.ecs_verification,
                )
            ):
                raise FinalizationError("recover rejects commit-only inputs")
            attempt = (
                _uuid4(args.apply_attempt_id, label="apply attempt ID")
                if args.apply_attempt_id is not None
                else _discover_attempt(
                    ledger,
                    intent_id=intent_id,
                    plan_sha256=plan_sha256,
                )
            )
            finalizer = ApplyFinalizer(
                client=ledger,
                intent_id=intent_id,
                plan_sha256=plan_sha256,
                apply_attempt_id=attempt,
            )
            result = finalizer.recover(output=args.out)
    except FinalizationNotFoundError:
        print(
            '{"code":"deployment_apply_finalization_not_found","ok":false}',
            file=sys.stderr,
        )
        return 3
    except FinalizationError:
        print(
            '{"code":"deployment_apply_finalization_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            '{"code":"deployment_apply_finalization_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
