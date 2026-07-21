"""Strict capability envelope consumed by the roleless media tool."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from teamagent.media.contracts import MAX_OUTPUT_BYTES, MediaJobRequest, S3ObjectRef

MAX_CONTROL_BYTES = 768 * 1024
MAX_COMPLETION_BYTES = 128 * 1024
_REGION = "ap-northeast-1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_POST_FIELD_LIMIT = 128 * 1024
_ALLOWED_SIGNING_FIELDS = {
    "policy",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-security-token",
    "x-amz-signature",
}


class ToolControlError(ValueError):
    """The roleless tool received an invalid or over-broad capability."""


@dataclass(frozen=True, slots=True)
class PresignedPost:
    url: str
    fields: tuple[tuple[str, str], ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class InputCapability:
    ref: S3ObjectRef
    get_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OutputCapability:
    name: str
    key: str
    max_bytes: int
    post: PresignedPost


@dataclass(frozen=True, slots=True)
class ToolControl:
    request: MediaJobRequest
    attempt_id: str
    attempt_version: int
    capability_secret: str = field(repr=False)
    inputs: tuple[InputCapability, ...]
    outputs: tuple[OutputCapability, ...]
    completion: OutputCapability


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ToolControlError("media control is not canonical JSON") from exc


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ToolControlError("media control contains a duplicate JSON key")
        value[key] = nested
    return value


def _decode_json(encoded: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            encoded,
            object_pairs_hook=_object_no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ToolControlError(f"{label} contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolControlError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ToolControlError(f"{label} must be a JSON object")
    return value


def _exact(value: Any, keys: Iterable[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ToolControlError(f"{label} schema is not exact")
    return value


def _strict_int(value: Any, *, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ToolControlError(f"{label} is outside its exact bound")
    return value


def _s3_host(bucket: str, host: str) -> bool:
    return host in {
        f"{bucket}.s3.amazonaws.com",
        f"{bucket}.s3.{_REGION}.amazonaws.com",
    }


def _validate_get_url(
    url: Any,
    ref: S3ObjectRef,
    request: MediaJobRequest,
) -> str:
    if not isinstance(url, str) or not 1 <= len(url) <= 16 * 1024 or not url.isascii():
        raise ToolControlError("input capability URL is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ToolControlError("input capability URL is malformed") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme != "https"
        or not _s3_host(ref.bucket, host)
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or unquote(parsed.path) != f"/{ref.key}"
        or not parsed.query
    ):
        raise ToolControlError("input capability is not the exact regional S3 object")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ToolControlError("input capability query is malformed") from exc
    allowed_query = {
        "versionId",
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Security-Token",
        "X-Amz-Signature",
        "x-id",
    }
    try:
        issued = dt.datetime.strptime(
            query["X-Amz-Date"][0],
            "%Y%m%dT%H%M%SZ",
        ).replace(tzinfo=dt.UTC)
        expires_s = int(query["X-Amz-Expires"][0])
    except (KeyError, ValueError) as exc:
        raise ToolControlError("input capability signing time is invalid") from exc
    if (
        set(query) - allowed_query
        or any(len(values) != 1 for values in query.values())
        or query.get("versionId") != [ref.version_id]
        or query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]
        or query.get("X-Amz-SignedHeaders") != ["host;x-amz-checksum-mode"]
        or not re.fullmatch(
            r"[^/]+/[0-9]{8}/ap-northeast-1/s3/aws4_request",
            query.get("X-Amz-Credential", [""])[0],
        )
        or not _HEX64.fullmatch(query.get("X-Amz-Signature", [""])[0])
        or not 1 <= expires_s <= 15 * 60
        or not request.created_at_epoch_s
        < int(issued.timestamp()) + expires_s
        <= request.deadline_epoch_s
        or ("x-id" in query and query["x-id"] != ["GetObject"])
    ):
        raise ToolControlError("input capability does not bind version and checksum mode")
    return url


def _post_policy(
    encoded: str,
    *,
    request: MediaJobRequest,
    key: str,
    max_bytes: int,
    fixed_fields: Mapping[str, str],
    signing_fields: Mapping[str, str],
) -> None:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ToolControlError("presigned POST policy is not strict base64") from exc
    if not 1 <= len(raw) <= _POST_FIELD_LIMIT:
        raise ToolControlError("presigned POST policy exceeds its bound")
    policy = _decode_json(raw, label="presigned POST policy")
    _exact(policy, {"expiration", "conditions"}, "presigned POST policy")
    expiration = policy["expiration"]
    if not isinstance(expiration, str):
        raise ToolControlError("presigned POST expiration is invalid")
    try:
        expiration_epoch = int(
            dt.datetime.fromisoformat(expiration.replace("Z", "+00:00")).timestamp()
        )
    except ValueError as exc:
        raise ToolControlError("presigned POST expiration is invalid") from exc
    if not request.created_at_epoch_s < expiration_epoch <= request.deadline_epoch_s:
        raise ToolControlError("presigned POST outlives the immutable job")
    conditions = policy["conditions"]
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 64:
        raise ToolControlError("presigned POST conditions are invalid")
    required_objects = {
        ("bucket", request.output_bucket),
        ("key", key),
        *fixed_fields.items(),
    }
    seen_objects: set[tuple[str, str]] = set()
    seen_starts: set[str] = set()
    seen_length = False
    for condition in conditions:
        if isinstance(condition, dict) and len(condition) == 1:
            name, value = next(iter(condition.items()))
            if not isinstance(name, str) or not isinstance(value, str):
                raise ToolControlError("presigned POST object condition is invalid")
            if (name, value) not in required_objects and (
                name,
                value,
            ) not in signing_fields.items():
                raise ToolControlError("presigned POST contains an unapproved condition")
            if (name, value) in seen_objects:
                raise ToolControlError("presigned POST contains a duplicate condition")
            seen_objects.add((name, value))
        elif isinstance(condition, list) and condition in (
            ["starts-with", "$x-amz-checksum-sha256", ""],
            ["starts-with", "$Content-Type", ""],
        ):
            if str(condition[1]) in seen_starts:
                raise ToolControlError("presigned POST contains a duplicate condition")
            seen_starts.add(str(condition[1]))
        elif condition == ["content-length-range", 1, max_bytes + 64 * 1024]:
            if seen_length:
                raise ToolControlError("presigned POST contains a duplicate condition")
            seen_length = True
        else:
            raise ToolControlError("presigned POST contains an unapproved condition")
    if (
        not required_objects.issubset(seen_objects)
        or len(conditions) != len(required_objects) + len(signing_fields) + 3
        or seen_starts != {"$x-amz-checksum-sha256", "$Content-Type"}
        or not seen_length
    ):
        raise ToolControlError("presigned POST policy is incomplete")


def _presigned_post(
    value: Any,
    *,
    request: MediaJobRequest,
    key: str,
    max_bytes: int,
    attempt_id: str,
    attempt_version: int,
    capability_secret: str,
) -> PresignedPost:
    post = _exact(value, {"url", "fields"}, "presigned POST")
    url = post["url"]
    if not isinstance(url, str) or not 1 <= len(url) <= 4096 or not url.isascii():
        raise ToolControlError("presigned POST URL is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ToolControlError("presigned POST URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or not _s3_host(request.output_bucket, (parsed.hostname or "").rstrip(".").lower())
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ToolControlError("presigned POST is not the exact regional S3 endpoint")
    raw_fields = post["fields"]
    if not isinstance(raw_fields, dict) or not 1 <= len(raw_fields) <= 32:
        raise ToolControlError("presigned POST fields are invalid")
    fields: dict[str, str] = {}
    for name, field_value in raw_fields.items():
        if (
            not isinstance(name, str)
            or not _FIELD_NAME.fullmatch(name)
            or not isinstance(field_value, str)
            or not field_value.isascii()
            or len(field_value.encode("utf-8")) > _POST_FIELD_LIMIT
            or any(character in field_value for character in ("\r", "\n", "\x00"))
        ):
            raise ToolControlError("presigned POST field is invalid")
        fields[name] = field_value
    capability_sha256 = hashlib.sha256(capability_secret.encode("ascii")).hexdigest()
    fixed_fields = {
        "x-amz-server-side-encryption": "AES256",
        "x-amz-checksum-algorithm": "SHA256",
        "x-amz-meta-job-id": request.job_id,
        "x-amz-meta-attempt-id": attempt_id,
        "x-amz-meta-attempt-version": str(attempt_version),
        "x-amz-meta-capability-sha256": capability_sha256,
    }
    required_fields = {"key": key, **fixed_fields}
    if any(
        not hmac.compare_digest(fields.get(name, ""), expected)
        for name, expected in required_fields.items()
    ):
        raise ToolControlError("presigned POST fields do not bind the exact attempt")
    if "Content-Type" in fields or "x-amz-checksum-sha256" in fields:
        raise ToolControlError("presigned POST dynamic integrity fields must be tool-owned")
    allowed_fields = set(required_fields) | _ALLOWED_SIGNING_FIELDS
    required_signing = {
        "policy",
        "x-amz-algorithm",
        "x-amz-credential",
        "x-amz-date",
        "x-amz-signature",
    }
    if (
        set(fields) - allowed_fields
        or not required_signing.issubset(fields)
        or fields["x-amz-algorithm"] != "AWS4-HMAC-SHA256"
        or not re.fullmatch(
            r"[^/]+/[0-9]{8}/ap-northeast-1/s3/aws4_request",
            fields["x-amz-credential"],
        )
        or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", fields["x-amz-date"])
        or not _HEX64.fullmatch(fields["x-amz-signature"])
    ):
        raise ToolControlError("presigned POST fields exceed the exact contract")
    signing_fields = {
        name: value
        for name, value in fields.items()
        if name
        in {
            "x-amz-algorithm",
            "x-amz-credential",
            "x-amz-date",
            "x-amz-security-token",
        }
    }
    _post_policy(
        fields["policy"],
        request=request,
        key=key,
        max_bytes=max_bytes,
        fixed_fields=fixed_fields,
        signing_fields=signing_fields,
    )
    return PresignedPost(url=url, fields=tuple(sorted(fields.items())))


def _input_refs(request: MediaJobRequest) -> tuple[S3ObjectRef, ...]:
    operation = request.operation.model_dump(mode="json")
    raw: list[dict[str, Any]] = []
    for name in ("source", "html", "template", "composer_json"):
        value = operation.get(name)
        if isinstance(value, dict):
            raw.append(value)
    evidence = operation.get("evidence", [])
    if isinstance(evidence, list):
        raw.extend(
            item["source"]
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("source"), dict)
        )
    refs = tuple(S3ObjectRef.model_validate(value) for value in raw)
    for ref in refs:
        if ref.bucket != request.output_bucket or not ref.key.startswith(
            f"{request.output_prefix}input/"
        ):
            raise ToolControlError("input reference escapes the exact job scope")
    if len(refs) != len({(ref.bucket, ref.key, ref.version_id) for ref in refs}):
        raise ToolControlError("input reference set contains duplicates")
    return refs


def _output_slots(
    request: MediaJobRequest, attempt_id: str, attempt_version: int
) -> tuple[tuple[str, str, int], ...]:
    operation = request.operation.model_dump(mode="json")
    kind = operation["kind"]
    prefix = f"{request.output_prefix}attempts/{attempt_version}/{attempt_id}/output/"
    slots: list[tuple[str, str, int]] = []
    if kind == "acquire":
        slots.append(("media", "media", int(operation["max_bytes"])))
    elif kind == "tiktok_acquire":
        slots.append(("posts.json", "posts.normalized.json", 16 * 1024 * 1024))
        if operation["artifact_mode"] == "full":
            slots.extend(
                (
                    ("config.json", "config.json", 256 * 1024),
                    ("manifest.json", "videos/manifest.json", 16 * 1024 * 1024),
                )
            )
            for keyword_index in range(len(operation["keywords"])):
                for rank in range(1, int(operation["n_per_kw"]) + 1):
                    pid = f"p{keyword_index + 1:02d}{rank:03d}"
                    slots.append((f"thumb-{pid}", f"thumbs/{pid}.jpg", 8 * 1024 * 1024))
                    slots.append(
                        (
                            f"video-{pid}",
                            f"videos/{pid}.mp4",
                            int(operation["max_video_bytes"]),
                        )
                    )
    elif kind == "proxy":
        slots.append(("proxy", "proxy", int(operation["limit_bytes"])))
    elif kind == "frame":
        for index in range(len(operation["timecodes"])):
            slots.append((f"frame-{index:02d}", f"frame-{index:02d}.jpg", 8 * 1024 * 1024))
        slots.append(("frames.json", "frames.json", 1024 * 1024))
    elif kind == "thumbnail":
        slots.extend(
            (
                ("thumbnail", "thumbnail.jpg", 8 * 1024 * 1024),
                ("thumbnail.json", "thumbnail.json", 256 * 1024),
            )
        )
    elif kind == "slides":
        slots.append(("slides.pptx", "slides.pptx", MAX_OUTPUT_BYTES))
    elif kind == "proposal_pptx":
        slots.append(("proposal.pptx", "proposal.pptx", MAX_OUTPUT_BYTES))
    elif kind == "pdf":
        slots.append(("document.pdf", "document.pdf", MAX_OUTPUT_BYTES))
    else:
        raise ToolControlError("media operation has no output contract")
    return tuple((name, f"{prefix}{relative}", maximum) for name, relative, maximum in slots)


def _parse_control(encoded: bytes) -> ToolControl:
    value = _decode_json(encoded, label="media control")
    if _canonical(value) != encoded:
        raise ToolControlError("media control JSON is not canonical")
    control = _exact(
        value,
        {
            "schema_version",
            "request",
            "attempt_id",
            "attempt_version",
            "capability_secret",
            "inputs",
            "outputs",
            "completion",
        },
        "media control",
    )
    if control["schema_version"] != "1":
        raise ToolControlError("media control schema version is unsupported")
    try:
        request = MediaJobRequest.model_validate(control["request"])
    except Exception as exc:
        raise ToolControlError("media control request is invalid") from exc
    attempt_id = control["attempt_id"]
    capability_secret = control["capability_secret"]
    if not isinstance(attempt_id, str) or not _UUID4.fullmatch(attempt_id):
        raise ToolControlError("media attempt id is invalid")
    attempt_version = _strict_int(
        control["attempt_version"],
        minimum=1,
        maximum=2**31 - 1,
        label="media attempt version",
    )
    if not isinstance(capability_secret, str) or not _HEX64.fullmatch(capability_secret):
        raise ToolControlError("media capability secret is invalid")
    expected_refs = _input_refs(request)
    raw_inputs = control["inputs"]
    if not isinstance(raw_inputs, list) or len(raw_inputs) != len(expected_refs):
        raise ToolControlError("media input capabilities are not exact")
    inputs: list[InputCapability] = []
    for raw, expected_ref in zip(raw_inputs, expected_refs, strict=True):
        item = _exact(raw, {"ref", "get_url"}, "input capability")
        try:
            ref = S3ObjectRef.model_validate(item["ref"])
        except Exception as exc:
            raise ToolControlError("input capability reference is invalid") from exc
        if ref != expected_ref:
            raise ToolControlError("input capability does not match the operation")
        inputs.append(
            InputCapability(
                ref=ref,
                get_url=_validate_get_url(item["get_url"], ref, request),
            )
        )
    expected_slots = _output_slots(request, attempt_id, attempt_version)
    raw_outputs = control["outputs"]
    if not isinstance(raw_outputs, list) or len(raw_outputs) != len(expected_slots):
        raise ToolControlError("media output capabilities are not exact")
    outputs: list[OutputCapability] = []
    for raw, (name, key, maximum) in zip(raw_outputs, expected_slots, strict=True):
        item = _exact(raw, {"name", "key", "max_bytes", "post"}, "output capability")
        if (
            item["name"] != name
            or item["key"] != key
            or item["max_bytes"] != maximum
            or type(item["max_bytes"]) is not int
        ):
            raise ToolControlError("media output capability differs from its exact slot")
        outputs.append(
            OutputCapability(
                name=name,
                key=key,
                max_bytes=maximum,
                post=_presigned_post(
                    item["post"],
                    request=request,
                    key=key,
                    max_bytes=maximum,
                    attempt_id=attempt_id,
                    attempt_version=attempt_version,
                    capability_secret=capability_secret,
                ),
            )
        )
    completion_key = (
        f"{request.output_prefix}attempts/{attempt_version}/{attempt_id}/_COMPLETION.json"
    )
    completion_value = _exact(
        control["completion"],
        {"key", "max_bytes", "post"},
        "completion capability",
    )
    if (
        completion_value["key"] != completion_key
        or completion_value["max_bytes"] != MAX_COMPLETION_BYTES
        or type(completion_value["max_bytes"]) is not int
    ):
        raise ToolControlError("completion capability differs from its exact slot")
    completion = OutputCapability(
        name="_completion",
        key=completion_key,
        max_bytes=MAX_COMPLETION_BYTES,
        post=_presigned_post(
            completion_value["post"],
            request=request,
            key=completion_key,
            max_bytes=MAX_COMPLETION_BYTES,
            attempt_id=attempt_id,
            attempt_version=attempt_version,
            capability_secret=capability_secret,
        ),
    )
    return ToolControl(
        request=request,
        attempt_id=attempt_id,
        attempt_version=attempt_version,
        capability_secret=capability_secret,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        completion=completion,
    )


def parse_control_from_env(environ: Mapping[str, str]) -> ToolControl:
    packed = environ.get("MEDIA_CONTROL_ZLIB_B64")
    expected_sha256 = environ.get("MEDIA_CONTROL_SHA256")
    if (
        not isinstance(packed, str)
        or not 1 <= len(packed) <= MAX_CONTROL_BYTES
        or any(character.isspace() for character in packed)
        or not isinstance(expected_sha256, str)
        or not _HEX64.fullmatch(expected_sha256)
    ):
        raise ToolControlError("media control environment is invalid")
    try:
        compressed = base64.b64decode(packed, validate=True)
    except (ValueError, TypeError) as exc:
        raise ToolControlError("media control environment is not strict base64") from exc
    decompressor = zlib.decompressobj()
    try:
        encoded = decompressor.decompress(compressed, MAX_CONTROL_BYTES + 1)
        if len(encoded) <= MAX_CONTROL_BYTES:
            encoded += decompressor.flush(MAX_CONTROL_BYTES + 1 - len(encoded))
    except zlib.error as exc:
        raise ToolControlError("media control compression is invalid") from exc
    if (
        not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or not 1 <= len(encoded) <= MAX_CONTROL_BYTES
        or not hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), expected_sha256)
    ):
        raise ToolControlError("media control compression or digest is invalid")
    return _parse_control(encoded)


__all__ = [
    "MAX_COMPLETION_BYTES",
    "MAX_CONTROL_BYTES",
    "InputCapability",
    "OutputCapability",
    "PresignedPost",
    "ToolControl",
    "ToolControlError",
    "parse_control_from_env",
]
