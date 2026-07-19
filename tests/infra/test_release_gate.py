from __future__ import annotations

import importlib.util
import os
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "infra/deploy/verify_release_gate.py"
TREE_PATH = ROOT / "infra/deploy/verify_source_tree.py"
DEPLOY = (ROOT / "infra/deploy/deploy_connectweb_unified.sh").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "infra/deploy/build_teamagent_image.sh").read_text(encoding="utf-8")
BUILDSPEC = (ROOT / "infra/codebuild/buildspec.yml").read_text(encoding="utf-8")
PUBLISHER = (ROOT / "infra/codebuild/mcp-source-publisher-buildspec.yml").read_text(
    encoding="utf-8"
)
COMMIT = "a" * 40
TREE = "b" * 40
BRANCH = "fix/runtime-hardening"
IMAGE = f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:{'c' * 64}"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load(GATE_PATH, "release_gate_verifier")
TREE_VERIFIER = _load(TREE_PATH, "source_tree_verifier")


def test_legacy_self_attestable_release_gate_is_disabled() -> None:
    assert not hasattr(GATE, "verify_release_gate")
    completed = subprocess.run(
        ["python3", str(GATE_PATH)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 64
    assert "immutable KMS-signed receipts" in completed.stderr


def test_source_tree_verifier_matches_git_archive_and_detects_mutation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"],
        check=True,
    )
    (repository / "regular.txt").write_text("regular\n", encoding="utf-8")
    executable = repository / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    os.symlink("regular.txt", repository / "link")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True)
    expected_tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive = tmp_path / "source.tar"
    with archive.open("wb") as output:
        subprocess.run(
            ["git", "-C", str(repository), "archive", "--format=tar", "HEAD"],
            check=True,
            stdout=output,
        )
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive, "r:") as bundle:
        bundle.extractall(extracted)

    assert TREE_VERIFIER.verify_source_tree(extracted, expected_tree) == expected_tree
    (extracted / "regular.txt").write_text("mutated\n", encoding="utf-8")
    with pytest.raises(TREE_VERIFIER.SourceTreeError, match="mismatch"):
        TREE_VERIFIER.verify_source_tree(extracted, expected_tree)


def test_canonical_build_and_deploy_paths_are_immutable_and_separate() -> None:
    assert "permanently disabled" in DEPLOY
    assert "aws " not in DEPLOY
    assert "register-task-definition" not in DEPLOY
    assert "update-service" not in DEPLOY
    assert "git ls-remote --exit-code --heads" in LAUNCHER
    assert "worktree add --quiet --detach" in LAUNCHER
    assert 'archive --format=tar "$REMOTE_COMMIT"' in LAUNCHER

    assert "git ls-remote --exit-code --heads" in PUBLISHER
    assert "worktree add --quiet --detach" in PUBLISHER
    assert "canonical_build_context.py" in PUBLISHER
    assert "source_provenance.py verify-source" in BUILDSPEC
    assert "canonical_build_context.py" in BUILDSPEC
    assert "SIGNED_BUILD_CONTEXT_SHA256" in BUILDSPEC
    assert ' - <"$BUILD_CONTEXT_TAR"' in BUILDSPEC
    assert '--build-arg "GIT_COMMIT=$GIT_COMMIT"' in BUILDSPEC
    assert '--build-arg "GIT_BRANCH=$GIT_BRANCH"' in BUILDSPEC
    assert '--build-arg "BUILD_CONTEXT_SHA256=$BUILD_CONTEXT_SHA256"' in BUILDSPEC
    assert "--provenance=mode=max" in BUILDSPEC
    assert "--sbom=true" in BUILDSPEC
    assert "--push" in BUILDSPEC
    assert "--load" not in BUILDSPEC
