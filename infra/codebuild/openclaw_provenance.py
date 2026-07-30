#!/usr/bin/env python3
"""Fail-closed OpenClaw source, core/media bundle, and OCI-referrer contract."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

CONTRACT_SCHEMA_VERSION = 1
SOURCE_MANIFEST_SCHEMA_VERSION = 1
RELEASE_EVIDENCE_SCHEMA_VERSION = 1
_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REPOSITORY_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*")

# The one place that decides which bundle subjects exist.  Both the count check
# and the exact repository mapping read from here, so re-adding a subject is a
# single edit and the two checks can never disagree.
#
# The media subject was declared in the contract but never built: no Dockerfile,
# no image in any of its three ECR repositories, no reference anywhere in the
# tree, and docs/openclaw/deploy_runbook.md recorded it as "未統合".  It was
# removed from the required set rather than satisfied with an empty image,
# because an image built only to make a contract check pass is a receipt for
# nothing.  The three media repositories are intentionally left in place so
# re-adding the subject stays a code-only change.
_EXPECTED_BUNDLE_SUBJECTS: list[dict[str, str]] = [
    {
        "name": "core",
        "quarantine_repository": "teamagent-openclaw-quarantine",
        "candidate_repository": "teamagent-openclaw-verified-candidates",
        "release_repository": "teamagent-openclaw",
    },
]
_ARTIFACT_TYPE_RE = re.compile(r"application/[A-Za-z0-9.+_-]{1,200}")
_S3_VERSION_ID_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
_BUILD_ID_RE = re.compile(
    r"teamagent-dev-openclaw-provenance-builder:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class ContractError(ValueError):
    """OpenClaw provenance input is malformed or fails an exact gate."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads(raw: str, *, label: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid {label} JSON: {exc}") from exc


def _load(path: Path, *, label: str) -> Any:
    try:
        return _loads(path.read_text(encoding="utf-8"), label=label)
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {path}: {exc}") from exc


def _exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        raise ContractError(f"{label} schema mismatch: missing={missing}; unknown={unknown}")


def _text(value: Any, *, label: str, minimum: int = 1) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) < minimum:
        raise ContractError(f"{label} must be a trimmed non-blank string")
    if len(value) > 2048 or any(ord(character) < 32 for character in value):
        raise ContractError(f"{label} contains unsupported characters")
    return value


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractError(f"{label} must be a lowercase SHA-256")
    return value


def _sha1(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA1_RE.fullmatch(value):
        raise ContractError(f"{label} must be a lowercase full Git SHA-1")
    return value


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ContractError(f"{label} must be a lowercase sha256 OCI digest")
    return value


def _version_id(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or value in {"None", "null"}
        or not _S3_VERSION_ID_RE.fullmatch(value)
    ):
        raise ContractError(f"{label} must be a usable S3 VersionId")
    return value


def _safe_s3_key(value: Any, *, label: str) -> str:
    value = _text(value, label=label)
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or "." in parsed.parts or ".." in parsed.parts or "\\" in value:
        raise ContractError(f"{label} is not a safe S3 key")
    return value


def validate_contract(value: Any, *, label: str = "OpenClaw bundle contract") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    _exact_keys(
        value,
        {"schema_version", "release", "source", "evidence", "bundle", "tooling"},
        label=label,
    )
    if value["schema_version"] != CONTRACT_SCHEMA_VERSION:
        raise ContractError(f"unsupported {label} schema")

    release = value["release"]
    source = value["source"]
    evidence = value["evidence"]
    bundle = value["bundle"]
    tooling = value["tooling"]
    contract_sections = (
        (release, "release"),
        (source, "source"),
        (evidence, "evidence"),
        (bundle, "bundle"),
        (tooling, "tooling"),
    )
    for item, name in contract_sections:
        if not isinstance(item, dict):
            raise ContractError(f"{label} {name} must be an object")
    _exact_keys(release, {"ready", "blocked_reason"}, label=f"{label} release")
    _exact_keys(source, {"repository", "branch"}, label=f"{label} source")
    _exact_keys(
        evidence,
        {
            "bucket",
            "source_manifest_prefix",
            "release_evidence_prefix",
            "signing_kms_key_alias",
            "encryption_kms_key_alias",
            "object_lock_mode",
            "retention_days",
        },
        label=f"{label} evidence",
    )
    _exact_keys(
        bundle,
        {
            "schema_version",
            "interfaces",
            "contract_oci_label",
            "arm64_subject_media_type",
            "subjects",
            "required_referrers",
            "signature_artifact_type",
            "scan_gate",
        },
        label=f"{label} bundle",
    )
    _exact_keys(
        tooling,
        {"trivy_db_repository", "trivy_java_db_repository"},
        label=f"{label} tooling",
    )
    if not isinstance(release["ready"], bool):
        raise ContractError(f"{label} release.ready must be a boolean")
    blocked_reason = release["blocked_reason"]
    if not isinstance(blocked_reason, str) or blocked_reason != blocked_reason.strip():
        raise ContractError(f"{label} release.blocked_reason must be trimmed")
    if release["ready"] and blocked_reason:
        raise ContractError(f"{label} ready release cannot have a blocked_reason")
    if not release["ready"]:
        _text(blocked_reason, label=f"{label} release.blocked_reason", minimum=20)

    repository = _text(source["repository"], label=f"{label} source.repository")
    if repository != "https://github.com/noirelumiere00/TeamAgent.git":
        raise ContractError(f"{label} source repository is not the fixed TeamAgent origin")
    if source["branch"] != "dev":
        raise ContractError(f"{label} source branch must be dev")
    expected_evidence = {
        "bucket": "teamagent-dev-openclaw-build-evidence",
        "source_manifest_prefix": "source-manifests",
        "release_evidence_prefix": "release-evidence",
        "signing_kms_key_alias": "alias/teamagent-dev-openclaw-build-publisher",
        "encryption_kms_key_alias": "alias/teamagent-dev-openclaw-build-evidence",
        "object_lock_mode": "GOVERNANCE",
        "retention_days": 3650,
    }
    if evidence != expected_evidence:
        raise ContractError(f"{label} immutable evidence settings are not fixed")

    if bundle["schema_version"] != 1:
        raise ContractError(f"{label} bundle schema must be 1")
    expected_interfaces = {
        "build": "infra/openclaw/build-bundle.sh",
        "attest": "infra/codebuild/verify_actual_image.sh",
        "promote": "infra/codebuild/image-promoter-buildspec.yml",
    }
    if bundle["interfaces"] != expected_interfaces:
        raise ContractError(f"{label} core/media helper interfaces are not fixed")
    if bundle["contract_oci_label"] != "io.teamagent.build.contract-sha256":
        raise ContractError(f"{label} contract OCI label is not fixed")
    if bundle["arm64_subject_media_type"] != "application/vnd.oci.image.manifest.v1+json":
        raise ContractError(f"{label} arm64 subject must be a single OCI image manifest")
    subjects = bundle["subjects"]
    if not isinstance(subjects, list) or len(subjects) != len(_EXPECTED_BUNDLE_SUBJECTS):
        raise ContractError(
            f"{label} bundle must declare exactly "
            + " and ".join(subject["name"] for subject in _EXPECTED_BUNDLE_SUBJECTS)
        )
    normalized_subjects: list[dict[str, Any]] = []
    for index, subject in enumerate(subjects):
        subject_label = f"{label} bundle.subjects[{index}]"
        if not isinstance(subject, dict):
            raise ContractError(f"{subject_label} must be an object")
        _exact_keys(
            subject,
            {
                "name",
                "quarantine_repository",
                "candidate_repository",
                "release_repository",
                "binary_probes",
            },
            label=subject_label,
        )
        normalized = {
            key: _text(subject[key], label=f"{subject_label}.{key}")
            for key in (
                "name",
                "quarantine_repository",
                "candidate_repository",
                "release_repository",
            )
        }
        probes = subject["binary_probes"]
        if not isinstance(probes, list):
            raise ContractError(f"{subject_label}.binary_probes must be an array")
        normalized_probes: list[dict[str, str]] = []
        seen_paths: set[str] = set()
        for probe_index, probe in enumerate(probes):
            probe_label = f"{subject_label}.binary_probes[{probe_index}]"
            if not isinstance(probe, dict):
                raise ContractError(f"{probe_label} must be an object")
            _exact_keys(probe, {"path", "sha256"}, label=probe_label)
            path = _text(probe["path"], label=f"{probe_label}.path")
            sha256 = _text(probe["sha256"], label=f"{probe_label}.sha256")
            if (
                not path.startswith("/")
                or ".." in path.split("/")
                or path in seen_paths
                or not _SHA256_RE.fullmatch(sha256)
            ):
                raise ContractError(f"{probe_label} is unsafe or invalid")
            seen_paths.add(path)
            normalized_probes.append({"path": path, "sha256": sha256})
        if normalized_probes != sorted(normalized_probes, key=lambda item: item["path"]):
            raise ContractError(f"{subject_label}.binary_probes must be sorted by path")
        if release["ready"] and not normalized_probes:
            raise ContractError(f"{subject_label}.binary_probes is required for release")
        normalized["binary_probes"] = normalized_probes
        for repository_name in (
            normalized["quarantine_repository"],
            normalized["release_repository"],
        ):
            if not _REPOSITORY_RE.fullmatch(repository_name) or "mcp" in repository_name:
                raise ContractError(f"{subject_label} cannot reference an MCP repository")
        normalized_subjects.append(normalized)
    expected_subjects = _EXPECTED_BUNDLE_SUBJECTS
    repository_mappings = [
        {
            key: subject[key]
            for key in (
                "name",
                "quarantine_repository",
                "candidate_repository",
                "release_repository",
            )
        }
        for subject in normalized_subjects
    ]
    if repository_mappings != expected_subjects:
        raise ContractError(f"{label} core/media repository mapping is not exact")

    required_referrers = bundle["required_referrers"]
    if not isinstance(required_referrers, list) or len(required_referrers) != 2:
        raise ContractError(f"{label} must require exactly provenance and SBOM referrers")
    normalized_referrers: list[dict[str, Any]] = []
    for index, referrer in enumerate(required_referrers):
        referrer_label = f"{label} bundle.required_referrers[{index}]"
        if not isinstance(referrer, dict):
            raise ContractError(f"{referrer_label} must be an object")
        _exact_keys(
            referrer,
            {"name", "artifact_type", "minimum", "signature_required"},
            label=referrer_label,
        )
        name = _text(referrer["name"], label=f"{referrer_label}.name")
        artifact_type = _text(referrer["artifact_type"], label=f"{referrer_label}.artifact_type")
        if not _ARTIFACT_TYPE_RE.fullmatch(artifact_type):
            raise ContractError(f"{referrer_label}.artifact_type is invalid")
        if referrer["minimum"] != 1 or referrer["signature_required"] is not True:
            raise ContractError(f"{referrer_label} must require at least one signed artifact")
        normalized_referrers.append(
            {
                "name": name,
                "artifact_type": artifact_type,
                "minimum": 1,
                "signature_required": True,
            }
        )
    expected_referrers = [
        {
            "name": "provenance",
            "artifact_type": "application/vnd.in-toto+json",
            "minimum": 1,
            "signature_required": True,
        },
        {
            "name": "sbom",
            "artifact_type": "application/spdx+json",
            "minimum": 1,
            "signature_required": True,
        },
    ]
    if normalized_referrers != expected_referrers:
        raise ContractError(f"{label} referrer allowlist is not exact")
    signature_type = bundle["signature_artifact_type"]
    if signature_type != "application/vnd.dev.cosign.simplesigning.v1+json":
        raise ContractError(f"{label} signature artifact type is not fixed")
    scan_gate = bundle["scan_gate"]
    if not isinstance(scan_gate, dict):
        raise ContractError(f"{label} scan_gate must be an object")
    _exact_keys(scan_gate, {"critical", "high"}, label=f"{label} scan_gate")
    if scan_gate != {"critical": 0, "high": 0}:
        raise ContractError(f"{label} scan gate must be CRITICAL=0/HIGH=0")
    if tooling != {
        "trivy_db_repository": "public.ecr.aws/aquasecurity/trivy-db:2",
        "trivy_java_db_repository": "public.ecr.aws/aquasecurity/trivy-java-db:1",
    }:
        raise ContractError(f"{label} Trivy DB repositories are not fixed")
    return {
        "schema_version": 1,
        "release": {"ready": release["ready"], "blocked_reason": blocked_reason},
        "source": dict(source),
        "evidence": dict(evidence),
        "bundle": {
            "schema_version": 1,
            "interfaces": dict(bundle["interfaces"]),
            "contract_oci_label": bundle["contract_oci_label"],
            "arm64_subject_media_type": bundle["arm64_subject_media_type"],
            "subjects": normalized_subjects,
            "required_referrers": normalized_referrers,
            "signature_artifact_type": signature_type,
            "scan_gate": {"critical": 0, "high": 0},
        },
        "tooling": dict(tooling),
    }


def load_contract(path: Path) -> dict[str, Any]:
    return validate_contract(_load(path, label="OpenClaw bundle contract"))


def contract_sha256(path: Path) -> str:
    contract = load_contract(path)
    del contract
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ContractError(f"cannot hash OpenClaw bundle contract: {exc}") from exc


def require_release_ready(contract: dict[str, Any]) -> None:
    if not contract["release"]["ready"]:
        raise ContractError(
            "OpenClaw core/media release is blocked: " + contract["release"]["blocked_reason"]
        )


def _git(repo_root: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractError(f"git {' '.join(arguments)} failed") from exc


def _git_object_id(kind: str, payload: bytes) -> str:
    framed = f"{kind} {len(payload)}\0".encode() + payload
    # SHA-1 is part of this repository's Git object format, not a security hash choice.
    return hashlib.sha1(framed).hexdigest()


def _tree_inventory(repo_root: Path, commit: str) -> tuple[int, list[str]]:
    raw = _git(repo_root, "ls-tree", "-r", "-z", "--full-tree", commit)
    file_count = 0
    executable_paths: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, _object_id = metadata.split(b" ", 2)
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContractError("unsupported Git tree entry") from exc
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ContractError(f"unsupported Git tree entry: {path}")
        if not path or path.startswith("/") or ".." in Path(path).parts or "\\" in path:
            raise ContractError(f"unsafe Git path: {path!r}")
        file_count += 1
        if mode == b"100755":
            executable_paths.append(path)
    return file_count, sorted(executable_paths, key=lambda item: item.encode())


def source_manifest(repo_root: Path, commit: str, contract_path: Path) -> dict[str, Any]:
    contract = load_contract(contract_path)
    if not _SHA1_RE.fullmatch(commit):
        raise ContractError("source commit must be a full lowercase SHA-1")
    resolved = _git(repo_root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved != commit:
        raise ContractError("source commit did not resolve exactly")
    if _git(repo_root, "rev-parse", "--show-object-format").decode().strip() != "sha1":
        raise ContractError("only Git SHA-1 object repositories are supported")
    commit_object = _git(repo_root, "cat-file", "commit", commit)
    if _git_object_id("commit", commit_object) != commit:
        raise ContractError("Git commit object does not hash to the requested commit")
    tree = _git(repo_root, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    if not _SHA1_RE.fullmatch(tree):
        raise ContractError("Git tree ID is invalid")
    file_count, executable_paths = _tree_inventory(repo_root, commit)
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "source": {
            "repository": contract["source"]["repository"],
            "branch": contract["source"]["branch"],
            "commit": commit,
            "tree": tree,
            "git_object_format": "sha1",
            "file_count": file_count,
            "executable_paths": executable_paths,
            "commit_object_base64": base64.b64encode(commit_object).decode("ascii"),
        },
        "bundle_contract_sha256": contract_sha256(contract_path),
    }


def write_source_manifest(repo_root: Path, commit: str, contract_path: Path, output: Path) -> None:
    manifest = source_manifest(repo_root, commit, contract_path)
    output.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def validate_source_manifest(
    value: Any,
    contract: dict[str, Any],
    expected_contract_sha256: str,
) -> dict[str, Any]:
    """Validate the signed source statement independently of the local checkout."""

    if not isinstance(value, dict):
        raise ContractError("signed source manifest must be an object")
    _exact_keys(
        value,
        {"schema_version", "source", "bundle_contract_sha256"},
        label="signed source manifest",
    )
    if value["schema_version"] != SOURCE_MANIFEST_SCHEMA_VERSION:
        raise ContractError("signed source manifest schema is unsupported")
    expected_contract_sha256 = _sha256(
        expected_contract_sha256,
        label="expected OpenClaw bundle contract SHA-256",
    )
    if value["bundle_contract_sha256"] != expected_contract_sha256:
        raise ContractError("signed source manifest contract SHA-256 mismatch")
    source = value["source"]
    if not isinstance(source, dict):
        raise ContractError("signed source manifest source must be an object")
    _exact_keys(
        source,
        {
            "repository",
            "branch",
            "commit",
            "tree",
            "git_object_format",
            "file_count",
            "executable_paths",
            "commit_object_base64",
        },
        label="signed source manifest source",
    )
    if source["repository"] != contract["source"]["repository"]:
        raise ContractError("signed source manifest repository mismatch")
    if source["branch"] != contract["source"]["branch"]:
        raise ContractError("signed source manifest branch mismatch")
    commit = _sha1(source["commit"], label="signed source manifest commit")
    tree = _sha1(source["tree"], label="signed source manifest tree")
    if source["git_object_format"] != "sha1":
        raise ContractError("signed source manifest Git object format must be sha1")
    file_count = source["file_count"]
    if not isinstance(file_count, int) or isinstance(file_count, bool) or file_count < 1:
        raise ContractError("signed source manifest file_count must be positive")
    executable_paths = source["executable_paths"]
    if not isinstance(executable_paths, list):
        raise ContractError("signed source manifest executable_paths must be an array")
    normalized_paths: list[str] = []
    for index, path in enumerate(executable_paths):
        normalized_paths.append(
            _safe_s3_key(path, label=f"signed source executable_paths[{index}]")
        )
    expected_order = sorted(normalized_paths, key=lambda item: item.encode("utf-8"))
    if normalized_paths != expected_order or len(normalized_paths) != len(set(normalized_paths)):
        raise ContractError("signed source executable paths must be unique and bytewise sorted")
    encoded_commit = source["commit_object_base64"]
    if not isinstance(encoded_commit, str):
        raise ContractError("signed source manifest commit proof must be base64")
    try:
        commit_object = base64.b64decode(encoded_commit, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContractError("signed source manifest commit proof is invalid") from exc
    if _git_object_id("commit", commit_object) != commit:
        raise ContractError("signed source manifest commit proof does not hash to commit")
    tree_headers = [
        line.removeprefix(b"tree ")
        for line in commit_object.split(b"\n\n", 1)[0].splitlines()
        if line.startswith(b"tree ")
    ]
    if len(tree_headers) != 1 or tree_headers[0].decode("ascii", errors="ignore") != tree:
        raise ContractError("signed source manifest tree does not match commit proof")
    return value


def verify_source_manifest(
    repo_root: Path,
    manifest_path: Path,
    contract_path: Path,
    expected_commit: str,
    expected_manifest_sha256: str,
) -> None:
    expected_manifest_sha256 = _sha256(
        expected_manifest_sha256, label="expected source manifest SHA-256"
    )
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read signed source manifest: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ContractError("signed source manifest SHA-256 mismatch")
    actual = _loads(raw.decode("utf-8"), label="signed source manifest")
    contract = load_contract(contract_path)
    validate_source_manifest(actual, contract, contract_sha256(contract_path))
    expected = source_manifest(repo_root, expected_commit, contract_path)
    if actual != expected:
        raise ContractError("signed source manifest does not exactly match the checked-out commit")
    head = _git(repo_root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    if head != expected_commit:
        raise ContractError("CodeBuild checkout HEAD is not the signed full commit")
    worktree_status = _git(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if worktree_status:
        raise ContractError("CodeBuild checkout does not exactly match the signed commit")
    source = actual.get("source") if isinstance(actual, dict) else None
    if not isinstance(source, dict):
        raise ContractError("signed source manifest source is missing")
    encoded = source.get("commit_object_base64")
    if not isinstance(encoded, str):
        raise ContractError("signed source manifest commit proof is missing")
    try:
        commit_object = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContractError("signed source manifest commit proof is invalid") from exc
    if _git_object_id("commit", commit_object) != expected_commit:
        raise ContractError("signed source manifest commit proof does not hash to the commit")


def validate_signed_source_manifest(
    manifest_path: Path,
    contract_path: Path,
    expected_commit: str,
    expected_manifest_sha256: str,
) -> None:
    """Validate immutable publisher evidence without trusting a source checkout."""

    expected_commit = _sha1(expected_commit, label="expected signed source commit")
    expected_manifest_sha256 = _sha256(
        expected_manifest_sha256,
        label="expected source manifest SHA-256",
    )
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read signed source manifest: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ContractError("signed source manifest SHA-256 mismatch")
    manifest = _loads(raw.decode("utf-8"), label="signed source manifest")
    contract = load_contract(contract_path)
    validated = validate_source_manifest(
        manifest,
        contract,
        contract_sha256(contract_path),
    )
    source = validated["source"]
    if source["commit"] != expected_commit:
        raise ContractError("signed source manifest commit mismatch")


def _referrers(path: Path) -> list[dict[str, Any]]:
    response = _load(path, label="ECR ListImageReferrers response")
    if not isinstance(response, dict):
        raise ContractError("ECR ListImageReferrers response must be an object")
    if set(response) not in ({"referrers"}, {"referrers", "nextToken"}):
        raise ContractError("ECR ListImageReferrers response schema is invalid")
    if response.get("nextToken") not in {None, ""}:
        raise ContractError("ECR ListImageReferrers response is truncated")
    referrers = response["referrers"]
    if not isinstance(referrers, list):
        raise ContractError("ECR referrers must be an array")
    normalized: list[dict[str, Any]] = []
    for index, referrer in enumerate(referrers):
        label = f"ECR referrers[{index}]"
        if not isinstance(referrer, dict):
            raise ContractError(f"{label} must be an object")
        required = {"digest", "mediaType", "size"}
        allowed = required | {"annotations", "artifactStatus", "artifactType"}
        if required - referrer.keys() or referrer.keys() - allowed:
            raise ContractError(f"{label} schema is invalid")
        digest = referrer["digest"]
        artifact_type = referrer.get("artifactType")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ContractError(f"{label} digest is invalid")
        if not isinstance(artifact_type, str) or not _ARTIFACT_TYPE_RE.fullmatch(artifact_type):
            raise ContractError(f"{label} artifactType is invalid")
        if referrer.get("artifactStatus") != "ACTIVE":
            raise ContractError(f"{label} must be ACTIVE")
        if not isinstance(referrer["size"], int) or isinstance(referrer["size"], bool):
            raise ContractError(f"{label} size is invalid")
        normalized.append({"digest": digest, "artifactType": artifact_type})
    if len({item["digest"] for item in normalized}) != len(normalized):
        raise ContractError("ECR referrer digests must be unique")
    return normalized


def verify_subject_referrers(path: Path, contract_path: Path) -> list[str]:
    contract = load_contract(contract_path)
    referrers = _referrers(path)
    counts = Counter(item["artifactType"] for item in referrers)
    allowed_types = {
        contract["bundle"]["signature_artifact_type"],
        *(item["artifact_type"] for item in contract["bundle"]["required_referrers"]),
    }
    unknown = sorted(set(counts) - allowed_types)
    if unknown:
        raise ContractError(f"subject has unknown OCI referrer artifact types: {unknown}")
    if counts[contract["bundle"]["signature_artifact_type"]] < 1:
        raise ContractError("arm64 child subject has no active signature referrer")
    attestation_digests: list[str] = []
    for requirement in contract["bundle"]["required_referrers"]:
        artifact_type = requirement["artifact_type"]
        if counts[artifact_type] < requirement["minimum"]:
            raise ContractError(f"arm64 child subject is missing {requirement['name']} referrer")
        attestation_digests.extend(
            item["digest"] for item in referrers if item["artifactType"] == artifact_type
        )
    return sorted(attestation_digests)


def verify_signature_referrers(path: Path, contract_path: Path) -> None:
    contract = load_contract(contract_path)
    referrers = _referrers(path)
    signature_type = contract["bundle"]["signature_artifact_type"]
    if not referrers or any(item["artifactType"] != signature_type for item in referrers):
        raise ContractError("attestation referrers must contain only active signatures")


def arm64_config_digest(
    path: Path,
    contract_path: Path,
    expected_image_digest: str,
    expected_repository: str,
    expected_registry_id: str,
) -> str:
    """Require an exact single OCI manifest and return its config digest."""

    contract = load_contract(contract_path)
    expected_image_digest = _digest(
        expected_image_digest, label="expected OpenClaw arm64 image digest"
    )
    allowed_repositories = {
        subject["quarantine_repository"] for subject in contract["bundle"]["subjects"]
    }
    if expected_repository not in allowed_repositories:
        raise ContractError("expected OpenClaw repository is invalid")
    if expected_registry_id != "718959508629":
        raise ContractError("expected OpenClaw registry ID is not fixed")
    response = _load(path, label="OpenClaw ECR BatchGetImage response")
    if not isinstance(response, dict):
        raise ContractError("OpenClaw ECR BatchGetImage response must be an object")
    if set(response) != {"images", "failures"}:
        raise ContractError("OpenClaw ECR BatchGetImage response schema is invalid")
    if not isinstance(response["failures"], list) or response["failures"]:
        raise ContractError("OpenClaw ECR BatchGetImage returned failures")
    images = response["images"]
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ContractError("OpenClaw ECR BatchGetImage must return one image")
    image = images[0]
    allowed_image_keys = {
        "registryId",
        "repositoryName",
        "imageId",
        "imageManifest",
        "imageManifestMediaType",
    }
    if image.keys() - allowed_image_keys:
        raise ContractError("OpenClaw ECR image response has unknown fields")
    if image.get("registryId") != expected_registry_id:
        raise ContractError("OpenClaw ECR image registry mismatch")
    if image.get("repositoryName") != expected_repository:
        raise ContractError("OpenClaw ECR image repository mismatch")
    image_id = image.get("imageId")
    if not isinstance(image_id, dict) or image_id.get("imageDigest") != expected_image_digest:
        raise ContractError("OpenClaw ECR returned a different image digest")
    media_type = contract["bundle"]["arm64_subject_media_type"]
    if image.get("imageManifestMediaType") != media_type:
        raise ContractError("OpenClaw candidate is not a single OCI image manifest")
    raw_manifest = image.get("imageManifest")
    if not isinstance(raw_manifest, str):
        raise ContractError("OpenClaw ECR image manifest is missing")
    actual_digest = "sha256:" + hashlib.sha256(raw_manifest.encode("utf-8")).hexdigest()
    if actual_digest != expected_image_digest:
        raise ContractError("OpenClaw ECR manifest bytes do not hash to its digest")
    manifest = _loads(raw_manifest, label="OpenClaw OCI image manifest")
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ContractError("OpenClaw OCI image manifest schema is unsupported")
    required_manifest_keys = {"schemaVersion", "mediaType", "config", "layers"}
    if required_manifest_keys - manifest.keys() or manifest.keys() - (
        required_manifest_keys | {"annotations"}
    ):
        raise ContractError("OpenClaw OCI image manifest fields are invalid")
    if manifest.get("mediaType") != media_type:
        raise ContractError("OpenClaw manifest payload is an index or unsupported type")
    config = manifest.get("config")
    if not isinstance(config, dict):
        raise ContractError("OpenClaw OCI image config descriptor is missing")
    required_config_keys = {"mediaType", "size", "digest"}
    if required_config_keys - config.keys() or config.keys() - (
        required_config_keys | {"annotations"}
    ):
        raise ContractError("OpenClaw OCI config descriptor fields are invalid")
    if config.get("mediaType") != "application/vnd.oci.image.config.v1+json":
        raise ContractError("OpenClaw OCI config descriptor media type is invalid")
    config_size = config.get("size")
    if not isinstance(config_size, int) or isinstance(config_size, bool) or config_size <= 0:
        raise ContractError("OpenClaw OCI config descriptor size is invalid")
    config_digest = config.get("digest")
    if not isinstance(config_digest, str) or not _DIGEST_RE.fullmatch(config_digest):
        raise ContractError("OpenClaw OCI config digest is invalid")
    if not isinstance(manifest.get("layers"), list):
        raise ContractError("OpenClaw OCI image layers are missing")
    return config_digest


def verify_arm64_config(
    path: Path,
    expected_config_digest: str,
    expected_commit: str,
) -> None:
    expected_config_digest = _digest(
        expected_config_digest, label="expected OpenClaw OCI config digest"
    )
    expected_commit = _sha1(expected_commit, label="expected OpenClaw OCI revision")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read OpenClaw OCI config: {exc}") from exc
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_config_digest:
        raise ContractError("OpenClaw OCI config bytes do not match the manifest")
    try:
        config = _loads(raw.decode("utf-8"), label="OpenClaw OCI config")
    except UnicodeDecodeError as exc:
        raise ContractError("OpenClaw OCI config is not UTF-8") from exc
    if not isinstance(config, dict):
        raise ContractError("OpenClaw OCI config must be an object")
    if config.get("os") != "linux" or config.get("architecture") != "arm64":
        raise ContractError("OpenClaw candidate is not linux/arm64")
    if config.get("variant") not in {None, "", "v8"}:
        raise ContractError("OpenClaw arm64 variant is unsupported")
    config_section = config.get("config")
    if not isinstance(config_section, dict):
        raise ContractError("OpenClaw OCI config section is missing")
    labels = config_section.get("Labels")
    if not isinstance(labels, dict):
        raise ContractError("OpenClaw OCI labels are missing")
    if labels.get("org.opencontainers.image.revision") != expected_commit:
        raise ContractError("OpenClaw OCI revision does not match the signed source commit")


def verify_bundle_receipt(
    path: Path,
    contract_path: Path,
    expected_commit: str,
    expected_contract_sha256: str,
) -> list[dict[str, str]]:
    contract = load_contract(contract_path)
    expected_contract_sha256 = _sha256(
        expected_contract_sha256, label="expected OpenClaw contract SHA-256"
    )
    if contract_sha256(contract_path) != expected_contract_sha256:
        raise ContractError("OpenClaw bundle contract SHA-256 mismatch")
    if not _SHA1_RE.fullmatch(expected_commit):
        raise ContractError("expected OpenClaw source commit is invalid")
    receipt = _load(path, label="OpenClaw core/media build receipt")
    if not isinstance(receipt, dict):
        raise ContractError("OpenClaw core/media build receipt must be an object")
    _exact_keys(
        receipt,
        {"schema_version", "source_commit", "bundle_contract_sha256", "subjects"},
        label="OpenClaw core/media build receipt",
    )
    if receipt["schema_version"] != 1:
        raise ContractError("unsupported OpenClaw core/media build receipt schema")
    if receipt["source_commit"] != expected_commit:
        raise ContractError("OpenClaw build receipt source commit mismatch")
    if receipt["bundle_contract_sha256"] != expected_contract_sha256:
        raise ContractError("OpenClaw build receipt contract SHA-256 mismatch")
    subjects = receipt["subjects"]
    # Derived from the contract, not a literal: the zip below is strict=True, so a
    # count that disagreed with the contract would raise a bare ValueError here
    # instead of this contract error.
    if not isinstance(subjects, list) or len(subjects) != len(contract["bundle"]["subjects"]):
        raise ContractError(
            "OpenClaw build receipt must contain exactly "
            + " and ".join(subject["name"] for subject in contract["bundle"]["subjects"])
        )
    normalized: list[dict[str, str]] = []
    for expected, subject in zip(contract["bundle"]["subjects"], subjects, strict=True):
        if not isinstance(subject, dict):
            raise ContractError("OpenClaw build receipt subject must be an object")
        _exact_keys(
            subject,
            {
                "name",
                "quarantine_repository",
                "release_repository",
                "tag",
                "index_digest",
                "arm64_digest",
                "scan",
            },
            label=f"OpenClaw build receipt {expected['name']}",
        )
        for key in ("name", "quarantine_repository", "release_repository"):
            if subject[key] != expected[key]:
                raise ContractError(f"OpenClaw build receipt {expected['name']} {key} mismatch")
        expected_tag = f"candidate-{expected_commit}-{expected['name']}"
        if subject["tag"] != expected_tag:
            raise ContractError(f"OpenClaw build receipt {expected['name']} tag mismatch")
        for key in ("index_digest", "arm64_digest"):
            if not isinstance(subject[key], str) or not _DIGEST_RE.fullmatch(subject[key]):
                raise ContractError(f"OpenClaw build receipt {expected['name']} {key} is invalid")
        if subject["index_digest"] == subject["arm64_digest"]:
            raise ContractError(
                f"OpenClaw build receipt {expected['name']} confuses index and arm64 child"
            )
        scan = subject["scan"]
        if not isinstance(scan, dict):
            raise ContractError(f"OpenClaw build receipt {expected['name']} scan is invalid")
        _exact_keys(scan, {"critical", "high"}, label=f"OpenClaw receipt {expected['name']} scan")
        if scan != contract["bundle"]["scan_gate"]:
            raise ContractError(f"OpenClaw build receipt {expected['name']} scan is not C/H0")
        normalized.append(
            {
                "name": subject["name"],
                "quarantine_repository": subject["quarantine_repository"],
                "release_repository": subject["release_repository"],
                "tag": subject["tag"],
                "index_digest": subject["index_digest"],
                "arm64_digest": subject["arm64_digest"],
            }
        )
    return normalized


def _subject_digest_arguments(
    values: Sequence[str],
    contract: dict[str, Any],
) -> dict[str, tuple[str, str]]:
    parsed: dict[str, tuple[str, str]] = {}
    for value in values:
        parts = value.split("=", 2)
        if len(parts) != 3:
            raise ContractError("--subject-digest must be NAME=QUARANTINE_DIGEST=RELEASE_DIGEST")
        name, quarantine_digest, release_digest = parts
        if name in parsed:
            raise ContractError(f"duplicate --subject-digest name: {name}")
        parsed[name] = (
            _digest(quarantine_digest, label=f"{name} quarantine digest"),
            _digest(release_digest, label=f"{name} release digest"),
        )
    expected_names = [subject["name"] for subject in contract["bundle"]["subjects"]]
    if set(parsed) != set(expected_names):
        raise ContractError(
            "--subject-digest must contain exactly " + " and ".join(expected_names)
        )
    return parsed


def _release_referrer_evidence(
    subject_name: str,
    response_path: Path,
    referrer_directory: Path,
    contract_path: Path,
) -> list[dict[str, Any]]:
    attestation_digests = set(verify_subject_referrers(response_path, contract_path))
    contract = load_contract(contract_path)
    signature_type = contract["bundle"]["signature_artifact_type"]
    normalized: list[dict[str, Any]] = []
    for referrer in _referrers(response_path):
        digest = referrer["digest"]
        artifact_type = referrer["artifactType"]
        signatures: list[str] = []
        if artifact_type != signature_type:
            if digest not in attestation_digests:
                raise ContractError("release attestation digest was not verified")
            signature_response = referrer_directory / (
                f"{subject_name}-{digest.removeprefix('sha256:')}-signature-referrers.json"
            )
            verify_signature_referrers(signature_response, contract_path)
            signatures = sorted(item["digest"] for item in _referrers(signature_response))
        normalized.append(
            {
                "digest": digest,
                "artifact_type": artifact_type,
                "signatures": signatures,
            }
        )
    return sorted(
        normalized,
        key=lambda item: (item["artifact_type"], item["digest"]),
    )


def validate_release_evidence(
    value: Any,
    contract_path: Path,
    *,
    expected_build_id: str | None = None,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    """Validate the exact signed, immutable OpenClaw release evidence schema."""

    contract = load_contract(contract_path)
    require_release_ready(contract)
    if not isinstance(value, dict):
        raise ContractError("OpenClaw release evidence must be an object")
    _exact_keys(
        value,
        {"schema_version", "build", "source", "bundle_contract_sha256", "subjects"},
        label="OpenClaw release evidence",
    )
    if value["schema_version"] != RELEASE_EVIDENCE_SCHEMA_VERSION:
        raise ContractError("OpenClaw release evidence schema is unsupported")
    expected_contract_sha256 = contract_sha256(contract_path)
    if value["bundle_contract_sha256"] != expected_contract_sha256:
        raise ContractError("OpenClaw release evidence contract SHA-256 mismatch")

    build = value["build"]
    if not isinstance(build, dict):
        raise ContractError("OpenClaw release evidence build must be an object")
    _exact_keys(
        build,
        {"project", "id", "source_version", "resolved_source_version"},
        label="OpenClaw release evidence build",
    )
    if build["project"] != "teamagent-dev-openclaw-provenance-builder":
        raise ContractError("OpenClaw release evidence project mismatch")
    build_id = build["id"]
    if not isinstance(build_id, str) or not _BUILD_ID_RE.fullmatch(build_id):
        raise ContractError("OpenClaw release evidence build ID is invalid")
    if expected_build_id is not None and build_id != expected_build_id:
        raise ContractError("OpenClaw release evidence build ID mismatch")
    source_version = _sha1(
        build["source_version"], label="OpenClaw release evidence source version"
    )
    resolved_source_version = _sha1(
        build["resolved_source_version"],
        label="OpenClaw release evidence resolved source version",
    )
    if source_version != resolved_source_version:
        raise ContractError("OpenClaw release evidence source versions differ")
    if expected_commit is not None and source_version != expected_commit:
        raise ContractError("OpenClaw release evidence source commit mismatch")

    source = value["source"]
    if not isinstance(source, dict):
        raise ContractError("OpenClaw release evidence source must be an object")
    _exact_keys(
        source,
        {
            "repository",
            "branch",
            "commit",
            "tree",
            "manifest_bucket",
            "manifest_key",
            "manifest_sha256",
            "manifest_version_id",
            "manifest_signature_key",
            "manifest_signature_version_id",
        },
        label="OpenClaw release evidence source",
    )
    if source["repository"] != contract["source"]["repository"]:
        raise ContractError("OpenClaw release evidence source repository mismatch")
    if source["branch"] != contract["source"]["branch"]:
        raise ContractError("OpenClaw release evidence source branch mismatch")
    commit = _sha1(source["commit"], label="OpenClaw release evidence commit")
    _sha1(source["tree"], label="OpenClaw release evidence tree")
    if commit != source_version:
        raise ContractError("OpenClaw release evidence commit differs from build")
    if source["manifest_bucket"] != contract["evidence"]["bucket"]:
        raise ContractError("OpenClaw release evidence manifest bucket mismatch")
    manifest_sha256 = _sha256(source["manifest_sha256"], label="OpenClaw source manifest SHA-256")
    expected_manifest_key = (
        f"{contract['evidence']['source_manifest_prefix']}/{commit}/{manifest_sha256}.json"
    )
    if _safe_s3_key(source["manifest_key"], label="source manifest key") != expected_manifest_key:
        raise ContractError("OpenClaw source manifest key is not commit/content addressed")
    if source["manifest_signature_key"] != f"{expected_manifest_key}.sig":
        raise ContractError("OpenClaw source manifest signature key mismatch")
    _version_id(source["manifest_version_id"], label="source manifest VersionId")
    _version_id(
        source["manifest_signature_version_id"],
        label="source manifest signature VersionId",
    )

    subjects = value["subjects"]
    if not isinstance(subjects, list) or len(subjects) != len(contract["bundle"]["subjects"]):
        raise ContractError(
            "OpenClaw release evidence must contain "
            + " and ".join(subject["name"] for subject in contract["bundle"]["subjects"])
        )
    signature_type = contract["bundle"]["signature_artifact_type"]
    required_types = {item["artifact_type"] for item in contract["bundle"]["required_referrers"]}
    allowed_types = required_types | {signature_type}
    for expected, subject in zip(contract["bundle"]["subjects"], subjects, strict=True):
        if not isinstance(subject, dict):
            raise ContractError("OpenClaw release evidence subject must be an object")
        _exact_keys(
            subject,
            {
                "name",
                "quarantine_repository",
                "quarantine_digest",
                "release_repository",
                "release_digest",
                "tag",
                "referrers",
            },
            label=f"OpenClaw release evidence {expected['name']}",
        )
        for key in ("name", "quarantine_repository", "release_repository"):
            if subject[key] != expected[key]:
                raise ContractError(f"OpenClaw release evidence {expected['name']} {key} mismatch")
        expected_tag = f"candidate-{commit}-{expected['name']}"
        if subject["tag"] != expected_tag:
            raise ContractError(f"OpenClaw release evidence {expected['name']} tag mismatch")
        quarantine_digest = _digest(
            subject["quarantine_digest"],
            label=f"OpenClaw {expected['name']} quarantine digest",
        )
        release_digest = _digest(
            subject["release_digest"],
            label=f"OpenClaw {expected['name']} release digest",
        )
        if quarantine_digest != release_digest:
            raise ContractError(
                f"OpenClaw {expected['name']} release digest differs from quarantine"
            )
        referrers = subject["referrers"]
        if not isinstance(referrers, list):
            raise ContractError(f"OpenClaw {expected['name']} referrers must be an array")
        normalized_referrers: list[dict[str, Any]] = []
        seen_digests: set[str] = set()
        counts: Counter[str] = Counter()
        for index, referrer in enumerate(referrers):
            referrer_label = f"OpenClaw {expected['name']} referrers[{index}]"
            if not isinstance(referrer, dict):
                raise ContractError(f"{referrer_label} must be an object")
            _exact_keys(
                referrer,
                {"digest", "artifact_type", "signatures"},
                label=referrer_label,
            )
            digest = _digest(referrer["digest"], label=f"{referrer_label} digest")
            artifact_type = referrer["artifact_type"]
            if artifact_type not in allowed_types:
                raise ContractError(f"{referrer_label} artifact type is not allowlisted")
            if digest in seen_digests:
                raise ContractError(f"{referrer_label} digest is duplicated")
            seen_digests.add(digest)
            counts[artifact_type] += 1
            signatures = referrer["signatures"]
            if not isinstance(signatures, list):
                raise ContractError(f"{referrer_label} signatures must be an array")
            normalized_signatures = [
                _digest(item, label=f"{referrer_label} signature") for item in signatures
            ]
            if normalized_signatures != sorted(set(normalized_signatures)):
                raise ContractError(f"{referrer_label} signatures must be unique and sorted")
            if artifact_type == signature_type and normalized_signatures:
                raise ContractError(f"{referrer_label} child signature cannot nest signatures")
            if artifact_type in required_types and not normalized_signatures:
                raise ContractError(f"{referrer_label} attestation must be signed")
            normalized_referrers.append(
                {
                    "digest": digest,
                    "artifact_type": artifact_type,
                    "signatures": normalized_signatures,
                }
            )
        expected_order = sorted(
            normalized_referrers,
            key=lambda item: (item["artifact_type"], item["digest"]),
        )
        if normalized_referrers != expected_order:
            raise ContractError(f"OpenClaw {expected['name']} referrers are not sorted")
        if counts[signature_type] < 1:
            raise ContractError(f"OpenClaw {expected['name']} child is unsigned")
        for required_type in required_types:
            if counts[required_type] < 1:
                raise ContractError(
                    f"OpenClaw {expected['name']} is missing a signed required referrer"
                )
    return value


def create_release_evidence(
    contract_path: Path,
    source_manifest_path: Path,
    source_manifest_key: str,
    source_manifest_version_id: str,
    source_signature_key: str,
    source_signature_version_id: str,
    build_id: str,
    commit: str,
    subject_digest_values: Sequence[str],
    referrer_directory: Path,
    output: Path,
) -> None:
    contract = load_contract(contract_path)
    require_release_ready(contract)
    commit = _sha1(commit, label="OpenClaw release evidence commit")
    if not _BUILD_ID_RE.fullmatch(build_id):
        raise ContractError("OpenClaw release evidence build ID is invalid")
    try:
        source_manifest_raw = source_manifest_path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read OpenClaw source manifest: {exc}") from exc
    source_manifest_sha256 = hashlib.sha256(source_manifest_raw).hexdigest()
    source_manifest_value = _loads(
        source_manifest_raw.decode("utf-8"), label="OpenClaw source manifest"
    )
    validate_source_manifest(
        source_manifest_value,
        contract,
        contract_sha256(contract_path),
    )
    source = source_manifest_value["source"]
    if source["commit"] != commit:
        raise ContractError("OpenClaw source manifest commit mismatch for release evidence")
    expected_manifest_key = (
        f"{contract['evidence']['source_manifest_prefix']}/{commit}/{source_manifest_sha256}.json"
    )
    if source_manifest_key != expected_manifest_key:
        raise ContractError("OpenClaw source manifest key mismatch for release evidence")
    if source_signature_key != f"{source_manifest_key}.sig":
        raise ContractError("OpenClaw source signature key mismatch for release evidence")
    subject_digests = _subject_digest_arguments(subject_digest_values, contract)
    subjects: list[dict[str, Any]] = []
    for subject in contract["bundle"]["subjects"]:
        name = subject["name"]
        quarantine_digest, release_digest = subject_digests[name]
        response_path = referrer_directory / f"{name}-subject-referrers.json"
        subjects.append(
            {
                "name": name,
                "quarantine_repository": subject["quarantine_repository"],
                "quarantine_digest": quarantine_digest,
                "release_repository": subject["release_repository"],
                "release_digest": release_digest,
                "tag": f"candidate-{commit}-{name}",
                "referrers": _release_referrer_evidence(
                    name,
                    response_path,
                    referrer_directory,
                    contract_path,
                ),
            }
        )
    evidence = {
        "schema_version": RELEASE_EVIDENCE_SCHEMA_VERSION,
        "build": {
            "project": "teamagent-dev-openclaw-provenance-builder",
            "id": build_id,
            "source_version": commit,
            "resolved_source_version": commit,
        },
        "source": {
            "repository": contract["source"]["repository"],
            "branch": contract["source"]["branch"],
            "commit": commit,
            "tree": source["tree"],
            "manifest_bucket": contract["evidence"]["bucket"],
            "manifest_key": source_manifest_key,
            "manifest_sha256": source_manifest_sha256,
            "manifest_version_id": _version_id(
                source_manifest_version_id, label="source manifest VersionId"
            ),
            "manifest_signature_key": source_signature_key,
            "manifest_signature_version_id": _version_id(
                source_signature_version_id,
                label="source manifest signature VersionId",
            ),
        },
        "bundle_contract_sha256": contract_sha256(contract_path),
        "subjects": subjects,
    }
    validate_release_evidence(
        evidence,
        contract_path,
        expected_build_id=build_id,
        expected_commit=commit,
    )
    output.write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def verify_release_evidence(
    evidence_path: Path,
    contract_path: Path,
    expected_build_id: str,
    expected_commit: str,
    expected_evidence_sha256: str,
) -> None:
    expected_evidence_sha256 = _sha256(
        expected_evidence_sha256,
        label="expected OpenClaw release evidence SHA-256",
    )
    try:
        raw = evidence_path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read OpenClaw release evidence: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != expected_evidence_sha256:
        raise ContractError("OpenClaw release evidence SHA-256 mismatch")
    value = _loads(raw.decode("utf-8"), label="OpenClaw release evidence")
    validate_release_evidence(
        value,
        contract_path,
        expected_build_id=expected_build_id,
        expected_commit=expected_commit,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    contract_hash = commands.add_parser("contract-sha256")
    contract_hash.add_argument("--contract", type=Path, required=True)
    release = commands.add_parser("assert-release-ready")
    release.add_argument("--contract", type=Path, required=True)
    create = commands.add_parser("create-source-manifest")
    create.add_argument("--repo-root", type=Path, required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--contract", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-source-manifest")
    verify.add_argument("--repo-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--contract", type=Path, required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--expected-manifest-sha256", required=True)

    validate_signed = commands.add_parser("validate-signed-source-manifest")
    validate_signed.add_argument("--manifest", type=Path, required=True)
    validate_signed.add_argument("--contract", type=Path, required=True)
    validate_signed.add_argument("--expected-commit", required=True)
    validate_signed.add_argument("--expected-manifest-sha256", required=True)
    arm64_manifest = commands.add_parser("arm64-config-digest")
    arm64_manifest.add_argument("--response", type=Path, required=True)
    arm64_manifest.add_argument("--contract", type=Path, required=True)
    arm64_manifest.add_argument("--expected-image-digest", required=True)
    arm64_manifest.add_argument("--expected-repository", required=True)
    arm64_manifest.add_argument("--expected-registry-id", required=True)
    arm64_config = commands.add_parser("verify-arm64-config")
    arm64_config.add_argument("--config", type=Path, required=True)
    arm64_config.add_argument("--expected-config-digest", required=True)
    arm64_config.add_argument("--expected-commit", required=True)
    receipt = commands.add_parser("verify-bundle-receipt")
    receipt.add_argument("--receipt", type=Path, required=True)
    receipt.add_argument("--contract", type=Path, required=True)
    receipt.add_argument("--expected-commit", required=True)
    receipt.add_argument("--expected-contract-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "contract-sha256":
            print(contract_sha256(args.contract))
        elif args.command == "assert-release-ready":
            require_release_ready(load_contract(args.contract))
            print("OpenClaw core/media bundle is release-ready")
        elif args.command == "create-source-manifest":
            write_source_manifest(args.repo_root, args.commit, args.contract, args.output)
            print(f"OpenClaw source manifest created: {args.commit}")
        elif args.command == "verify-source-manifest":
            verify_source_manifest(
                args.repo_root,
                args.manifest,
                args.contract,
                args.expected_commit,
                args.expected_manifest_sha256,
            )
        elif args.command == "validate-signed-source-manifest":
            validate_signed_source_manifest(
                args.manifest,
                args.contract,
                args.expected_commit,
                args.expected_manifest_sha256,
            )
            print(f"OpenClaw signed source manifest verified: {args.expected_commit}")
        elif args.command == "arm64-config-digest":
            print(
                arm64_config_digest(
                    args.response,
                    args.contract,
                    args.expected_image_digest,
                    args.expected_repository,
                    args.expected_registry_id,
                )
            )
        elif args.command == "verify-arm64-config":
            verify_arm64_config(
                args.config,
                args.expected_config_digest,
                args.expected_commit,
            )
            print("OpenClaw single linux/arm64 OCI config verified")
        elif args.command == "verify-bundle-receipt":
            for subject in verify_bundle_receipt(
                args.receipt,
                args.contract,
                args.expected_commit,
                args.expected_contract_sha256,
            ):
                print(
                    "\t".join(
                        subject[key]
                        for key in (
                            "name",
                            "quarantine_repository",
                            "release_repository",
                            "tag",
                            "index_digest",
                            "arm64_digest",
                        )
                    )
                )
        else:  # pragma: no cover
            raise ContractError(f"unsupported command: {args.command}")
    except (ContractError, UnicodeDecodeError) as exc:
        print(f"FATAL OpenClaw provenance verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
