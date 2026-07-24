#!/usr/bin/env python3
"""Create and verify the fail-closed TeamAgent CodeBuild source contract.

The S3 object itself is a ``git archive`` ZIP with one generated manifest added
at its root.  The manifest carries the raw Git commit object and expected tree.
That lets CodeBuild prove that every extracted source byte belongs to the
declared commit even though an S3 source archive has no ``.git`` directory.

This module also verifies the remotely stored OCI config used by the local
build launcher, so the ECR image label is tied to the digest returned by ECR.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = ".teamagent-source-manifest.json"
SOURCE_KEY = "codebuild/source.zip"
APP_HTML_BUCKET = "teamagent-dev-raw-files"
APP_HTML_KEY = "codebuild/connect-web-app.html"
SCHEMA_VERSION = 3
RUNTIME_CONTRACT_PATH = "infra/codebuild/teamagent_runtime_contract.json"
RUNTIME_CONTRACT_SCHEMA_VERSION = 4
RUNTIME_RECEIPT_SCHEMA_VERSION = 2
TEAMAGENT_LABEL_PREFIX = "io.teamagent.build."
RUNTIME_ENTRY_LABEL_PREFIX = "io.teamagent.contract."
SCRAPE_TOOLS_LABEL = "io.teamagent.build.with-scrape-tools"
APP_HTML_SHA256_LABEL = "io.teamagent.build.app-html-sha256"
APP_HTML_VERSION_ID_LABEL = "io.teamagent.build.app-html-version-id"
RUNTIME_CONTRACT_SHA256_LABEL = "io.teamagent.build.runtime-contract-sha256"
RUNTIME_RECEIPT_LABEL = "io.teamagent.build.runtime-receipt"
RUNTIME_RECEIPT_SHA256_LABEL = "io.teamagent.build.runtime-receipt-sha256"
RUNTIME_CONTRACT_SHA256_ARG = "RUNTIME_CONTRACT_SHA256"
RUNTIME_RECEIPT_B64_ARG = "RUNTIME_RECEIPT_B64"
RUNTIME_RECEIPT_SHA256_ARG = "RUNTIME_RECEIPT_SHA256"
_RESERVED_RUNTIME_BUILD_ARGS = {
    RUNTIME_CONTRACT_SHA256_ARG,
    RUNTIME_RECEIPT_B64_ARG,
    RUNTIME_RECEIPT_SHA256_ARG,
    "APP_HTML_SHA256",
    "APP_HTML_VERSION_ID",
    "GIT_BRANCH",
    "GIT_COMMIT",
    "WITH_SCRAPE_TOOLS",
}
_RESERVED_TEAMAGENT_LABELS = {
    SCRAPE_TOOLS_LABEL,
    APP_HTML_SHA256_LABEL,
    APP_HTML_VERSION_ID_LABEL,
    RUNTIME_CONTRACT_SHA256_LABEL,
    RUNTIME_RECEIPT_LABEL,
    RUNTIME_RECEIPT_SHA256_LABEL,
}
_EVIDENCE_VALUE_KINDS = {
    "artifact_sha256": {"sha256"},
    "base_image_digest": {"sha256_digest"},
    "binary_sha256": {"sha256"},
    "component_version": {"package_version", "positive_integer", "version"},
    "model_revision": {"git_sha1"},
    "package_version": {"package_version"},
}
_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_S3_VERSION_ID_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")
_PACKAGE_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~:-]{0,255}")
_DECIMAL_RE = re.compile(r"[1-9][0-9]*")
_RECEIPT_KEY_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_COMPONENT_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_BUILD_ARG_RE = re.compile(r"[A-Z][A-Z0-9_]{1,127}")
_OCI_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,254}")
_RFC3339_UTC_RE = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z"
)
_SUPPORTED_IMAGE_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}


class ProvenanceError(ValueError):
    """The provenance contract is malformed or does not match its payload."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads_strict(raw: str, *, label: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProvenanceError(f"invalid {label} JSON: {exc}") from exc


def _load_json(path: Path, *, label: str) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvenanceError(f"cannot read {label}: {path}: {exc}") from exc
    return _loads_strict(raw, label=label)


def _require_exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing={missing}")
        if unknown:
            parts.append(f"unknown={unknown}")
        raise ProvenanceError(f"{label} schema mismatch: {'; '.join(parts)}")


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ProvenanceError(f"git {' '.join(args)} failed{suffix}") from exc
    return completed.stdout


def _git_object_id(kind: str, payload: bytes, *, algorithm: str = "sha1") -> str:
    if algorithm != "sha1":
        raise ProvenanceError(f"unsupported Git object format: {algorithm}")
    framed = f"{kind} {len(payload)}\0".encode("ascii") + payload
    # SHA-1 is part of this repository's Git object format, not a security hash choice.
    return hashlib.sha1(framed).hexdigest()


def _validate_archive_path(path: str, *, label: str) -> None:
    if not path or "\\" in path or any(ord(char) < 32 for char in path):
        raise ProvenanceError(f"unsafe {label} path: {path!r}")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or "." in parsed.parts:
        raise ProvenanceError(f"unsafe {label} path: {path!r}")


def _validate_s3_version_id(version_id: Any, *, label: str) -> str:
    if (
        not isinstance(version_id, str)
        or version_id in {"None", "null"}
        or not _S3_VERSION_ID_RE.fullmatch(version_id)
    ):
        raise ProvenanceError(f"{label} must be a usable S3 VersionId")
    return version_id


def _validate_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a lowercase SHA-256")
    return value


def _required_contract_text(value: Any, *, label: str, minimum: int = 1) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) < minimum:
        raise ProvenanceError(f"{label} must be a trimmed non-blank string")
    if len(value) > 2048 or any(ord(character) < 32 for character in value):
        raise ProvenanceError(f"{label} contains unsupported characters")
    return value


def _validate_contract_value(value: Any, kind: Any, *, label: str) -> str:
    value = _required_contract_text(value, label=label)
    validators = {
        "git_sha1": _SHA1_RE,
        "package_version": _PACKAGE_VERSION_RE,
        "positive_integer": _DECIMAL_RE,
        "sha256": _SHA256_RE,
        "sha256_digest": _SHA256_DIGEST_RE,
        "version": _VERSION_RE,
    }
    if kind not in validators:
        raise ProvenanceError(f"{label} has unsupported value_kind: {kind!r}")
    if not validators[kind].fullmatch(value):
        raise ProvenanceError(f"{label} does not match value_kind {kind!r}")
    return value


def _validate_rfc3339_utc(value: Any, *, label: str) -> str:
    value = _required_contract_text(value, label=label)
    if not _RFC3339_UTC_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be an RFC3339 UTC timestamp at second precision")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ProvenanceError(f"{label} is not a valid UTC timestamp") from exc
    return value


def _validate_approval_record(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvenanceError(f"{label} must be an object")
    _require_exact_keys(
        value,
        {
            "approved_at_utc",
            "approved_by",
            "source_commit",
            "observations",
            "decision",
        },
        label=label,
    )
    approved_at_utc = _validate_rfc3339_utc(
        value["approved_at_utc"],
        label=f"{label}.approved_at_utc",
    )
    approved_by = _required_contract_text(
        value["approved_by"],
        label=f"{label}.approved_by",
    )
    source_commit = _required_contract_text(
        value["source_commit"],
        label=f"{label}.source_commit",
    )
    if not _SHA1_RE.fullmatch(source_commit):
        raise ProvenanceError(f"{label}.source_commit must be a full lowercase Git SHA-1")
    decision = _required_contract_text(
        value["decision"],
        label=f"{label}.decision",
        minimum=len("APPROVED: "),
    )
    if not decision.startswith("APPROVED: "):
        raise ProvenanceError(f"{label}.decision must begin with 'APPROVED: '")

    observations = value["observations"]
    if not isinstance(observations, list) or not observations:
        raise ProvenanceError(f"{label}.observations must be a non-empty array")
    if len(observations) > 64:
        raise ProvenanceError(f"{label}.observations exceeds the 64-entry limit")
    normalized_observations: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for index, observation in enumerate(observations):
        observation_label = f"{label}.observations[{index}]"
        if not isinstance(observation, dict):
            raise ProvenanceError(f"{observation_label} must be an object")
        _require_exact_keys(
            observation,
            {"key", "value", "observed_at_utc", "source"},
            label=observation_label,
        )
        key = _required_contract_text(
            observation["key"],
            label=f"{observation_label}.key",
        )
        if not _RECEIPT_KEY_RE.fullmatch(key):
            raise ProvenanceError(f"{observation_label}.key is not canonical")
        if key in seen_keys:
            raise ProvenanceError(f"duplicate {label} observation key: {key}")
        seen_keys.add(key)
        normalized_observations.append(
            {
                "key": key,
                "value": _required_contract_text(
                    observation["value"],
                    label=f"{observation_label}.value",
                ),
                "observed_at_utc": _validate_rfc3339_utc(
                    observation["observed_at_utc"],
                    label=f"{observation_label}.observed_at_utc",
                ),
                "source": _required_contract_text(
                    observation["source"],
                    label=f"{observation_label}.source",
                ),
            }
        )

    return {
        "approved_at_utc": approved_at_utc,
        "approved_by": approved_by,
        "source_commit": source_commit,
        "observations": normalized_observations,
        "decision": decision,
    }


def validate_runtime_contract(value: Any, *, label: str = "runtime contract") -> dict[str, Any]:
    """Validate the generic, exact runtime receipt and its Docker/OCI bindings."""

    if not isinstance(value, dict):
        raise ProvenanceError(f"{label} must be a JSON object")
    _require_exact_keys(
        value,
        {"schema_version", "release", "approval_record", "receipt"},
        label=label,
    )
    if value["schema_version"] != RUNTIME_CONTRACT_SCHEMA_VERSION:
        raise ProvenanceError(f"unsupported {label} schema: {value['schema_version']!r}")

    release = value["release"]
    receipt = value["receipt"]
    if not isinstance(release, dict):
        raise ProvenanceError(f"{label} release must be an object")
    if not isinstance(receipt, dict):
        raise ProvenanceError(f"{label} receipt must be an object")
    _require_exact_keys(release, {"ready", "blocked_reason"}, label=f"{label} release")
    _require_exact_keys(
        receipt,
        {"schema_version", "subject", "entries"},
        label=f"{label} receipt",
    )
    if not isinstance(release["ready"], bool):
        raise ProvenanceError(f"{label} release.ready must be a boolean")
    blocked_reason = release["blocked_reason"]
    if not isinstance(blocked_reason, str) or blocked_reason != blocked_reason.strip():
        raise ProvenanceError(f"{label} release.blocked_reason must be a trimmed string")
    if release["ready"] and blocked_reason:
        raise ProvenanceError(f"{label} ready release cannot have a blocked_reason")
    if not release["ready"]:
        _required_contract_text(
            blocked_reason,
            label=f"{label} release.blocked_reason",
            minimum=20,
        )
    approval_record = value["approval_record"]
    if release["ready"]:
        normalized_approval_record: dict[str, Any] | None = _validate_approval_record(
            approval_record,
            label=f"{label} approval_record",
        )
    else:
        if approval_record is not None:
            raise ProvenanceError(f"{label} blocked release approval_record must be null")
        normalized_approval_record = None
    if receipt["schema_version"] != RUNTIME_RECEIPT_SCHEMA_VERSION:
        raise ProvenanceError(f"unsupported {label} receipt schema: {receipt['schema_version']!r}")
    if receipt["subject"] != "core":
        raise ProvenanceError(f"{label} receipt.subject must be 'core'")
    entries = receipt["entries"]
    if not isinstance(entries, list) or not entries:
        raise ProvenanceError(f"{label} receipt.entries must be a non-empty array")
    if len(entries) > 64:
        raise ProvenanceError(f"{label} receipt.entries exceeds the 64-entry limit")

    normalized_entries: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_build_args: set[str] = set()
    seen_labels: set[str] = set()
    entry_fields = {
        "key",
        "component",
        "evidence",
        "value_kind",
        "value",
        "build_arg",
        "oci_label",
        "dockerfile_uses",
    }
    for index, raw_entry in enumerate(entries):
        entry_label = f"{label} receipt.entries[{index}]"
        if not isinstance(raw_entry, dict):
            raise ProvenanceError(f"{entry_label} must be an object")
        _require_exact_keys(raw_entry, entry_fields, label=entry_label)
        key = _required_contract_text(raw_entry["key"], label=f"{entry_label}.key")
        component = _required_contract_text(
            raw_entry["component"], label=f"{entry_label}.component"
        )
        evidence = raw_entry["evidence"]
        value_kind = raw_entry["value_kind"]
        build_arg = _required_contract_text(
            raw_entry["build_arg"], label=f"{entry_label}.build_arg"
        )
        oci_label = _required_contract_text(
            raw_entry["oci_label"], label=f"{entry_label}.oci_label"
        )
        if not _RECEIPT_KEY_RE.fullmatch(key):
            raise ProvenanceError(f"{entry_label}.key is not canonical")
        if not _COMPONENT_RE.fullmatch(component):
            raise ProvenanceError(f"{entry_label}.component is not canonical")
        if evidence not in _EVIDENCE_VALUE_KINDS:
            raise ProvenanceError(f"{entry_label}.evidence is unsupported: {evidence!r}")
        if value_kind not in _EVIDENCE_VALUE_KINDS[evidence]:
            raise ProvenanceError(
                f"{entry_label}.value_kind {value_kind!r} is invalid for {evidence!r}"
            )
        value_text = _validate_contract_value(
            raw_entry["value"], value_kind, label=f"{entry_label}.value"
        )
        if not _BUILD_ARG_RE.fullmatch(build_arg):
            raise ProvenanceError(f"{entry_label}.build_arg is not canonical")
        if build_arg in _RESERVED_RUNTIME_BUILD_ARGS:
            raise ProvenanceError(f"{entry_label}.build_arg is reserved: {build_arg}")
        if not _OCI_LABEL_RE.fullmatch(oci_label) or not oci_label.startswith(
            RUNTIME_ENTRY_LABEL_PREFIX
        ):
            raise ProvenanceError(
                f"{entry_label}.oci_label must use {RUNTIME_ENTRY_LABEL_PREFIX}"
            )
        if oci_label in _RESERVED_TEAMAGENT_LABELS:
            raise ProvenanceError(f"{entry_label}.oci_label is reserved: {oci_label}")
        uses = raw_entry["dockerfile_uses"]
        if not isinstance(uses, list) or not uses:
            raise ProvenanceError(f"{entry_label}.dockerfile_uses must be non-empty")
        normalized_uses: list[str] = []
        for use_index, raw_use in enumerate(uses):
            normalized_uses.append(
                _required_contract_text(
                    raw_use,
                    label=f"{entry_label}.dockerfile_uses[{use_index}]",
                )
            )
        if len(normalized_uses) != len(set(normalized_uses)):
            raise ProvenanceError(f"{entry_label}.dockerfile_uses contains duplicates")
        if build_arg not in "\n".join(normalized_uses):
            raise ProvenanceError(
                f"{entry_label}.dockerfile_uses does not prove use of {build_arg}"
            )
        for item, seen, item_label in (
            (key, seen_keys, "key"),
            (build_arg, seen_build_args, "build_arg"),
            (oci_label, seen_labels, "oci_label"),
        ):
            if item in seen:
                raise ProvenanceError(f"duplicate {label} receipt {item_label}: {item}")
            seen.add(item)
        normalized_entries.append(
            {
                "key": key,
                "component": component,
                "evidence": evidence,
                "value_kind": value_kind,
                "value": value_text,
                "build_arg": build_arg,
                "oci_label": oci_label,
                "dockerfile_uses": normalized_uses,
            }
        )

    ordered_keys = [entry["key"] for entry in normalized_entries]
    if ordered_keys != sorted(ordered_keys):
        raise ProvenanceError(f"{label} receipt entries must be sorted by key")

    normalized = {
        "schema_version": RUNTIME_CONTRACT_SCHEMA_VERSION,
        "release": {"ready": release["ready"], "blocked_reason": blocked_reason},
        "approval_record": normalized_approval_record,
        "receipt": {
            "schema_version": RUNTIME_RECEIPT_SCHEMA_VERSION,
            "subject": "core",
            "entries": normalized_entries,
        },
    }
    if release["ready"]:
        require_release_ready(normalized, label=label)
    return normalized


def require_release_ready(contract: dict[str, Any], *, label: str = "runtime contract") -> None:
    """Require the complete core-only evidence map before a release can be built."""

    release = contract["release"]
    if not release["ready"]:
        raise ProvenanceError(f"{label} release is blocked: {release['blocked_reason']}")
    entries_by_key = {entry["key"]: entry for entry in contract["receipt"]["entries"]}
    required = {
        "artifact.torch.arm64-wheel.sha256": (
            "torch",
            "artifact_sha256",
            "TORCH_ARM64_WHEEL_SHA256",
        ),
        "base.builder.arm64.digest": (
            "python-builder",
            "base_image_digest",
            "PYTHON_BUILDER_ARM64_DIGEST",
        ),
        "base.runtime.arm64.digest": (
            "python-runtime",
            "base_image_digest",
            "PYTHON_RUNTIME_ARM64_DIGEST",
        ),
        "base.uv.arm64.digest": (
            "uv",
            "base_image_digest",
            "UV_ARM64_DIGEST",
        ),
        "binary.python.sha256": (
            "python",
            "binary_sha256",
            "PYTHON_BINARY_SHA256",
        ),
        "binary.uv.sha256": (
            "uv",
            "binary_sha256",
            "UV_BINARY_SHA256",
        ),
        "component.python.version": (
            "python",
            "component_version",
            "PYTHON_VERSION",
        ),
        "component.torch.version": (
            "torch",
            "component_version",
            "TORCH_VERSION",
        ),
        "component.uv.version": (
            "uv",
            "component_version",
            "UV_VERSION",
        ),
        "model.e5.revision": ("e5", "model_revision", "E5_MODEL_REVISION"),
    }
    missing = sorted(set(required) - set(entries_by_key))
    unknown = sorted(set(entries_by_key) - set(required))
    if missing or unknown:
        raise ProvenanceError(
            f"{label} release evidence key set is invalid: missing={missing}; unknown={unknown}"
        )
    mismatches = {
        key: {
            "expected": expected,
            "actual": (
                entries_by_key[key]["component"],
                entries_by_key[key]["evidence"],
                entries_by_key[key]["build_arg"],
            ),
        }
        for key, expected in required.items()
        if (
            entries_by_key[key]["component"],
            entries_by_key[key]["evidence"],
            entries_by_key[key]["build_arg"],
        )
        != expected
    }
    if mismatches:
        raise ProvenanceError(f"{label} release evidence bindings are invalid: {mismatches}")


def load_runtime_contract(path: Path) -> dict[str, Any]:
    return validate_runtime_contract(_load_json(path, label="runtime contract"))


def runtime_contract_sha256(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProvenanceError(f"cannot read runtime contract: {path}: {exc}") from exc
    load_runtime_contract(path)
    return hashlib.sha256(raw).hexdigest()


def runtime_environment(contract: dict[str, Any]) -> dict[str, str]:
    """Return generic Docker ARG values; no runtime technology names are hard-coded."""

    validated = validate_runtime_contract(contract)
    return {entry["build_arg"]: entry["value"] for entry in validated["receipt"]["entries"]}


def runtime_receipt_bytes(contract: dict[str, Any]) -> bytes:
    validated = validate_runtime_contract(contract)
    payload = {
        "schema_version": RUNTIME_RECEIPT_SCHEMA_VERSION,
        "subject": validated["receipt"]["subject"],
        "values": {entry["key"]: entry["value"] for entry in validated["receipt"]["entries"]},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def runtime_build_arguments(contract: dict[str, Any], contract_sha256: str) -> dict[str, str]:
    validated = validate_runtime_contract(contract)
    _validate_sha256(contract_sha256, label="runtime contract SHA-256")
    receipt = runtime_receipt_bytes(validated)
    arguments = runtime_environment(validated)
    arguments[RUNTIME_CONTRACT_SHA256_ARG] = contract_sha256
    arguments[RUNTIME_RECEIPT_B64_ARG] = base64.b64encode(receipt).decode("ascii")
    arguments[RUNTIME_RECEIPT_SHA256_ARG] = hashlib.sha256(receipt).hexdigest()
    return arguments


def runtime_expected_labels(contract: dict[str, Any], contract_sha256: str) -> dict[str, str]:
    validated = validate_runtime_contract(contract)
    arguments = runtime_build_arguments(validated, contract_sha256)
    labels = {entry["oci_label"]: entry["value"] for entry in validated["receipt"]["entries"]}
    labels[RUNTIME_CONTRACT_SHA256_LABEL] = arguments[RUNTIME_CONTRACT_SHA256_ARG]
    labels[RUNTIME_RECEIPT_LABEL] = arguments[RUNTIME_RECEIPT_B64_ARG]
    labels[RUNTIME_RECEIPT_SHA256_LABEL] = arguments[RUNTIME_RECEIPT_SHA256_ARG]
    return labels


def runtime_binary_probes(contract: dict[str, Any]) -> list[tuple[str, str]]:
    """Return the exact in-image paths and hashes declared by binary evidence."""

    validated = validate_runtime_contract(contract)
    require_release_ready(validated)
    probes: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for entry in validated["receipt"]["entries"]:
        if entry["evidence"] != "binary_sha256":
            continue
        matches: list[str] = []
        for use in entry["dockerfile_uses"]:
            if "sha256sum -c -" not in use:
                continue
            matches.extend(
                re.findall(r"(?:^|\s)(/[A-Za-z0-9][A-Za-z0-9_./+-]{0,511})(?:\s|[\"'])", use)
            )
        unique_matches = sorted(set(matches))
        if len(unique_matches) != 1:
            raise ProvenanceError(
                f"binary evidence {entry['key']} must bind one canonical runtime path"
            )
        path = unique_matches[0]
        if ".." in PurePosixPath(path).parts or path in seen_paths:
            raise ProvenanceError(f"binary evidence path is unsafe or duplicate: {path}")
        seen_paths.add(path)
        probes.append((path, entry["value"]))
    if not probes:
        raise ProvenanceError("runtime contract has no actual binary hash probes")
    return sorted(probes)


def _runtime_contract_at_commit(repo_root: Path, commit: str) -> tuple[dict[str, Any], str]:
    raw = _git(repo_root, "show", f"{commit}:{RUNTIME_CONTRACT_PATH}")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("runtime contract in Git commit is not UTF-8") from exc
    contract = validate_runtime_contract(
        _loads_strict(decoded, label="runtime contract in Git commit"),
        label="runtime contract in Git commit",
    )
    return contract, hashlib.sha256(raw).hexdigest()


def verify_runtime_contract_digest(path: Path, expected_sha256: str) -> dict[str, Any]:
    expected_sha256 = _validate_sha256(expected_sha256, label="expected runtime contract SHA-256")
    contract = load_runtime_contract(path)
    try:
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProvenanceError(f"cannot read runtime contract: {path}: {exc}") from exc
    if actual_sha256 != expected_sha256:
        raise ProvenanceError(
            f"runtime contract SHA-256 mismatch: expected={expected_sha256}, actual={actual_sha256}"
        )
    return contract


def verify_dockerfile_contract(contract_path: Path, dockerfile_path: Path) -> None:
    """Prove every receipt entry is a fixed ARG, meaningful use, and OCI label."""

    contract = load_runtime_contract(contract_path)
    try:
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvenanceError(f"cannot read Dockerfile: {dockerfile_path}: {exc}") from exc

    effective_lines = [
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    ]
    effective_dockerfile = "\n".join(effective_lines)
    instructions: list[str] = []
    continued = ""
    for line in effective_lines:
        stripped = line.strip()
        if not stripped and not continued:
            continue
        continued = f"{continued} {stripped}".strip() if continued else stripped
        if continued.endswith("\\"):
            continued = continued[:-1].rstrip()
            continue
        instructions.append(continued)
        continued = ""
    if continued:
        raise ProvenanceError("Dockerfile ends with an incomplete continued instruction")

    def instruction_parts(instruction: str) -> tuple[str, str]:
        match = re.fullmatch(r"([A-Za-z]+)(?:[ \t]+(.*))?", instruction)
        if match is None:
            raise ProvenanceError("Dockerfile contains a malformed instruction")
        return match.group(1).upper(), match.group(2) or ""

    label_instructions = "\n".join(
        instruction for instruction in instructions if instruction_parts(instruction)[0] == "LABEL"
    )

    digest_arguments: set[str] = set()
    for entry in contract["receipt"]["entries"]:
        build_arg = entry["build_arg"]
        value = entry["value"]
        declarations = re.findall(
            rf"^ARG\s+{re.escape(build_arg)}(?:=(.*))?$",
            effective_dockerfile,
            re.MULTILINE,
        )
        if declarations.count(value) != 1 or any(
            declaration not in {"", value} for declaration in declarations
        ):
            raise ProvenanceError(f"Dockerfile does not fix {build_arg} to its contract value")
        for required_use in entry["dockerfile_uses"]:
            if required_use not in effective_dockerfile:
                raise ProvenanceError(
                    f"Dockerfile does not implement {build_arg} use: {required_use!r}"
                )
        variable = rf"(?:\${build_arg}|\$\{{{build_arg}\}})"
        label_binding = re.compile(
            rf"\b{re.escape(entry['oci_label'])}=(?:\"{variable}\"|{variable})(?:\s|$)"
        )
        if not label_binding.search(label_instructions):
            raise ProvenanceError(f"Dockerfile does not bind {entry['oci_label']} to {build_arg}")
        if entry["value_kind"] == "sha256_digest":
            digest_arguments.add(build_arg)

    receipt_arguments = (
        RUNTIME_CONTRACT_SHA256_ARG,
        RUNTIME_RECEIPT_B64_ARG,
        RUNTIME_RECEIPT_SHA256_ARG,
    )
    receipt_bindings = {
        RUNTIME_CONTRACT_SHA256_LABEL: RUNTIME_CONTRACT_SHA256_ARG,
        RUNTIME_RECEIPT_LABEL: RUNTIME_RECEIPT_B64_ARG,
        RUNTIME_RECEIPT_SHA256_LABEL: RUNTIME_RECEIPT_SHA256_ARG,
    }
    for label_name, build_arg in receipt_bindings.items():
        variable = rf"(?:\${build_arg}|\$\{{{build_arg}\}})"
        if not re.search(
            rf"\b{re.escape(label_name)}=(?:\"{variable}\"|{variable})(?:\s|$)",
            label_instructions,
        ):
            raise ProvenanceError(f"Dockerfile is missing receipt label {label_name}")
    receipt_proofs = {
        "inner runtime contract COPY": (
            f"COPY {RUNTIME_CONTRACT_PATH} /tmp/teamagent_runtime_contract.json"
        ),
        "inner contract raw bytes": (
            "pathlib.Path('/tmp/teamagent_runtime_contract.json').read_bytes()"
        ),
        "inner contract JSON": "contract = json.loads(raw)",
        "inner contract raw SHA-256": (
            "assert hashlib.sha256(raw).hexdigest() == "
            "os.environ['RUNTIME_CONTRACT_SHA256']"
        ),
        "inner receipt entries": "entries = contract['receipt']['entries']",
        "strict receipt base64 decode": "base64.b64decode(encoded, validate=True)",
        "canonical receipt base64": (
            "assert base64.b64encode(receipt).decode('ascii') == encoded"
        ),
        "receipt bytes SHA-256": (
            "assert hashlib.sha256(receipt).hexdigest() == "
            "os.environ['RUNTIME_RECEIPT_SHA256']"
        ),
        "contract values": (
            "{entry['key']: entry['value'] for entry in entries}"
        ),
        "receipt subject binding": (
            "'subject': contract['receipt']['subject']"
        ),
        "canonical receipt bytes": (
            "assert receipt == json.dumps(expected_receipt, sort_keys=True, "
            "separators=(',', ':')).encode('utf-8')"
        ),
        "core receipt subject": "assert parsed_receipt['subject'] == 'core'",
        "exact receipt values": "assert parsed_receipt['values'] == expected_values",
        "receipt entry ARG values": (
            "assert all(os.environ[entry['build_arg']] == entry['value'] "
            "for entry in entries)"
        ),
    }
    for proof_name, proof in receipt_proofs.items():
        if proof not in effective_dockerfile:
            raise ProvenanceError(
                f"Dockerfile is missing runtime receipt proof ({proof_name}): {proof}"
            )

    declared_stages: set[str] = set()
    declared_stage_count = 0
    current_stage: str | None = None
    receipt_arg_declarations: dict[str, list[tuple[str | None, str | None]]] = {
        build_arg: [] for build_arg in receipt_arguments
    }
    receipt_label_stages: dict[str, list[str | None]] = {
        label_name: [] for label_name in receipt_bindings
    }
    inner_contract_copy_stages: list[str | None] = []
    receipt_proof_runs: list[tuple[str | None, str]] = []
    for instruction in instructions:
        reference: str | None = None
        stage_alias: str | None = None
        allow_scratch = False
        instruction_name, instruction_arguments = instruction_parts(instruction)
        if instruction_name == "FROM":
            from_match = re.fullmatch(
                r"(?:--platform=\S+[ \t]+)?(\S+)"
                r"(?:[ \t]+AS[ \t]+([A-Za-z0-9_.-]+))?",
                instruction_arguments,
                re.IGNORECASE,
            )
            if from_match is None:
                raise ProvenanceError("Dockerfile contains an unsupported FROM instruction")
            reference, stage_alias = from_match.groups()
            allow_scratch = True
        elif instruction_name == "COPY":
            copy_match = re.search(
                r"(?:^|\s)--from=(\S+)(?:\s|$)",
                instruction_arguments,
                re.IGNORECASE,
            )
            if copy_match is not None:
                reference = copy_match.group(1)
            if (
                instruction_arguments
                == f"{RUNTIME_CONTRACT_PATH} /tmp/teamagent_runtime_contract.json"
            ):
                inner_contract_copy_stages.append(current_stage)
        elif instruction_name == "ARG":
            argument_match = re.fullmatch(
                r"([A-Z][A-Z0-9_]*)(?:=(.*))?",
                instruction_arguments,
            )
            if argument_match is None:
                raise ProvenanceError("Dockerfile contains an unsupported ARG instruction")
            argument_name, default = argument_match.groups()
            if argument_name in receipt_arg_declarations:
                receipt_arg_declarations[argument_name].append((current_stage, default))
        elif instruction_name == "LABEL":
            for label_name in receipt_label_stages:
                if re.search(rf"\b{re.escape(label_name)}=", instruction_arguments):
                    receipt_label_stages[label_name].append(current_stage)
        elif instruction_name == "RUN" and (
            "pathlib.Path('/tmp/teamagent_runtime_contract.json').read_bytes()"
            in instruction_arguments
        ):
            receipt_proof_runs.append((current_stage, instruction_arguments))

        if reference is not None:
            numeric_stage = re.fullmatch(r"0|[1-9][0-9]*", reference)
            numeric_stage_limit = declared_stage_count
            if instruction_name != "FROM":
                numeric_stage_limit -= 1
            named_local_stage = reference in declared_stages and (
                instruction_name == "FROM" or reference != current_stage
            )
            local_stage = named_local_stage or (
                numeric_stage is not None and int(reference) < numeric_stage_limit
            )
            literal_digest = re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", reference)
            argument_digest = re.fullmatch(
                r"\S+@\$\{([A-Z][A-Z0-9_]*)\}",
                reference,
            )
            if not (
                local_stage
                or (allow_scratch and reference == "scratch")
                or literal_digest is not None
                or (argument_digest is not None and argument_digest.group(1) in digest_arguments)
            ):
                raise ProvenanceError(
                    f"Dockerfile external image is not digest pinned: {reference}"
                )
        if instruction_name == "FROM":
            declared_stage_count += 1
            current_stage = stage_alias
        if stage_alias is not None:
            declared_stages.add(stage_alias)

    expected_receipt_arg_declarations = [("builder", None), ("final", None)]
    for build_arg, declarations in receipt_arg_declarations.items():
        if declarations != expected_receipt_arg_declarations:
            raise ProvenanceError(
                "Dockerfile receipt ARG must be declared exactly once without a default "
                f"in builder and final stages: {build_arg}; actual={declarations}"
            )
    if inner_contract_copy_stages != ["builder"]:
        raise ProvenanceError(
            "Dockerfile must COPY the inner runtime contract exactly once in builder: "
            f"actual={inner_contract_copy_stages}"
        )
    if len(receipt_proof_runs) != 1 or receipt_proof_runs[0][0] != "builder":
        raise ProvenanceError(
            "Dockerfile must verify the runtime receipt exactly once in builder: "
            f"actual={[stage for stage, _instruction in receipt_proof_runs]}"
        )
    receipt_proof_run = receipt_proof_runs[0][1]
    for proof_name, proof in receipt_proofs.items():
        if proof_name == "inner runtime contract COPY":
            continue
        if proof not in receipt_proof_run:
            raise ProvenanceError(
                "Dockerfile runtime receipt proof must be atomic in one builder RUN "
                f"({proof_name})"
            )
    for label_name, stages in receipt_label_stages.items():
        if stages != ["final"]:
            raise ProvenanceError(
                f"Dockerfile receipt label must be declared exactly once in final: "
                f"{label_name}; actual={stages}"
            )


def _decode_ls_tree(raw: bytes) -> tuple[int, list[str]]:
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
            raise ProvenanceError("unsupported entry in git ls-tree output") from exc
        _validate_archive_path(path, label="Git tree")
        if path == MANIFEST_NAME:
            raise ProvenanceError(f"{MANIFEST_NAME} must be generated, not tracked")
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ProvenanceError(
                f"unsupported Git entry (only regular files are allowed): {mode.decode()} {path}"
            )
        file_count += 1
        if mode == b"100755":
            executable_paths.append(path)
    executable_paths.sort(key=lambda item: item.encode("utf-8"))
    return file_count, executable_paths


def create_manifest(
    repo_root: Path,
    commit: str,
    branch: str,
    with_scrape_tools: str,
    app_html_version_id: str,
    app_html_sha256: str,
    output: Path,
) -> dict[str, Any]:
    """Write a manifest tied to a full SHA-1 commit and its source tree."""

    repo_root = repo_root.resolve()
    if not _SHA1_RE.fullmatch(commit):
        raise ProvenanceError("commit must be a full lowercase SHA-1")
    if not branch or "\n" in branch or "\r" in branch:
        raise ProvenanceError("branch must be a non-empty single-line name")
    if with_scrape_tools not in {"true", "false"}:
        raise ProvenanceError("with_scrape_tools must be exactly 'true' or 'false'")
    _validate_s3_version_id(app_html_version_id, label="app_html_version_id")
    _validate_sha256(app_html_sha256, label="app_html_sha256")

    object_format = _git(repo_root, "rev-parse", "--show-object-format").decode().strip()
    if object_format != "sha1":
        raise ProvenanceError(f"unsupported Git object format: {object_format}")
    resolved = _git(repo_root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved != commit:
        raise ProvenanceError(f"commit did not resolve exactly: expected {commit}, got {resolved}")
    _contract, runtime_contract_sha256 = _runtime_contract_at_commit(repo_root, commit)

    tracked_manifest = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{commit}:{MANIFEST_NAME}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if tracked_manifest.returncode == 0:
        raise ProvenanceError(f"{MANIFEST_NAME} must not be tracked")

    commit_object = _git(repo_root, "cat-file", "commit", commit)
    if _git_object_id("commit", commit_object) != commit:
        raise ProvenanceError("Git commit object did not hash to the requested commit")
    tree = _git(repo_root, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    if not _SHA1_RE.fullmatch(tree):
        raise ProvenanceError("Git returned an invalid tree object ID")
    file_count, executable_paths = _decode_ls_tree(
        _git(repo_root, "ls-tree", "-r", "-z", "--full-tree", commit)
    )

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "commit": commit,
        "branch": branch,
        "build_parameters": {
            "with_scrape_tools": with_scrape_tools == "true",
            "app_html": {
                "bucket": APP_HTML_BUCKET,
                "key": APP_HTML_KEY,
                "version_id": app_html_version_id,
                "sha256": app_html_sha256,
            },
            "runtime_contract": {
                "path": RUNTIME_CONTRACT_PATH,
                "sha256": runtime_contract_sha256,
            },
        },
        "archive": {
            "format": "zip",
            "producer": "git archive",
            "source_key": SOURCE_KEY,
            "manifest_path": MANIFEST_NAME,
            "verification": "git-tree-object-v1",
            "git_object_format": object_format,
            "tree": tree,
            "file_count": file_count,
            "executable_paths": executable_paths,
        },
        "commit_object_base64": base64.b64encode(commit_object).decode("ascii"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _validate_manifest_schema(
    manifest: Any,
) -> tuple[str, str, str, str, str, str, dict[str, Any], bytes]:
    if not isinstance(manifest, dict):
        raise ProvenanceError("source manifest must be a JSON object")
    _require_exact_keys(
        manifest,
        {
            "schema_version",
            "commit",
            "branch",
            "build_parameters",
            "archive",
            "commit_object_base64",
        },
        label="source manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ProvenanceError(f"unsupported source manifest schema: {manifest['schema_version']!r}")

    commit = manifest["commit"]
    branch = manifest["branch"]
    build_parameters = manifest["build_parameters"]
    archive = manifest["archive"]
    encoded_commit = manifest["commit_object_base64"]
    if not isinstance(commit, str) or not _SHA1_RE.fullmatch(commit):
        raise ProvenanceError("manifest commit must be a full lowercase SHA-1")
    if not isinstance(branch, str) or not branch or "\n" in branch or "\r" in branch:
        raise ProvenanceError("manifest branch must be a non-empty single-line string")
    if not isinstance(build_parameters, dict):
        raise ProvenanceError("manifest build_parameters must be an object")
    _require_exact_keys(
        build_parameters,
        {"with_scrape_tools", "app_html", "runtime_contract"},
        label="source manifest build_parameters",
    )
    if not isinstance(build_parameters["with_scrape_tools"], bool):
        raise ProvenanceError("manifest with_scrape_tools must be a JSON boolean")
    with_scrape_tools = "true" if build_parameters["with_scrape_tools"] else "false"
    app_html = build_parameters["app_html"]
    if not isinstance(app_html, dict):
        raise ProvenanceError("manifest app_html contract must be an object")
    _require_exact_keys(
        app_html,
        {"bucket", "key", "version_id", "sha256"},
        label="source manifest app_html",
    )
    if app_html["bucket"] != APP_HTML_BUCKET or app_html["key"] != APP_HTML_KEY:
        raise ProvenanceError("manifest app_html S3 object does not match the fixed contract")
    app_html_version_id = _validate_s3_version_id(
        app_html["version_id"], label="manifest app_html version_id"
    )
    app_html_sha256 = _validate_sha256(app_html["sha256"], label="manifest app_html sha256")
    runtime_contract = build_parameters["runtime_contract"]
    if not isinstance(runtime_contract, dict):
        raise ProvenanceError("manifest runtime_contract must be an object")
    _require_exact_keys(
        runtime_contract,
        {"path", "sha256"},
        label="source manifest runtime_contract",
    )
    if runtime_contract["path"] != RUNTIME_CONTRACT_PATH:
        raise ProvenanceError("manifest runtime_contract path does not match the fixed contract")
    runtime_contract_sha256 = _validate_sha256(
        runtime_contract["sha256"], label="manifest runtime_contract sha256"
    )
    if not isinstance(archive, dict):
        raise ProvenanceError("manifest archive contract must be an object")
    _require_exact_keys(
        archive,
        {
            "format",
            "producer",
            "source_key",
            "manifest_path",
            "verification",
            "git_object_format",
            "tree",
            "file_count",
            "executable_paths",
        },
        label="source manifest archive",
    )
    constants = {
        "format": "zip",
        "producer": "git archive",
        "source_key": SOURCE_KEY,
        "manifest_path": MANIFEST_NAME,
        "verification": "git-tree-object-v1",
        "git_object_format": "sha1",
    }
    for key, expected in constants.items():
        if archive[key] != expected:
            raise ProvenanceError(
                f"manifest archive {key} mismatch: expected {expected!r}, got {archive[key]!r}"
            )
    if not isinstance(archive["tree"], str) or not _SHA1_RE.fullmatch(archive["tree"]):
        raise ProvenanceError("manifest archive tree must be a full lowercase SHA-1")
    if not isinstance(archive["file_count"], int) or isinstance(archive["file_count"], bool):
        raise ProvenanceError("manifest archive file_count must be an integer")
    if archive["file_count"] < 1:
        raise ProvenanceError("manifest archive file_count must be positive")
    executable_paths = archive["executable_paths"]
    if not isinstance(executable_paths, list) or not all(
        isinstance(path, str) for path in executable_paths
    ):
        raise ProvenanceError("manifest executable_paths must be an array of strings")
    for path in executable_paths:
        _validate_archive_path(path, label="executable")
        if path == MANIFEST_NAME:
            raise ProvenanceError("generated manifest cannot be an executable source path")
    expected_order = sorted(executable_paths, key=lambda item: item.encode("utf-8"))
    if executable_paths != expected_order or len(executable_paths) != len(set(executable_paths)):
        raise ProvenanceError("manifest executable_paths must be unique and bytewise sorted")
    if not isinstance(encoded_commit, str):
        raise ProvenanceError("manifest commit_object_base64 must be a string")
    try:
        commit_object = base64.b64decode(encoded_commit, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProvenanceError("manifest commit_object_base64 is invalid") from exc
    return (
        commit,
        branch,
        with_scrape_tools,
        app_html_version_id,
        app_html_sha256,
        runtime_contract_sha256,
        archive,
        commit_object,
    )


def _commit_tree(commit_object: bytes) -> str:
    headers = commit_object.split(b"\n\n", 1)[0].splitlines()
    trees = [line[5:] for line in headers if line.startswith(b"tree ")]
    if len(trees) != 1:
        raise ProvenanceError("commit proof must contain exactly one tree header")
    try:
        tree = trees[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("commit proof tree header is not ASCII") from exc
    if not _SHA1_RE.fullmatch(tree):
        raise ProvenanceError("commit proof tree header is invalid")
    return tree


def _tree_object_id(
    directory: Path,
    root: Path,
    executable_paths: set[str],
    *,
    is_root: bool,
) -> tuple[str, int]:
    entries: list[tuple[bytes, bytes, str]] = []
    file_count = 0
    try:
        children = list(os.scandir(directory))
    except OSError as exc:
        raise ProvenanceError(f"cannot enumerate source directory: {directory}: {exc}") from exc

    for child in children:
        relative = Path(child.path).relative_to(root).as_posix()
        if is_root and relative == MANIFEST_NAME:
            continue
        _validate_archive_path(relative, label="source")
        try:
            child_stat = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise ProvenanceError(f"cannot stat source entry: {relative}: {exc}") from exc
        if stat.S_ISLNK(child_stat.st_mode):
            raise ProvenanceError(f"symlinks are not supported by the archive contract: {relative}")
        try:
            raw_name = child.name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProvenanceError(f"source path is not UTF-8: {relative!r}") from exc

        if stat.S_ISDIR(child_stat.st_mode):
            object_id, nested_count = _tree_object_id(
                Path(child.path), root, executable_paths, is_root=False
            )
            mode = b"40000"
            sort_key = raw_name + b"/"
            file_count += nested_count
        elif stat.S_ISREG(child_stat.st_mode):
            try:
                payload = Path(child.path).read_bytes()
            except OSError as exc:
                raise ProvenanceError(f"cannot read source file: {relative}: {exc}") from exc
            object_id = _git_object_id("blob", payload)
            mode = b"100755" if relative in executable_paths else b"100644"
            sort_key = raw_name + b"\0"
            file_count += 1
        else:
            raise ProvenanceError(f"unsupported source entry type: {relative}")
        entries.append((sort_key, mode + b" " + raw_name + b"\0", object_id))

    entries.sort(key=lambda item: item[0])
    payload = b"".join(prefix + bytes.fromhex(object_id) for _key, prefix, object_id in entries)
    return _git_object_id("tree", payload), file_count


def verify_source(
    source_root: Path,
    manifest_path: Path,
    expected_commit: str,
    expected_branch: str,
    expected_with_scrape_tools: str,
    expected_app_html_version_id: str,
    expected_app_html_sha256: str,
    expected_runtime_contract_sha256: str,
) -> None:
    """Verify env values, commit proof, and every extracted source byte."""

    source_root = source_root.resolve()
    manifest_path = manifest_path.resolve()
    if manifest_path.parent != source_root or manifest_path.name != MANIFEST_NAME:
        raise ProvenanceError(f"manifest must be {MANIFEST_NAME} at the source root")
    if not source_root.is_dir() or not manifest_path.is_file():
        raise ProvenanceError("source root or generated manifest is missing")
    manifest = _load_json(manifest_path, label="source manifest")
    (
        commit,
        branch,
        with_scrape_tools,
        app_html_version_id,
        app_html_sha256,
        runtime_contract_sha256,
        archive,
        commit_object,
    ) = _validate_manifest_schema(manifest)
    if expected_commit != commit:
        raise ProvenanceError(
            f"GIT_COMMIT mismatch: environment={expected_commit!r}, manifest={commit!r}"
        )
    if expected_branch != branch:
        raise ProvenanceError(
            f"GIT_BRANCH mismatch: environment={expected_branch!r}, manifest={branch!r}"
        )
    if expected_with_scrape_tools != with_scrape_tools:
        raise ProvenanceError(
            "WITH_SCRAPE_TOOLS mismatch: "
            f"environment={expected_with_scrape_tools!r}, manifest={with_scrape_tools!r}"
        )
    if expected_app_html_version_id != app_html_version_id:
        raise ProvenanceError(
            "APP_HTML_VERSION_ID mismatch: "
            f"environment={expected_app_html_version_id!r}, manifest={app_html_version_id!r}"
        )
    if expected_app_html_sha256 != app_html_sha256:
        raise ProvenanceError(
            "APP_HTML_SHA256 mismatch: "
            f"environment={expected_app_html_sha256!r}, manifest={app_html_sha256!r}"
        )
    expected_runtime_contract_sha256 = _validate_sha256(
        expected_runtime_contract_sha256,
        label="expected runtime contract SHA-256",
    )
    if expected_runtime_contract_sha256 != runtime_contract_sha256:
        raise ProvenanceError(
            "RUNTIME_CONTRACT_SHA256 mismatch: "
            f"environment={expected_runtime_contract_sha256!r}, "
            f"manifest={runtime_contract_sha256!r}"
        )
    verify_runtime_contract_digest(
        source_root / RUNTIME_CONTRACT_PATH,
        expected_runtime_contract_sha256,
    )
    if _git_object_id("commit", commit_object) != commit:
        raise ProvenanceError("commit proof does not hash to manifest commit")
    proof_tree = _commit_tree(commit_object)
    if proof_tree != archive["tree"]:
        raise ProvenanceError(
            f"commit proof tree mismatch: proof={proof_tree}, manifest={archive['tree']}"
        )

    executable_paths = set(archive["executable_paths"])
    actual_tree, actual_count = _tree_object_id(
        source_root, source_root, executable_paths, is_root=True
    )
    if actual_count != archive["file_count"]:
        raise ProvenanceError(
            f"source file count mismatch: archive={actual_count}, manifest={archive['file_count']}"
        )
    present_files = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    missing_executables = sorted(executable_paths - present_files)
    if missing_executables:
        raise ProvenanceError(f"manifest executable paths are missing: {missing_executables}")
    if actual_tree != archive["tree"]:
        raise ProvenanceError(
            f"source tree mismatch: archive={actual_tree}, commit={archive['tree']}"
        )


def ecr_config_digest(batch_response_path: Path, expected_image_digest: str) -> str:
    """Validate an ECR BatchGetImage response and return its config digest."""

    if not _SHA256_DIGEST_RE.fullmatch(expected_image_digest):
        raise ProvenanceError("expected ECR image digest is invalid")
    response = _load_json(batch_response_path, label="ECR BatchGetImage response")
    if not isinstance(response, dict):
        raise ProvenanceError("ECR BatchGetImage response must be an object")
    failures = response.get("failures")
    images = response.get("images")
    if not isinstance(failures, list) or failures:
        raise ProvenanceError(f"ECR BatchGetImage returned failures: {failures!r}")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ProvenanceError("ECR BatchGetImage must return exactly one image")
    image = images[0]
    image_id = image.get("imageId")
    image_manifest_raw = image.get("imageManifest")
    if not isinstance(image_id, dict) or image_id.get("imageDigest") != expected_image_digest:
        raise ProvenanceError("ECR BatchGetImage returned a different image digest")
    if not isinstance(image_manifest_raw, str):
        raise ProvenanceError("ECR BatchGetImage imageManifest is missing")
    actual_manifest_digest = (
        "sha256:" + hashlib.sha256(image_manifest_raw.encode("utf-8")).hexdigest()
    )
    if actual_manifest_digest != expected_image_digest:
        raise ProvenanceError("ECR image manifest bytes do not hash to the requested image digest")

    image_manifest = _loads_strict(image_manifest_raw, label="OCI image manifest")
    if not isinstance(image_manifest, dict) or image_manifest.get("schemaVersion") != 2:
        raise ProvenanceError("unsupported OCI image manifest schema")
    media_type = image_manifest.get("mediaType")
    if media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
        raise ProvenanceError(f"unsupported OCI image manifest media type: {media_type!r}")
    config = image_manifest.get("config")
    if not isinstance(config, dict):
        raise ProvenanceError("OCI image manifest config descriptor is missing")
    digest = config.get("digest")
    if not isinstance(digest, str) or not _SHA256_DIGEST_RE.fullmatch(digest):
        raise ProvenanceError("OCI config digest is invalid")
    return digest


def verify_oci_revision(
    config_path: Path,
    expected_config_digest: str,
    expected_commit: str,
    runtime_contract_path: Path,
    expected_runtime_contract_sha256: str,
    expected_os: str = "linux",
    expected_architecture: str = "arm64",
) -> None:
    """Verify OCI identity and the exact core runtime contract receipt."""

    if not _SHA256_DIGEST_RE.fullmatch(expected_config_digest):
        raise ProvenanceError("expected OCI config digest is invalid")
    if not _SHA1_RE.fullmatch(expected_commit):
        raise ProvenanceError("expected OCI revision must be a full lowercase SHA-1")
    contract = verify_runtime_contract_digest(
        runtime_contract_path,
        expected_runtime_contract_sha256,
    )
    require_release_ready(contract)
    expected_runtime_labels = runtime_expected_labels(
        contract,
        expected_runtime_contract_sha256,
    )
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ProvenanceError(f"cannot read OCI config: {exc}") from exc
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_config_digest:
        raise ProvenanceError(
            f"OCI config digest mismatch: expected={expected_config_digest}, actual={actual_digest}"
        )
    try:
        config = _loads_strict(raw.decode("utf-8"), label="OCI config")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("OCI config is not UTF-8") from exc
    if not isinstance(config, dict) or not isinstance(config.get("config"), dict):
        raise ProvenanceError("OCI config object is missing config")
    if config.get("os") != expected_os or config.get("architecture") != expected_architecture:
        raise ProvenanceError(
            "OCI platform mismatch: "
            f"expected={expected_os}/{expected_architecture}, "
            f"actual={config.get('os')!r}/{config.get('architecture')!r}"
        )
    variant = config.get("variant")
    if expected_architecture == "arm64" and variant not in {None, "", "v8"}:
        raise ProvenanceError(f"OCI arm64 variant is unsupported: {variant!r}")
    labels = config["config"].get("Labels")
    if not isinstance(labels, dict):
        raise ProvenanceError("OCI config labels are missing")
    revision = labels.get("org.opencontainers.image.revision")
    if revision != expected_commit:
        raise ProvenanceError(
            f"OCI revision mismatch: expected={expected_commit}, actual={revision!r}"
        )
    for label_name, expected_value in expected_runtime_labels.items():
        actual_value = labels.get(label_name)
        if actual_value != expected_value:
            raise ProvenanceError(
                f"OCI {label_name} mismatch: expected={expected_value!r}, actual={actual_value!r}"
            )

    encoded_receipt = labels[RUNTIME_RECEIPT_LABEL]
    if not isinstance(encoded_receipt, str):
        raise ProvenanceError("OCI runtime receipt label must be a string")
    try:
        receipt_bytes = base64.b64decode(encoded_receipt, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProvenanceError("OCI runtime receipt is not canonical base64") from exc
    if base64.b64encode(receipt_bytes).decode("ascii") != encoded_receipt:
        raise ProvenanceError("OCI runtime receipt is not canonical base64")
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_sha256 != labels[RUNTIME_RECEIPT_SHA256_LABEL]:
        raise ProvenanceError("OCI runtime receipt bytes do not match its SHA-256 label")
    try:
        decoded_receipt = receipt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("OCI runtime receipt is not UTF-8") from exc
    receipt = _loads_strict(decoded_receipt, label="OCI runtime receipt")
    if not isinstance(receipt, dict):
        raise ProvenanceError("OCI runtime receipt must be an object")
    _require_exact_keys(
        receipt,
        {"schema_version", "subject", "values"},
        label="OCI runtime receipt",
    )
    if receipt["schema_version"] != RUNTIME_RECEIPT_SCHEMA_VERSION:
        raise ProvenanceError("OCI runtime receipt schema is unsupported")
    if receipt["subject"] != "core":
        raise ProvenanceError("OCI runtime receipt subject must be 'core'")
    values = receipt["values"]
    if not isinstance(values, dict):
        raise ProvenanceError("OCI runtime receipt values must be an object")
    expected_values = {entry["key"]: entry["value"] for entry in contract["receipt"]["entries"]}
    _require_exact_keys(values, set(expected_values), label="OCI runtime receipt values")
    if values != expected_values or receipt_bytes != runtime_receipt_bytes(contract):
        raise ProvenanceError("OCI runtime receipt does not exactly match the expected allowlist")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-manifest")
    create.add_argument("--repo-root", type=Path, required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--with-scrape-tools", choices=("true", "false"), required=True)
    create.add_argument("--app-html-version-id", required=True)
    create.add_argument("--app-html-sha256", required=True)
    create.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify-source")
    verify.add_argument("--source-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--expected-branch", required=True)
    verify.add_argument("--expected-with-scrape-tools", choices=("true", "false"), required=True)
    verify.add_argument("--expected-app-html-version-id", required=True)
    verify.add_argument("--expected-app-html-sha256", required=True)
    verify.add_argument("--expected-runtime-contract-sha256", required=True)

    contract_sha256 = subparsers.add_parser("contract-sha256")
    contract_sha256.add_argument("--contract", type=Path, required=True)

    release_ready = subparsers.add_parser("assert-release-ready")
    release_ready.add_argument("--contract", type=Path, required=True)

    build_arguments = subparsers.add_parser("docker-build-arguments")
    build_arguments.add_argument("--contract", type=Path, required=True)
    build_arguments.add_argument("--expected-contract-sha256", required=True)

    expected_labels = subparsers.add_parser("expected-runtime-labels")
    expected_labels.add_argument("--contract", type=Path, required=True)
    expected_labels.add_argument("--expected-contract-sha256", required=True)

    binary_probes = subparsers.add_parser("runtime-binary-probes")
    binary_probes.add_argument("--contract", type=Path, required=True)

    dockerfile_contract = subparsers.add_parser("verify-dockerfile-contract")
    dockerfile_contract.add_argument("--contract", type=Path, required=True)
    dockerfile_contract.add_argument("--dockerfile", type=Path, required=True)

    config_digest = subparsers.add_parser("ecr-config-digest")
    config_digest.add_argument("--batch-response", type=Path, required=True)
    config_digest.add_argument("--expected-image-digest", required=True)

    revision = subparsers.add_parser("verify-oci-revision")
    revision.add_argument("--config", type=Path, required=True)
    revision.add_argument("--expected-config-digest", required=True)
    revision.add_argument("--expected-commit", required=True)
    revision.add_argument("--contract", type=Path, required=True)
    revision.add_argument("--expected-runtime-contract-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create-manifest":
            create_manifest(
                args.repo_root,
                args.commit,
                args.branch,
                args.with_scrape_tools,
                args.app_html_version_id,
                args.app_html_sha256,
                args.output,
            )
            print(f"source manifest created for {args.commit}")
        elif args.command == "verify-source":
            verify_source(
                args.source_root,
                args.manifest,
                args.expected_commit,
                args.expected_branch,
                args.expected_with_scrape_tools,
                args.expected_app_html_version_id,
                args.expected_app_html_sha256,
                args.expected_runtime_contract_sha256,
            )
            print(f"source provenance verified: {args.expected_commit} ({args.expected_branch})")
        elif args.command == "contract-sha256":
            print(runtime_contract_sha256(args.contract))
        elif args.command == "assert-release-ready":
            require_release_ready(load_runtime_contract(args.contract))
            print("runtime contract release evidence is complete")
        elif args.command == "docker-build-arguments":
            contract = verify_runtime_contract_digest(
                args.contract,
                args.expected_contract_sha256,
            )
            for name, value in runtime_build_arguments(
                contract,
                args.expected_contract_sha256,
            ).items():
                print(f"{name}={value}")
        elif args.command == "expected-runtime-labels":
            contract = verify_runtime_contract_digest(
                args.contract,
                args.expected_contract_sha256,
            )
            for name, value in runtime_expected_labels(
                contract,
                args.expected_contract_sha256,
            ).items():
                print(f"{name}\t{value}")
        elif args.command == "runtime-binary-probes":
            for path, digest in runtime_binary_probes(load_runtime_contract(args.contract)):
                print(f"{path}\t{digest}")
        elif args.command == "verify-dockerfile-contract":
            verify_dockerfile_contract(args.contract, args.dockerfile)
            print("Dockerfile implements the complete runtime contract")
        elif args.command == "ecr-config-digest":
            print(ecr_config_digest(args.batch_response, args.expected_image_digest))
        elif args.command == "verify-oci-revision":
            verify_oci_revision(
                args.config,
                args.expected_config_digest,
                args.expected_commit,
                args.contract,
                args.expected_runtime_contract_sha256,
            )
            print(f"OCI revision verified: {args.expected_commit}")
        else:  # pragma: no cover - argparse enforces a known command.
            raise ProvenanceError(f"unsupported command: {args.command}")
    except ProvenanceError as exc:
        print(f"FATAL provenance verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
