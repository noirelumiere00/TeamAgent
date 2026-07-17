#!/usr/bin/env python3
"""Validate signed source declarations and image release receipts.

This module is intentionally dependency-free so the same bytes can be embedded
in the fixed CodeBuild attestor/promoter projects and used by Terraform's
plan-time release gate.  KMS verification is performed by the caller; this
module validates the exact, signed payload after that cryptographic check.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ACCOUNT_ID = "718959508629"
REGION = "ap-northeast-1"
REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
EVIDENCE_BUCKET = "teamagent-dev-image-release-evidence"
SOURCE_BUCKET = "teamagent-dev-raw-files"
SOURCE_KEY = "codebuild/source.zip"
APP_HTML_KEY = "codebuild/connect-web-app.html"
APP_HTML_VERSION_ID = "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee"
APP_HTML_SHA256 = "03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c"
VAULT_MANIFEST_SHA256 = "aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e"
BUILD_INPUTS_SHA256 = "6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf"
SOURCE_REPOSITORY = "https://github.com/noirelumiere00/TeamAgent.git"
SOURCE_BRANCH = "dev"
SOURCE_DECLARATION_KIND = "teamagent.source-declaration"
RELEASE_RECEIPT_KIND = "teamagent.release-receipt"
SOURCE_DECLARATION_SCHEMA = 2
RELEASE_RECEIPT_SCHEMA = 2
MAX_RELEASE_RECEIPT_LIFETIME_SECONDS = 3600
MAX_CANDIDATE_RECEIPT_LIFETIME_SECONDS = 30 * 24 * 60 * 60
SINGLE_ARM64_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
REFERRER_ARTIFACT_TYPES = {
    "sbom": "application/spdx+json",
    "provenance": "application/vnd.in-toto+json",
}
SIGNATURE_ARTIFACT_TYPES = {
    "application/vnd.dev.cosign.simplesigning.v1+json",
    "application/vnd.dsse.envelope.v1+json",
}
PIPELINES: dict[str, dict[str, Any]] = {
    "mcp": {
        "build_project": "teamagent-dev-image-builder",
        "contract_path": "infra/codebuild/teamagent_runtime_contract.json",
        "contract_label": "io.teamagent.build.runtime-contract-sha256",
        "subjects": {
            "mcp": (
                "teamagent-mcp-quarantine",
                "teamagent-mcp-verified-candidates",
                "teamagent-mcp",
            ),
        },
    },
    "tiktok": {
        "build_project": "teamagent-dev-tiktok-image-builder",
        "contract_path": "infra/codebuild/tiktok_release_contract.json",
        "contract_label": "io.teamagent.build.contract-sha256",
        "subjects": {
            "tiktok": (
                "teamagent-dev-tiktok-acquire-quarantine",
                "teamagent-dev-tiktok-acquire-verified-candidates",
                "teamagent-dev-tiktok-acquire",
            ),
        },
    },
    "openclaw": {
        "build_project": "teamagent-dev-openclaw-provenance-builder",
        "contract_path": "infra/codebuild/openclaw_bundle_contract.json",
        "contract_label": "io.teamagent.build.contract-sha256",
        "subjects": {
            "core": (
                "teamagent-openclaw-quarantine",
                "teamagent-openclaw-verified-candidates",
                "teamagent-openclaw",
            ),
            "media": (
                "teamagent-openclaw-media-quarantine",
                "teamagent-openclaw-media-verified-candidates",
                "teamagent-openclaw-media",
            ),
        },
    },
}

_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_S3_VERSION_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
_BUILD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/._-]{0,511}")
_PATH_RE = re.compile(r"/[A-Za-z0-9][A-Za-z0-9_./+-]{0,511}")
_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,254}")
_KEY_ARN_RE = re.compile(
    rf"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/[0-9a-f-]{{36}}"
)


class EvidenceError(ValueError):
    """The signed evidence is malformed, stale, or outside the allowlist."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid {label}: {exc}") from exc


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        raise EvidenceError(f"{label} schema mismatch: missing={missing}, unknown={unknown}")


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _string(value: Any, *, label: str, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise EvidenceError(f"{label} must be a canonical non-empty string")
    return value


def _sha1(value: Any, *, label: str) -> str:
    value = _string(value, label=label)
    if not _SHA1_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a full lowercase Git SHA")
    return value


def _sha256(value: Any, *, label: str) -> str:
    value = _string(value, label=label)
    if not _SHA256_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a lowercase SHA-256")
    return value


def _digest(value: Any, *, label: str) -> str:
    value = _string(value, label=label)
    if not _DIGEST_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a sha256 digest")
    return value


def _version_id(value: Any, *, label: str) -> str:
    value = _string(value, label=label, maximum=1024)
    if value in {"None", "null"} or not _S3_VERSION_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a usable S3 VersionId")
    return value


def _timestamp(value: Any, *, label: str) -> dt.datetime:
    value = _string(value, label=label, maximum=64)
    if not value.endswith("Z"):
        raise EvidenceError(f"{label} must be UTC with a Z suffix")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError(f"{label} is not an RFC3339 timestamp") from exc
    if parsed.microsecond:
        raise EvidenceError(f"{label} must use whole seconds")
    return parsed


def _metadata_timestamp(value: Any, *, label: str) -> dt.datetime:
    value = _string(value, label=label, maximum=64)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{label} is not an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise EvidenceError(f"{label} must be UTC")
    return parsed


def _bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceError(f"{label} must be a boolean")
    return value


def _zero(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != 0:
        raise EvidenceError(f"{label} must be exactly zero")
    return value


def validate_source_declaration(
    value: Any,
    *,
    expected_commit: str | None = None,
    expected_source_version: str | None = None,
    expected_app_version: str | None = None,
    expected_app_sha256: str | None = None,
    expected_vault_manifest_sha256: str | None = None,
    expected_build_inputs_sha256: str | None = None,
    expected_contract_sha256: str | None = None,
) -> dict[str, Any]:
    declaration = _mapping(value, label="source declaration")
    _exact_keys(
        declaration,
        {
            "schema_version",
            "kind",
            "publisher",
            "source",
            "app_html",
            "application_provenance",
            "contract",
        },
        label="source declaration",
    )
    if declaration["schema_version"] != SOURCE_DECLARATION_SCHEMA:
        raise EvidenceError("unsupported source declaration schema")
    if declaration["kind"] != SOURCE_DECLARATION_KIND:
        raise EvidenceError("source declaration kind mismatch")

    publisher = _mapping(declaration["publisher"], label="source publisher")
    _exact_keys(
        publisher,
        {"project_arn", "build_id", "repository", "branch", "commit"},
        label="source publisher",
    )
    expected_project = (
        f"arn:aws:codebuild:{REGION}:{ACCOUNT_ID}:project/"
        "teamagent-dev-mcp-source-publisher"
    )
    if publisher["project_arn"] != expected_project:
        raise EvidenceError("source publisher project is not allowlisted")
    if not _BUILD_ID_RE.fullmatch(_string(publisher["build_id"], label="publisher build ID")):
        raise EvidenceError("publisher build ID is invalid")
    if publisher["repository"] != SOURCE_REPOSITORY or publisher["branch"] != SOURCE_BRANCH:
        raise EvidenceError("source repository or branch is not allowlisted")
    commit = _sha1(publisher["commit"], label="source commit")

    source = _mapping(declaration["source"], label="source archive")
    _exact_keys(
        source,
        {"bucket", "key", "version_id", "sha256", "manifest_sha256"},
        label="source archive",
    )
    if source["bucket"] != SOURCE_BUCKET or source["key"] != SOURCE_KEY:
        raise EvidenceError("source archive location is not allowlisted")
    source_version = _version_id(source["version_id"], label="source archive VersionId")
    _sha256(source["sha256"], label="source archive SHA-256")
    _sha256(source["manifest_sha256"], label="embedded source manifest SHA-256")

    app_html = _mapping(declaration["app_html"], label="app HTML")
    _exact_keys(
        app_html,
        {"bucket", "key", "version_id", "sha256"},
        label="app HTML",
    )
    if app_html["bucket"] != SOURCE_BUCKET or app_html["key"] != APP_HTML_KEY:
        raise EvidenceError("app HTML location is not allowlisted")
    app_version = _version_id(app_html["version_id"], label="app HTML VersionId")
    app_sha256 = _sha256(app_html["sha256"], label="app HTML SHA-256")
    if app_version != APP_HTML_VERSION_ID or app_sha256 != APP_HTML_SHA256:
        raise EvidenceError("app HTML is not the production canonical object")

    application_provenance = _mapping(
        declaration["application_provenance"],
        label="application provenance",
    )
    _exact_keys(
        application_provenance,
        {"vault_manifest_sha256", "build_inputs_sha256"},
        label="application provenance",
    )
    vault_manifest_sha256 = _sha256(
        application_provenance["vault_manifest_sha256"],
        label="Vault manifest SHA-256",
    )
    build_inputs_sha256 = _sha256(
        application_provenance["build_inputs_sha256"],
        label="build_inputs SHA-256",
    )
    if vault_manifest_sha256 != VAULT_MANIFEST_SHA256:
        raise EvidenceError("Vault manifest SHA-256 is not production canonical")
    if build_inputs_sha256 != BUILD_INPUTS_SHA256:
        raise EvidenceError("build_inputs SHA-256 is not production canonical")

    contract = _mapping(declaration["contract"], label="source contract")
    _exact_keys(contract, {"path", "sha256"}, label="source contract")
    if contract["path"] != PIPELINES["mcp"]["contract_path"]:
        raise EvidenceError("source contract path is not allowlisted")
    contract_sha256 = _sha256(contract["sha256"], label="source contract SHA-256")

    expected_values = (
        (expected_commit, commit, "source commit"),
        (expected_source_version, source_version, "source archive VersionId"),
        (expected_app_version, app_version, "app HTML VersionId"),
        (expected_app_sha256, app_sha256, "app HTML SHA-256"),
        (
            expected_vault_manifest_sha256,
            vault_manifest_sha256,
            "Vault manifest SHA-256",
        ),
        (
            expected_build_inputs_sha256,
            build_inputs_sha256,
            "build_inputs SHA-256",
        ),
        (expected_contract_sha256, contract_sha256, "source contract SHA-256"),
    )
    for expected, actual, label in expected_values:
        if expected is not None and expected != actual:
            raise EvidenceError(f"{label} mismatch")
    return dict(declaration)


def source_declaration(
    *,
    project_arn: str,
    build_id: str,
    commit: str,
    source_version: str,
    source_sha256: str,
    manifest_sha256: str,
    app_version: str,
    app_sha256: str,
    vault_manifest_sha256: str,
    build_inputs_sha256: str,
    contract_sha256: str,
) -> dict[str, Any]:
    value = {
        "schema_version": SOURCE_DECLARATION_SCHEMA,
        "kind": SOURCE_DECLARATION_KIND,
        "publisher": {
            "project_arn": project_arn,
            "build_id": build_id,
            "repository": SOURCE_REPOSITORY,
            "branch": SOURCE_BRANCH,
            "commit": commit,
        },
        "source": {
            "bucket": SOURCE_BUCKET,
            "key": SOURCE_KEY,
            "version_id": source_version,
            "sha256": source_sha256,
            "manifest_sha256": manifest_sha256,
        },
        "app_html": {
            "bucket": SOURCE_BUCKET,
            "key": APP_HTML_KEY,
            "version_id": app_version,
            "sha256": app_sha256,
        },
        "application_provenance": {
            "vault_manifest_sha256": vault_manifest_sha256,
            "build_inputs_sha256": build_inputs_sha256,
        },
        "contract": {
            "path": PIPELINES["mcp"]["contract_path"],
            "sha256": contract_sha256,
        },
    }
    validate_source_declaration(value)
    return value


def _expected_tag(channel: str, commit: str, subject: str, subject_count: int) -> str:
    suffix = f"-{subject}" if subject_count > 1 else ""
    prefix = {
        "verified-candidate": "verified",
        "active": "active",
        "rollback": "rollback",
    }.get(channel)
    if prefix is None:
        raise EvidenceError("receipt channel is not allowlisted")
    return f"{prefix}-{commit}{suffix}"


def _validate_signature(
    value: Any,
    *,
    label: str,
    expected_subject_digest: str,
) -> dict[str, Any]:
    signature = _mapping(value, label=label)
    _exact_keys(
        signature,
        {
            "verified",
            "key_arn",
            "subject_digest",
            "referrer_digest",
            "bundle_sha256",
        },
        label=label,
    )
    if not _bool(signature["verified"], label=f"{label}.verified"):
        raise EvidenceError(f"{label} must be cryptographically verified")
    key_arn = _string(signature["key_arn"], label=f"{label}.key_arn")
    if not _KEY_ARN_RE.fullmatch(key_arn):
        raise EvidenceError(f"{label}.key_arn is not an allowlisted account key")
    if _digest(signature["subject_digest"], label=f"{label}.subject_digest") != (
        expected_subject_digest
    ):
        raise EvidenceError(f"{label} does not bind the expected subject digest")
    referrer_digest = _digest(
        signature["referrer_digest"],
        label=f"{label}.referrer_digest",
    )
    if referrer_digest == expected_subject_digest:
        raise EvidenceError(f"{label} referrer digest must differ from its subject")
    _sha256(signature["bundle_sha256"], label=f"{label}.bundle_sha256")
    return dict(signature)


def _validate_referrer(
    value: Any,
    *,
    label: str,
    kind: str,
    subject_digest: str,
) -> dict[str, Any]:
    referrer = _mapping(value, label=label)
    _exact_keys(
        referrer,
        {"digest", "artifact_type", "payload_sha256", "signature"},
        label=label,
    )
    digest = _digest(referrer["digest"], label=f"{label}.digest")
    if referrer["artifact_type"] != REFERRER_ARTIFACT_TYPES[kind]:
        raise EvidenceError(f"{label}.artifact_type mismatch")
    _sha256(referrer["payload_sha256"], label=f"{label}.payload_sha256")
    signature = _validate_signature(
        referrer["signature"],
        label=f"{label}.signature",
        expected_subject_digest=digest,
    )
    if signature["subject_digest"] == subject_digest:
        raise EvidenceError(f"{label} signature must bind the referrer, not only the image")
    return dict(referrer)


def validate_release_receipt(
    value: Any,
    *,
    expected_pipeline: str | None = None,
    expected_commit: str | None = None,
    expected_contract_sha256: str | None = None,
    allowed_channels: set[str] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    receipt = _mapping(value, label="release receipt")
    _exact_keys(
        receipt,
        {
            "schema_version",
            "kind",
            "pipeline",
            "channel",
            "issued_at",
            "expires_at",
            "build",
            "contract",
            "source_evidence",
            "subjects",
        },
        label="release receipt",
    )
    if receipt["schema_version"] != RELEASE_RECEIPT_SCHEMA:
        raise EvidenceError("unsupported release receipt schema")
    if receipt["kind"] != RELEASE_RECEIPT_KIND:
        raise EvidenceError("release receipt kind mismatch")
    pipeline = _string(receipt["pipeline"], label="pipeline")
    if pipeline not in PIPELINES:
        raise EvidenceError("release receipt pipeline is not allowlisted")
    if expected_pipeline is not None and pipeline != expected_pipeline:
        raise EvidenceError("release receipt pipeline mismatch")
    channel = _string(receipt["channel"], label="release channel")
    if channel not in {"verified-candidate", "active", "rollback"}:
        raise EvidenceError("release receipt channel is not allowlisted")
    if allowed_channels is not None and channel not in allowed_channels:
        raise EvidenceError("release receipt channel is not valid for this operation")

    issued_at = _timestamp(receipt["issued_at"], label="issued_at")
    expires_at = _timestamp(receipt["expires_at"], label="expires_at")
    lifetime = (expires_at - issued_at).total_seconds()
    maximum_lifetime = (
        MAX_CANDIDATE_RECEIPT_LIFETIME_SECONDS
        if channel == "verified-candidate"
        else MAX_RELEASE_RECEIPT_LIFETIME_SECONDS
    )
    if lifetime <= 0 or lifetime > maximum_lifetime:
        raise EvidenceError("release receipt validity window is invalid")
    current = now or dt.datetime.now(dt.UTC).replace(microsecond=0)
    if current < issued_at - dt.timedelta(minutes=5):
        raise EvidenceError("release receipt is not valid yet")
    if current >= expires_at:
        raise EvidenceError("release receipt is stale")

    pipeline_contract = PIPELINES[pipeline]
    build = _mapping(receipt["build"], label="release build")
    _exact_keys(
        build,
        {"project_arn", "build_id", "source_commit"},
        label="release build",
    )
    expected_project_arn = (
        f"arn:aws:codebuild:{REGION}:{ACCOUNT_ID}:project/"
        f"{pipeline_contract['build_project']}"
    )
    if build["project_arn"] != expected_project_arn:
        raise EvidenceError("release build project is not allowlisted")
    if not _BUILD_ID_RE.fullmatch(_string(build["build_id"], label="release build ID")):
        raise EvidenceError("release build ID is invalid")
    commit = _sha1(build["source_commit"], label="release source commit")
    if expected_commit is not None and commit != expected_commit:
        raise EvidenceError("release source commit mismatch")

    contract = _mapping(receipt["contract"], label="release contract")
    _exact_keys(contract, {"path", "sha256", "release_ready"}, label="release contract")
    if contract["path"] != pipeline_contract["contract_path"]:
        raise EvidenceError("release contract path is not allowlisted")
    contract_sha256 = _sha256(contract["sha256"], label="release contract SHA-256")
    if expected_contract_sha256 is not None and contract_sha256 != expected_contract_sha256:
        raise EvidenceError("release contract SHA-256 mismatch")
    if not _bool(contract["release_ready"], label="release contract ready"):
        raise EvidenceError("release contract is not ready")

    source_evidence = _mapping(receipt["source_evidence"], label="source evidence")
    _exact_keys(
        source_evidence,
        {
            "bucket",
            "key",
            "version_id",
            "sha256",
            "signature_key",
            "signature_version_id",
        },
        label="source evidence",
    )
    expected_source_bucket = (
        "teamagent-dev-openclaw-build-evidence"
        if pipeline == "openclaw"
        else EVIDENCE_BUCKET
    )
    if source_evidence["bucket"] != expected_source_bucket:
        raise EvidenceError("source evidence bucket is not allowlisted")
    key = _string(source_evidence["key"], label="source evidence key")
    allowed_prefix = {
        "mcp": f"source-declarations/mcp/{commit}/",
        "tiktok": f"source-manifests/tiktok/{commit}/",
        "openclaw": f"source-manifests/{commit}/",
    }[pipeline]
    if not key.startswith(allowed_prefix) or ".." in key or key.startswith("/"):
        raise EvidenceError("source evidence key is not allowlisted")
    _version_id(source_evidence["version_id"], label="source evidence VersionId")
    _sha256(source_evidence["sha256"], label="source evidence SHA-256")
    if source_evidence["signature_key"] != f"{key}.sig":
        raise EvidenceError("source evidence signature key mismatch")
    _version_id(
        source_evidence["signature_version_id"],
        label="source evidence signature VersionId",
    )

    subjects = receipt["subjects"]
    expected_subjects: Mapping[str, tuple[str, str, str]] = pipeline_contract["subjects"]
    if not isinstance(subjects, list) or len(subjects) != len(expected_subjects):
        raise EvidenceError("release receipt subject count mismatch")
    normalized_subjects: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, raw_subject in enumerate(subjects):
        label = f"release subject[{index}]"
        subject = _mapping(raw_subject, label=label)
        _exact_keys(
            subject,
            {
                "name",
                "quarantine_repository",
                "candidate_repository",
                "release_repository",
                "candidate_tag",
                "release_tag",
                "digest",
                "media_type",
                "config_digest",
                "platform",
                "labels",
                "binaries",
                "scan",
                "sbom",
                "provenance",
                "image_signature",
            },
            label=label,
        )
        name = _string(subject["name"], label=f"{label}.name")
        if name in seen_names or name not in expected_subjects:
            raise EvidenceError(f"{label}.name is duplicate or not allowlisted")
        seen_names.add(name)
        (
            quarantine_repository,
            candidate_repository,
            release_repository,
        ) = expected_subjects[name]
        if (
            subject["quarantine_repository"] != quarantine_repository
            or subject["candidate_repository"] != candidate_repository
            or subject["release_repository"] != release_repository
        ):
            raise EvidenceError(f"{label} repositories do not match the allowlist")
        candidate_tag = commit if pipeline == "tiktok" else f"candidate-{commit}"
        if pipeline != "tiktok" and len(expected_subjects) > 1:
            candidate_tag += f"-{name}"
        if subject["candidate_tag"] != candidate_tag:
            raise EvidenceError(f"{label}.candidate_tag must use the full source commit")
        expected_tag = _expected_tag(channel, commit, name, len(expected_subjects))
        if subject["release_tag"] != expected_tag:
            raise EvidenceError(f"{label}.release_tag is not canonical")
        digest = _digest(subject["digest"], label=f"{label}.digest")
        if subject["media_type"] not in SINGLE_ARM64_MEDIA_TYPES:
            raise EvidenceError(f"{label} must be a single scan-capable image manifest")
        _digest(subject["config_digest"], label=f"{label}.config_digest")
        platform = _mapping(subject["platform"], label=f"{label}.platform")
        _exact_keys(platform, {"os", "architecture"}, label=f"{label}.platform")
        if platform != {"os": "linux", "architecture": "arm64"}:
            raise EvidenceError(f"{label} must be linux/arm64")

        labels = _mapping(subject["labels"], label=f"{label}.labels")
        if not labels:
            raise EvidenceError(f"{label}.labels must not be empty")
        for label_name, label_value in labels.items():
            if not isinstance(label_name, str) or not _LABEL_RE.fullmatch(label_name):
                raise EvidenceError(f"{label}.labels contains an invalid OCI label")
            _string(label_value, label=f"{label}.labels[{label_name}]", maximum=8192)
        if labels.get("org.opencontainers.image.revision") != commit:
            raise EvidenceError(f"{label} OCI revision does not match the full commit")
        contract_label = pipeline_contract["contract_label"]
        if labels.get(contract_label) != contract_sha256:
            raise EvidenceError(f"{label} OCI contract hash does not match the receipt")

        binaries = subject["binaries"]
        if not isinstance(binaries, list) or not binaries:
            raise EvidenceError(f"{label}.binaries must contain actual-image probes")
        seen_paths: set[str] = set()
        for binary_index, raw_binary in enumerate(binaries):
            binary_label = f"{label}.binaries[{binary_index}]"
            binary = _mapping(raw_binary, label=binary_label)
            _exact_keys(binary, {"path", "sha256"}, label=binary_label)
            path = _string(binary["path"], label=f"{binary_label}.path", maximum=512)
            if not _PATH_RE.fullmatch(path) or ".." in path or path in seen_paths:
                raise EvidenceError(f"{binary_label}.path is unsafe or duplicate")
            seen_paths.add(path)
            _sha256(binary["sha256"], label=f"{binary_label}.sha256")

        scan = _mapping(subject["scan"], label=f"{label}.scan")
        _exact_keys(
            scan,
            {
                "scanner",
                "actual_image",
                "critical",
                "high",
                "secrets",
                "report_sha256",
            },
            label=f"{label}.scan",
        )
        if scan["scanner"] != "trivy":
            raise EvidenceError(f"{label}.scan must use Trivy")
        expected_image = f"{REGISTRY}/{quarantine_repository}@{digest}"
        if scan["actual_image"] != expected_image:
            raise EvidenceError(f"{label}.scan does not bind the quarantine digest")
        _zero(scan["critical"], label=f"{label}.scan.critical")
        _zero(scan["high"], label=f"{label}.scan.high")
        _zero(scan["secrets"], label=f"{label}.scan.secrets")
        _sha256(scan["report_sha256"], label=f"{label}.scan.report_sha256")

        _validate_referrer(
            subject["sbom"],
            label=f"{label}.sbom",
            kind="sbom",
            subject_digest=digest,
        )
        _validate_referrer(
            subject["provenance"],
            label=f"{label}.provenance",
            kind="provenance",
            subject_digest=digest,
        )
        _validate_signature(
            subject["image_signature"],
            label=f"{label}.image_signature",
            expected_subject_digest=digest,
        )
        normalized_subjects.append(dict(subject))

    if seen_names != set(expected_subjects):
        raise EvidenceError("release receipt is missing an allowlisted subject")
    if [subject["name"] for subject in normalized_subjects] != sorted(seen_names):
        raise EvidenceError("release receipt subjects must be sorted by name")
    return dict(receipt)


def release_receipt(
    *,
    pipeline: str,
    channel: str,
    issued_at: str,
    expires_at: str,
    build_project_arn: str,
    build_id: str,
    commit: str,
    contract_path: str,
    contract_sha256: str,
    source_bucket: str,
    source_key: str,
    source_version: str,
    source_sha256: str,
    source_signature_key: str,
    source_signature_version: str,
    subjects: list[Mapping[str, Any]],
) -> dict[str, Any]:
    value = {
        "schema_version": RELEASE_RECEIPT_SCHEMA,
        "kind": RELEASE_RECEIPT_KIND,
        "pipeline": pipeline,
        "channel": channel,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "build": {
            "project_arn": build_project_arn,
            "build_id": build_id,
            "source_commit": commit,
        },
        "contract": {
            "path": contract_path,
            "sha256": contract_sha256,
            "release_ready": True,
        },
        "source_evidence": {
            "bucket": source_bucket,
            "key": source_key,
            "version_id": source_version,
            "sha256": source_sha256,
            "signature_key": source_signature_key,
            "signature_version_id": source_signature_version,
        },
        "subjects": [dict(subject) for subject in subjects],
    }
    validate_release_receipt(
        value,
        expected_pipeline=pipeline,
        expected_commit=commit,
        expected_contract_sha256=contract_sha256,
        allowed_channels={channel},
        now=_timestamp(issued_at, label="issued_at"),
    )
    return value


def authorize_release_receipt(
    locator: Mapping[str, Any],
    *,
    channel: str,
    issued_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """Create fresh active/rollback evidence from a signed verified candidate.

    The caller must cryptographically verify the locator before invoking this
    function. The locator must still be within its signed candidate window at
    the new authorization time; the new receipt receives a separate short
    active/rollback window.
    """

    if channel not in {"active", "rollback"}:
        raise EvidenceError("authorization channel must be active or rollback")
    authorization_time = _timestamp(issued_at, label="issued_at")
    validated = validate_release_receipt(
        locator,
        allowed_channels={"verified-candidate"},
        now=authorization_time,
    )
    value = copy.deepcopy(validated)
    value["channel"] = channel
    value["issued_at"] = issued_at
    value["expires_at"] = expires_at
    commit = value["build"]["source_commit"]
    subject_count = len(value["subjects"])
    for subject in value["subjects"]:
        subject["release_tag"] = _expected_tag(
            channel,
            commit,
            subject["name"],
            subject_count,
        )
    validate_release_receipt(
        value,
        expected_pipeline=value["pipeline"],
        expected_commit=commit,
        expected_contract_sha256=value["contract"]["sha256"],
        allowed_channels={channel},
        now=_timestamp(issued_at, label="issued_at"),
    )
    return value


def validate_deploy_reference(
    receipt: Mapping[str, Any],
    *,
    pipeline: str,
    image: str,
    contract_sha256: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    validated = validate_release_receipt(
        receipt,
        expected_pipeline=pipeline,
        expected_contract_sha256=contract_sha256,
        allowed_channels={"active", "rollback"},
        now=now,
    )
    matches = [
        subject
        for subject in validated["subjects"]
        if image == f"{REGISTRY}/{subject['release_repository']}@{subject['digest']}"
    ]
    if len(matches) != 1:
        raise EvidenceError(f"{pipeline} image does not match signed release evidence")
    return validated


def validate_lifecycle_preview(
    value: Any,
    *,
    protected_digests: set[str],
) -> None:
    preview = _mapping(value, label="ECR lifecycle preview")
    if preview.get("nextToken") not in {None, ""}:
        raise EvidenceError("ECR lifecycle preview is truncated")
    results = preview.get("lifecyclePolicyPreviewResults")
    if not isinstance(results, list):
        raise EvidenceError("ECR lifecycle preview results are missing")
    normalized_protected = {
        _digest(item, label="protected digest") for item in protected_digests
    }
    expiring: set[str] = set()
    for index, raw_result in enumerate(results):
        result = _mapping(raw_result, label=f"lifecycle result[{index}]")
        action = _mapping(result.get("action"), label=f"lifecycle result[{index}].action")
        if action.get("type") != "EXPIRE":
            continue
        expiring.add(_digest(result.get("imageDigest"), label="preview image digest"))
    unsafe = sorted(expiring & normalized_protected)
    if unsafe:
        raise EvidenceError(f"lifecycle preview expires active/rollback digests: {unsafe}")


def _write_json(path: Path, value: Any) -> None:
    try:
        path.write_bytes(canonical_bytes(value))
    except OSError as exc:
        raise EvidenceError(f"cannot write evidence: {exc}") from exc


def _aws(*args: str, output: Path | None = None) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
            "AWS_DEFAULT_REGION": REGION,
            "AWS_REGION": REGION,
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_PAGER": "",
        }
    )
    for key in tuple(environment):
        if key.startswith("AWS_ENDPOINT_URL") or key in {"AWS_PROFILE", "AWS_DEFAULT_PROFILE"}:
            environment.pop(key, None)
    command = ["aws", *args]
    if output is not None:
        command.append(str(output))
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError("AWS evidence verification failed without exposing details") from exc
    return completed.stdout


def _aws_json(label: str, *args: str) -> Mapping[str, Any]:
    try:
        value = json.loads(_aws(*args), object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid {label} response") from exc
    return _mapping(value, label=label)


def _release_referrers(
    repository: str,
    subject_digest: str,
    *,
    label: str,
) -> list[Mapping[str, Any]]:
    response = _aws_json(
        f"{label} referrers",
        "ecr",
        "list-image-referrers",
        "--region",
        REGION,
        "--registry-id",
        ACCOUNT_ID,
        "--repository-name",
        repository,
        "--subject-id",
        f"imageDigest={subject_digest}",
        "--max-results",
        "50",
        "--output",
        "json",
    )
    if response.get("nextToken") not in {None, ""}:
        raise EvidenceError(f"{label} referrers are truncated")
    referrers = response.get("referrers")
    if not isinstance(referrers, list):
        raise EvidenceError(f"{label} referrers are missing")
    normalized: list[Mapping[str, Any]] = []
    for index, raw_referrer in enumerate(referrers):
        referrer = _mapping(raw_referrer, label=f"{label} referrer[{index}]")
        _digest(referrer.get("digest"), label=f"{label} referrer[{index}].digest")
        if referrer.get("artifactStatus") != "ACTIVE":
            raise EvidenceError(f"{label} referrer[{index}] is not ACTIVE")
        normalized.append(referrer)
    return normalized


def _validate_promoted_release(receipt: Mapping[str, Any]) -> None:
    """Prove promotion completed for every subject before Terraform can deploy."""

    for subject in receipt["subjects"]:
        repository = subject["release_repository"]
        digest = subject["digest"]
        label = f"{receipt['pipeline']}/{subject['name']} release"
        response = _aws_json(
            f"{label} image",
            "ecr",
            "batch-get-image",
            "--region",
            REGION,
            "--registry-id",
            ACCOUNT_ID,
            "--repository-name",
            repository,
            "--image-ids",
            f"imageDigest={digest}",
            "--accepted-media-types",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "--output",
            "json",
        )
        images = response.get("images")
        failures = response.get("failures")
        if (
            not isinstance(images, list)
            or len(images) != 1
            or not isinstance(failures, list)
            or failures
        ):
            raise EvidenceError(f"{label} exact digest is not present")
        image = _mapping(images[0], label=f"{label} image")
        image_id = _mapping(image.get("imageId"), label=f"{label} image ID")
        if (
            image.get("registryId") != ACCOUNT_ID
            or image.get("repositoryName") != repository
            or image_id.get("imageDigest") != digest
            or image.get("imageManifestMediaType") != subject["media_type"]
            or image.get("imageManifestMediaType") not in SINGLE_ARM64_MEDIA_TYPES
        ):
            raise EvidenceError(f"{label} digest or single-manifest type mismatch")

        referrers = _release_referrers(repository, digest, label=label)
        expected_artifacts = (
            ("SBOM", subject["sbom"]),
            ("provenance", subject["provenance"]),
        )
        for artifact_label, artifact in expected_artifacts:
            matches = [
                referrer
                for referrer in referrers
                if (
                    referrer.get("digest") == artifact["digest"]
                    and referrer.get("artifactType") == artifact["artifact_type"]
                    and isinstance(referrer.get("annotations"), dict)
                    and referrer["annotations"].get(
                        "io.teamagent.build.payload-sha256"
                    )
                    == artifact["payload_sha256"]
                )
            ]
            if len(matches) != 1:
                raise EvidenceError(
                    f"{label} exact {artifact_label} referrer is missing or ambiguous"
                )
            artifact_signatures = _release_referrers(
                repository,
                artifact["digest"],
                label=f"{label} {artifact_label}",
            )
            exact_artifact_signatures = [
                referrer
                for referrer in artifact_signatures
                if (
                    referrer.get("digest") == artifact["signature"]["referrer_digest"]
                    and referrer.get("artifactType") in SIGNATURE_ARTIFACT_TYPES
                )
            ]
            if len(exact_artifact_signatures) != 1:
                raise EvidenceError(
                    f"{label} exact {artifact_label} signature is missing or ambiguous"
                )
        exact_image_signatures = [
            referrer
            for referrer in referrers
            if (
                referrer.get("digest")
                == subject["image_signature"]["referrer_digest"]
                and referrer.get("artifactType") in SIGNATURE_ARTIFACT_TYPES
            )
        ]
        if len(exact_image_signatures) != 1:
            raise EvidenceError(f"{label} exact image signature is missing or ambiguous")


def _terraform_gate(query: Mapping[str, Any]) -> dict[str, str]:
    _exact_keys(
        query,
        {
            "images_json",
            "evidence_json",
            "contracts_json",
            "contract_ready_json",
            "signing_key_arn",
            "encryption_key_arn",
        },
        label="Terraform gate query",
    )
    try:
        images = json.loads(_string(query["images_json"], label="images_json"))
        evidence = json.loads(_string(query["evidence_json"], label="evidence_json"))
        contracts = json.loads(_string(query["contracts_json"], label="contracts_json"))
        ready = json.loads(_string(query["contract_ready_json"], label="contract_ready_json"))
    except json.JSONDecodeError as exc:
        raise EvidenceError("Terraform gate query contains invalid JSON") from exc
    if not all(isinstance(item, dict) for item in (images, evidence, contracts, ready)):
        raise EvidenceError("Terraform gate query maps are invalid")
    signing_key_arn = _string(query["signing_key_arn"], label="signing key ARN")
    encryption_key_arn = _string(query["encryption_key_arn"], label="encryption key ARN")
    if not _KEY_ARN_RE.fullmatch(signing_key_arn) or not _KEY_ARN_RE.fullmatch(
        encryption_key_arn
    ):
        raise EvidenceError("Terraform gate KMS key is outside the fixed account")

    selected = {name: image for name, image in images.items() if image}
    if not selected:
        return {"verified": "true", "verified_pipelines": ""}
    if set(selected) - PIPELINES.keys():
        raise EvidenceError("Terraform selected an unknown image pipeline")

    verified: list[str] = []
    with tempfile.TemporaryDirectory(prefix="teamagent-release-gate.") as temporary:
        root = Path(temporary)
        for pipeline, image in sorted(selected.items()):
            if ready.get(pipeline) is not True:
                raise EvidenceError(f"{pipeline} release.ready is false")
            contract_sha256 = _sha256(
                contracts.get(pipeline),
                label=f"{pipeline} contract SHA-256",
            )
            reference = _mapping(evidence.get(pipeline), label=f"{pipeline} evidence reference")
            _exact_keys(
                reference,
                {
                    "bucket",
                    "key",
                    "version_id",
                    "signature_key",
                    "signature_version_id",
                },
                label=f"{pipeline} evidence reference",
            )
            if reference["bucket"] != EVIDENCE_BUCKET:
                raise EvidenceError("release evidence bucket is not fixed")
            key = _string(reference["key"], label="release evidence key")
            signature_key = _string(
                reference["signature_key"], label="release signature key"
            )
            expected_key_pattern = re.compile(
                rf"release-receipts/{re.escape(pipeline)}/"
                r"([0-9a-f]{40})/([0-9a-f]{64})\.json"
            )
            key_match = expected_key_pattern.fullmatch(key)
            if (
                key_match is None
                or signature_key != f"{key}.sig"
                or ".." in key
            ):
                raise EvidenceError("release evidence key is not content-addressed")
            version_id = _version_id(reference["version_id"], label="release VersionId")
            signature_version_id = _version_id(
                reference["signature_version_id"],
                label="release signature VersionId",
            )
            receipt_path = root / f"{pipeline}.json"
            signature_path = root / f"{pipeline}.sig"
            for object_key, object_version, destination, label in (
                (key, version_id, receipt_path, "receipt"),
                (
                    signature_key,
                    signature_version_id,
                    signature_path,
                    "signature",
                ),
            ):
                head_raw = _aws(
                    "s3api",
                    "head-object",
                    "--region",
                    REGION,
                    "--bucket",
                    EVIDENCE_BUCKET,
                    "--key",
                    object_key,
                    "--version-id",
                    object_version,
                    "--expected-bucket-owner",
                    ACCOUNT_ID,
                    "--output",
                    "json",
                )
                try:
                    head = json.loads(head_raw)
                except json.JSONDecodeError as exc:
                    raise EvidenceError(f"invalid {label} object metadata") from exc
                if (
                    head.get("VersionId") != object_version
                    or head.get("ObjectLockMode") != "COMPLIANCE"
                    or head.get("ServerSideEncryption") != "aws:kms"
                    or head.get("SSEKMSKeyId") != encryption_key_arn
                ):
                    raise EvidenceError(f"{label} object is not immutable exact evidence")
                retained = _metadata_timestamp(
                    head.get("ObjectLockRetainUntilDate", ""),
                    label=f"{label} retention",
                )
                if retained <= dt.datetime.now(dt.UTC):
                    raise EvidenceError(f"{label} evidence retention has expired")
                response = _aws(
                    "s3api",
                    "get-object",
                    "--region",
                    REGION,
                    "--bucket",
                    EVIDENCE_BUCKET,
                    "--key",
                    object_key,
                    "--version-id",
                    object_version,
                    "--expected-bucket-owner",
                    ACCOUNT_ID,
                    "--query",
                    "VersionId",
                    "--output",
                    "text",
                    output=destination,
                ).strip()
                if response != object_version:
                    raise EvidenceError(f"{label} download VersionId mismatch")

            assert key_match is not None
            if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != key_match.group(2):
                raise EvidenceError("release receipt bytes do not match the content key")
            digest_path = root / f"{pipeline}.sha256"
            digest_path.write_bytes(hashlib.sha256(receipt_path.read_bytes()).digest())
            verify_raw = _aws(
                "kms",
                "verify",
                "--region",
                REGION,
                "--key-id",
                signing_key_arn,
                "--message-type",
                "DIGEST",
                "--message",
                f"fileb://{digest_path}",
                "--signature",
                f"fileb://{signature_path}",
                "--signing-algorithm",
                "RSASSA_PSS_SHA_256",
                "--output",
                "json",
            )
            try:
                signature_valid = json.loads(verify_raw).get("SignatureValid")
            except json.JSONDecodeError as exc:
                raise EvidenceError("invalid KMS verification response") from exc
            if signature_valid is not True:
                raise EvidenceError("release evidence KMS signature is invalid")
            receipt = load_json(receipt_path, label=f"{pipeline} release receipt")
            if (
                not isinstance(receipt, dict)
                or not isinstance(receipt.get("build"), dict)
                or receipt["build"].get("source_commit") != key_match.group(1)
            ):
                raise EvidenceError("release receipt commit does not match its content key")
            validated_receipt = validate_deploy_reference(
                receipt,
                pipeline=pipeline,
                image=_string(image, label=f"{pipeline} image"),
                contract_sha256=contract_sha256,
            )
            _validate_promoted_release(validated_receipt)
            verified.append(pipeline)
    return {"verified": "true", "verified_pipelines": ",".join(verified)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    source = commands.add_parser("create-source-declaration")
    source.add_argument("--project-arn", required=True)
    source.add_argument("--build-id", required=True)
    source.add_argument("--commit", required=True)
    source.add_argument("--source-version", required=True)
    source.add_argument("--source-sha256", required=True)
    source.add_argument("--manifest-sha256", required=True)
    source.add_argument("--app-version", required=True)
    source.add_argument("--app-sha256", required=True)
    source.add_argument("--vault-manifest-sha256", required=True)
    source.add_argument("--build-inputs-sha256", required=True)
    source.add_argument("--contract-sha256", required=True)
    source.add_argument("--output", type=Path, required=True)

    verify_source = commands.add_parser("verify-source-declaration")
    verify_source.add_argument("--declaration", type=Path, required=True)
    verify_source.add_argument("--expected-commit", required=True)
    verify_source.add_argument("--expected-source-version", required=True)
    verify_source.add_argument("--expected-app-version", required=True)
    verify_source.add_argument("--expected-app-sha256", required=True)
    verify_source.add_argument("--expected-vault-manifest-sha256", required=True)
    verify_source.add_argument("--expected-build-inputs-sha256", required=True)
    verify_source.add_argument("--expected-contract-sha256", required=True)

    receipt = commands.add_parser("verify-release-receipt")
    receipt.add_argument("--receipt", type=Path, required=True)
    receipt.add_argument("--expected-pipeline", choices=sorted(PIPELINES), required=True)
    receipt.add_argument("--expected-commit", required=True)
    receipt.add_argument("--expected-contract-sha256", required=True)
    receipt.add_argument(
        "--allowed-channel",
        action="append",
        choices=("verified-candidate", "active", "rollback"),
        required=True,
    )
    receipt.add_argument("--now")

    locator = commands.add_parser("verify-release-locator")
    locator.add_argument("--receipt", type=Path, required=True)
    locator.add_argument("--expected-pipeline", choices=sorted(PIPELINES), required=True)
    locator.add_argument("--expected-contract-sha256", required=True)

    create_receipt = commands.add_parser("create-release-receipt")
    create_receipt.add_argument("--pipeline", choices=sorted(PIPELINES), required=True)
    create_receipt.add_argument(
        "--channel",
        choices=("verified-candidate", "active", "rollback"),
        required=True,
    )
    create_receipt.add_argument("--issued-at", required=True)
    create_receipt.add_argument("--expires-at", required=True)
    create_receipt.add_argument("--build-project-arn", required=True)
    create_receipt.add_argument("--build-id", required=True)
    create_receipt.add_argument("--commit", required=True)
    create_receipt.add_argument("--contract-path", required=True)
    create_receipt.add_argument("--contract-sha256", required=True)
    create_receipt.add_argument("--source-bucket", required=True)
    create_receipt.add_argument("--source-key", required=True)
    create_receipt.add_argument("--source-version", required=True)
    create_receipt.add_argument("--source-sha256", required=True)
    create_receipt.add_argument("--source-signature-key", required=True)
    create_receipt.add_argument("--source-signature-version", required=True)
    create_receipt.add_argument("--subject", action="append", type=Path, required=True)
    create_receipt.add_argument("--output", type=Path, required=True)

    authorize_receipt = commands.add_parser("authorize-release-receipt")
    authorize_receipt.add_argument("--locator", type=Path, required=True)
    authorize_receipt.add_argument(
        "--channel",
        choices=("active", "rollback"),
        required=True,
    )
    authorize_receipt.add_argument("--issued-at", required=True)
    authorize_receipt.add_argument("--expires-at", required=True)
    authorize_receipt.add_argument("--output", type=Path, required=True)

    lifecycle = commands.add_parser("verify-lifecycle-preview")
    lifecycle.add_argument("--preview", type=Path, required=True)
    lifecycle.add_argument("--protected-digest", action="append", required=True)

    commands.add_parser("terraform-gate")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create-source-declaration":
            value = source_declaration(
                project_arn=args.project_arn,
                build_id=args.build_id,
                commit=args.commit,
                source_version=args.source_version,
                source_sha256=args.source_sha256,
                manifest_sha256=args.manifest_sha256,
                app_version=args.app_version,
                app_sha256=args.app_sha256,
                vault_manifest_sha256=args.vault_manifest_sha256,
                build_inputs_sha256=args.build_inputs_sha256,
                contract_sha256=args.contract_sha256,
            )
            _write_json(args.output, value)
        elif args.command == "verify-source-declaration":
            validate_source_declaration(
                load_json(args.declaration, label="source declaration"),
                expected_commit=args.expected_commit,
                expected_source_version=args.expected_source_version,
                expected_app_version=args.expected_app_version,
                expected_app_sha256=args.expected_app_sha256,
                expected_vault_manifest_sha256=args.expected_vault_manifest_sha256,
                expected_build_inputs_sha256=args.expected_build_inputs_sha256,
                expected_contract_sha256=args.expected_contract_sha256,
            )
        elif args.command == "verify-release-receipt":
            now = _timestamp(args.now, label="now") if args.now else None
            validate_release_receipt(
                load_json(args.receipt, label="release receipt"),
                expected_pipeline=args.expected_pipeline,
                expected_commit=args.expected_commit,
                expected_contract_sha256=args.expected_contract_sha256,
                allowed_channels=set(args.allowed_channel),
                now=now,
            )
        elif args.command == "verify-release-locator":
            locator_receipt = load_json(args.receipt, label="release locator receipt")
            validate_release_receipt(
                locator_receipt,
                expected_pipeline=args.expected_pipeline,
                expected_contract_sha256=args.expected_contract_sha256,
                allowed_channels={"verified-candidate"},
            )
        elif args.command == "create-release-receipt":
            subjects = [
                _mapping(load_json(path, label="release subject"), label="release subject")
                for path in args.subject
            ]
            value = release_receipt(
                pipeline=args.pipeline,
                channel=args.channel,
                issued_at=args.issued_at,
                expires_at=args.expires_at,
                build_project_arn=args.build_project_arn,
                build_id=args.build_id,
                commit=args.commit,
                contract_path=args.contract_path,
                contract_sha256=args.contract_sha256,
                source_bucket=args.source_bucket,
                source_key=args.source_key,
                source_version=args.source_version,
                source_sha256=args.source_sha256,
                source_signature_key=args.source_signature_key,
                source_signature_version=args.source_signature_version,
                subjects=subjects,
            )
            _write_json(args.output, value)
        elif args.command == "authorize-release-receipt":
            value = authorize_release_receipt(
                _mapping(
                    load_json(args.locator, label="verified-candidate locator"),
                    label="verified-candidate locator",
                ),
                channel=args.channel,
                issued_at=args.issued_at,
                expires_at=args.expires_at,
            )
            _write_json(args.output, value)
        elif args.command == "verify-lifecycle-preview":
            validate_lifecycle_preview(
                load_json(args.preview, label="lifecycle preview"),
                protected_digests=set(args.protected_digest),
            )
        elif args.command == "terraform-gate":
            query = json.load(sys.stdin, object_pairs_hook=_reject_duplicate_keys)
            print(json.dumps(_terraform_gate(query), sort_keys=True))
        else:
            raise EvidenceError("unsupported command")
    except (EvidenceError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
