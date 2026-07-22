#!/usr/bin/env python3
"""Materialize a credential-free, exact origin/dev checkout for bootstrap wrappers."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

EXPECTED_ORIGIN = "git@github.com:noirelumiere00/TeamAgent.git"
EXPECTED_REMOTE = "https://github.com/noirelumiere00/TeamAgent.git"
EXPECTED_REF = "refs/heads/dev"
EXPECTED_TRACKING_REF = "refs/remotes/origin/dev"
BOOTSTRAP_GIT_TOKEN_ENV = "TEAMAGENT_BOOTSTRAP_GIT_TOKEN"
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
CREDENTIAL_ENV = {
    "AWS_ACCESS_KEY_ID",
    "AWS_CONFIG_FILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_PROFILE",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SECURITY_TOKEN",
    "AWS_SESSION_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
}
INFLUENTIAL_GIT_ENV = {
    "CURL_CA_BUNDLE",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_ASKPASS",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PROXY_COMMAND",
    "GIT_REPLACE_REF_BASE",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "GIT_SSL_NO_VERIFY",
    "GIT_TRACE",
    "GIT_TRACE_CURL",
    "GIT_TRACE_CURL_NO_DATA",
    "GIT_TRACE_PACKET",
    "GIT_TRACE_REDACT",
    "GIT_CURL_VERBOSE",
    "GIT_WORK_TREE",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
}
PROFILE_CHILDREN = {
    "bootstrap-iam": (
        "infra/bootstrap/bootstrap_contract.json",
        "infra/bootstrap/provenance_iam_bootstrap.py",
        "infra/bootstrap/seed-stack.yaml",
        "infra/bootstrap/wrapper_provenance.py",
        "infra/deploy/bootstrap_provenance_iam.sh",
    ),
    "provenance-session": (
        "infra/bootstrap/wrapper_provenance.py",
        "infra/codebuild/openclaw_bundle_contract.json",
        "infra/codebuild/openclaw_provenance.py",
        "infra/codebuild/release_evidence.py",
        "infra/codebuild/resolve_ecr_image.py",
        "infra/codebuild/source_provenance.py",
        "infra/codebuild/teamagent_bundle_provenance.py",
        "infra/codebuild/teamagent_core_media_release_contract.json",
        "infra/codebuild/teamagent_runtime_contract.json",
        "infra/codebuild/tiktok_release_contract.json",
        "infra/codebuild/tiktok_source_provenance.py",
        "infra/deploy_log.md",
        "infra/deploy/authorize_image_release.sh",
        "infra/deploy/bootstrap_provenance_session.sh",
        "infra/deploy/build_openclaw_image.sh",
        "infra/deploy/build_teamagent_image.sh",
        "infra/deploy/build_tiktok_image.sh",
    ),
    "runtime-session": (
        "infra/bootstrap/wrapper_provenance.py",
        "infra/codebuild/release_evidence.py",
        "infra/deploy/bootstrap_runtime_session.sh",
        "infra/deploy/deployment_apply_finalizer.py",
        "infra/deploy/media_cutover_apply_authorizer.py",
        "infra/deploy/run_image_deployment_gate.sh",
        "infra/deploy/runtime_evidence_guard.py",
        "infra/deploy/terraform_plan_contract.py",
        "infra/deploy/terraform_runtime_guard.jq",
        "infra/deploy/terraform_runtime_guard.sh",
        "infra/deploy/terraform_runtime_migrations.json",
        "infra/openclaw/effective-tool-scope.json",
        "infra/openclaw/run-live-rollout-gates.mjs",
        "infra/terraform/ecs_service_apply_saga.py",
        "infra/terraform/eventbridge_apply_saga.py",
        "infra/terraform/image_release_context.py",
        "infra/terraform/stage_saved_plan.py",
        "infra/terraform/terraform_apply_supervisor.py",
        "scripts/hmac_rollout_gate.py",
        "scripts/preflight_hmac_rotation.py",
        "scripts/terraform_hmac_payload.py",
        "scripts/verify_worker_bundle_provenance.py",
    ),
}


class ProvenanceError(RuntimeError):
    pass


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        list(arguments),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ProvenanceError(f"credential-free Git command failed: {arguments[1]}: {error}")
    return completed


def _git_env(source: dict[str, str]) -> dict[str, str]:
    present = sorted(name for name in CREDENTIAL_ENV if source.get(name))
    if present:
        raise ProvenanceError(
            "wrapper provenance must run without credential selectors: " + ", ".join(present)
        )
    influential = sorted(
        name
        for name in source
        if name in INFLUENTIAL_GIT_ENV or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    )
    if influential:
        raise ProvenanceError(
            "wrapper provenance rejects influential Git environment: " + ", ".join(influential)
        )
    result = dict(source)
    result.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return result


def _http_auth_args(env: dict[str, str]) -> list[str]:
    """Explicit read-only token for credential-free HTTPS verification of a
    PRIVATE origin. Empty when no token -> public-repo behaviour unchanged.
    Token rides argv only (never persisted). GitHub git-over-HTTPS requires
    HTTP Basic (base64("x-access-token:"+token)); Bearer is rejected."""
    token = env.get(BOOTSTRAP_GIT_TOKEN_ENV, "").strip()
    if not token:
        return []
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in token):
        raise ProvenanceError("bootstrap Git token contains control characters")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraHeader=Authorization: Basic {basic}"]


def _decode_line(value: bytes, *, label: str) -> str:
    try:
        result = value.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ProvenanceError(f"{label} is not UTF-8") from exc
    if not result or "\n" in result or "\r" in result:
        raise ProvenanceError(f"{label} is malformed")
    return result


def _assert_safe_config(repo_root: Path, env: dict[str, str]) -> None:
    raw = _run(
        ["git", "config", "--local", "--name-only", "--null", "--list"],
        cwd=repo_root,
        env=env,
    ).stdout
    try:
        keys = [item.lower() for item in raw.decode("utf-8").split("\0") if item]
    except UnicodeDecodeError as exc:
        raise ProvenanceError("local Git configuration is not UTF-8") from exc
    unsafe = [
        key
        for key in keys
        if key.startswith(("http.", "protocol.", "url.", "include", "core.sshcommand"))
        or key
        in {
            "core.fsmonitor",
            "core.gitproxy",
            "remote.origin.proxy",
            "remote.origin.uploadpack",
        }
    ]
    if unsafe:
        raise ProvenanceError(
            "local Git configuration can redirect provenance: " + ", ".join(sorted(set(unsafe)))
        )


def _tree_inventory(
    repo_root: Path,
    *,
    commit: str,
    env: dict[str, str],
    verify_worktree: bool,
) -> tuple[str, dict[str, str]]:
    raw = _run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", commit],
        cwd=repo_root,
        env=env,
    ).stdout
    records = [record for record in raw.split(b"\0") if record]
    if not records:
        raise ProvenanceError("reviewed Git tree is empty")
    manifest = hashlib.sha256()
    file_hashes: dict[str, str] = {}
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, object_type, expected_oid = metadata.split(b" ", 2)
            relative = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProvenanceError("reviewed Git tree inventory is malformed") from exc
        if (
            object_type != b"blob"
            or raw_mode not in {b"100644", b"100755"}
            or not re.fullmatch(rb"[0-9a-f]{40}", expected_oid)
        ):
            raise ProvenanceError(f"unsupported tracked object: {relative}")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ProvenanceError(f"tracked path escapes checkout: {relative}")
        if verify_worktree:
            path = repo_root / relative_path
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ProvenanceError(f"tracked child is not a regular file: {relative}")
            if bool(before.st_mode & 0o111) != (raw_mode == b"100755"):
                raise ProvenanceError(f"tracked child mode differs: {relative}")
            actual_oid = _decode_line(
                _run(
                    ["git", "hash-object", "--no-filters", "--", str(path)],
                    cwd=repo_root,
                    env=env,
                ).stdout,
                label=f"blob hash {relative}",
            )
            if actual_oid != expected_oid.decode():
                raise ProvenanceError(f"tracked child hash differs: {relative}")
            file_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.update(raw_mode)
        manifest.update(b"\0")
        manifest.update(raw_path)
        manifest.update(b"\0")
        manifest.update(expected_oid)
        manifest.update(b"\0")
    return manifest.hexdigest(), file_hashes


def _chmod_immutable(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ProvenanceError(f"immutable checkout contains a symlink: {path}")
        if stat.S_ISDIR(mode):
            path.chmod(0o500)
        elif stat.S_ISREG(mode):
            path.chmod(0o500 if mode & 0o111 else 0o400)
        else:
            raise ProvenanceError(f"immutable checkout contains a special file: {path}")
    root.chmod(0o500)


def prepare(
    *,
    repo_root: Path,
    checkout_dir: Path,
    receipt_path: Path,
    profile: str,
    process_env: dict[str, str],
) -> None:
    env = _git_env(process_env)
    repo_root = repo_root.resolve(strict=True)
    _assert_safe_config(repo_root, env)
    status = _run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        cwd=repo_root,
        env=env,
    )
    if status.stdout:
        raise ProvenanceError("wrapper requires an exact clean worktree")
    symbolic = _run(
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        cwd=repo_root,
        env=env,
        check=False,
    )
    if symbolic.returncode == 0:
        raise ProvenanceError("wrapper must run from a detached HEAD")
    if symbolic.returncode not in {1, 128}:
        raise ProvenanceError("could not prove detached HEAD")
    origin = _decode_line(
        _run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_root,
            env=env,
        ).stdout,
        label="Git origin",
    )
    if origin != EXPECTED_ORIGIN:
        raise ProvenanceError("Git origin is not the exact reviewed repository")
    commit = _decode_line(
        _run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo_root,
            env=env,
        ).stdout,
        label="HEAD commit",
    )
    tracking_commit = _decode_line(
        _run(
            ["git", "rev-parse", "--verify", f"{EXPECTED_TRACKING_REF}^{{commit}}"],
            cwd=repo_root,
            env=env,
        ).stdout,
        label="origin/dev commit",
    )
    if not SHA1_RE.fullmatch(commit) or tracking_commit != commit:
        raise ProvenanceError("detached HEAD is not the exact local origin/dev commit")
    auth_args = _http_auth_args(env)
    remote_lines = (
        _run(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "http.followRedirects=false",
                "-c",
                "http.sslVerify=true",
                *auth_args,
                "ls-remote",
                "--exit-code",
                "--heads",
                EXPECTED_REMOTE,
                EXPECTED_REF,
            ],
            cwd=repo_root,
            env=env,
        )
        .stdout.decode("utf-8")
        .splitlines()
    )
    exact = [line.split() for line in remote_lines if line.split()[1:] == [EXPECTED_REF]]
    if len(exact) != 1 or exact[0][0] != commit:
        raise ProvenanceError("detached HEAD is not the fresh protected origin/dev commit")
    tree_sha256, source_hashes = _tree_inventory(
        repo_root,
        commit=commit,
        env=env,
        verify_worktree=True,
    )
    missing_children = [child for child in PROFILE_CHILDREN[profile] if child not in source_hashes]
    if missing_children:
        raise ProvenanceError(
            "transitive wrapper child is not tracked: " + ", ".join(missing_children)
        )

    checkout_parent = checkout_dir.parent.resolve(strict=True)
    receipt_parent = receipt_path.parent.resolve(strict=True)
    parent_stat = checkout_parent.stat()
    if (
        receipt_parent != checkout_parent
        or parent_stat.st_uid != os.getuid()
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise ProvenanceError("reviewed checkout and receipt require the same owned 0700 parent")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ProvenanceError("wrapper provenance receipt path must not exist")
    if checkout_dir.exists() or checkout_dir.is_symlink():
        raise ProvenanceError("reviewed checkout path must not exist")
    bare = checkout_parent / "reviewed-objects.git"
    if bare.exists() or bare.is_symlink():
        raise ProvenanceError("reviewed object repository path must not exist")
    _run(["git", "init", "--bare", "--quiet", str(bare)], cwd=checkout_parent, env=env)
    _run(
        [
            "git",
            "-C",
            str(bare),
            "remote",
            "add",
            "origin",
            EXPECTED_ORIGIN,
        ],
        cwd=checkout_parent,
        env=env,
    )
    _run(
        [
            "git",
            "-C",
            str(bare),
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.sslVerify=true",
            *auth_args,
            "fetch",
            "--quiet",
            "--no-tags",
            EXPECTED_REMOTE,
            f"{EXPECTED_REF}:{EXPECTED_TRACKING_REF}",
        ],
        cwd=checkout_parent,
        env=env,
    )
    fetched = _decode_line(
        _run(
            ["git", "-C", str(bare), "rev-parse", "--verify", EXPECTED_TRACKING_REF],
            cwd=checkout_parent,
            env=env,
        ).stdout,
        label="fetched origin/dev",
    )
    if fetched != commit:
        raise ProvenanceError("fetched origin/dev changed during materialization")
    _run(
        [
            "git",
            "-C",
            str(bare),
            "worktree",
            "add",
            "--quiet",
            "--detach",
            str(checkout_dir),
            commit,
        ],
        cwd=checkout_parent,
        env=env,
    )
    reviewed_tree_sha256, reviewed_hashes = _tree_inventory(
        checkout_dir,
        commit=commit,
        env=env,
        verify_worktree=True,
    )
    child_hashes = {child: reviewed_hashes[child] for child in PROFILE_CHILDREN[profile]}
    if reviewed_tree_sha256 != tree_sha256 or any(
        child_hashes[child] != source_hashes[child] for child in child_hashes
    ):
        raise ProvenanceError("transitive child hashes changed in reviewed checkout")

    receipt = {
        "kind": "teamagent-bootstrap-wrapper-provenance",
        "schema_version": 1,
        "profile": profile,
        "origin": EXPECTED_ORIGIN,
        "remote": EXPECTED_REMOTE,
        "ref": EXPECTED_REF,
        "commit": commit,
        "source_tree_sha256": tree_sha256,
        "transitive_child_sha256": child_hashes,
        "credential_free": not auth_args,
        "detached": True,
    }
    data = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
    descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _chmod_immutable(checkout_dir)
    _chmod_immutable(bare)
    directory = os.open(receipt_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--checkout-dir", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--profile", choices=sorted(PROFILE_CHILDREN), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepare(
            repo_root=Path(args.repo_root),
            checkout_dir=Path(args.checkout_dir),
            receipt_path=Path(args.receipt),
            profile=args.profile,
            process_env=dict(os.environ),
        )
    except (OSError, ProvenanceError, subprocess.SubprocessError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
