from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "openclaw_provenance.py"
ACTIVE_CONTRACT = ROOT / "infra" / "codebuild" / "openclaw_bundle_contract.json"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("openclaw_provenance_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provenance = _load_module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _ready_contract(tmp_path: Path) -> Path:
    value = json.loads(ACTIVE_CONTRACT.read_text(encoding="utf-8"))
    value["release"] = {"ready": True, "blocked_reason": ""}
    for index, subject in enumerate(value["bundle"]["subjects"], start=1):
        subject["binary_probes"] = [
            {
                "path": f"/usr/bin/openclaw-{subject['name']}",
                "sha256": str(index) * 64,
            }
        ]
    path = tmp_path / "openclaw-contract.json"
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "dev")
    _git(repo, "config", "user.name", "CodeBuild Test")
    _git(repo, "config", "user.email", "codebuild@example.invalid")
    (repo / "README.md").write_text("signed OpenClaw source\n", encoding="utf-8")
    (repo / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "run.sh").chmod(0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/dev", commit)
    return repo, commit


def _manifest_response(
    *,
    repository: str,
    image_digest: str,
    media_type: str = "application/vnd.oci.image.manifest.v1+json",
) -> dict[str, Any]:
    config_digest = "sha256:" + "a" * 64
    manifest = {
        "schemaVersion": 2,
        "mediaType": media_type,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": 123,
            "digest": config_digest,
        },
        "layers": [],
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    actual_digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    assert image_digest == actual_digest
    return {
        "images": [
            {
                "registryId": "718959508629",
                "repositoryName": repository,
                "imageId": {"imageDigest": image_digest},
                "imageManifest": raw,
                "imageManifestMediaType": media_type,
            }
        ],
        "failures": [],
    }


def test_active_contract_is_fail_closed_and_names_all_three_repository_stages() -> None:
    contract = provenance.load_contract(ACTIVE_CONTRACT)

    assert contract["release"]["ready"] is False
    with pytest.raises(provenance.ContractError, match="release is blocked"):
        provenance.require_release_ready(contract)
    assert contract["bundle"]["interfaces"] == {
        "build": "infra/openclaw/build-bundle.sh",
        "attest": "infra/codebuild/verify_actual_image.sh",
        "promote": "infra/codebuild/image-promoter-buildspec.yml",
    }
    for subject in contract["bundle"]["subjects"]:
        assert subject["quarantine_repository"].endswith("-quarantine")
        assert subject["candidate_repository"].endswith("-verified-candidates")
        assert not subject["release_repository"].endswith(("-quarantine", "-verified-candidates"))


def test_ready_contract_requires_binary_hashes_and_signed_sbom_provenance() -> None:
    value = json.loads(ACTIVE_CONTRACT.read_text(encoding="utf-8"))
    value["release"] = {"ready": True, "blocked_reason": ""}
    with pytest.raises(provenance.ContractError, match="binary_probes is required"):
        provenance.validate_contract(value)

    assert value["bundle"]["required_referrers"] == [
        {
            "name": "provenance",
            "artifact_type": "application/vnd.in-toto+json",
            "minimum": 1,
            "signature_required": True,
        },
        {
            "name": "sbom",
            "artifact_type": "application/spdx+json",
            "minimum": 1,
            "signature_required": True,
        },
    ]


def test_signed_source_manifest_binds_full_commit_tree_and_contract(tmp_path: Path) -> None:
    contract = _ready_contract(tmp_path)
    repo, commit = _source_repo(tmp_path)
    manifest = tmp_path / "manifest.json"
    provenance.write_source_manifest(repo, commit, contract, manifest)
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()

    provenance.verify_source_manifest(repo, manifest, contract, commit, digest)
    provenance.validate_signed_source_manifest(manifest, contract, commit, digest)

    hostile = json.loads(manifest.read_text(encoding="utf-8"))
    hostile["source"]["tree"] = "f" * 40
    manifest.write_text(json.dumps(hostile), encoding="utf-8")
    hostile_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(provenance.ContractError, match=r"commit proof|source manifest"):
        provenance.validate_signed_source_manifest(
            manifest,
            contract,
            commit,
            hostile_digest,
        )


def test_arm64_config_gate_rejects_index_and_release_repository(tmp_path: Path) -> None:
    contract = _ready_contract(tmp_path)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": 123,
            "digest": "sha256:" + "a" * 64,
        },
        "layers": [],
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    response = _manifest_response(
        repository="teamagent-openclaw-quarantine",
        image_digest=digest,
    )
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(response), encoding="utf-8")

    assert (
        provenance.arm64_config_digest(
            path,
            contract,
            digest,
            "teamagent-openclaw-quarantine",
            "718959508629",
        )
        == "sha256:" + "a" * 64
    )
    with pytest.raises(provenance.ContractError, match="repository is invalid"):
        provenance.arm64_config_digest(
            path,
            contract,
            digest,
            "teamagent-openclaw",
            "718959508629",
        )
    response["images"][0]["imageManifestMediaType"] = "application/vnd.oci.image.index.v1+json"
    path.write_text(json.dumps(response), encoding="utf-8")
    with pytest.raises(provenance.ContractError, match="single OCI image manifest"):
        provenance.arm64_config_digest(
            path,
            contract,
            digest,
            "teamagent-openclaw-quarantine",
            "718959508629",
        )


def test_bundle_receipt_requires_full_sha_tags_distinct_child_and_ch_zero(
    tmp_path: Path,
) -> None:
    contract = _ready_contract(tmp_path)
    commit = "1" * 40
    contract_sha = hashlib.sha256(contract.read_bytes()).hexdigest()
    subjects = []
    for index, expected in enumerate(
        json.loads(contract.read_text())["bundle"]["subjects"], start=1
    ):
        subjects.append(
            {
                "name": expected["name"],
                "quarantine_repository": expected["quarantine_repository"],
                "release_repository": expected["release_repository"],
                "tag": f"candidate-{commit}-{expected['name']}",
                "index_digest": "sha256:" + str(index) * 64,
                "arm64_digest": "sha256:" + str(index + 2) * 64,
                "scan": {"critical": 0, "high": 0},
            }
        )
    receipt = {
        "schema_version": 1,
        "source_commit": commit,
        "bundle_contract_sha256": contract_sha,
        "subjects": subjects,
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    provenance.verify_bundle_receipt(path, contract, commit, contract_sha)

    short = copy.deepcopy(receipt)
    short["subjects"][0]["tag"] = f"candidate-{commit[:12]}-core"
    path.write_text(json.dumps(short), encoding="utf-8")
    with pytest.raises(provenance.ContractError, match="tag mismatch"):
        provenance.verify_bundle_receipt(path, contract, commit, contract_sha)

    weak = copy.deepcopy(receipt)
    weak["subjects"][0]["scan"]["high"] = 1
    path.write_text(json.dumps(weak), encoding="utf-8")
    with pytest.raises(provenance.ContractError, match="not C/H0"):
        provenance.verify_bundle_receipt(path, contract, commit, contract_sha)


def test_legacy_referrer_only_release_commands_are_not_exposed() -> None:
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "create-release-evidence"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "invalid choice" in completed.stderr
