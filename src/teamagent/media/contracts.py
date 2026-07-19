"""Core/media worker 間の署名可能な厳格 JSON 契約。

大きな入力は S3 参照だけを許し、SQS body は 128 KiB 以下に制限する。各 S3 参照は
サイズと SHA-256 を必須にして、worker が取得後に再検証する。``payload_sha256`` は
それ自身を除いた canonical JSON の digest であり、SQS/Lambda/ECS のどの境界でも
改変を検出できる。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from teamagent.media.url_policy import acquire_host_allowed

MAX_JOB_BODY_BYTES = 128 * 1024
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_JOB_BUDGET_SECONDS = 15 * 60
MAX_DEADLINE_SECONDS = MAX_JOB_BUDGET_SECONDS
ARTIFACT_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_PRESIGNED_URL_SECONDS = 7 * 24 * 60 * 60
DDB_RETENTION_GRACE_SECONDS = 24 * 60 * 60

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
JobId = Annotated[str, StringConstraints(pattern=r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$")]
S3Bucket = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$",
    ),
]
S3Key = Annotated[str, StringConstraints(min_length=1, max_length=1024)]

_SAFE_KEY = re.compile(r"^[A-Za-z0-9!_.*'()/+=:@-]+$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class S3ObjectRef(_StrictModel):
    """暗号化済み S3 object の content-addressed 参照。"""

    bucket: S3Bucket
    key: S3Key
    sha256: Sha256
    size: int = Field(ge=0, le=MAX_INPUT_BYTES)
    content_type: str = Field(min_length=1, max_length=128)

    @field_validator("key")
    @classmethod
    def _safe_key(cls, value: str) -> str:
        if not _SAFE_KEY.fullmatch(value) or "\\" in value:
            raise ValueError("unsafe S3 key")
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("S3 key must not contain empty or traversal segments")
        return value


class AcquireOperation(_StrictModel):
    kind: Literal["acquire"]
    url: str = Field(min_length=8, max_length=2048)
    max_bytes: int = Field(default=30 * 1024 * 1024, ge=1, le=MAX_OUTPUT_BYTES)

    @field_validator("url")
    @classmethod
    def _allowed_site(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("acquire URL must be canonical HTTPS")
        if not acquire_host_allowed(host):
            raise ValueError("acquire URL host is not allowlisted")
        if parsed.fragment:
            raise ValueError("acquire URL fragments are not allowed")
        return value


class TikTokClientConfig(_StrictModel):
    client: str | None = Field(default=None, max_length=200)
    client_short: str | None = Field(default=None, max_length=200)
    competitors: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    industry: str | None = Field(default=None, max_length=200)


class TikTokAcquireOperation(_StrictModel):
    kind: Literal["tiktok_acquire"]
    search_type: Literal["keyword", "hashtag"] = "keyword"
    keywords: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=100)],
        ...,
    ] = Field(min_length=1, max_length=10)
    n_per_kw: int = Field(default=30, ge=1, le=30)
    videos_per_kw: int = Field(default=6, ge=0, le=10)
    sort: Literal["display", "save_rate", "recent"] = "display"
    max_video_bytes: int = Field(default=30 * 1024 * 1024, ge=1, le=MAX_OUTPUT_BYTES)
    client: TikTokClientConfig = Field(default_factory=TikTokClientConfig)

    @field_validator("keywords")
    @classmethod
    def _unique_keywords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(keyword.strip() for keyword in value)
        if any(not keyword for keyword in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("keywords must be non-empty and unique")
        return normalized


class ProxyOperation(_StrictModel):
    kind: Literal["proxy"]
    source: S3ObjectRef
    limit_bytes: int = Field(default=18 * 1024 * 1024, ge=1024, le=MAX_OUTPUT_BYTES)
    long_edge: int = Field(default=1280, ge=240, le=2160)
    preview: bool = False


class FrameOperation(_StrictModel):
    kind: Literal["frame"]
    source: S3ObjectRef
    timecodes: tuple[float, ...] = Field(min_length=1, max_length=12)
    width: int = Field(default=320, ge=64, le=1920)

    @field_validator("timecodes")
    @classmethod
    def _valid_timecodes(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(sec < 0 or sec > 6 * 60 * 60 for sec in value):
            raise ValueError("timecode is out of range")
        if tuple(sorted(set(value))) != value:
            raise ValueError("timecodes must be unique and sorted")
        return value


class ThumbnailOperation(_StrictModel):
    kind: Literal["thumbnail"]
    source: S3ObjectRef | None = None
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    width: int = Field(default=480, ge=64, le=1920)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ThumbnailOperation:
        if (self.source is None) == (self.url is None):
            raise ValueError("thumbnail requires exactly one of source or url")
        if self.url is not None:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
            ):
                raise ValueError("thumbnail URL must be canonical HTTPS")
        return self


class SlidesOperation(_StrictModel):
    kind: Literal["slides"]
    html: S3ObjectRef
    selector: str = Field(default=".slide", min_length=1, max_length=80)
    width: int = Field(default=1280, ge=320, le=1920)
    height: int = Field(default=720, ge=180, le=1080)
    device_scale_factor: int = Field(default=2, ge=1, le=2)

    @field_validator("selector")
    @classmethod
    def _safe_selector(cls, value: str) -> str:
        if not re.fullmatch(r"[.#][A-Za-z][A-Za-z0-9_-]{0,78}", value):
            raise ValueError("only a simple class/id selector is allowed")
        return value


class ProposalEvidence(_StrictModel):
    placeholder_id: int = Field(ge=1, le=103)
    rank: int = Field(ge=1, le=20)
    source: S3ObjectRef


class ProposalPptxOperation(_StrictModel):
    kind: Literal["proposal_pptx"]
    template: S3ObjectRef
    composer_json: S3ObjectRef
    evidence: tuple[ProposalEvidence, ...] = Field(default_factory=tuple, max_length=20)
    fail_if_missing: bool = True


class PdfOperation(_StrictModel):
    kind: Literal["pdf"]
    html: S3ObjectRef


MediaOperation = Annotated[
    AcquireOperation
    | TikTokAcquireOperation
    | ProxyOperation
    | FrameOperation
    | ThumbnailOperation
    | SlidesOperation
    | ProposalPptxOperation
    | PdfOperation,
    Field(discriminator="kind"),
]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def semantic_request_sha256(
    operation: MediaOperation,
    request_fingerprint: str,
) -> str:
    """Hash retry-stable intent, excluding timestamps and the delivery envelope."""

    return hashlib.sha256(
        _canonical_json(
            {
                "schema_version": "1",
                "operation": operation.model_dump(mode="json"),
                "request_fingerprint": request_fingerprint,
            }
        )
    ).hexdigest()


class MediaJobRequest(_StrictModel):
    """SQS に載せる media job request。"""

    schema_version: Literal["1"] = "1"
    job_id: JobId
    idempotency_key: Sha256
    created_at_epoch_s: int = Field(ge=1)
    deadline_epoch_s: int = Field(ge=1)
    artifact_ttl_s: int = Field(
        default=ARTIFACT_RETENTION_SECONDS,
        ge=300,
        le=ARTIFACT_RETENTION_SECONDS,
    )
    audit_principal_hash: Sha256 | None = None
    output_bucket: S3Bucket
    output_prefix: S3Key
    operation: MediaOperation
    payload_sha256: Sha256

    @field_validator("output_prefix")
    @classmethod
    def _prefix_shape(cls, value: str) -> str:
        if not value.startswith("media-jobs/") or not value.endswith("/"):
            raise ValueError("output_prefix must use the media-jobs namespace and end with /")
        if not _SAFE_KEY.fullmatch(value):
            raise ValueError("unsafe output_prefix")
        return value

    @model_validator(mode="after")
    def _validate_envelope(self) -> MediaJobRequest:
        delta = self.deadline_epoch_s - self.created_at_epoch_s
        if delta < 1 or delta > MAX_DEADLINE_SECONDS:
            raise ValueError("deadline must be within the bounded execution window")
        if f"/{self.job_id}/" not in f"/{self.output_prefix}":
            raise ValueError("output_prefix must be scoped to job_id")
        expected = self.compute_payload_sha256()
        if self.payload_sha256 != expected:
            raise ValueError("payload_sha256 mismatch")
        if len(self.to_json_bytes_unchecked()) > MAX_JOB_BODY_BYTES:
            raise ValueError("job payload exceeds bounded SQS body size")
        return self

    def compute_payload_sha256(self) -> str:
        raw = self.model_dump(mode="json", exclude={"payload_sha256"})
        return hashlib.sha256(_canonical_json(raw)).hexdigest()

    def to_json_bytes_unchecked(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json"))

    def to_json_bytes(self) -> bytes:
        body = self.to_json_bytes_unchecked()
        if len(body) > MAX_JOB_BODY_BYTES:
            raise ValueError("job payload exceeds bounded SQS body size")
        return body

    def assert_not_expired(self, *, now_epoch_s: int | None = None) -> None:
        now = int(time.time()) if now_epoch_s is None else now_epoch_s
        if now >= self.deadline_epoch_s:
            raise ValueError("job deadline exceeded")


class MediaArtifact(_StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    object: S3ObjectRef


class MediaJobResult(_StrictModel):
    schema_version: Literal["1"] = "1"
    job_id: JobId
    status: Literal["queued", "running", "done", "failed"]
    artifacts: tuple[MediaArtifact, ...] = Field(default_factory=tuple, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(
        default=None,
        max_length=80,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )

    @model_validator(mode="after")
    def _status_shape(self) -> MediaJobResult:
        if self.status == "done" and not self.artifacts:
            raise ValueError("done result requires artifacts")
        names = [artifact.name for artifact in self.artifacts]
        keys = [(artifact.object.bucket, artifact.object.key) for artifact in self.artifacts]
        if len(names) != len(set(names)) or len(keys) != len(set(keys)):
            raise ValueError("artifact names and object keys must be unique")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed result requires error_code")
        if self.status != "failed" and self.error_code is not None:
            raise ValueError("error_code is only valid for failed results")
        if len(_canonical_json(self.metadata)) > 32 * 1024:
            raise ValueError("result metadata exceeds limit")
        return self


def artifact_manifest_sha256(artifacts: tuple[MediaArtifact, ...]) -> str:
    """Digest the complete content-addressed artifact manifest.

    The independent DynamoDB field produced from this representation prevents a
    partially updated ``detail`` document from silently changing an artifact
    key, size, or digest.
    """

    rows = sorted(
        (
            {
                "name": artifact.name,
                "bucket": artifact.object.bucket,
                "key": artifact.object.key,
                "sha256": artifact.object.sha256,
                "size": artifact.object.size,
                "content_type": artifact.object.content_type,
            }
            for artifact in artifacts
        ),
        key=lambda row: (row["name"], row["key"]),
    )
    return hashlib.sha256(_canonical_json(rows)).hexdigest()


def make_job_request(
    *,
    operation: MediaOperation,
    output_bucket: str,
    request_fingerprint: str,
    now_epoch_s: int | None = None,
    timeout_s: int = MAX_DEADLINE_SECONDS,
    deadline_epoch_s: int | None = None,
    artifact_ttl_s: int = ARTIFACT_RETENTION_SECONDS,
    job_id: str | None = None,
    audit_principal_hash: str | None = None,
    output_prefix: str | None = None,
) -> MediaJobRequest:
    """canonical operation + caller fingerprint から冪等 request を作る。"""

    if timeout_s < 1 or timeout_s > MAX_DEADLINE_SECONDS:
        raise ValueError("timeout_s is out of range")
    now = int(time.time()) if now_epoch_s is None else now_epoch_s
    resolved_deadline = now + timeout_s if deadline_epoch_s is None else deadline_epoch_s
    if resolved_deadline <= now or resolved_deadline - now > MAX_DEADLINE_SECONDS:
        raise ValueError("deadline_epoch_s is outside the bounded execution window")
    idempotency = semantic_request_sha256(operation, request_fingerprint)
    resolved_job_id = job_id or f"mj_{idempotency[:24]}"
    resolved_prefix = output_prefix or f"media-jobs/{resolved_job_id}/"
    payload_without_hash: dict[str, Any] = {
        "schema_version": "1",
        "job_id": resolved_job_id,
        "idempotency_key": idempotency,
        "created_at_epoch_s": now,
        "deadline_epoch_s": resolved_deadline,
        "artifact_ttl_s": artifact_ttl_s,
        "audit_principal_hash": audit_principal_hash,
        "output_bucket": output_bucket,
        "output_prefix": resolved_prefix,
        "operation": operation.model_dump(mode="json"),
    }
    raw = {
        **payload_without_hash,
        "operation": operation,
        "payload_sha256": hashlib.sha256(_canonical_json(payload_without_hash)).hexdigest(),
    }
    return MediaJobRequest.model_validate(raw)


def parse_job_request(body: bytes | str) -> MediaJobRequest:
    """上限確認後に strict model + payload hash を検証する。"""

    encoded = body.encode("utf-8") if isinstance(body, str) else body
    if len(encoded) > MAX_JOB_BODY_BYTES:
        raise ValueError("job payload exceeds bounded SQS body size")
    return MediaJobRequest.model_validate_json(encoded)


__all__ = [
    "ARTIFACT_RETENTION_SECONDS",
    "DDB_RETENTION_GRACE_SECONDS",
    "MAX_DEADLINE_SECONDS",
    "MAX_INPUT_BYTES",
    "MAX_JOB_BODY_BYTES",
    "MAX_JOB_BUDGET_SECONDS",
    "MAX_OUTPUT_BYTES",
    "MAX_PRESIGNED_URL_SECONDS",
    "AcquireOperation",
    "FrameOperation",
    "MediaArtifact",
    "MediaJobRequest",
    "MediaJobResult",
    "MediaOperation",
    "PdfOperation",
    "ProposalEvidence",
    "ProposalPptxOperation",
    "ProxyOperation",
    "S3ObjectRef",
    "SlidesOperation",
    "ThumbnailOperation",
    "TikTokAcquireOperation",
    "TikTokClientConfig",
    "artifact_manifest_sha256",
    "make_job_request",
    "parse_job_request",
    "semantic_request_sha256",
]
