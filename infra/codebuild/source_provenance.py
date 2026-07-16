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
SCHEMA_VERSION = 1
SCRAPE_TOOLS_LABEL = "io.teamagent.build.with-scrape-tools"
_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
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

    object_format = _git(repo_root, "rev-parse", "--show-object-format").decode().strip()
    if object_format != "sha1":
        raise ProvenanceError(f"unsupported Git object format: {object_format}")
    resolved = _git(repo_root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved != commit:
        raise ProvenanceError(f"commit did not resolve exactly: expected {commit}, got {resolved}")

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
) -> tuple[str, str, str, dict[str, Any], bytes]:
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
        {"with_scrape_tools"},
        label="source manifest build_parameters",
    )
    if not isinstance(build_parameters["with_scrape_tools"], bool):
        raise ProvenanceError("manifest with_scrape_tools must be a JSON boolean")
    with_scrape_tools = "true" if build_parameters["with_scrape_tools"] else "false"
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
    return commit, branch, with_scrape_tools, archive, commit_object


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
) -> None:
    """Verify env values, commit proof, and every extracted source byte."""

    source_root = source_root.resolve()
    manifest_path = manifest_path.resolve()
    if manifest_path.parent != source_root or manifest_path.name != MANIFEST_NAME:
        raise ProvenanceError(f"manifest must be {MANIFEST_NAME} at the source root")
    if not source_root.is_dir() or not manifest_path.is_file():
        raise ProvenanceError("source root or generated manifest is missing")
    manifest = _load_json(manifest_path, label="source manifest")
    commit, branch, with_scrape_tools, archive, commit_object = _validate_manifest_schema(manifest)
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
    if not isinstance(config, dict) or not isinstance(config.get("digest"), str):
        raise ProvenanceError("OCI image manifest config descriptor is missing")
    digest = config["digest"]
    if not _SHA256_DIGEST_RE.fullmatch(digest):
        raise ProvenanceError("OCI config digest is invalid")
    return digest


def verify_oci_revision(
    config_path: Path,
    expected_config_digest: str,
    expected_commit: str,
    expected_with_scrape_tools: str,
) -> None:
    """Verify downloaded OCI config bytes, revision, and build-profile labels."""

    if not _SHA256_DIGEST_RE.fullmatch(expected_config_digest):
        raise ProvenanceError("expected OCI config digest is invalid")
    if not _SHA1_RE.fullmatch(expected_commit):
        raise ProvenanceError("expected OCI revision must be a full lowercase SHA-1")
    if expected_with_scrape_tools not in {"true", "false"}:
        raise ProvenanceError("expected OCI scrape-tools label must be 'true' or 'false'")
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-manifest")
    create.add_argument("--repo-root", type=Path, required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--with-scrape-tools", choices=("true", "false"), required=True)
    create.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify-source")
    verify.add_argument("--source-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--expected-branch", required=True)
    verify.add_argument("--expected-with-scrape-tools", choices=("true", "false"), required=True)

    config_digest = subparsers.add_parser("ecr-config-digest")
    config_digest.add_argument("--batch-response", type=Path, required=True)
    config_digest.add_argument("--expected-image-digest", required=True)

    revision = subparsers.add_parser("verify-oci-revision")
    revision.add_argument("--config", type=Path, required=True)
    revision.add_argument("--expected-config-digest", required=True)
    revision.add_argument("--expected-commit", required=True)
    revision.add_argument("--expected-with-scrape-tools", choices=("true", "false"), required=True)
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
            )
            print(f"source provenance verified: {args.expected_commit} ({args.expected_branch})")
        elif args.command == "ecr-config-digest":
            print(ecr_config_digest(args.batch_response, args.expected_image_digest))
        elif args.command == "verify-oci-revision":
            verify_oci_revision(
                args.config,
                args.expected_config_digest,
                args.expected_commit,
                args.expected_with_scrape_tools,
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
