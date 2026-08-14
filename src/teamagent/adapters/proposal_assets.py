"""Provision pinned proposal-builder assets into the task-local filesystem.

The integrated PowerPoint template is intentionally too large for the runtime
image, and the account database must not be stored in the repository.  Both are
therefore supplied as immutable, integrity-pinned S3 object versions at startup.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import posixpath
import re
import stat
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

_ASSET_DIRECTORY = Path("/tmp/teamagent/proposal-builder")  # nosec B108
_TEMPLATE_PATH = _ASSET_DIRECTORY / "integrated_fmt.pptx"
_ACCOUNT_DB_PATH = _ASSET_DIRECTORY / "account_db.json"

_TEMPLATE_MAX_BYTES = 256 * 1024 * 1024
_ACCOUNT_DB_MAX_BYTES = 5 * 1024 * 1024
_STREAM_CHUNK_BYTES = 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_KMS_KEY_ARN_PATTERN = re.compile(r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[0-9a-fA-F-]{36}")
_NUMERIC_PLACEHOLDER = re.compile(r"[｛{]\s*(\d+)\s*[:：]?[^｝}]*[｝}]")
_DATE_PLACEHOLDER = re.compile(r"\{\{PB-DATE:([+-]?\d{1,3}):(?:%Y/%m/%d|%m/%d|%Y年%m月%d日)\}\}")
_TEMPLATE_VERSION_PLACEHOLDER = re.compile(r"\{\{PB-TEMPLATE:([a-z0-9-]{1,40})\}\}")
_AUXILIARY_PLACEHOLDER = re.compile(r"\{\{(PB-[A-Z0-9_-]{1,60})\}\}")
_LEGACY_INSTRUCTION = re.compile(r"自動入力|貼り付けてください|はめ込|転記|差し替え")
_BRACE_CHARACTER = re.compile(r"[{}｛｝]")
_VALID_NUMERIC_IDS = frozenset(range(1, 104)) - frozenset(range(48, 56))
_REQUIRED_AUXILIARY = frozenset(
    {
        "PB-ACCOUNTS",
        "PB-CASES",
        "PB-CLIENT-NAME",
        "PB-DATETIME",
        "PB-EXPERIENCE",
        "PB-KEY-MESSAGE",
        "PB-MONTH",
        "PB-PRODUCT-NAME",
    }
)
_EXPECTED_SLIDE_COUNT = 83
_REQUIRED_DATE_OFFSETS = frozenset(range(-56, 22, 7))
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_SLIDE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"


class ProposalAssetProvisionError(RuntimeError):
    """Raised when a proposal-builder asset cannot be provisioned safely."""


@dataclass(frozen=True)
class ProvisionedProposalAssets:
    """Task-local paths published after both assets pass integrity checks."""

    template_path: Path
    account_db_path: Path


@dataclass(frozen=True)
class _AssetSpec:
    label: str
    bucket: str
    key: str
    version_id: str
    sha256: str
    kms_key_arn: str
    size: int
    maximum_size: int
    destination: Path
    content_validator: Callable[[Path], None]


def provision_proposal_builder_assets(
    environment: Mapping[str, str],
) -> ProvisionedProposalAssets:
    """Download and validate the configured immutable proposal-builder assets.

    Configuration is validated in full before the S3 client is created.  boto3
    is imported lazily so the disabled proposal-builder path keeps the existing
    startup behavior.
    """

    template = _asset_spec(
        environment,
        prefix="PROPOSAL_BUILDER_TEMPLATE",
        label="template",
        maximum_size=_TEMPLATE_MAX_BYTES,
        destination=_TEMPLATE_PATH,
        content_validator=_validate_template,
    )
    account_db = _asset_spec(
        environment,
        prefix="PROPOSAL_BUILDER_ACCOUNT",
        label="account database",
        maximum_size=_ACCOUNT_DB_MAX_BYTES,
        destination=_ACCOUNT_DB_PATH,
        content_validator=_validate_account_db,
    )

    _prepare_asset_directory()
    s3 = _new_s3_client()
    _provision_asset(s3, template)
    _provision_asset(s3, account_db)

    return ProvisionedProposalAssets(
        template_path=template.destination,
        account_db_path=account_db.destination,
    )


def _asset_spec(
    environment: Mapping[str, str],
    *,
    prefix: str,
    label: str,
    maximum_size: int,
    destination: Path,
    content_validator: Callable[[Path], None],
) -> _AssetSpec:
    bucket = _required_environment_value(environment, f"{prefix}_S3_BUCKET")
    key = _required_environment_value(environment, f"{prefix}_S3_KEY")
    version_id = _required_environment_value(environment, f"{prefix}_S3_VERSION_ID")
    if version_id.strip().lower() in {"null", "none"}:
        raise ProposalAssetProvisionError(f"{prefix}_S3_VERSION_ID must be a concrete version")

    sha256 = _required_environment_value(environment, f"{prefix}_S3_SHA256").strip().lower()
    if _SHA256_PATTERN.fullmatch(sha256) is None:
        raise ProposalAssetProvisionError(f"{prefix}_S3_SHA256 must be a SHA-256 hex digest")

    raw_size = _required_environment_value(environment, f"{prefix}_S3_SIZE").strip()
    try:
        size = int(raw_size, 10)
    except ValueError:
        raise ProposalAssetProvisionError(f"{prefix}_S3_SIZE must be an integer") from None
    if size < 1 or size > maximum_size:
        raise ProposalAssetProvisionError(f"{prefix}_S3_SIZE is outside the allowed range")
    kms_key_arn = _required_environment_value(
        environment, "PROPOSAL_BUILDER_ASSETS_KMS_KEY_ARN"
    ).strip()
    if _KMS_KEY_ARN_PATTERN.fullmatch(kms_key_arn) is None:
        raise ProposalAssetProvisionError(
            "PROPOSAL_BUILDER_ASSETS_KMS_KEY_ARN must be a concrete KMS key ARN"
        )

    return _AssetSpec(
        label=label,
        bucket=bucket,
        key=key,
        version_id=version_id,
        sha256=sha256,
        kms_key_arn=kms_key_arn,
        size=size,
        maximum_size=maximum_size,
        destination=destination,
        content_validator=content_validator,
    )


def _required_environment_value(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise ProposalAssetProvisionError(f"{name} is required")
    return value


def _new_s3_client() -> Any:
    try:
        import boto3

        return boto3.client("s3")
    except Exception:
        raise ProposalAssetProvisionError("S3 client could not be created") from None


def _prepare_asset_directory() -> None:
    try:
        _ASSET_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_stat = _ASSET_DIRECTORY.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ProposalAssetProvisionError("proposal asset directory is not a directory")
        os.chmod(_ASSET_DIRECTORY, 0o700)
    except ProposalAssetProvisionError:
        raise
    except OSError:
        raise ProposalAssetProvisionError(
            "proposal asset directory could not be prepared"
        ) from None


def _provision_asset(s3: Any, spec: _AssetSpec) -> None:
    head = _s3_head(s3, spec)
    _assert_exact_s3_response(head, spec)

    response = _s3_get(s3, spec)
    _assert_exact_s3_response(response, spec)
    _stream_and_publish(response, spec)


def _s3_head(s3: Any, spec: _AssetSpec) -> Mapping[str, Any]:
    try:
        response = s3.head_object(
            Bucket=spec.bucket,
            Key=spec.key,
            VersionId=spec.version_id,
            ChecksumMode="ENABLED",
        )
    except Exception:
        raise ProposalAssetProvisionError(f"{spec.label} S3 head failed") from None
    if not isinstance(response, Mapping):
        raise ProposalAssetProvisionError(f"{spec.label} S3 head response is invalid")
    return response


def _s3_get(s3: Any, spec: _AssetSpec) -> Mapping[str, Any]:
    try:
        response = s3.get_object(
            Bucket=spec.bucket,
            Key=spec.key,
            VersionId=spec.version_id,
            ChecksumMode="ENABLED",
        )
    except Exception:
        raise ProposalAssetProvisionError(f"{spec.label} S3 download failed") from None
    if not isinstance(response, Mapping):
        raise ProposalAssetProvisionError(f"{spec.label} S3 response is invalid")
    return response


_COMPOSITE_CHECKSUM = re.compile(r"^[A-Za-z0-9+/]+={0,2}-[0-9]+$")


def _checksum_header_matches(returned: str, spec: _AssetSpec) -> bool:
    """Accept a whole-object SHA-256, or a composite one for multipart uploads.

    マルチパートで上がったオブジェクトの ``ChecksumSHA256`` は各パートの
    ダイジェストを畳んだ合成値 (``<base64>-<パート数>``) であり、全体の
    SHA-256 とは原理的に一致しない。統合 FMT は 143MB あり CLI 既定手順
    (8MB 超で自動マルチパート) では必ず合成値になるため、完全一致のみを
    要求する検査は設計時から不通過だった。本文の完全性は
    ``_stream_and_publish`` が全バイトの SHA-256 を ``spec.sha256`` と
    照合して担保するので、ここでは形式が合成値でないときのみ完全一致を要求する。
    """

    if not returned:
        return False
    if _COMPOSITE_CHECKSUM.fullmatch(returned):
        return True
    expected = base64.b64encode(bytes.fromhex(spec.sha256)).decode("ascii")
    return hmac.compare_digest(returned, expected)


def _assert_exact_s3_response(response: Mapping[str, Any], spec: _AssetSpec) -> None:
    returned_checksum = str(response.get("ChecksumSHA256") or "")
    if (
        response.get("VersionId") != spec.version_id
        or response.get("ContentLength") != spec.size
        or response.get("ServerSideEncryption") != "aws:kms"
        or response.get("SSEKMSKeyId") != spec.kms_key_arn
        or not _checksum_header_matches(returned_checksum, spec)
    ):
        raise ProposalAssetProvisionError(f"{spec.label} S3 metadata did not match its pin")


def _stream_and_publish(response: Mapping[str, Any], spec: _AssetSpec) -> None:
    body = response.get("Body")
    if body is None or not callable(getattr(body, "read", None)):
        raise ProposalAssetProvisionError(f"{spec.label} S3 body is invalid")

    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{spec.destination.name}.",
            suffix=".tmp",
            dir=_ASSET_DIRECTORY,
        )
        os.fchmod(descriptor, 0o600)
        digest = hashlib.sha256()
        received = 0

        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            while True:
                read_limit = min(_STREAM_CHUNK_BYTES, spec.size - received + 1)
                try:
                    chunk = body.read(read_limit)
                except Exception:
                    raise ProposalAssetProvisionError(
                        f"{spec.label} S3 body could not be read"
                    ) from None
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ProposalAssetProvisionError(f"{spec.label} S3 body is not binary")
                received += len(chunk)
                if received > spec.size or received > spec.maximum_size:
                    raise ProposalAssetProvisionError(f"{spec.label} exceeded its declared size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if received != spec.size or not hmac.compare_digest(digest.hexdigest(), spec.sha256):
            raise ProposalAssetProvisionError(f"{spec.label} content integrity check failed")

        temporary_path = Path(temporary_name)
        spec.content_validator(temporary_path)
        _assert_private_regular_file(temporary_path, spec.label)
        os.replace(temporary_path, spec.destination)
        temporary_name = ""
        _assert_private_regular_file(spec.destination, spec.label)
    except ProposalAssetProvisionError:
        raise
    except OSError:
        raise ProposalAssetProvisionError(f"{spec.label} could not be published") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            body.close()
        except Exception:
            pass
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _assert_private_regular_file(path: Path, label: str) -> None:
    try:
        file_stat = path.lstat()
    except OSError:
        raise ProposalAssetProvisionError(f"{label} file metadata could not be read") from None
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise ProposalAssetProvisionError(f"{label} is not a private regular file")


def _validate_template(path: Path) -> None:
    try:
        with path.open("rb") as source:
            magic = source.read(2)
    except OSError:
        raise ProposalAssetProvisionError("template could not be inspected") from None
    if magic != b"PK":
        raise ProposalAssetProvisionError("template is not a ZIP-based PowerPoint file")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if (
                "[Content_Types].xml" not in names
                or "ppt/presentation.xml" not in names
                or "ppt/_rels/presentation.xml.rels" not in names
            ):
                raise ProposalAssetProvisionError("template is not an OOXML presentation")
            presentation = _read_bounded_xml(
                archive,
                "ppt/presentation.xml",
                maximum_bytes=2 * 1024 * 1024,
            )
            relationships = _read_bounded_xml(
                archive,
                "ppt/_rels/presentation.xml.rels",
                maximum_bytes=2 * 1024 * 1024,
            )
            slide_names = _linked_slide_names(
                presentation,
                relationships,
                names,
            )
            if not slide_names:
                raise ProposalAssetProvisionError("template contains no slides")
            if len(slide_names) != _EXPECTED_SLIDE_COUNT:
                raise ProposalAssetProvisionError("integrated template slide count is invalid")
            text_bodies: list[str] = []
            total_slide_xml_bytes = 0
            for name in slide_names:
                xml_size = archive.getinfo(name).file_size
                total_slide_xml_bytes += xml_size
                if xml_size > 5 * 1024 * 1024 or total_slide_xml_bytes > 32 * 1024 * 1024:
                    raise ProposalAssetProvisionError("template slide XML exceeds its bound")
                root = _read_bounded_xml(
                    archive,
                    name,
                    maximum_bytes=5 * 1024 * 1024,
                )
                for text_body in root.iter(f"{{{_PRESENTATION_NS}}}txBody"):
                    text_bodies.append(
                        "".join(text.text or "" for text in text_body.iter(f"{{{_DRAWING_NS}}}t"))
                    )
                for text_body in root.iter(f"{{{_DRAWING_NS}}}txBody"):
                    text_bodies.append(
                        "".join(text.text or "" for text in text_body.iter(f"{{{_DRAWING_NS}}}t"))
                    )
    except ProposalAssetProvisionError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise ProposalAssetProvisionError("template OOXML could not be inspected") from None

    numeric_ids = frozenset(
        int(match.group(1)) for text in text_bodies for match in _NUMERIC_PLACEHOLDER.finditer(text)
    )
    if numeric_ids != _VALID_NUMERIC_IDS:
        raise ProposalAssetProvisionError("integrated template numeric inventory is incomplete")
    auxiliary_keys = frozenset(
        match.group(1) for text in text_bodies for match in _AUXILIARY_PLACEHOLDER.finditer(text)
    )
    if auxiliary_keys != _REQUIRED_AUXILIARY:
        raise ProposalAssetProvisionError("integrated template auxiliary inventory is invalid")
    template_versions = frozenset(
        match.group(1)
        for text in text_bodies
        for match in _TEMPLATE_VERSION_PLACEHOLDER.finditer(text)
    )
    if template_versions != {"proposal-builder-v1"}:
        raise ProposalAssetProvisionError("integrated template version marker is missing")
    date_offsets = frozenset(
        int(match.group(1)) for text in text_bodies for match in _DATE_PLACEHOLDER.finditer(text)
    )
    if not _REQUIRED_DATE_OFFSETS.issubset(date_offsets):
        raise ProposalAssetProvisionError("integrated template schedule inventory is incomplete")
    legacy_artifacts = [
        artifact for text in text_bodies for artifact in _find_legacy_template_artifacts(text)
    ]
    if legacy_artifacts:
        raise ProposalAssetProvisionError(
            "integrated template contains legacy placeholders or operator instructions"
        )


def _find_legacy_template_artifacts(text: str) -> list[str]:
    """Reject every brace token outside the integrated template grammar."""

    scrubbed = text
    for pattern in (
        _NUMERIC_PLACEHOLDER,
        _DATE_PLACEHOLDER,
        _TEMPLATE_VERSION_PLACEHOLDER,
        _AUXILIARY_PLACEHOLDER,
    ):
        scrubbed = pattern.sub("", scrubbed)
    findings: list[str] = []
    if _BRACE_CHARACTER.search(scrubbed):
        findings.append("brace")
    findings.extend(match.group(0) for match in _LEGACY_INSTRUCTION.finditer(scrubbed))
    return findings


def _read_bounded_xml(
    archive: zipfile.ZipFile,
    name: str,
    *,
    maximum_bytes: int,
) -> ElementTree.Element:
    if name not in archive.namelist() or archive.getinfo(name).file_size > maximum_bytes:
        raise ProposalAssetProvisionError("template XML part is missing or exceeds its bound")
    try:
        # 入力は社内 provision 済み FMT テンプレート zip の XML パートのみ（サイズ上限・
        # プレースホルダ検査済みの信頼境界内）。stdlib ElementTree は外部実体を解決しない。
        return ElementTree.fromstring(archive.read(name))  # nosec B314
    except ElementTree.ParseError:
        raise ProposalAssetProvisionError("template contains invalid XML") from None


def _linked_slide_names(
    presentation: ElementTree.Element,
    relationships: ElementTree.Element,
    names: set[str],
) -> list[str]:
    """Resolve the slide order used by PowerPoint/python-pptx, ignoring orphan parts."""

    targets: dict[str, str] = {}
    for relationship in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        if relationship.get("Type") != _SLIDE_REL_TYPE:
            continue
        relationship_id = relationship.get("Id")
        target = relationship.get("Target")
        if (
            not relationship_id
            or not target
            or relationship.get("TargetMode", "Internal") != "Internal"
            or relationship_id in targets
        ):
            raise ProposalAssetProvisionError("template slide relationship is invalid")
        if target.startswith("/"):
            resolved = posixpath.normpath(target.lstrip("/"))
        else:
            resolved = posixpath.normpath(posixpath.join("ppt", target))
        if resolved not in names or re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", resolved) is None:
            raise ProposalAssetProvisionError("template slide target is invalid")
        targets[relationship_id] = resolved

    ordered: list[str] = []
    for slide_id in presentation.findall(
        f"./{{{_PRESENTATION_NS}}}sldIdLst/{{{_PRESENTATION_NS}}}sldId"
    ):
        relationship_id = slide_id.get(f"{{{_OFFICE_REL_NS}}}id")
        target = targets.get(relationship_id or "")
        if target is None or target in ordered:
            raise ProposalAssetProvisionError("template slide order is invalid")
        ordered.append(target)
    return ordered


def _validate_account_db(path: Path) -> None:
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ProposalAssetProvisionError("account database is not valid JSON") from None

    if not isinstance(value, dict):
        raise ProposalAssetProvisionError("account database JSON shape is invalid")
    metadata = value.get("_meta")
    accounts = value.get("accounts")
    if (
        not isinstance(metadata, dict)
        or not isinstance(accounts, list)
        or not accounts
        or set(value) != {"_meta", "accounts"}
        or set(metadata) != {"source", "count", "note"}
        or any(not isinstance(metadata[key], str) for key in {"source", "note"})
        or type(metadata.get("count")) is not int
        or metadata.get("count") != len(accounts)
    ):
        raise ProposalAssetProvisionError("account database JSON shape is invalid")
    required = {"name", "category", "desc", "tt", "ig", "yt"}
    for account in accounts:
        if (
            not isinstance(account, dict)
            or set(account) != required
            or not isinstance(account["category"], list)
            or any(not isinstance(item, str) for item in account["category"])
            or any(not isinstance(account[key], str) for key in required - {"category"})
        ):
            raise ProposalAssetProvisionError("account database record shape is invalid")
