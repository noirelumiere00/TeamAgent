from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "infra/deploy/verify_release_gate.py"
TREE_PATH = ROOT / "infra/deploy/verify_source_tree.py"
DEPLOY = (ROOT / "infra/deploy/deploy_connectweb_unified.sh").read_text(encoding="utf-8")
BUILDSPEC = (ROOT / "infra/codebuild/buildspec.yml").read_text(encoding="utf-8")
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


def _subject(*, image: bool) -> dict[str, str]:
    value = {
        "git_commit": COMMIT,
        "git_tree": TREE,
        "git_branch": BRANCH,
    }
    if image:
        value["image_uri"] = IMAGE
    return value


def _gate() -> dict[str, Any]:
    required = [
        "local_runtime_evidence",
        "ecr_basic_scan",
        "fargate_runtime_smoke",
        "image_provenance",
    ]
    return {
        "schema_version": "1",
        "decision": "ACCEPTED",
        "reviewed": {
            "git_commit": COMMIT,
            "git_tree": TREE,
            "git_branch": BRANCH,
            "image_uri": IMAGE,
        },
        "required_gates": required,
        "gates": {
            name: {
                "status": "ACCEPTED",
                "evidence_sha256": f"{index + 1:064x}",
                "subject": _subject(image=name != "local_runtime_evidence"),
            }
            for index, name in enumerate(required)
        },
    }


def _write_gate(tmp_path: Path, value: dict[str, Any]) -> tuple[Path, str]:
    path = tmp_path / "release-gate.json"
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_gate_accepts_baseline_plus_future_provenance_gate(tmp_path: Path) -> None:
    path, digest = _write_gate(tmp_path, _gate())

    summary = GATE.verify_release_gate(
        path,
        expected_sha256=digest,
        expected_commit=COMMIT,
        expected_tree=TREE,
        expected_branch=BRANCH,
        expected_image_uri=IMAGE,
    )

    assert summary["release_gate_sha256"] == digest
    assert "image_provenance" in summary["required_gates"]


@pytest.mark.parametrize(
    "mutation",
    (
        "decision",
        "reviewed",
        "status",
        "required",
        "evidence",
        "subject",
        "provenance_subject",
        "file_digest",
    ),
)
def test_release_gate_rejects_mutated_or_incomplete_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    value = copy.deepcopy(_gate())
    if mutation == "decision":
        value["decision"] = "REJECTED"
    elif mutation == "reviewed":
        value["reviewed"]["git_tree"] = "d" * 40
    elif mutation == "status":
        value["gates"]["ecr_basic_scan"]["status"] = "PENDING"
    elif mutation == "required":
        value["required_gates"].remove("fargate_runtime_smoke")
        value["gates"].pop("fargate_runtime_smoke")
    elif mutation == "evidence":
        value["gates"]["local_runtime_evidence"]["evidence_sha256"] = ""
    elif mutation == "subject":
        value["gates"]["fargate_runtime_smoke"]["subject"]["image_uri"] = IMAGE[:-1] + "d"
    elif mutation == "provenance_subject":
        value["gates"]["image_provenance"]["subject"].pop("image_uri")
    path, digest = _write_gate(tmp_path, value)
    if mutation == "file_digest":
        digest = "e" * 64

    with pytest.raises(GATE.ReleaseGateError):
        GATE.verify_release_gate(
            path,
            expected_sha256=digest,
            expected_commit=COMMIT,
            expected_tree=TREE,
            expected_branch=BRANCH,
            expected_image_uri=IMAGE,
        )


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


def test_canonical_deploy_separates_exact_archive_build_from_gated_digest_deploy() -> None:
    assert 'git -C "$REPO_ROOT" archive --format=zip "$EXPECTED_COMMIT"' in DEPLOY
    assert 'ACTUAL_TREE=$(git -C "$REPO_ROOT" rev-parse "$EXPECTED_COMMIT^{tree}")' in DEPLOY
    for name in (
        "GIT_COMMIT",
        "GIT_TREE",
        "GIT_BRANCH",
        "SOURCE_ARCHIVE_SHA256",
        "EXPECTED_SOURCE_VERSION_ID",
    ):
        assert f"name={name},value=$" in DEPLOY
    assert '--source-version "$SOURCE_VERSION_ID"' in DEPLOY
    assert "--image-uri must be an immutable ECR digest URI" in DEPLOY
    gate_index = DEPLOY.index('python3 "$VERIFY_GATE"')
    first_deploy_aws = DEPLOY.index("aws iam get-role-policy")
    assert gate_index < first_deploy_aws
    assert "build-candidate uploads/builds only" in DEPLOY
    assert 'cp "$SRC_HTML"' not in DEPLOY
    assert "zip -rq" not in DEPLOY

    assert "verify_source_tree.py" in BUILDSPEC
    assert '--expected-tree "$GIT_TREE"' in BUILDSPEC
    assert 'test "$CODEBUILD_RESOLVED_SOURCE_VERSION" = "$EXPECTED_SOURCE_VERSION_ID"' in BUILDSPEC
    assert '--build-arg "GIT_COMMIT=$GIT_COMMIT"' in BUILDSPEC
    assert '--build-arg "GIT_BRANCH=$GIT_BRANCH"' in BUILDSPEC
    assert "org.opencontainers.image.revision" in BUILDSPEC
    assert "org.opencontainers.image.ref.name" in BUILDSPEC
    assert "--provenance=mode=max" in BUILDSPEC
    assert "--sbom=true" in BUILDSPEC
    assert "--push" in BUILDSPEC
    assert "--load" not in BUILDSPEC
    assert 'test "$DIGEST" = "$BUILD_DIGEST"' in BUILDSPEC
