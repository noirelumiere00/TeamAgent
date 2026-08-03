#!/usr/bin/env python3
"""Validate the TeamAgent core/media release and application provenance interface."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

_TRUSTED_HELPER_DIRECTORY = str(Path(__file__).resolve().parent)
if _TRUSTED_HELPER_DIRECTORY not in sys.path:
    sys.path.insert(0, _TRUSTED_HELPER_DIRECTORY)

from teamagent_release_approval import APPROVAL_OBSERVATION_KEYS  # noqa: E402
from teamagent_schema_versions import SCHEMA_VERSIONS  # noqa: E402

CONTRACT_KIND = "teamagent.core-media-release-contract"
CONTRACT_SCHEMA_VERSION = SCHEMA_VERSIONS.outer_core_media_contract
RUNTIME_CONTRACT_SCHEMA_VERSION = SCHEMA_VERSIONS.inner_runtime_contract
RUNTIME_RECEIPT_SCHEMA_VERSION = 2
PROVENANCE_RECORD_RE = re.compile(
    r"^<!-- PRODUCTION_APP_PROVENANCE=(\{.+\}) -->$",
    re.MULTILINE,
)
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
S3_VERSION_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,254}")
ARG_RE = re.compile(r"[A-Z][A-Z0-9_]*")
CONTRACT_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
PRODUCTION_KEYS = {
    "app_html_s3_version_id",
    "app_html_sha256",
    "vault_manifest_sha256",
    "build_inputs_sha256",
}
EXPECTED_SUBJECTS = {
    "core": {
        "dockerfile": "infra/docker/Dockerfile.teamagent-mcp",
        "runtime_kind": "core",
        "repositories": {
            "quarantine": "teamagent-mcp-quarantine",
            "verified_candidate": "teamagent-mcp-verified-candidates",
            "release": "teamagent-mcp",
        },
    },
    "media": {
        "dockerfile": "infra/docker/Dockerfile.teamagent-media-worker",
        "runtime_kind": "media-worker",
        "repositories": {
            "quarantine": "teamagent-media-worker-quarantine",
            "verified_candidate": "teamagent-media-worker-verified-candidates",
            "release": "teamagent-media-worker",
        },
    },
}
UNTRUSTED_PLACEHOLDER_VALUES = {
    "",
    "n/a",
    "none",
    "null",
    "placeholder",
    "tbd",
    "unknown",
}
COMMON_STATIC_TEAMAGENT_LABELS = {
    "io.teamagent.runtime.uid": "10001",
    "io.teamagent.runtime.gid": "10001",
    "io.teamagent.runtime.volume": "/tmp",
    "io.teamagent.runtime.contract": "fargate-readonly-v1",
}
CORE_RECEIPT_LABEL_BINDINGS = {
    "io.teamagent.build.runtime-contract-sha256": "RUNTIME_CONTRACT_SHA256",
    "io.teamagent.build.runtime-receipt": "RUNTIME_RECEIPT_B64",
    "io.teamagent.build.runtime-receipt-sha256": "RUNTIME_RECEIPT_SHA256",
}
RELEASE_APPROVAL_BUILD_ARG = "RELEASE_APPROVAL_SHA256"
RELEASE_APPROVAL_LABEL = "io.teamagent.build.release-approval-sha256"
EXPECTED_PROBE_PATHS = {
    "core": {
        "app.baked-fallback.sha256": "/app/src/teamagent/connect_web/static/app.html",
        "binary.python.sha256": "/usr/bin/python3.14",
    },
    "media": {
        "binary.chromium.sha256": "/usr/lib/chromium/chromium",
        "binary.ffmpeg.sha256": "/usr/bin/ffmpeg",
        "binary.node.sha256": "/usr/bin/node",
        "binary.python.sha256": "/usr/bin/python3",
    },
}
FORBIDDEN_LEGACY_ARGUMENTS = {
    "NODE_IMAGE_DIGEST",
}


class ProvenanceError(ValueError):
    """A release or application provenance invariant failed."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads(value: str, *, label: str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ProvenanceError(f"{label} is not valid JSON") from exc


def _load(path: Path, *, label: str) -> Any:
    try:
        return _loads(path.read_text(encoding="utf-8"), label=label)
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvenanceError(f"cannot read {label}: {path}") from exc


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProvenanceError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        raise ProvenanceError(f"{label} schema mismatch: missing={missing}, unknown={unknown}")


def _text(value: Any, *, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProvenanceError(f"{label} must be non-empty text")
    return value


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a lowercase SHA-256")
    return value


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a sha256 digest")
    return value


def _version_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not S3_VERSION_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be an exact S3 VersionId")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def latest_production_record(deploy_log: Path) -> dict[str, Any]:
    """Return the newest production /app record, failing on a malformed newest entry."""

    try:
        body = deploy_log.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvenanceError("cannot read the production deploy log") from exc
    sections = re.split(r"(?m)^## ", body)
    if len(sections) < 2:
        raise ProvenanceError("production deploy log has no entries")
    section = next(
        (
            item.split("\n---\n", maxsplit=1)[0]
            for item in sections[1:]
            if "/app" in item.partition("\n")[0] and "本番" in item.partition("\n")[0]
        ),
        None,
    )
    if section is None:
        raise ProvenanceError("production deploy log has no /app production entry")
    matches = PROVENANCE_RECORD_RE.findall(section)
    if len(matches) != 1:
        raise ProvenanceError(
            "newest /app production entry must contain exactly one provenance record"
        )
    record = _mapping(_loads(matches[0], label="production app record"), label="record")
    _exact_keys(record, {"schema_version", *PRODUCTION_KEYS}, label="record")
    if record["schema_version"] != 1:
        raise ProvenanceError("production app record schema is unsupported")
    _version_id(
        record["app_html_s3_version_id"],
        label="production app S3 VersionId",
    )
    for key in PRODUCTION_KEYS - {"app_html_s3_version_id"}:
        _sha256(record[key], label=key)
    prose = PROVENANCE_RECORD_RE.sub("", section)
    for key in sorted(PRODUCTION_KEYS):
        if record[key] not in prose:
            raise ProvenanceError(f"production prose does not corroborate {key}")
    return dict(record)


def load_contract(path: Path) -> dict[str, Any]:
    contract = _mapping(_load(path, label="core/media release contract"), label="contract")
    _exact_keys(
        contract,
        {
            "schema_version",
            "kind",
            "release",
            "source_runtime_contract",
            "app_html",
            "subjects",
        },
        label="contract",
    )
    if contract["schema_version"] != CONTRACT_SCHEMA_VERSION or contract["kind"] != CONTRACT_KIND:
        raise ProvenanceError("core/media release contract kind or schema mismatch")

    release = _mapping(contract["release"], label="contract.release")
    _exact_keys(release, {"ready", "blocked_reason"}, label="contract.release")
    if not isinstance(release["ready"], bool):
        raise ProvenanceError("contract.release.ready must be boolean")
    if not isinstance(release["blocked_reason"], str):
        raise ProvenanceError("contract.release.blocked_reason must be text")
    if release["ready"] == bool(release["blocked_reason"]):
        raise ProvenanceError(
            "ready contracts need an empty blocked_reason; blocked contracts need a reason"
        )
    runtime_contract = _mapping(
        contract["source_runtime_contract"],
        label="contract.source_runtime_contract",
    )
    _exact_keys(
        runtime_contract,
        {"path", "sha256"},
        label="contract.source_runtime_contract",
    )
    if runtime_contract["path"] != "infra/codebuild/teamagent_runtime_contract.json":
        raise ProvenanceError("source runtime contract path is not fixed")
    _sha256(runtime_contract["sha256"], label="source runtime contract SHA-256")

    app_html = _mapping(contract["app_html"], label="contract.app_html")
    _exact_keys(
        app_html,
        {"bucket", "key", "production", "baked_fallback"},
        label="contract.app_html",
    )
    if (
        app_html["bucket"] != "teamagent-dev-raw-files"
        or app_html["key"] != "codebuild/connect-web-app.html"
    ):
        raise ProvenanceError("application S3 location is not fixed")
    production = _mapping(app_html["production"], label="contract app production")
    _exact_keys(production, PRODUCTION_KEYS, label="contract app production")
    _version_id(
        production["app_html_s3_version_id"],
        label="contract production VersionId",
    )
    for key in PRODUCTION_KEYS - {"app_html_s3_version_id"}:
        _sha256(production[key], label=f"contract {key}")
    fallback = _mapping(app_html["baked_fallback"], label="contract baked fallback")
    _exact_keys(
        fallback,
        {"key", "s3_version_id", "sha256"},
        label="contract baked fallback",
    )
    if fallback["key"] != "codebuild/baked-fallback/connect-web-app.html":
        raise ProvenanceError("baked fallback S3 key is not fixed")
    if fallback["key"] == app_html["key"]:
        raise ProvenanceError("baked fallback must not share the live application key")
    _sha256(fallback["sha256"], label="baked fallback SHA-256")
    if fallback["s3_version_id"] is not None:
        _version_id(fallback["s3_version_id"], label="baked fallback VersionId")
    if release["ready"] and fallback["s3_version_id"] is None:
        raise ProvenanceError("ready contract lacks the baked fallback S3 VersionId")

    subjects = contract["subjects"]
    if not isinstance(subjects, list) or len(subjects) != len(EXPECTED_SUBJECTS):
        raise ProvenanceError("contract must define exactly core and media subjects")
    seen: set[str] = set()
    for index, raw_subject in enumerate(subjects):
        subject = _mapping(raw_subject, label=f"contract.subjects[{index}]")
        _exact_keys(
            subject,
            {
                "name",
                "dockerfile",
                "runtime_kind",
                "repositories",
                "required_build_args",
                "required_label_bindings",
                "source_assertions",
                "binary_probes",
            },
            label=f"contract.subjects[{index}]",
        )
        name = _text(subject["name"], label="subject name")
        expected = EXPECTED_SUBJECTS.get(name)
        if expected is None or name in seen:
            raise ProvenanceError("subject name is duplicate or not allowlisted")
        seen.add(name)
        for field in ("dockerfile", "runtime_kind"):
            if subject[field] != expected[field]:
                raise ProvenanceError(f"{name} {field} does not match the interface")
        repositories = _mapping(subject["repositories"], label=f"{name} repositories")
        _exact_keys(
            repositories,
            {"quarantine", "verified_candidate", "release"},
            label=f"{name} repositories",
        )
        if dict(repositories) != expected["repositories"]:
            raise ProvenanceError(f"{name} repositories do not match the allowlist")
        arguments = subject["required_build_args"]
        if (
            not isinstance(arguments, list)
            or arguments != sorted(arguments)
            or len(arguments) != len(set(arguments))
            or not all(isinstance(item, str) and ARG_RE.fullmatch(item) for item in arguments)
        ):
            raise ProvenanceError(f"{name} required build args are not canonical")
        bindings = _mapping(
            subject["required_label_bindings"],
            label=f"{name} label bindings",
        )
        if not bindings:
            raise ProvenanceError(f"{name} label bindings are empty")
        for label_name, binding in bindings.items():
            if not LABEL_RE.fullmatch(label_name):
                raise ProvenanceError(f"{name} has an invalid OCI label")
            if binding != subject["runtime_kind"] and (
                not isinstance(binding, str) or binding not in arguments
            ):
                raise ProvenanceError(f"{name} label binding is not a build arg")
        assertions = subject["source_assertions"]
        if not isinstance(assertions, list):
            raise ProvenanceError(f"{name} source assertions must be an array")
        previous_key = ""
        assertion_labels: set[str] = set()
        for assertion_index, raw_assertion in enumerate(assertions):
            assertion_label = f"{name} source assertion[{assertion_index}]"
            assertion = _mapping(raw_assertion, label=assertion_label)
            _exact_keys(
                assertion,
                {"key", "value", "build_arg", "oci_label", "dockerfile_uses"},
                label=assertion_label,
            )
            key = _text(assertion["key"], label=f"{assertion_label} key")
            if not CONTRACT_KEY_RE.fullmatch(key) or key <= previous_key:
                raise ProvenanceError(
                    f"{name} source assertion keys must be canonical, unique, and sorted"
                )
            previous_key = key
            _text(
                assertion["value"],
                label=f"{assertion_label} value",
                maximum=16384,
            )
            build_arg = assertion["build_arg"]
            if not isinstance(build_arg, str) or not ARG_RE.fullmatch(build_arg):
                raise ProvenanceError(f"{assertion_label} build_arg is invalid")
            oci_label = assertion["oci_label"]
            if (
                not isinstance(oci_label, str)
                or not LABEL_RE.fullmatch(oci_label)
                or not oci_label.startswith("io.teamagent.contract.")
                or oci_label in assertion_labels
                or oci_label in bindings
            ):
                raise ProvenanceError(f"{name} source assertion OCI label is invalid or duplicate")
            assertion_labels.add(oci_label)
            uses = assertion["dockerfile_uses"]
            if not isinstance(uses, list) or not uses or len(uses) != len(set(uses)):
                raise ProvenanceError(
                    f"{name} source assertion dockerfile_uses must be non-empty and unique"
                )
            for use in uses:
                _text(use, label=f"{assertion_label} Dockerfile use", maximum=16384)
        probes = subject["binary_probes"]
        if not isinstance(probes, list) or not probes:
            raise ProvenanceError(f"{name} binary probes are empty")
        previous_key = ""
        probe_keys: set[tuple[str, str]] = set()
        probe_paths: set[str] = set()
        for probe_index, probe in enumerate(probes):
            probe_label = f"{name} binary probe[{probe_index}]"
            value = _mapping(probe, label=probe_label)
            _exact_keys(value, {"key", "path", "sha256"}, label=probe_label)
            key = _text(value["key"], label=f"{probe_label} key")
            identity = (name, key)
            if not CONTRACT_KEY_RE.fullmatch(key) or key <= previous_key or identity in probe_keys:
                raise ProvenanceError(
                    f"{name} binary probe (subject, key) identities must be unique and sorted"
                )
            previous_key = key
            probe_keys.add(identity)
            path_value = _text(value["path"], label=f"{name} binary probe path")
            if (
                not path_value.startswith("/")
                or ".." in PurePosixPath(path_value).parts
                or path_value in probe_paths
            ):
                raise ProvenanceError(f"{name} binary probe paths are unsafe or duplicate")
            probe_paths.add(path_value)
            _sha256(value["sha256"], label=f"{name} binary probe SHA-256")
        expected_probe_paths = EXPECTED_PROBE_PATHS[name]
        actual_probe_paths = {
            probe["key"]: probe["path"] for probe in probes if isinstance(probe, dict)
        }
        if actual_probe_paths != expected_probe_paths:
            raise ProvenanceError(
                f"{name} binary probe key/path interface does not match the allowlist"
            )
    if seen != set(EXPECTED_SUBJECTS):
        raise ProvenanceError("contract subject set is incomplete")
    return dict(contract)


def _load_runtime_contract(path: Path) -> dict[str, Any]:
    contract = _mapping(_load(path, label="runtime contract"), label="runtime contract")
    _exact_keys(
        contract,
        {"schema_version", "release", "receipt"},
        label="runtime contract",
    )
    if contract["schema_version"] != RUNTIME_CONTRACT_SCHEMA_VERSION:
        raise ProvenanceError("runtime contract schema is unsupported")
    release = _mapping(contract["release"], label="runtime contract.release")
    _exact_keys(release, {"ready", "blocked_reason"}, label="runtime contract.release")
    if not isinstance(release["ready"], bool):
        raise ProvenanceError("runtime contract.release.ready must be boolean")
    if not isinstance(release["blocked_reason"], str):
        raise ProvenanceError("runtime contract.release.blocked_reason must be text")
    if release["ready"] == bool(release["blocked_reason"]):
        raise ProvenanceError(
            "ready runtime contracts need an empty blocked_reason; "
            "blocked runtime contracts need a reason"
        )
    receipt = _mapping(contract["receipt"], label="runtime contract.receipt")
    _exact_keys(
        receipt,
        {"schema_version", "subject", "entries"},
        label="runtime contract.receipt",
    )
    if receipt["schema_version"] != RUNTIME_RECEIPT_SCHEMA_VERSION or receipt["subject"] != "core":
        raise ProvenanceError("runtime receipt schema or subject is unsupported")
    entries = receipt["entries"]
    if not isinstance(entries, list) or not entries:
        raise ProvenanceError("runtime contract receipt entries are missing")
    previous_key = ""
    seen_args: set[str] = set()
    seen_labels: set[str] = set()
    for index, raw_entry in enumerate(entries):
        entry_label = f"runtime contract.receipt.entries[{index}]"
        entry = _mapping(raw_entry, label=entry_label)
        _exact_keys(
            entry,
            {
                "key",
                "component",
                "evidence",
                "value_kind",
                "value",
                "build_arg",
                "oci_label",
                "dockerfile_uses",
            },
            label=entry_label,
        )
        key = _text(entry["key"], label=f"{entry_label}.key")
        if not CONTRACT_KEY_RE.fullmatch(key) or key <= previous_key:
            raise ProvenanceError(
                "runtime contract receipt keys must be canonical, unique, and sorted"
            )
        previous_key = key
        for field in ("component", "evidence", "value_kind"):
            _text(entry[field], label=f"{entry_label}.{field}")
        value = _text(entry["value"], label=f"{entry_label}.value", maximum=16384)
        value_kind = entry["value_kind"]
        if value_kind == "sha256":
            _sha256(value, label=f"{entry_label}.value")
        elif value_kind == "sha256_digest":
            _digest(value, label=f"{entry_label}.value")
        elif value_kind == "git_sha1":
            if not SHA1_RE.fullmatch(value):
                raise ProvenanceError(f"{entry_label}.value must be a lowercase Git SHA")
        build_arg = entry["build_arg"]
        if (
            not isinstance(build_arg, str)
            or not ARG_RE.fullmatch(build_arg)
            or build_arg in seen_args
        ):
            raise ProvenanceError(f"{entry_label}.build_arg is invalid or duplicate")
        seen_args.add(build_arg)
        oci_label = entry["oci_label"]
        if (
            not isinstance(oci_label, str)
            or not LABEL_RE.fullmatch(oci_label)
            or not oci_label.startswith("io.teamagent.contract.")
            or oci_label in seen_labels
        ):
            raise ProvenanceError(f"{entry_label}.oci_label is invalid or duplicate")
        seen_labels.add(oci_label)
        uses = entry["dockerfile_uses"]
        if not isinstance(uses, list) or not uses or len(uses) != len(set(uses)):
            raise ProvenanceError(f"{entry_label}.dockerfile_uses must be non-empty and unique")
        for use in uses:
            _text(use, label=f"{entry_label} Dockerfile use", maximum=16384)
    return dict(contract)


def _runtime_receipt_bytes(contract: Mapping[str, Any]) -> bytes:
    receipt = _mapping(contract["receipt"], label="runtime contract.receipt")
    entries = receipt["entries"]
    return json.dumps(
        {
            "schema_version": RUNTIME_RECEIPT_SCHEMA_VERSION,
            "subject": "core",
            "values": {entry["key"]: entry["value"] for entry in entries},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _runtime_expected_labels(
    contract: Mapping[str, Any],
    contract_sha256_value: str,
) -> dict[str, str]:
    receipt = _runtime_receipt_bytes(contract)
    entries = _mapping(contract["receipt"], label="runtime contract.receipt")["entries"]
    return {
        **{entry["oci_label"]: entry["value"] for entry in entries},
        "io.teamagent.build.runtime-contract-sha256": contract_sha256_value,
        "io.teamagent.build.runtime-receipt": base64.b64encode(receipt).decode("ascii"),
        "io.teamagent.build.runtime-receipt-sha256": hashlib.sha256(receipt).hexdigest(),
    }


def _entry_assertion(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "key": entry["key"],
        "value": entry["value"],
        "build_arg": entry["build_arg"],
        "oci_label": entry["oci_label"],
        "dockerfile_uses": entry["dockerfile_uses"],
        "_inner_runtime": True,
    }


def _source_assertions(
    contract: Mapping[str, Any],
    runtime_contract: Mapping[str, Any],
    subject_name: str,
) -> list[Mapping[str, Any]]:
    subject = _subject(contract, subject_name)
    assertions = [dict(assertion) for assertion in subject["source_assertions"]]
    if subject_name == "core":
        entries = _mapping(
            runtime_contract["receipt"],
            label="runtime contract.receipt",
        )["entries"]
        assertions.extend(_entry_assertion(entry) for entry in entries)
    labels: set[str] = set()
    keys: set[str] = set()
    for assertion in assertions:
        if assertion["key"] in keys:
            raise ProvenanceError(
                f"{subject_name} source assertion key is ambiguous across contracts"
            )
        if assertion["oci_label"] in labels:
            raise ProvenanceError(
                f"{subject_name} source assertion label is ambiguous across contracts"
            )
        keys.add(assertion["key"])
        labels.add(assertion["oci_label"])
    return assertions


def contract_sha256(path: Path) -> str:
    load_contract(path)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProvenanceError("cannot hash the core/media release contract") from exc


def require_release_ready(contract: Mapping[str, Any]) -> None:
    release = _mapping(contract["release"], label="contract.release")
    if release["ready"] is not True:
        raise ProvenanceError(f"release is blocked: {release['blocked_reason']}")


def verify_production_record(
    contract: Mapping[str, Any],
    deploy_log: Path,
) -> dict[str, Any]:
    record = latest_production_record(deploy_log)
    production = _mapping(
        _mapping(contract["app_html"], label="contract.app_html")["production"],
        label="contract production",
    )
    if record != {"schema_version": 1, **dict(production)}:
        raise ProvenanceError(
            "release contract does not match the latest production application record"
        )
    return record


def application_provenance(
    contract: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    app_html = _mapping(contract["app_html"], label="contract.app_html")
    fallback = _mapping(app_html["baked_fallback"], label="contract fallback")
    return {
        "schema_version": 1,
        "app_html": {
            "bucket": app_html["bucket"],
            "key": app_html["key"],
            "version_id": record["app_html_s3_version_id"],
            "sha256": record["app_html_sha256"],
        },
        "application_provenance": {
            "vault_manifest_sha256": record["vault_manifest_sha256"],
            "build_inputs_sha256": record["build_inputs_sha256"],
        },
        "baked_fallback": {
            "version_id": fallback["s3_version_id"],
            "sha256": fallback["sha256"],
        },
    }


def application_provenance_sha256(
    contract: Mapping[str, Any],
    record: Mapping[str, Any],
) -> str:
    return hashlib.sha256(_canonical_bytes(application_provenance(contract, record))).hexdigest()


def _subject(contract: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    matches = [
        item for item in contract["subjects"] if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise ProvenanceError("contract subject is missing or ambiguous")
    return matches[0]


def _verify_runtime_contract_pin(
    runtime_contract_path: Path,
    contract: Mapping[str, Any],
) -> None:
    try:
        runtime_bytes = runtime_contract_path.read_bytes()
    except OSError as exc:
        raise ProvenanceError("cannot read runtime contract bytes") from exc
    runtime_pin = _mapping(
        contract["source_runtime_contract"],
        label="contract.source_runtime_contract",
    )
    if hashlib.sha256(runtime_bytes).hexdigest() != runtime_pin["sha256"]:
        raise ProvenanceError(
            "outer source runtime contract SHA-256 does not match inner raw bytes"
        )


def _approval_observation_values(
    runtime_contract: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, str]:
    inner_entries = {
        entry["key"]: entry["value"]
        for entry in _mapping(
            runtime_contract["receipt"],
            label="runtime contract.receipt",
        )["entries"]
    }
    media_probes = {
        probe["key"]: probe["sha256"] for probe in _subject(contract, "media")["binary_probes"]
    }
    available = {
        "core.base.builder.arm64.digest": inner_entries.get("base.builder.arm64.digest"),
        "core.binary.python.sha256": inner_entries.get("binary.python.sha256"),
        **{
            f"media.{key}": value
            for key, value in media_probes.items()
            if key.startswith("binary.")
        },
    }
    missing = [key for key in APPROVAL_OBSERVATION_KEYS if available.get(key) is None]
    unknown = sorted(set(available) - set(APPROVAL_OBSERVATION_KEYS))
    if missing or unknown:
        raise ProvenanceError(
            "paired contracts do not expose the exact approval observation set: "
            f"missing={missing}, unknown={unknown}"
        )
    return {
        key: _text(
            available[key],
            label=f"approval observation {key}",
            maximum=16384,
        )
        for key in APPROVAL_OBSERVATION_KEYS
    }


def approval_observation_values(
    runtime_contract_path: Path,
    contract_path: Path,
) -> dict[str, str]:
    """Return the exact six external-approval values from validated contract bytes."""

    contract = load_contract(contract_path)
    runtime_contract = _load_runtime_contract(runtime_contract_path)
    _verify_runtime_contract_pin(runtime_contract_path, contract)
    return _approval_observation_values(runtime_contract, contract)


def _dockerfile_instructions(body: str) -> list[str]:
    instructions: list[str] = []
    continued = ""
    for raw_line in body.splitlines():
        if raw_line.lstrip().startswith("#"):
            continue
        stripped = raw_line.strip()
        if not stripped and not continued:
            continue
        continued = f"{continued} {stripped}".strip() if continued else stripped
        if continued.endswith("\\"):
            continued = continued[:-1].rstrip()
            continue
        instructions.append(continued)
        continued = ""
    if continued:
        raise ProvenanceError("Dockerfile has an incomplete continued instruction")
    return instructions


def _instruction_parts(instruction: str) -> tuple[str, str]:
    match = re.fullmatch(r"([A-Za-z]+)(?:[ \t]+(.*))?", instruction)
    if match is None:
        raise ProvenanceError("Dockerfile contains a malformed instruction")
    return match.group(1).upper(), match.group(2) or ""


def _verify_external_image_pins(
    instructions: Sequence[str],
    *,
    subject_name: str,
    digest_arguments: Mapping[str, str],
) -> None:
    before_first_from = True
    global_arguments: dict[str, str] = {}
    declared_stages: set[str] = set()
    declared_stage_count = 0
    current_stage: str | None = None
    for instruction in instructions:
        instruction_name, instruction_arguments = _instruction_parts(instruction)
        if instruction_name == "ARG":
            argument_match = re.fullmatch(
                r"([A-Z][A-Z0-9_]*)(?:=(.*))?",
                instruction_arguments,
            )
            if argument_match is None:
                raise ProvenanceError(f"{subject_name} Dockerfile contains malformed ARG")
            argument, default = argument_match.groups()
            if argument in FORBIDDEN_LEGACY_ARGUMENTS or argument.startswith("WOLFI_"):
                raise ProvenanceError(
                    f"{subject_name} Dockerfile reintroduces legacy ARG {argument}"
                )
            if before_first_from and default is not None:
                existing = global_arguments.get(argument)
                if existing is not None and existing != default:
                    raise ProvenanceError(
                        f"{subject_name} Dockerfile has conflicting global ARG {argument}"
                    )
                global_arguments[argument] = default

        reference: str | None = None
        stage_alias: str | None = None
        allow_scratch = False
        if instruction_name == "FROM":
            before_first_from = False
            from_match = re.fullmatch(
                r"(?:--platform=\S+[ \t]+)?(\S+)"
                r"(?:[ \t]+AS[ \t]+([A-Za-z0-9_.-]+))?",
                instruction_arguments,
                re.IGNORECASE,
            )
            if from_match is None:
                raise ProvenanceError(
                    f"{subject_name} Dockerfile contains an unsupported FROM instruction"
                )
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
            fixed_argument_digest = (
                argument_digest is not None
                and digest_arguments.get(argument_digest.group(1))
                == global_arguments.get(argument_digest.group(1))
                and DIGEST_RE.fullmatch(digest_arguments.get(argument_digest.group(1), ""))
                is not None
            )
            if not (
                local_stage
                or (allow_scratch and reference == "scratch")
                or literal_digest is not None
                or fixed_argument_digest
            ):
                raise ProvenanceError(
                    f"{subject_name} Dockerfile external image is not digest pinned: {reference}"
                )
        if instruction_name == "FROM":
            declared_stage_count += 1
            current_stage = stage_alias
        if stage_alias is not None:
            declared_stages.add(stage_alias)


def _verify_dockerfile_contract(
    dockerfile_path: Path,
    *,
    subject_name: str,
    subject: Mapping[str, Any],
    assertions: Sequence[Mapping[str, Any]],
) -> None:
    try:
        body = dockerfile_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProvenanceError(f"{subject_name} Dockerfile is missing") from exc
    effective_lines = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
    effective_dockerfile = "\n".join(effective_lines)
    instructions = _dockerfile_instructions(body)
    label_instructions = "\n".join(
        instruction for instruction in instructions if _instruction_parts(instruction)[0] == "LABEL"
    )

    required_arguments = subject["required_build_args"]
    for argument in required_arguments:
        declarations = re.findall(
            rf"^[ \t]*ARG[ \t]+{re.escape(argument)}(?:=(.*))?$",
            effective_dockerfile,
            re.MULTILINE,
        )
        if not declarations:
            raise ProvenanceError(f"{subject_name} Dockerfile lacks required ARG {argument}")
    for argument in (
        "GIT_COMMIT",
        "GIT_BRANCH",
        "BUILD_CONTEXT_SHA256",
        "RELEASE_CONTRACT_SHA256",
        "APP_PROVENANCE_SHA256",
    ):
        declarations = re.findall(
            rf"^[ \t]*ARG[ \t]+{argument}(?:=(.*))?$",
            effective_dockerfile,
            re.MULTILINE,
        )
        if declarations != [""]:
            raise ProvenanceError(
                f"{subject_name} Dockerfile must declare {argument} once without a default"
            )
    if subject_name == "core":
        for label_name, argument in CORE_RECEIPT_LABEL_BINDINGS.items():
            declarations = re.findall(
                rf"^[ \t]*ARG[ \t]+{argument}(?:=(.*))?$",
                effective_dockerfile,
                re.MULTILINE,
            )
            if declarations != ["", ""]:
                raise ProvenanceError(
                    f"core Dockerfile must declare {argument} in builder/final without defaults"
                )
            variable = rf"(?:\${argument}|\$\{{{argument}\}})"
            if (
                re.search(
                    rf"\b{re.escape(label_name)}="
                    rf"(?:\"{variable}\"|{variable})(?:\s|$)",
                    label_instructions,
                )
                is None
            ):
                raise ProvenanceError(f"core Dockerfile lacks receipt label binding {label_name}")

    assertion_values_by_arg: dict[str, str] = {}
    digest_arguments: dict[str, str] = {}
    for assertion in assertions:
        build_arg = assertion["build_arg"]
        value = assertion["value"]
        existing = assertion_values_by_arg.get(build_arg)
        if existing is not None and existing != value:
            raise ProvenanceError(f"{subject_name} source assertions disagree on ARG {build_arg}")
        assertion_values_by_arg[build_arg] = value
        declarations = re.findall(
            rf"^[ \t]*ARG[ \t]+{re.escape(build_arg)}(?:=(.*))?$",
            effective_dockerfile,
            re.MULTILINE,
        )
        if value not in declarations or any(
            declaration not in {"", value} for declaration in declarations
        ):
            raise ProvenanceError(
                f"{subject_name} Dockerfile does not fix {build_arg} to its contract value"
            )
        variable_forms = (f"${build_arg}", f"${{{build_arg}}}")
        if not assertion.get("_inner_runtime") and not any(
            variable in required_use
            for required_use in assertion["dockerfile_uses"]
            for variable in variable_forms
        ):
            raise ProvenanceError(
                f"{subject_name} source assertion uses do not reference {build_arg}"
            )
        for required_use in assertion["dockerfile_uses"]:
            if required_use not in effective_dockerfile:
                raise ProvenanceError(
                    f"{subject_name} Dockerfile does not implement {build_arg} use: "
                    f"{required_use!r}"
                )
            if any(
                required_use in label_instruction
                for label_instruction in label_instructions.splitlines()
            ):
                raise ProvenanceError(
                    f"{subject_name} source assertion use for {build_arg} may not be a LABEL"
                )
        variable = rf"(?:\${build_arg}|\$\{{{build_arg}\}})"
        if (
            re.search(
                rf"\b{re.escape(assertion['oci_label'])}="
                rf"(?:\"{variable}\"|{variable})(?:\s|$)",
                label_instructions,
            )
            is None
        ):
            raise ProvenanceError(
                f"{subject_name} Dockerfile does not bind {assertion['oci_label']} to {build_arg}"
            )
        if DIGEST_RE.fullmatch(value):
            digest_arguments[build_arg] = value

    for label_name, binding in subject["required_label_bindings"].items():
        if binding == subject["runtime_kind"]:
            expected = rf"\b{re.escape(label_name)}=[\"']?{re.escape(binding)}[\"']?(?:\s|$)"
        else:
            expected = (
                rf"\b{re.escape(label_name)}=[\"']?"
                rf"(?:\${re.escape(binding)}|\$\{{{re.escape(binding)}\}})"
                rf"[\"']?(?:\s|$)"
            )
        if re.search(expected, label_instructions) is None:
            raise ProvenanceError(f"{subject_name} Dockerfile lacks label binding {label_name}")

    approval_argument_declarations = re.findall(
        rf"^[ \t]*ARG[ \t]+{RELEASE_APPROVAL_BUILD_ARG}(?:=(.*))?$",
        effective_dockerfile,
        re.MULTILINE,
    )
    if not approval_argument_declarations or any(
        declaration != "" for declaration in approval_argument_declarations
    ):
        raise ProvenanceError(
            f"{subject_name} Dockerfile must require {RELEASE_APPROVAL_BUILD_ARG}"
        )
    approval_label_pattern = (
        rf"\b{re.escape(RELEASE_APPROVAL_LABEL)}=[\"']?"
        rf"(?:\${RELEASE_APPROVAL_BUILD_ARG}|\$\{{{RELEASE_APPROVAL_BUILD_ARG}\}})"
        rf"[\"']?(?:\s|$)"
    )
    if re.search(approval_label_pattern, label_instructions) is None:
        raise ProvenanceError(
            f"{subject_name} Dockerfile lacks label binding {RELEASE_APPROVAL_LABEL}"
        )

    expected_teamagent_labels = {
        *COMMON_STATIC_TEAMAGENT_LABELS,
        RELEASE_APPROVAL_LABEL,
        *(
            label_name
            for label_name in subject["required_label_bindings"]
            if label_name.startswith("io.teamagent.")
        ),
        *(assertion["oci_label"] for assertion in assertions),
    }
    if subject_name == "core":
        expected_teamagent_labels.update(CORE_RECEIPT_LABEL_BINDINGS)
    actual_teamagent_labels = set(
        re.findall(r"\b(io\.teamagent\.[a-z0-9.-]+)=", label_instructions)
    )
    if actual_teamagent_labels != expected_teamagent_labels:
        missing = sorted(expected_teamagent_labels - actual_teamagent_labels)
        unknown = sorted(actual_teamagent_labels - expected_teamagent_labels)
        raise ProvenanceError(
            f"{subject_name} Dockerfile TeamAgent label set mismatch: "
            f"missing={missing}, unknown={unknown}"
        )

    _verify_external_image_pins(
        instructions,
        subject_name=subject_name,
        digest_arguments=digest_arguments,
    )


def validate_contract_pair(
    runtime_contract_path: Path,
    contract_path: Path,
    repo_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = load_contract(contract_path)
    runtime_contract = _load_runtime_contract(runtime_contract_path)
    _verify_runtime_contract_pin(runtime_contract_path, contract)
    runtime_pin = _mapping(
        contract["source_runtime_contract"],
        label="contract.source_runtime_contract",
    )
    expected_runtime_path = (repo_root / runtime_pin["path"]).resolve()
    if runtime_contract_path.resolve() != expected_runtime_path:
        raise ProvenanceError(
            "runtime contract argument does not resolve to the outer pinned source path"
        )

    outer_release = _mapping(contract["release"], label="contract.release")
    inner_release = _mapping(
        runtime_contract["release"],
        label="runtime contract.release",
    )
    if outer_release["ready"] and not inner_release["ready"]:
        raise ProvenanceError("outer release cannot be ready while inner release is blocked")

    inner_entries = {
        entry["key"]: entry
        for entry in _mapping(
            runtime_contract["receipt"],
            label="runtime contract.receipt",
        )["entries"]
    }
    core_python = inner_entries.get("binary.python.sha256")
    core_builder = inner_entries.get("base.builder.arm64.digest")
    if core_python is None or core_builder is None:
        raise ProvenanceError("inner runtime contract lacks core Python or builder-base evidence")
    core_probes = {probe["key"]: probe for probe in _subject(contract, "core")["binary_probes"]}
    core_python_probe = core_probes["binary.python.sha256"]
    if (
        core_python_probe["path"] != "/usr/bin/python3.14"
        or core_python_probe["sha256"] != core_python["value"]
    ):
        raise ProvenanceError(
            "core Python probe path/value does not match the inner runtime contract"
        )

    for subject_name in ("core", "media"):
        subject = _subject(contract, subject_name)
        assertions = _source_assertions(contract, runtime_contract, subject_name)
        assertions_by_key = {assertion["key"]: assertion for assertion in assertions}
        for probe in subject["binary_probes"]:
            if not probe["key"].startswith("binary."):
                continue
            assertion = assertions_by_key.get(probe["key"])
            if assertion is None:
                raise ProvenanceError(
                    f"{subject_name} binary probe lacks a matching source assertion: {probe['key']}"
                )
            if assertion["value"] != probe["sha256"]:
                raise ProvenanceError(
                    f"{subject_name} binary probe value differs from source assertion: "
                    f"{probe['key']}"
                )
            if not any(probe["path"] in use for use in assertion["dockerfile_uses"]):
                raise ProvenanceError(
                    f"{subject_name} binary assertion does not prove its canonical path: "
                    f"{probe['key']}"
                )
        dockerfile_path = repo_root / subject["dockerfile"]
        _verify_dockerfile_contract(
            dockerfile_path,
            subject_name=subject_name,
            subject=subject,
            assertions=assertions,
        )

    _approval_observation_values(runtime_contract, contract)
    return contract, runtime_contract


def verify_source_interface(
    repo_root: Path,
    contract_path: Path,
    deploy_log: Path,
    baked_fallback_path: Path | None = None,
) -> None:
    contract = load_contract(contract_path)
    verify_production_record(contract, deploy_log)
    runtime = _mapping(
        contract["source_runtime_contract"],
        label="contract source runtime contract",
    )
    runtime_path = repo_root / runtime["path"]
    validate_contract_pair(
        runtime_path,
        contract_path,
        repo_root,
    )

    fallback = _mapping(
        _mapping(contract["app_html"], label="contract.app_html")["baked_fallback"],
        label="contract fallback",
    )
    fallback_path = baked_fallback_path or (repo_root / "src/teamagent/connect_web/static/app.html")
    try:
        fallback_sha256 = hashlib.sha256(fallback_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProvenanceError("baked fallback app.html is missing from the source archive") from exc
    if fallback_sha256 != fallback["sha256"]:
        raise ProvenanceError("baked fallback app.html SHA-256 mismatch")


def verify_oci_config(
    config_path: Path,
    *,
    subject_name: str,
    commit: str,
    expected_config_digest: str,
    contract_path: Path,
    expected_contract_sha256: str,
    runtime_contract_path: Path | None = None,
    expected_build_context_sha256: str | None = None,
    expected_release_approval_sha256: str,
) -> dict[str, str]:
    if not SHA1_RE.fullmatch(commit):
        raise ProvenanceError("expected commit must be a full lowercase Git SHA")
    contract = load_contract(contract_path)
    actual_contract_sha256 = contract_sha256(contract_path)
    if actual_contract_sha256 != expected_contract_sha256:
        raise ProvenanceError("release contract SHA-256 mismatch")
    if not isinstance(expected_config_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        expected_config_digest,
    ):
        raise ProvenanceError("expected OCI config digest is invalid")
    production = _mapping(
        _mapping(contract["app_html"], label="contract.app_html")["production"],
        label="contract production",
    )
    record = {"schema_version": 1, **dict(production)}
    subject = _subject(contract, subject_name)
    if runtime_contract_path is None:
        raise ProvenanceError("runtime contract is required for the core/media OCI interface")
    runtime_contract = _load_runtime_contract(runtime_contract_path)
    try:
        runtime_contract_bytes = runtime_contract_path.read_bytes()
    except OSError as exc:
        raise ProvenanceError("cannot read runtime contract bytes") from exc
    runtime_contract_sha256 = hashlib.sha256(runtime_contract_bytes).hexdigest()
    runtime_pin = _mapping(
        contract["source_runtime_contract"],
        label="contract.source_runtime_contract",
    )
    if runtime_contract_sha256 != runtime_pin["sha256"]:
        raise ProvenanceError(
            "outer source runtime contract SHA-256 does not match inner raw bytes"
        )
    try:
        config_bytes = config_path.read_bytes()
    except OSError as exc:
        raise ProvenanceError("cannot read OCI config") from exc
    actual_config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    if actual_config_digest != expected_config_digest:
        raise ProvenanceError("OCI config bytes do not match the manifest digest")
    try:
        config_value = _loads(config_bytes.decode("utf-8"), label="OCI config")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("OCI config is not UTF-8") from exc
    config = _mapping(config_value, label="OCI config")
    if config.get("os") != "linux" or config.get("architecture") != "arm64":
        raise ProvenanceError("OCI config is not linux/arm64")
    config_section = _mapping(config.get("config"), label="OCI config.config")
    raw_labels = _mapping(config_section.get("Labels"), label="OCI config labels")
    labels = {
        key: value
        for key, value in raw_labels.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    if len(labels) != len(raw_labels):
        raise ProvenanceError("OCI labels must be string pairs")
    for label_name, value in labels.items():
        # プレースホルダ禁止は自前契約ラベルに限定する。digest固定の上流ベース
        # (例: chainguard python が dev.chainguard.package.main='' を同梱) の
        # 継承ラベルまで対象にすると原理的に通らない検査になる（実測）。
        if not label_name.startswith("io.teamagent."):
            continue
        if value.strip().lower() in UNTRUSTED_PLACEHOLDER_VALUES:
            raise ProvenanceError(f"OCI label {label_name} uses an untrusted placeholder")
    if expected_build_context_sha256 is None:
        raise ProvenanceError("expected build context SHA-256 is required")
    _sha256(
        expected_build_context_sha256,
        label="expected build context SHA-256",
    )
    _sha256(
        expected_release_approval_sha256,
        label="expected release approval SHA-256",
    )
    app_html = _mapping(contract["app_html"], label="contract.app_html")
    fallback = _mapping(app_html["baked_fallback"], label="contract fallback")
    build_argument_values: dict[str, str] = {
        "GIT_COMMIT": commit,
        "GIT_BRANCH": "dev",
        "BUILD_CONTEXT_SHA256": expected_build_context_sha256,
        "RELEASE_CONTRACT_SHA256": expected_contract_sha256,
        "APP_PROVENANCE_SHA256": application_provenance_sha256(contract, record),
        "APP_HTML_SOURCE": "s3",
        "APP_HTML_SHA256": record["app_html_sha256"],
        "APP_HTML_VERSION_ID": record["app_html_s3_version_id"],
        "APP_HTML_MANIFEST_SHA256": record["vault_manifest_sha256"],
        "APP_HTML_BUILD_INPUTS_SHA256": record["build_inputs_sha256"],
        "BAKED_APP_HTML_SHA256": fallback["sha256"],
    }
    if fallback["s3_version_id"] is not None:
        build_argument_values["BAKED_APP_HTML_VERSION_ID"] = fallback["s3_version_id"]

    expected: dict[str, str] = dict(COMMON_STATIC_TEAMAGENT_LABELS)
    for label_name, binding in subject["required_label_bindings"].items():
        if binding == subject["runtime_kind"]:
            expected[label_name] = subject["runtime_kind"]
            continue
        expected_value = build_argument_values.get(binding)
        if expected_value is None:
            raise ProvenanceError(
                f"{subject_name} OCI label binding cannot be resolved: {label_name}"
            )
        expected[label_name] = expected_value
    for assertion in subject["source_assertions"]:
        if assertion["oci_label"] in expected:
            raise ProvenanceError(
                f"{subject_name} OCI label is ambiguously declared: {assertion['oci_label']}"
            )
        expected[assertion["oci_label"]] = assertion["value"]
    if subject_name == "core":
        runtime_labels = _runtime_expected_labels(
            runtime_contract,
            runtime_contract_sha256,
        )
        overlap = expected.keys() & runtime_labels.keys()
        if overlap:
            raise ProvenanceError(
                f"core OCI labels are ambiguous across contracts: {sorted(overlap)}"
            )
        expected.update(runtime_labels)

    expected["org.opencontainers.image.revision"] = commit
    expected["org.opencontainers.image.ref.name"] = "dev"
    expected[RELEASE_APPROVAL_LABEL] = expected_release_approval_sha256
    expected_teamagent_labels = {
        label_name for label_name in expected if label_name.startswith("io.teamagent.")
    }
    actual_teamagent_labels = {
        label_name for label_name in labels if label_name.startswith("io.teamagent.")
    }
    if actual_teamagent_labels != expected_teamagent_labels:
        missing = sorted(expected_teamagent_labels - actual_teamagent_labels)
        unknown = sorted(actual_teamagent_labels - expected_teamagent_labels)
        raise ProvenanceError(
            f"OCI TeamAgent label allowlist mismatch: missing={missing}, unknown={unknown}"
        )
    for label_name, expected_value in expected.items():
        if labels.get(label_name) != expected_value:
            raise ProvenanceError(f"OCI label mismatch: {label_name}")
    return dict(sorted(labels.items()))


def binary_probes(contract: Mapping[str, Any], subject_name: str) -> list[Mapping[str, str]]:
    subject = _subject(contract, subject_name)
    return sorted(
        (dict(probe) for probe in subject["binary_probes"]),
        key=lambda probe: probe["path"],
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("production-record")
    record.add_argument("--deploy-log", type=Path, required=True)
    record.add_argument("--format", choices=("json", "lines"), default="json")
    contract_hash = commands.add_parser("contract-sha256")
    contract_hash.add_argument("--contract", type=Path, required=True)
    ready = commands.add_parser("assert-contract-ready")
    ready.add_argument("--contract", type=Path, required=True)
    app_hash = commands.add_parser("app-provenance-sha256")
    app_hash.add_argument("--contract", type=Path, required=True)
    app_hash.add_argument("--deploy-log", type=Path, required=True)
    source = commands.add_parser("verify-source-interface")
    source.add_argument("--repo-root", type=Path, required=True)
    source.add_argument("--contract", type=Path, required=True)
    source.add_argument("--deploy-log", type=Path, required=True)
    source.add_argument("--baked-fallback", type=Path)
    pair = commands.add_parser("validate-contract-pair")
    pair.add_argument("--runtime-contract", type=Path, required=True)
    pair.add_argument("--contract", type=Path, required=True)
    pair.add_argument("--repo-root", type=Path, required=True)
    oci = commands.add_parser("verify-oci-config")
    oci.add_argument("--config", type=Path, required=True)
    oci.add_argument("--subject", choices=("core", "media"), required=True)
    oci.add_argument("--commit", required=True)
    oci.add_argument("--expected-config-digest", required=True)
    oci.add_argument("--contract", type=Path, required=True)
    oci.add_argument("--expected-contract-sha256", required=True)
    oci.add_argument("--runtime-contract", type=Path, required=True)
    oci.add_argument("--expected-build-context-sha256", required=True)
    oci.add_argument("--expected-release-approval-sha256", required=True)
    probes = commands.add_parser("binary-probes")
    probes.add_argument("--contract", type=Path, required=True)
    probes.add_argument("--subject", choices=("core", "media"), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "production-record":
            record = latest_production_record(args.deploy_log)
            if args.format == "json":
                print(_canonical_bytes(record).decode(), end="")
            else:
                for key in (
                    "app_html_s3_version_id",
                    "app_html_sha256",
                    "vault_manifest_sha256",
                    "build_inputs_sha256",
                ):
                    print(record[key])
        elif args.command == "contract-sha256":
            print(contract_sha256(args.contract))
        elif args.command == "assert-contract-ready":
            require_release_ready(load_contract(args.contract))
        elif args.command == "app-provenance-sha256":
            contract = load_contract(args.contract)
            record = verify_production_record(contract, args.deploy_log)
            print(application_provenance_sha256(contract, record))
        elif args.command == "verify-source-interface":
            verify_source_interface(
                args.repo_root,
                args.contract,
                args.deploy_log,
                args.baked_fallback,
            )
        elif args.command == "validate-contract-pair":
            validate_contract_pair(
                args.runtime_contract,
                args.contract,
                args.repo_root,
            )
        elif args.command == "verify-oci-config":
            verify_oci_config(
                args.config,
                subject_name=args.subject,
                commit=args.commit,
                expected_config_digest=args.expected_config_digest,
                contract_path=args.contract,
                expected_contract_sha256=args.expected_contract_sha256,
                runtime_contract_path=args.runtime_contract,
                expected_build_context_sha256=args.expected_build_context_sha256,
                expected_release_approval_sha256=(args.expected_release_approval_sha256),
            )
        elif args.command == "binary-probes":
            for probe in binary_probes(load_contract(args.contract), args.subject):
                print(f"{probe['path']}\t{probe['sha256']}")
    except ProvenanceError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
