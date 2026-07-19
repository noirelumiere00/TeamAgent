#!/usr/bin/env python3
"""Create and verify the signed TikTok full-commit source statement."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
KIND = "teamagent.tiktok-source-manifest"
REPOSITORY = "https://github.com/noirelumiere00/tiktok-data-service.git"
BRANCH = "main"
CONTRACT_PATH = "infra/codebuild/tiktok_release_contract.json"
_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ProvenanceError(ValueError):
    """TikTok source evidence is malformed or does not bind the checkout."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProvenanceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(path: Path, *, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"invalid {label}: {exc}") from exc


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise ProvenanceError(
            f"{label} schema mismatch: "
            f"missing={sorted(expected - value.keys())}, "
            f"unknown={sorted(value.keys() - expected)}"
        )


def _sha1(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA1_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a full lowercase Git SHA")
    return value


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a lowercase SHA-256")
    return value


def _git(repo: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProvenanceError("Git source verification failed") from exc


def _git_object_id(kind: str, payload: bytes) -> str:
    framed = f"{kind} {len(payload)}\0".encode() + payload
    return hashlib.sha1(framed).hexdigest()


def _contract_sha256(contract_path: Path) -> str:
    contract = _load(contract_path, label="TikTok release contract")
    if not isinstance(contract, dict):
        raise ProvenanceError("TikTok release contract must be an object")
    source = contract.get("source")
    if source != {"repository": REPOSITORY, "branch": BRANCH}:
        raise ProvenanceError("TikTok source repository/branch contract is not fixed")
    return hashlib.sha256(contract_path.read_bytes()).hexdigest()


def _tree_inventory(repo: Path, commit: str) -> tuple[int, list[str]]:
    raw = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    count = 0
    executable_paths: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, _object_id = metadata.split(b" ", 2)
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProvenanceError("unsupported Git tree entry") from exc
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ProvenanceError("TikTok source permits only regular tracked files")
        if not path or path.startswith("/") or "\\" in path or ".." in Path(path).parts:
            raise ProvenanceError("TikTok source tree contains an unsafe path")
        count += 1
        if mode == b"100755":
            executable_paths.append(path)
    if count < 1:
        raise ProvenanceError("TikTok source tree is empty")
    return count, sorted(executable_paths, key=lambda item: item.encode())


def create_manifest(repo_root: Path, commit: str, contract_path: Path) -> dict[str, Any]:
    repo = repo_root.resolve()
    commit = _sha1(commit, label="TikTok source commit")
    contract_sha256 = _contract_sha256(contract_path)
    if _git(repo, "rev-parse", "--show-object-format").decode().strip() != "sha1":
        raise ProvenanceError("TikTok repository must use Git SHA-1 object IDs")
    if _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip() != commit:
        raise ProvenanceError("TikTok checkout HEAD does not match the requested commit")
    if (
        _git(repo, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}").decode().strip()
        != commit
    ):
        raise ProvenanceError("TikTok commit is not the fetched origin/main head")
    if _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ):
        raise ProvenanceError("TikTok checkout is dirty")
    commit_object = _git(repo, "cat-file", "commit", commit)
    if _git_object_id("commit", commit_object) != commit:
        raise ProvenanceError("TikTok commit proof does not hash to the requested commit")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    tree = _sha1(tree, label="TikTok source tree")
    file_count, executable_paths = _tree_inventory(repo, commit)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "source": {
            "repository": REPOSITORY,
            "branch": BRANCH,
            "commit": commit,
            "tree": tree,
            "git_object_format": "sha1",
            "file_count": file_count,
            "executable_paths": executable_paths,
            "commit_object_base64": base64.b64encode(commit_object).decode(),
        },
        "contract": {
            "path": CONTRACT_PATH,
            "sha256": contract_sha256,
        },
    }


def validate_manifest(
    value: Any,
    *,
    expected_commit: str,
    expected_contract_sha256: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvenanceError("TikTok source manifest must be an object")
    _exact_keys(
        value,
        {"schema_version", "kind", "source", "contract"},
        label="TikTok source manifest",
    )
    if value["schema_version"] != SCHEMA_VERSION or value["kind"] != KIND:
        raise ProvenanceError("TikTok source manifest schema/kind mismatch")
    source = value["source"]
    contract = value["contract"]
    if not isinstance(source, dict) or not isinstance(contract, dict):
        raise ProvenanceError("TikTok source manifest sections must be objects")
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
        label="TikTok source",
    )
    _exact_keys(contract, {"path", "sha256"}, label="TikTok source contract")
    if source["repository"] != REPOSITORY or source["branch"] != BRANCH:
        raise ProvenanceError("TikTok source repository/branch mismatch")
    commit = _sha1(source["commit"], label="TikTok source commit")
    if commit != _sha1(expected_commit, label="expected TikTok source commit"):
        raise ProvenanceError("TikTok source commit mismatch")
    tree = _sha1(source["tree"], label="TikTok source tree")
    if source["git_object_format"] != "sha1":
        raise ProvenanceError("TikTok source Git object format mismatch")
    count = source["file_count"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ProvenanceError("TikTok source file count is invalid")
    executable_paths = source["executable_paths"]
    if not isinstance(executable_paths, list) or not all(
        isinstance(path, str)
        and path
        and not path.startswith("/")
        and "\\" not in path
        and ".." not in Path(path).parts
        for path in executable_paths
    ):
        raise ProvenanceError("TikTok executable path inventory is invalid")
    if executable_paths != sorted(set(executable_paths), key=lambda item: item.encode()):
        raise ProvenanceError("TikTok executable paths must be unique and bytewise sorted")
    encoded = source["commit_object_base64"]
    if not isinstance(encoded, str):
        raise ProvenanceError("TikTok commit proof must be base64")
    try:
        commit_object = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProvenanceError("TikTok commit proof is invalid") from exc
    if _git_object_id("commit", commit_object) != commit:
        raise ProvenanceError("TikTok commit proof does not hash to the source commit")
    tree_headers = [
        line.removeprefix(b"tree ")
        for line in commit_object.split(b"\n\n", 1)[0].splitlines()
        if line.startswith(b"tree ")
    ]
    if len(tree_headers) != 1 or tree_headers[0].decode(errors="ignore") != tree:
        raise ProvenanceError("TikTok commit proof tree mismatch")
    if contract["path"] != CONTRACT_PATH:
        raise ProvenanceError("TikTok source contract path mismatch")
    contract_sha256 = _sha256(contract["sha256"], label="TikTok contract SHA-256")
    if contract_sha256 != _sha256(
        expected_contract_sha256,
        label="expected TikTok contract SHA-256",
    ):
        raise ProvenanceError("TikTok source contract SHA-256 mismatch")
    return value


def validate_manifest_file(
    manifest_path: Path,
    contract_path: Path,
    expected_commit: str,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ProvenanceError("cannot read TikTok source manifest") from exc
    if hashlib.sha256(raw).hexdigest() != _sha256(
        expected_manifest_sha256,
        label="expected TikTok manifest SHA-256",
    ):
        raise ProvenanceError("TikTok source manifest SHA-256 mismatch")
    return validate_manifest(
        _load(manifest_path, label="TikTok source manifest"),
        expected_commit=expected_commit,
        expected_contract_sha256=_contract_sha256(contract_path),
    )


def verify_checkout(
    repo_root: Path,
    manifest_path: Path,
    contract_path: Path,
    expected_commit: str,
    expected_manifest_sha256: str,
) -> None:
    actual = validate_manifest_file(
        manifest_path,
        contract_path,
        expected_commit,
        expected_manifest_sha256,
    )
    expected = create_manifest(repo_root, expected_commit, contract_path)
    if actual != expected:
        raise ProvenanceError("signed TikTok source does not exactly match the checkout")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-manifest")
    create.add_argument("--repo-root", type=Path, required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--contract", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate-manifest")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--contract", type=Path, required=True)
    validate.add_argument("--expected-commit", required=True)
    validate.add_argument("--expected-manifest-sha256", required=True)
    verify = commands.add_parser("verify-checkout")
    verify.add_argument("--repo-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--contract", type=Path, required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--expected-manifest-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create-manifest":
            args.output.write_bytes(
                _canonical(create_manifest(args.repo_root, args.commit, args.contract))
            )
        elif args.command == "validate-manifest":
            validate_manifest_file(
                args.manifest,
                args.contract,
                args.expected_commit,
                args.expected_manifest_sha256,
            )
        elif args.command == "verify-checkout":
            verify_checkout(
                args.repo_root,
                args.manifest,
                args.contract,
                args.expected_commit,
                args.expected_manifest_sha256,
            )
        else:  # pragma: no cover
            raise ProvenanceError("unsupported command")
    except (OSError, ProvenanceError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
