"""Offline contract tests for the CodeBuild image launcher (no AWS writes)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "deploy" / "build_teamagent_image.sh"
PROVENANCE = ROOT / "infra" / "codebuild" / "source_provenance.py"

FAKE_AWS = r"""#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]


def value(flag):
    return args[args.index(flag) + 1]


def image_values():
    config_bytes = Path(os.environ["FAKE_CONFIG"]).read_bytes()
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": len(config_bytes),
            "digest": config_digest,
        },
        "layers": [],
    }
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    image_digest = "sha256:" + hashlib.sha256(manifest_raw.encode()).hexdigest()
    return config_digest, manifest_raw, image_digest


record = {"args": args}
if args[:2] == ["codebuild", "start-build"]:
    env_path = value("--environment-variables-override").removeprefix("file://")
    record["environment"] = json.loads(Path(env_path).read_text())
with Path(os.environ["FAKE_AWS_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")

if args[:2] == ["s3api", "put-object"]:
    shutil.copyfile(value("--body"), os.environ["CAPTURE_ZIP"])
    print(os.environ.get("FAKE_VERSION", "version-123"))
elif args[:2] == ["codebuild", "start-build"]:
    print("fixture-project:11111111-2222-3333-4444-555555555555")
elif args[:2] == ["codebuild", "batch-get-builds"]:
    resolved = os.environ.get("FAKE_RESOLVED_VERSION", os.environ.get("FAKE_VERSION", "version-123"))
    print(f"SUCCEEDED\t{resolved}")
elif args[:2] == ["ecr", "describe-images"]:
    _config_digest, _manifest_raw, image_digest = image_values()
    print(image_digest)
elif args[:2] == ["ecr", "batch-get-image"]:
    _config_digest, manifest_raw, image_digest = image_values()
    print(json.dumps({
        "images": [{"imageId": {"imageDigest": image_digest}, "imageManifest": manifest_raw}],
        "failures": [],
    }))
elif args[:2] == ["ecr", "get-download-url-for-layer"]:
    print("https://example.invalid/private-oci-config")
else:
    print(f"unexpected fake aws command: {args}", file=sys.stderr)
    raise SystemExit(91)
"""

FAKE_CURL = r"""#!/usr/bin/env python3
import os
import shutil
import sys

args = sys.argv[1:]
output = args[args.index("--output") + 1]
shutil.copyfile(os.environ["FAKE_CONFIG"], output)
"""


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fixture(
    tmp_path: Path,
    *,
    profile_label: str = "true",
) -> tuple[Path, str, dict[str, str], Path, Path]:
    repo = tmp_path / "repo"
    (repo / "infra" / "deploy").mkdir(parents=True)
    (repo / "infra" / "codebuild").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "infra" / "deploy" / SCRIPT.name)
    shutil.copy2(PROVENANCE, repo / "infra" / "codebuild" / PROVENANCE.name)
    (repo / ".gitignore").write_text("ignored-secret.env\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("archive me\n", encoding="utf-8")
    _run_git(repo, "init", "-b", "fixture-branch")
    _run_git(repo, "config", "user.name", "CodeBuild Test")
    _run_git(repo, "config", "user.email", "codebuild-test@example.invalid")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "fixture")
    commit = _run_git(repo, "rev-parse", "HEAD")
    (repo / "ignored-secret.env").write_text("DO_NOT_ARCHIVE=secret-value\n", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "aws", FAKE_AWS)
    _write_executable(fake_bin / "curl", FAKE_CURL)
    log_path = tmp_path / "aws.jsonl"
    captured_zip = tmp_path / "captured-source.zip"
    config_path = tmp_path / "config.json"
    config = {
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": commit,
                "io.teamagent.build.with-scrape-tools": profile_label,
            }
        }
    }
    config_path.write_text(
        json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_AWS_LOG": str(log_path),
            "FAKE_CONFIG": str(config_path),
            "CAPTURE_ZIP": str(captured_zip),
            "FAKE_VERSION": "version-123",
        }
    )
    return repo, commit, env, log_path, captured_zip


def _args(repo: Path, *, tag: str = "candidate-123", profile: str = "true") -> list[str]:
    return [
        "bash",
        str(repo / "infra" / "deploy" / SCRIPT.name),
        "--image-tag",
        tag,
        "--with-scrape-tools",
        profile,
        "--source-bucket",
        "fixture-source-bucket",
        "--project-name",
        "fixture-project",
        "--repository-name",
        "fixture-repo",
        "--poll-seconds",
        "1",
        "--timeout-seconds",
        "30",
    ]


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _record(records: list[dict[str, Any]], prefix: list[str]) -> dict[str, Any]:
    return next(record for record in records if record["args"][: len(prefix)] == prefix)


def test_bash_syntax_is_valid() -> None:
    completed = _run(["bash", "-n", str(SCRIPT)], cwd=ROOT)
    assert completed.returncode == 0, completed.stderr


def test_scrape_profile_is_required_and_has_no_default() -> None:
    completed = _run(["bash", str(SCRIPT), "--image-tag", "candidate-1"], cwd=ROOT)

    assert completed.returncode != 0
    assert "--with-scrape-tools true|false is required" in completed.stderr


@pytest.mark.parametrize("tag", ["bad/tag", "-leading-dash", "space tag", "x" * 129])
def test_unsafe_image_tag_fails_before_any_aws_call(tag: str) -> None:
    completed = _run(
        [
            "bash",
            str(SCRIPT),
            "--image-tag",
            tag,
            "--with-scrape-tools",
            "true",
        ],
        cwd=ROOT,
    )

    assert completed.returncode != 0
    assert "unsafe image tag" in completed.stderr


def test_dirty_or_untracked_worktree_fails_before_upload(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    (repo / "untracked-sensitive.txt").write_text("not uploaded\n", encoding="utf-8")

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "Git worktree is dirty" in completed.stderr
    assert _records(log_path) == []


def test_git_archive_version_pin_env_binding_and_remote_labels(tmp_path: Path) -> None:
    repo, commit, env, log_path, captured_zip = _fixture(tmp_path)

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = _records(log_path)
    put = _record(records, ["s3api", "put-object"])
    assert put["args"][put["args"].index("--key") + 1] == "codebuild/source.zip"
    start = _record(records, ["codebuild", "start-build"])
    assert start["args"][start["args"].index("--source-version") + 1] == "version-123"
    assert start["args"][start["args"].index("--buildspec-override") + 1] == (
        "infra/codebuild/buildspec.yml"
    )
    overrides = {item["name"]: item["value"] for item in start["environment"]}
    assert overrides == {
        "GIT_COMMIT": commit,
        "GIT_BRANCH": "fixture-branch",
        "IMAGE_TAG": "candidate-123",
        "WITH_SCRAPE_TOOLS": "true",
    }
    assert all(item["type"] == "PLAINTEXT" for item in start["environment"])

    with zipfile.ZipFile(captured_zip) as source_zip:
        names = set(source_zip.namelist())
        manifest = json.loads(source_zip.read(".teamagent-source-manifest.json"))
    assert "tracked.txt" in names
    assert "ignored-secret.env" not in names
    assert not any(name == ".git" or name.startswith(".git/") for name in names)
    assert manifest["commit"] == commit
    assert manifest["branch"] == "fixture-branch"
    assert manifest["build_parameters"] == {"with_scrape_tools": True}
    assert "private-oci-config" not in completed.stdout + completed.stderr
    assert "no deployment performed" in completed.stdout
    assert not any(record["args"][0] in {"ecs", "events"} for record in records)


def test_missing_s3_version_id_fails_before_start_build(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_VERSION"] = "None"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "S3 did not return a usable VersionId" in completed.stderr
    assert not any(
        record["args"][:2] == ["codebuild", "start-build"] for record in _records(log_path)
    )


def test_resolved_source_version_mismatch_fails_before_ecr_lookup(tmp_path: Path) -> None:
    repo, _commit, env, log_path, _captured_zip = _fixture(tmp_path)
    env["FAKE_RESOLVED_VERSION"] = "different-version"

    completed = _run(_args(repo), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "resolvedSourceVersion does not match" in completed.stderr
    assert not any(record["args"][0] == "ecr" for record in _records(log_path))


def test_remote_candidate_scrape_label_mismatch_fails_closed(tmp_path: Path) -> None:
    repo, _commit, env, _log_path, _captured_zip = _fixture(tmp_path, profile_label="false")

    completed = _run(_args(repo, profile="true"), cwd=repo, env=env)

    assert completed.returncode != 0
    assert "io.teamagent.build.with-scrape-tools mismatch" in completed.stderr
