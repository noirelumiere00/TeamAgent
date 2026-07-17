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
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = ".teamagent-source-manifest.json"
SOURCE_KEY = "codebuild/source.zip"
APP_HTML_BUCKET = "teamagent-dev-raw-files"
APP_HTML_KEY = "codebuild/connect-web-app.html"
SCHEMA_VERSION = 2
RUNTIME_CONTRACT_PATH = "infra/codebuild/teamagent_runtime_contract.json"
RUNTIME_CONTRACT_SCHEMA_VERSION = 1
SCRAPE_TOOLS_LABEL = "io.teamagent.build.with-scrape-tools"
APP_HTML_SHA256_LABEL = "io.teamagent.build.app-html-sha256"
APP_HTML_VERSION_ID_LABEL = "io.teamagent.build.app-html-version-id"
RUNTIME_FIELDS = (
    (
        "E5_MODEL_REVISION",
        "model",
        "e5_revision",
        "io.teamagent.build.e5-model-revision",
    ),
    (
        "NODE_IMAGE_DIGEST",
        "node",
        "image_digest",
        "io.teamagent.build.node-image-digest",
    ),
    ("NODE_VERSION", "node", "version", "io.teamagent.build.node-version"),
    (
        "NODE_BINARY_SHA256",
        "node",
        "binary_sha256",
        "io.teamagent.build.node-binary-sha256",
    ),
    (
        "PLAYWRIGHT_VERSION",
        "playwright",
        "version",
        "io.teamagent.build.playwright-version",
    ),
    (
        "PLAYWRIGHT_CHROMIUM_REVISION",
        "chromium",
        "revision",
        "io.teamagent.build.chromium-revision",
    ),
    (
        "PLAYWRIGHT_CHROMIUM_VERSION",
        "chromium",
        "version",
        "io.teamagent.build.chromium-version",
    ),
    (
        "PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256",
        "chromium",
        "archive_sha256",
        "io.teamagent.build.chromium-archive-sha256",
    ),
    (
        "PLAYWRIGHT_CHROMIUM_SHA256",
        "chromium",
        "binary_sha256",
        "io.teamagent.build.chromium-sha256",
    ),
)
_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_S3_VERSION_ID_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
_THREE_PART_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2}")
_FOUR_PART_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){3}")
_DECIMAL_RE = re.compile(r"[1-9][0-9]*")
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


def validate_runtime_contract(value: Any, *, label: str = "runtime contract") -> dict[str, Any]:
    """Validate and normalize the exact model, Node, Playwright, and Chromium contract."""

    if not isinstance(value, dict):
        raise ProvenanceError(f"{label} must be a JSON object")
    _require_exact_keys(
        value,
        {"schema_version", "model", "node", "playwright", "chromium"},
        label=label,
    )
    if value["schema_version"] != RUNTIME_CONTRACT_SCHEMA_VERSION:
        raise ProvenanceError(f"unsupported {label} schema: {value['schema_version']!r}")

    model = value["model"]
    node = value["node"]
    playwright = value["playwright"]
    chromium = value["chromium"]
    if not isinstance(model, dict):
        raise ProvenanceError(f"{label} model must be an object")
    if not isinstance(node, dict):
        raise ProvenanceError(f"{label} node must be an object")
    if not isinstance(playwright, dict):
        raise ProvenanceError(f"{label} playwright must be an object")
    if not isinstance(chromium, dict):
        raise ProvenanceError(f"{label} chromium must be an object")
    _require_exact_keys(model, {"e5_revision"}, label=f"{label} model")
    _require_exact_keys(
        node,
        {"image_digest", "version", "binary_sha256"},
        label=f"{label} node",
    )
    _require_exact_keys(playwright, {"version"}, label=f"{label} playwright")
    _require_exact_keys(
        chromium,
        {"revision", "version", "archive_sha256", "binary_sha256"},
        label=f"{label} chromium",
    )

    e5_revision = model["e5_revision"]
    if not isinstance(e5_revision, str) or not _SHA1_RE.fullmatch(e5_revision):
        raise ProvenanceError(f"{label} model e5_revision must be a full lowercase SHA-1")
    node_image_digest = node["image_digest"]
    if not isinstance(node_image_digest, str) or not _SHA256_DIGEST_RE.fullmatch(node_image_digest):
        raise ProvenanceError(f"{label} node image_digest must be a sha256 digest")
    node_version = node["version"]
    if not isinstance(node_version, str) or not _THREE_PART_VERSION_RE.fullmatch(node_version):
        raise ProvenanceError(f"{label} node version must have three numeric components")
    playwright_version = playwright["version"]
    if not isinstance(playwright_version, str) or not _THREE_PART_VERSION_RE.fullmatch(
        playwright_version
    ):
        raise ProvenanceError(f"{label} playwright version must have three numeric components")
    chromium_revision = chromium["revision"]
    if not isinstance(chromium_revision, str) or not _DECIMAL_RE.fullmatch(chromium_revision):
        raise ProvenanceError(f"{label} chromium revision must be a positive decimal string")
    chromium_version = chromium["version"]
    if not isinstance(chromium_version, str) or not _FOUR_PART_VERSION_RE.fullmatch(
        chromium_version
    ):
        raise ProvenanceError(f"{label} chromium version must have four numeric components")

    return {
        "schema_version": RUNTIME_CONTRACT_SCHEMA_VERSION,
        "model": {"e5_revision": e5_revision},
        "node": {
            "image_digest": node_image_digest,
            "version": node_version,
            "binary_sha256": _validate_sha256(
                node["binary_sha256"], label=f"{label} node binary_sha256"
            ),
        },
        "playwright": {"version": playwright_version},
        "chromium": {
            "revision": chromium_revision,
            "version": chromium_version,
            "archive_sha256": _validate_sha256(
                chromium["archive_sha256"], label=f"{label} chromium archive_sha256"
            ),
            "binary_sha256": _validate_sha256(
                chromium["binary_sha256"], label=f"{label} chromium binary_sha256"
            ),
        },
    }


def load_runtime_contract(path: Path) -> dict[str, Any]:
    return validate_runtime_contract(_load_json(path, label="runtime contract"))


def runtime_environment(contract: dict[str, Any]) -> dict[str, str]:
    validated = validate_runtime_contract(contract)
    return {
        environment_name: validated[section][field]
        for environment_name, section, field, _label_name in RUNTIME_FIELDS
    }


def _runtime_contract_from_environment(expected: dict[str, str]) -> dict[str, Any]:
    if not isinstance(expected, dict):
        raise ProvenanceError("expected runtime environment must be an object")
    expected_keys = {field[0] for field in RUNTIME_FIELDS}
    _require_exact_keys(expected, expected_keys, label="expected runtime environment")
    return validate_runtime_contract(
        {
            "schema_version": RUNTIME_CONTRACT_SCHEMA_VERSION,
            "model": {"e5_revision": expected["E5_MODEL_REVISION"]},
            "node": {
                "image_digest": expected["NODE_IMAGE_DIGEST"],
                "version": expected["NODE_VERSION"],
                "binary_sha256": expected["NODE_BINARY_SHA256"],
            },
            "playwright": {"version": expected["PLAYWRIGHT_VERSION"]},
            "chromium": {
                "revision": expected["PLAYWRIGHT_CHROMIUM_REVISION"],
                "version": expected["PLAYWRIGHT_CHROMIUM_VERSION"],
                "archive_sha256": expected["PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256"],
                "binary_sha256": expected["PLAYWRIGHT_CHROMIUM_SHA256"],
            },
        },
        label="expected runtime environment",
    )


def _runtime_contract_at_commit(repo_root: Path, commit: str) -> dict[str, Any]:
    raw = _git(repo_root, "show", f"{commit}:{RUNTIME_CONTRACT_PATH}")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceError("runtime contract in Git commit is not UTF-8") from exc
    return validate_runtime_contract(
        _loads_strict(decoded, label="runtime contract in Git commit"),
        label="runtime contract in Git commit",
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
    _runtime_contract_at_commit(repo_root, commit)

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
        _git(repo_root, "ls-tree", "-rz", "--full-tree", commit)
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
) -> tuple[str, str, str, str, str, dict[str, Any], bytes]:
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
        {"with_scrape_tools", "app_html"},
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
    expected_runtime: dict[str, str],
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
    source_runtime_contract = load_runtime_contract(source_root / RUNTIME_CONTRACT_PATH)
    expected_runtime_contract = _runtime_contract_from_environment(expected_runtime)
    source_runtime_environment = runtime_environment(source_runtime_contract)
    expected_runtime_environment = runtime_environment(expected_runtime_contract)
    for environment_name, expected_value in expected_runtime_environment.items():
        source_value = source_runtime_environment[environment_name]
        if expected_value != source_value:
            raise ProvenanceError(
                f"{environment_name} mismatch: "
                f"environment={expected_value!r}, source contract={source_value!r}"
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
    expected_with_scrape_tools: str,
    expected_app_html_version_id: str,
    expected_app_html_sha256: str,
    expected_runtime: dict[str, str],
    expected_os: str = "linux",
    expected_architecture: str = "arm64",
) -> None:
    """Verify downloaded OCI config bytes and all provenance labels."""

    if not _SHA256_DIGEST_RE.fullmatch(expected_config_digest):
        raise ProvenanceError("expected OCI config digest is invalid")
    if not _SHA1_RE.fullmatch(expected_commit):
        raise ProvenanceError("expected OCI revision must be a full lowercase SHA-1")
    if expected_with_scrape_tools not in {"true", "false"}:
        raise ProvenanceError("expected OCI scrape-tools label must be 'true' or 'false'")
    _validate_s3_version_id(expected_app_html_version_id, label="expected OCI app HTML VersionId")
    _validate_sha256(expected_app_html_sha256, label="expected OCI app HTML SHA-256")
    expected_runtime_environment = runtime_environment(
        _runtime_contract_from_environment(expected_runtime)
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
    scrape_tools = labels.get(SCRAPE_TOOLS_LABEL)
    if scrape_tools != expected_with_scrape_tools:
        raise ProvenanceError(
            f"OCI {SCRAPE_TOOLS_LABEL} mismatch: "
            f"expected={expected_with_scrape_tools!r}, actual={scrape_tools!r}"
        )
    app_html_version_id = labels.get(APP_HTML_VERSION_ID_LABEL)
    if app_html_version_id != expected_app_html_version_id:
        raise ProvenanceError(
            f"OCI {APP_HTML_VERSION_ID_LABEL} mismatch: "
            f"expected={expected_app_html_version_id!r}, actual={app_html_version_id!r}"
        )
    app_html_sha256 = labels.get(APP_HTML_SHA256_LABEL)
    if app_html_sha256 != expected_app_html_sha256:
        raise ProvenanceError(
            f"OCI {APP_HTML_SHA256_LABEL} mismatch: "
            f"expected={expected_app_html_sha256!r}, actual={app_html_sha256!r}"
        )
    runtime_labels = {environment_name: label for environment_name, _, _, label in RUNTIME_FIELDS}
    for environment_name, expected_value in expected_runtime_environment.items():
        label = runtime_labels[environment_name]
        actual_value = labels.get(label)
        if actual_value != expected_value:
            raise ProvenanceError(
                f"OCI {label} mismatch: expected={expected_value!r}, actual={actual_value!r}"
            )


def _add_expected_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    for environment_name, _section, _field, _label_name in RUNTIME_FIELDS:
        parser.add_argument(
            "--expected-" + environment_name.lower().replace("_", "-"),
            required=True,
        )


def _runtime_environment_from_arguments(args: argparse.Namespace) -> dict[str, str]:
    return {
        environment_name: getattr(args, "expected_" + environment_name.lower())
        for environment_name, _section, _field, _label_name in RUNTIME_FIELDS
    }


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
    _add_expected_runtime_arguments(verify)

    runtime_values = subparsers.add_parser("runtime-values")
    runtime_values.add_argument("--contract", type=Path, required=True)

    config_digest = subparsers.add_parser("ecr-config-digest")
    config_digest.add_argument("--batch-response", type=Path, required=True)
    config_digest.add_argument("--expected-image-digest", required=True)

    revision = subparsers.add_parser("verify-oci-revision")
    revision.add_argument("--config", type=Path, required=True)
    revision.add_argument("--expected-config-digest", required=True)
    revision.add_argument("--expected-commit", required=True)
    revision.add_argument("--expected-with-scrape-tools", choices=("true", "false"), required=True)
    revision.add_argument("--expected-app-html-version-id", required=True)
    revision.add_argument("--expected-app-html-sha256", required=True)
    _add_expected_runtime_arguments(revision)
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
                _runtime_environment_from_arguments(args),
            )
            print(f"source provenance verified: {args.expected_commit} ({args.expected_branch})")
        elif args.command == "runtime-values":
            values = runtime_environment(load_runtime_contract(args.contract))
            print("\t".join(values[field[0]] for field in RUNTIME_FIELDS))
        elif args.command == "ecr-config-digest":
            print(ecr_config_digest(args.batch_response, args.expected_image_digest))
        elif args.command == "verify-oci-revision":
            verify_oci_revision(
                args.config,
                args.expected_config_digest,
                args.expected_commit,
                args.expected_with_scrape_tools,
                args.expected_app_html_version_id,
                args.expected_app_html_sha256,
                _runtime_environment_from_arguments(args),
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
