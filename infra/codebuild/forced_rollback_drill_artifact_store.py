#!/usr/bin/env python3
"""Persist local forced-rollback drill artifacts as immutable signed evidence."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from teamagent_release_approval import ProvenanceError, canonical_json_bytes

SIGNING_ALGORITHM = "RSASSA_PSS_SHA_256"

_MANIFEST_KEYS = {"drill_id", "artifacts"}
_ARTIFACT_KEYS = {"name", "path", "key", "content_type"}
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_ACCOUNT_ID_RE = re.compile(r"[0-9]{12}")
_REGION_RE = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]")
_S3_BUCKET_RE = re.compile(
    r"(?=.{3,63}\Z)(?![0-9]+(?:\.[0-9]+){3}\Z)"
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
)
_VERSION_ID_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
_CONTENT_TYPE_RE = re.compile(
    r"[a-z0-9][a-z0-9.+-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,126}"
)


class ArtifactStoreError(ValueError):
    """The artifact manifest or an immutable persistence result is invalid."""


@dataclass(frozen=True)
class Artifact:
    """One validated local artifact and its intended immutable S3 key."""

    name: str
    path: Path
    key: str
    content_type: str


def _fail(message: str) -> None:
    raise ArtifactStoreError(message)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    _fail(f"non-finite JSON number is forbidden: {value}")


def _exact_object(
    value: Any,
    expected_keys: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        _fail(f"{label} must be a built-in JSON object")
    actual_keys = set(value)
    missing = sorted(expected_keys - actual_keys)
    unknown = sorted(actual_keys - expected_keys)
    if missing or unknown:
        _fail(f"{label} schema mismatch: missing={missing}, unknown={unknown}")
    return value


def _canonical_text(value: Any, *, label: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        _fail(f"{label} must be canonical non-empty text")
    return value


def _safe_s3_key(value: Any, *, label: str) -> str:
    key = _canonical_text(value, label=label, maximum=1024)
    parts = key.split("/")
    if (
        PurePosixPath(key).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in key
    ):
        _fail(f"{label} must be a safe relative S3 key")
    return key


def _read_regular_file(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactStoreError(f"cannot open {label}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            _fail(f"{label} must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        value = b"".join(chunks)
        final_stat = os.fstat(descriptor)
        if (
            file_stat.st_dev != final_stat.st_dev
            or file_stat.st_ino != final_stat.st_ino
            or file_stat.st_size != final_stat.st_size
            or file_stat.st_mtime_ns != final_stat.st_mtime_ns
            or file_stat.st_ctime_ns != final_stat.st_ctime_ns
            or len(value) != final_stat.st_size
        ):
            _fail(f"{label} changed while it was being read")
        if not value:
            _fail(f"{label} must be non-empty")
        return value
    finally:
        os.close(descriptor)


def _load_manifest(path: Path, *, evidence_prefix: str) -> tuple[str, list[Artifact]]:
    raw = _read_regular_file(path, label="artifact manifest")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactStoreError("artifact manifest is not valid JSON") from exc
    manifest = _exact_object(value, _MANIFEST_KEYS, label="manifest")

    drill_id = manifest["drill_id"]
    if type(drill_id) is not str or not _UUID_RE.fullmatch(drill_id):
        _fail("manifest.drill_id must be a canonical lowercase UUID")

    raw_artifacts = manifest["artifacts"]
    if type(raw_artifacts) is not list or not raw_artifacts or len(raw_artifacts) > 128:
        _fail("manifest.artifacts must contain between 1 and 128 artifacts")

    base_key = f"{evidence_prefix}/{drill_id}/"
    artifacts: list[Artifact] = []
    names: set[str] = set()
    all_object_keys: set[str] = set()
    for index, raw_artifact in enumerate(raw_artifacts):
        label = f"manifest.artifacts[{index}]"
        item = _exact_object(raw_artifact, _ARTIFACT_KEYS, label=label)

        name = item["name"]
        if type(name) is not str or not _NAME_RE.fullmatch(name):
            _fail(f"{label}.name must be a lowercase artifact identifier")
        if name in names:
            _fail(f"{label}.name is duplicated")
        names.add(name)

        path_text = _canonical_text(
            item["path"],
            label=f"{label}.path",
            maximum=4096,
        )
        path = Path(path_text)
        if not path.is_absolute():
            path = (Path.cwd() / path).absolute()

        key = _safe_s3_key(item["key"], label=f"{label}.key")
        if not key.startswith(base_key) or key == base_key:
            _fail(f"{label}.key must be below {base_key}")
        signature_key = _safe_s3_key(
            f"{key}.sig",
            label=f"{label}.signature_key",
        )
        if key in all_object_keys or signature_key in all_object_keys:
            _fail(f"{label}.key collides with another payload or signature")
        all_object_keys.update((key, signature_key))

        content_type = item["content_type"]
        if type(content_type) is not str or not _CONTENT_TYPE_RE.fullmatch(content_type):
            _fail(f"{label}.content_type must be a canonical media type")

        artifacts.append(
            Artifact(
                name=name,
                path=path,
                key=key,
                content_type=content_type,
            )
        )

    artifacts.sort(key=lambda artifact: artifact.name)
    return drill_id, artifacts


class Store:
    """AWS-backed immutable storage for one validated artifact manifest."""

    def __init__(
        self,
        *,
        aws_bin: str,
        account_id: str,
        region: str,
        bucket: str,
        encryption_key_alias: str,
        signing_key_alias: str,
        signing_algorithm: str,
        minimum_retention_days: int,
        now_epoch: int,
        temporary_directory: Path,
    ) -> None:
        self.aws_bin = aws_bin
        self.account_id = account_id
        self.region = region
        self.bucket = bucket
        self.encryption_key_alias = encryption_key_alias
        self.signing_key_alias = signing_key_alias
        self.signing_algorithm = signing_algorithm
        self.temporary_directory = temporary_directory

        if type(minimum_retention_days) is not int or minimum_retention_days < 1:
            _fail("minimum retention days must be an integer >= 1")
        if type(now_epoch) is not int or now_epoch < 0:
            _fail("now epoch must be a non-negative integer")
        try:
            requested = (
                dt.datetime.fromtimestamp(now_epoch, tz=dt.UTC)
                + dt.timedelta(days=minimum_retention_days + 1)
            ).replace(microsecond=0)
        except (OverflowError, OSError, ValueError) as exc:
            raise ArtifactStoreError("retention timestamp is invalid") from exc
        self.requested_retain_until = requested
        self.requested_retain_until_text = requested.strftime("%Y-%m-%dT%H:%M:%SZ")

        self._kms_arn_pattern = re.compile(
            rf"arn:aws:kms:{re.escape(region)}:{re.escape(account_id)}:"
            r"key/[0-9a-f-]{36}"
        )

    def aws_json(
        self,
        service: str,
        operation: str,
        arguments: list[str],
    ) -> dict[str, Any]:
        endpoint_service = "s3" if service == "s3api" else service
        command = [
            self.aws_bin,
            service,
            operation,
            "--region",
            self.region,
            "--endpoint-url",
            f"https://{endpoint_service}.{self.region}.amazonaws.com",
            *arguments,
            "--no-cli-pager",
            "--no-paginate",
            "--output",
            "json",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArtifactStoreError(f"AWS {service} {operation} could not run") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[:1000]
            _fail(f"AWS {service} {operation} failed: {detail}")
        try:
            value = json.loads(
                completed.stdout or "{}",
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ArtifactStoreError(f"AWS {service} {operation} returned invalid JSON") from exc
        if type(value) is not dict:
            _fail(f"AWS {service} {operation} returned a non-object")
        return value

    def resolve_key(
        self,
        alias: str,
        *,
        key_usage: str,
        key_spec: str | None = None,
    ) -> str:
        response = self.aws_json(
            "kms",
            "describe-key",
            ["--key-id", alias],
        )
        metadata = response.get("KeyMetadata")
        if type(metadata) is not dict:
            _fail(f"KMS describe-key returned no metadata for {alias}")
        arn = metadata.get("Arn")
        if (
            type(arn) is not str
            or not self._kms_arn_pattern.fullmatch(arn)
            or metadata.get("Enabled") is not True
            or metadata.get("KeyState") != "Enabled"
            or metadata.get("KeyUsage") != key_usage
            or (key_spec is not None and metadata.get("KeySpec") != key_spec)
        ):
            _fail(f"KMS alias {alias} did not resolve to the required enabled key")
        return arn

    @staticmethod
    def _exact_version_id(
        response: dict[str, Any],
        *,
        label: str,
    ) -> str:
        version_id = response.get("VersionId")
        if (
            type(version_id) is not str
            or not _VERSION_ID_RE.fullmatch(version_id)
            or version_id in {"None", "null"}
        ):
            _fail(f"S3 did not return an exact {label} VersionId")
        return version_id

    @staticmethod
    def _normalize_timestamp(
        value: Any,
        *,
        label: str,
    ) -> tuple[str, dt.datetime]:
        if type(value) is not str:
            _fail(f"{label} is missing")
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ArtifactStoreError(f"{label} is invalid") from exc
        if parsed.tzinfo is None:
            _fail(f"{label} is not timezone-aware")
        normalized = parsed.astimezone(dt.UTC).replace(microsecond=0)
        return normalized.strftime("%Y-%m-%dT%H:%M:%SZ"), normalized

    def put_object(
        self,
        *,
        key: str,
        body: Path,
        content_type: str,
        encryption_key_arn: str,
    ) -> str:
        response = self.aws_json(
            "s3api",
            "put-object",
            [
                "--bucket",
                self.bucket,
                "--key",
                key,
                "--body",
                str(body),
                "--content-type",
                content_type,
                "--server-side-encryption",
                "aws:kms",
                "--ssekms-key-id",
                encryption_key_arn,
                "--object-lock-mode",
                "COMPLIANCE",
                "--object-lock-retain-until-date",
                self.requested_retain_until_text,
                "--expected-bucket-owner",
                self.account_id,
                "--if-none-match",
                "*",
            ],
        )
        return self._exact_version_id(response, label=key)

    def exact_version_download(
        self,
        *,
        key: str,
        version_id: str,
        expected_bytes: bytes,
        content_type: str,
        encryption_key_arn: str,
        destination: Path,
    ) -> tuple[str, str]:
        metadata = self.aws_json(
            "s3api",
            "get-object",
            [
                "--bucket",
                self.bucket,
                "--key",
                key,
                "--version-id",
                version_id,
                "--expected-bucket-owner",
                self.account_id,
                str(destination),
            ],
        )
        downloaded = _read_regular_file(
            destination,
            label=f"exact-version download for {key}",
        )
        downloaded_sha256 = hashlib.sha256(downloaded).hexdigest()
        retain_until, parsed_retain_until = self._normalize_timestamp(
            metadata.get("ObjectLockRetainUntilDate"),
            label=f"{key} ObjectLockRetainUntilDate",
        )
        if (
            downloaded != expected_bytes
            or metadata.get("VersionId") != version_id
            or type(metadata.get("ContentLength")) is not int
            or metadata.get("ContentLength") != len(expected_bytes)
            or metadata.get("ContentType") != content_type
            or metadata.get("ServerSideEncryption") != "aws:kms"
            or metadata.get("SSEKMSKeyId") != encryption_key_arn
            or metadata.get("ObjectLockMode") != "COMPLIANCE"
            or parsed_retain_until < self.requested_retain_until
        ):
            _fail(f"immutable artifact exact-version download did not match: {key}")
        return downloaded_sha256, retain_until

    def persist(
        self,
        *,
        artifact: Artifact,
        payload_bytes: bytes,
        drill_id: str,
        index: int,
        encryption_key_arn: str,
        signing_key_arn: str,
    ) -> dict[str, Any]:
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        signature_key = f"{artifact.key}.sig"
        directory = self.temporary_directory / f"artifact-{index:03d}"
        directory.mkdir(mode=0o700)
        payload_path = directory / "payload"
        digest_path = directory / "payload.sha256"
        raw_signature_path = directory / "payload.sig"
        envelope_path = directory / "payload.sig.json"
        downloaded_payload_path = directory / "downloaded-payload"
        downloaded_signature_path = directory / "downloaded-signature"
        payload_path.write_bytes(payload_bytes)
        digest_path.write_bytes(bytes.fromhex(payload_sha256))

        signed = self.aws_json(
            "kms",
            "sign",
            [
                "--key-id",
                signing_key_arn,
                "--message",
                f"fileb://{digest_path}",
                "--message-type",
                "DIGEST",
                "--signing-algorithm",
                self.signing_algorithm,
            ],
        )
        signature_base64 = signed.get("Signature")
        if (
            signed.get("KeyId") != signing_key_arn
            or signed.get("SigningAlgorithm") != self.signing_algorithm
            or type(signature_base64) is not str
        ):
            _fail(f"KMS returned an invalid signature for {artifact.name}")
        try:
            signature_bytes = base64.b64decode(
                signature_base64,
                validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise ArtifactStoreError(
                f"KMS returned malformed signature bytes for {artifact.name}"
            ) from exc
        if len(signature_bytes) != 384:
            _fail(f"KMS returned a non-RSA-3072 signature for {artifact.name}")
        raw_signature_path.write_bytes(signature_bytes)

        signature_envelope = {
            "schema_version": 1,
            "drill_id": drill_id,
            "payload_key": artifact.key,
            "payload_sha256": payload_sha256,
            "signing_kms_key_arn": signing_key_arn,
            "signing_algorithm": self.signing_algorithm,
            "signature_base64": signature_base64,
        }
        try:
            envelope_bytes = canonical_json_bytes(signature_envelope)
        except ProvenanceError as exc:
            raise ArtifactStoreError(
                f"cannot canonicalize signature envelope for {artifact.name}"
            ) from exc
        envelope_path.write_bytes(envelope_bytes)

        payload_version_id = self.put_object(
            key=artifact.key,
            body=payload_path,
            content_type=artifact.content_type,
            encryption_key_arn=encryption_key_arn,
        )
        signature_version_id = self.put_object(
            key=signature_key,
            body=envelope_path,
            content_type="application/json",
            encryption_key_arn=encryption_key_arn,
        )
        downloaded_payload_sha256, returned_retain_until = self.exact_version_download(
            key=artifact.key,
            version_id=payload_version_id,
            expected_bytes=payload_bytes,
            content_type=artifact.content_type,
            encryption_key_arn=encryption_key_arn,
            destination=downloaded_payload_path,
        )
        downloaded_signature_sha256, _ = self.exact_version_download(
            key=signature_key,
            version_id=signature_version_id,
            expected_bytes=envelope_bytes,
            content_type="application/json",
            encryption_key_arn=encryption_key_arn,
            destination=downloaded_signature_path,
        )

        verified = self.aws_json(
            "kms",
            "verify",
            [
                "--key-id",
                signing_key_arn,
                "--message",
                f"fileb://{digest_path}",
                "--message-type",
                "DIGEST",
                "--signature",
                f"fileb://{raw_signature_path}",
                "--signing-algorithm",
                self.signing_algorithm,
            ],
        )
        if (
            verified.get("KeyId") != signing_key_arn
            or verified.get("SigningAlgorithm") != self.signing_algorithm
            or verified.get("SignatureValid") is not True
        ):
            _fail(f"KMS signature verification failed for {artifact.name}")

        return {
            "bucket": self.bucket,
            "key": artifact.key,
            "version_id": payload_version_id,
            "sha256": payload_sha256,
            "size": len(payload_bytes),
            "content_type": artifact.content_type,
            "object_lock_mode": "COMPLIANCE",
            "retain_until": returned_retain_until,
            "encryption_kms_key_arn": encryption_key_arn,
            "signature": {
                "key": signature_key,
                "version_id": signature_version_id,
                "sha256": downloaded_signature_sha256,
                "verified": True,
            },
            "signer": {
                "kms_key_arn": signing_key_arn,
                "algorithm": self.signing_algorithm,
            },
            "exact_version_redownload": {
                "requested_version_id": payload_version_id,
                "returned_version_id": payload_version_id,
                "sha256": downloaded_payload_sha256,
                "size": len(payload_bytes),
                "bytes_match": True,
            },
        }


def _validate_cli(args: argparse.Namespace) -> str:
    aws_path = Path(args.aws_bin)
    if not aws_path.is_absolute() or not aws_path.is_file() or not os.access(aws_path, os.X_OK):
        _fail("--aws-bin must be an absolute executable file")
    if not _ACCOUNT_ID_RE.fullmatch(args.account_id):
        _fail("--account-id must be 12 decimal digits")
    if not _REGION_RE.fullmatch(args.region):
        _fail("--region must be a canonical AWS region")
    if (
        not _S3_BUCKET_RE.fullmatch(args.bucket)
        or ".." in args.bucket
        or ".-" in args.bucket
        or "-." in args.bucket
    ):
        _fail("--bucket must be a canonical S3 bucket")
    evidence_prefix = _safe_s3_key(args.prefix, label="--prefix")
    if evidence_prefix.endswith("/"):
        _fail("--prefix must not end with '/'")
    _canonical_text(
        args.encryption_key_alias,
        label="--encryption-key-alias",
        maximum=512,
    )
    _canonical_text(
        args.signing_key_alias,
        label="--signing-key-alias",
        maximum=512,
    )
    if args.signing_algorithm != SIGNING_ALGORITHM:
        _fail(f"--signing-algorithm must be {SIGNING_ALGORITHM}")
    if os.path.lexists(args.out):
        _fail("--out already exists")
    if not args.out.parent.is_dir():
        _fail("--out parent directory does not exist")
    return evidence_prefix


def persist_manifest(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Validate and immutably persist all artifacts in ``args.manifest``."""

    evidence_prefix = _validate_cli(args)
    drill_id, artifacts = _load_manifest(
        args.manifest,
        evidence_prefix=evidence_prefix,
    )
    payloads = {
        artifact.name: _read_regular_file(
            artifact.path,
            label=f"artifact {artifact.name}",
        )
        for artifact in artifacts
    }

    with tempfile.TemporaryDirectory(
        prefix=".forced-rollback-artifacts.",
        dir=args.out.parent,
    ) as temporary_directory:
        store = Store(
            aws_bin=args.aws_bin,
            account_id=args.account_id,
            region=args.region,
            bucket=args.bucket,
            encryption_key_alias=args.encryption_key_alias,
            signing_key_alias=args.signing_key_alias,
            signing_algorithm=args.signing_algorithm,
            minimum_retention_days=args.minimum_retention_days,
            now_epoch=args.now_epoch,
            temporary_directory=Path(temporary_directory),
        )
        encryption_key_arn = store.resolve_key(
            args.encryption_key_alias,
            key_usage="ENCRYPT_DECRYPT",
        )
        signing_key_arn = store.resolve_key(
            args.signing_key_alias,
            key_usage="SIGN_VERIFY",
            key_spec="RSA_3072",
        )
        if encryption_key_arn == signing_key_arn:
            _fail("artifact encryption and signing keys must be distinct")

        locators = {
            artifact.name: store.persist(
                artifact=artifact,
                payload_bytes=payloads[artifact.name],
                drill_id=drill_id,
                index=index,
                encryption_key_arn=encryption_key_arn,
                signing_key_arn=signing_key_arn,
            )
            for index, artifact in enumerate(artifacts)
        }
    return locators


def _write_exclusive(path: Path, value: dict[str, dict[str, Any]]) -> None:
    try:
        payload = canonical_json_bytes(value)
    except ProvenanceError as exc:
        raise ArtifactStoreError("artifact locator map is not canonical JSON data") from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ArtifactStoreError("cannot create exclusive output") from exc
    opened_stat = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            current_stat = path.lstat()
            if (
                current_stat.st_dev == opened_stat.st_dev
                and current_stat.st_ino == opened_stat.st_ino
            ):
                path.unlink()
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--aws-bin", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--encryption-key-alias", required=True)
    parser.add_argument("--signing-key-alias", required=True)
    parser.add_argument("--signing-algorithm", required=True)
    parser.add_argument("--minimum-retention-days", type=int, required=True)
    parser.add_argument("--now-epoch", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        locators = persist_manifest(args)
        _write_exclusive(args.out, locators)
    except (ArtifactStoreError, OSError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
