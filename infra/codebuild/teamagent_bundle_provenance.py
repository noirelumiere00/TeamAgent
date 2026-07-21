#!/usr/bin/env python3
"""Validate the TeamAgent core/media release and application provenance interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

CONTRACT_KIND = "teamagent.core-media-release-contract"
PROVENANCE_RECORD_RE = re.compile(
    r"^<!-- PRODUCTION_APP_PROVENANCE=(\{.+\}) -->$",
    re.MULTILINE,
)
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
S3_VERSION_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,254}")
ARG_RE = re.compile(r"[A-Z][A-Z0-9_]*")
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
APP_LABELS = {
    "io.teamagent.contract.app-html-source": "app_html_source",
    "io.teamagent.contract.baked-app-html-sha256": "baked_fallback_sha256",
    "io.teamagent.contract.baked-app-html-version-id": "baked_fallback_version_id",
    "io.teamagent.contract.app-html-version-id": "app_html_s3_version_id",
    "io.teamagent.contract.app-html-sha256": "app_html_sha256",
    "io.teamagent.contract.app-html-manifest-sha256": "vault_manifest_sha256",
    "io.teamagent.contract.app-html-build-inputs-sha256": "build_inputs_sha256",
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
CORE_RUNTIME_LABELS = {
    "io.teamagent.build.app-html-sha256",
    "io.teamagent.build.app-html-version-id",
    "io.teamagent.build.runtime-contract-sha256",
    "io.teamagent.build.runtime-receipt",
    "io.teamagent.build.runtime-receipt-sha256",
    "io.teamagent.build.with-scrape-tools",
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
    if contract["schema_version"] != 1 or contract["kind"] != CONTRACT_KIND:
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
        probes = subject["binary_probes"]
        if not isinstance(probes, list) or not probes:
            raise ProvenanceError(f"{name} binary probes are empty")
        previous_path = ""
        for probe in probes:
            value = _mapping(probe, label=f"{name} binary probe")
            _exact_keys(value, {"path", "sha256"}, label=f"{name} binary probe")
            path_value = _text(value["path"], label=f"{name} binary probe path")
            if (
                not path_value.startswith("/")
                or ".." in PurePosixPath(path_value).parts
                or path_value <= previous_path
            ):
                raise ProvenanceError(f"{name} binary probe paths are unsafe or unsorted")
            previous_path = path_value
            _sha256(value["sha256"], label=f"{name} binary probe SHA-256")
    if seen != set(EXPECTED_SUBJECTS):
        raise ProvenanceError("contract subject set is incomplete")
    return dict(contract)


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
    try:
        runtime_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProvenanceError("source runtime contract is missing") from exc
    if runtime_sha256 != runtime["sha256"]:
        raise ProvenanceError("source runtime contract SHA-256 mismatch")

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

    for name in ("core", "media"):
        subject = _subject(contract, name)
        dockerfile_path = repo_root / subject["dockerfile"]
        try:
            body = dockerfile_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProvenanceError(f"{name} Dockerfile is missing") from exc
        instructions = _dockerfile_instructions(body)
        arg_instructions = [
            instruction for instruction in instructions if instruction.upper().startswith("ARG ")
        ]
        labels = " ".join(
            instruction for instruction in instructions if instruction.upper().startswith("LABEL ")
        )
        for arg in subject["required_build_args"]:
            declarations = [
                instruction
                for instruction in arg_instructions
                if re.fullmatch(rf"ARG\s+{re.escape(arg)}(?:=.*)?", instruction)
            ]
            if not declarations:
                raise ProvenanceError(f"{name} Dockerfile lacks ARG {arg}")
        for provenance_arg in (
            "GIT_COMMIT",
            "GIT_BRANCH",
            "RELEASE_CONTRACT_SHA256",
            "APP_PROVENANCE_SHA256",
        ):
            declarations = [
                instruction
                for instruction in arg_instructions
                if re.fullmatch(rf"ARG\s+{provenance_arg}(?:=.*)?", instruction)
            ]
            if declarations != [f"ARG {provenance_arg}"]:
                raise ProvenanceError(
                    f"{name} Dockerfile must declare {provenance_arg} without a default"
                )
        for label_name, binding in subject["required_label_bindings"].items():
            if binding == subject["runtime_kind"]:
                expected = rf"\b{re.escape(label_name)}=[\"']?{re.escape(binding)}[\"']?(?:\s|$)"
            else:
                expected = (
                    rf"\b{re.escape(label_name)}=[\"']?"
                    rf"(?:\${re.escape(binding)}|\$\{{{re.escape(binding)}\}})"
                    rf"[\"']?(?:\s|$)"
                )
            if re.search(expected, labels) is None:
                raise ProvenanceError(f"{name} Dockerfile lacks label binding {label_name}")


def verify_oci_config(
    config_path: Path,
    *,
    subject_name: str,
    commit: str,
    expected_config_digest: str,
    contract_path: Path,
    expected_contract_sha256: str,
    runtime_contract_path: Path | None = None,
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
        if value.strip().lower() in UNTRUSTED_PLACEHOLDER_VALUES:
            raise ProvenanceError(f"OCI label {label_name} uses an untrusted placeholder")
    expected = {
        "org.opencontainers.image.revision": commit,
        "org.opencontainers.image.ref.name": "dev",
        "io.teamagent.runtime.kind": subject["runtime_kind"],
        "io.teamagent.build.release-contract-sha256": expected_contract_sha256,
        "io.teamagent.build.app-provenance-sha256": application_provenance_sha256(
            contract,
            record,
        ),
    }
    if subject_name == "core":
        fallback = _mapping(
            _mapping(contract["app_html"], label="contract.app_html")["baked_fallback"],
            label="contract fallback",
        )
        values = {
            **record,
            "app_html_source": "s3",
            "baked_fallback_sha256": fallback["sha256"],
            "baked_fallback_version_id": fallback["s3_version_id"],
        }
        expected.update({label: values[key] for label, key in APP_LABELS.items()})
    expected_teamagent_labels = {
        label_name for label_name in expected if label_name.startswith("io.teamagent.")
    }
    actual_teamagent_labels = {
        label_name for label_name in labels if label_name.startswith("io.teamagent.")
    }
    allowed_teamagent_labels = set(expected_teamagent_labels)
    if subject_name == "core" and runtime_contract_path is not None:
        runtime_contract = _mapping(
            _load(runtime_contract_path, label="runtime contract"),
            label="runtime contract",
        )
        receipt = _mapping(
            runtime_contract.get("receipt"),
            label="runtime contract receipt",
        )
        entries = receipt.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ProvenanceError("runtime contract receipt entries are missing")
        allowed_teamagent_labels.update(CORE_RUNTIME_LABELS)
        for index, raw_entry in enumerate(entries):
            entry = _mapping(
                raw_entry,
                label=f"runtime contract receipt entry[{index}]",
            )
            label_name = entry.get("oci_label")
            if not isinstance(label_name, str) or not LABEL_RE.fullmatch(label_name):
                raise ProvenanceError("runtime contract OCI label is invalid")
            allowed_teamagent_labels.add(label_name)
    if not expected_teamagent_labels.issubset(
        actual_teamagent_labels
    ) or not actual_teamagent_labels.issubset(allowed_teamagent_labels):
        missing = sorted(expected_teamagent_labels - actual_teamagent_labels)
        unknown = sorted(actual_teamagent_labels - allowed_teamagent_labels)
        raise ProvenanceError(
            f"OCI TeamAgent label allowlist mismatch: missing={missing}, unknown={unknown}"
        )
    for label_name, expected_value in expected.items():
        if labels.get(label_name) != expected_value:
            raise ProvenanceError(f"OCI label mismatch: {label_name}")
    return dict(sorted(labels.items()))


def binary_probes(contract: Mapping[str, Any], subject_name: str) -> list[Mapping[str, str]]:
    subject = _subject(contract, subject_name)
    return [dict(probe) for probe in subject["binary_probes"]]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("production-record")
    record.add_argument("--deploy-log", type=Path, required=True)
    record.add_argument("--format", choices=("json", "lines"), default="json")
    contract_hash = commands.add_parser("contract-sha256")
    contract_hash.add_argument("--contract", type=Path, required=True)
    ready = commands.add_parser("assert-release-ready")
    ready.add_argument("--contract", type=Path, required=True)
    app_hash = commands.add_parser("app-provenance-sha256")
    app_hash.add_argument("--contract", type=Path, required=True)
    app_hash.add_argument("--deploy-log", type=Path, required=True)
    source = commands.add_parser("verify-source-interface")
    source.add_argument("--repo-root", type=Path, required=True)
    source.add_argument("--contract", type=Path, required=True)
    source.add_argument("--deploy-log", type=Path, required=True)
    source.add_argument("--baked-fallback", type=Path)
    oci = commands.add_parser("verify-oci-config")
    oci.add_argument("--config", type=Path, required=True)
    oci.add_argument("--subject", choices=("core", "media"), required=True)
    oci.add_argument("--commit", required=True)
    oci.add_argument("--expected-config-digest", required=True)
    oci.add_argument("--contract", type=Path, required=True)
    oci.add_argument("--expected-contract-sha256", required=True)
    oci.add_argument("--runtime-contract", type=Path)
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
        elif args.command == "assert-release-ready":
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
        elif args.command == "verify-oci-config":
            verify_oci_config(
                args.config,
                subject_name=args.subject,
                commit=args.commit,
                expected_config_digest=args.expected_config_digest,
                contract_path=args.contract,
                expected_contract_sha256=args.expected_contract_sha256,
                runtime_contract_path=args.runtime_contract,
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
