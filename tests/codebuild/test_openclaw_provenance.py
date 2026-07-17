from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "openclaw_provenance.py"
ACTIVE_CONTRACT = ROOT / "infra" / "codebuild" / "openclaw_bundle_contract.json"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("openclaw_contract", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provenance = _load_module()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _ready_contract(tmp_path: Path) -> Path:
    value = json.loads(ACTIVE_CONTRACT.read_text(encoding="utf-8"))
    value["release"] = {"ready": True, "blocked_reason": ""}
    path = tmp_path / "openclaw-contract.json"
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "dev")
    _git(repo, "config", "user.name", "OpenClaw Test")
    _git(repo, "config", "user.email", "openclaw-test@example.invalid")
    nested = repo / "infra" / "openclaw" / "fixture.sh"
    nested.parent.mkdir(parents=True)
    nested.write_text("#!/usr/bin/env bash\necho fixture\n", encoding="utf-8")
    nested.chmod(0o755)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _referrer(
    digest_character: str,
    artifact_type: str,
    *,
    status: str = "ACTIVE",
) -> dict[str, object]:
    return {
        "digest": "sha256:" + digest_character * 64,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": artifact_type,
        "artifactStatus": status,
        "size": 123,
    }


def _subject_response() -> dict[str, object]:
    return {
        "referrers": [
            _referrer("1", "application/vnd.dev.cosign.simplesigning.v1+json"),
            _referrer("2", "application/vnd.in-toto+json"),
            _referrer("3", "application/spdx+json"),
        ]
    }


def _signature_response(character: str) -> dict[str, object]:
    return {"referrers": [_referrer(character, "application/vnd.dev.cosign.simplesigning.v1+json")]}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _referrer_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "referrers"
    directory.mkdir()
    for subject in ("core", "media"):
        _write_json(directory / f"{subject}-subject-referrers.json", _subject_response())
        _write_json(
            directory / f"{subject}-{'2' * 64}-signature-referrers.json",
            _signature_response("4"),
        )
        _write_json(
            directory / f"{subject}-{'3' * 64}-signature-referrers.json",
            _signature_response("5"),
        )
    return directory


def test_active_openclaw_contract_blocks_release_until_boyle_bundle_lands() -> None:
    contract = provenance.load_contract(ACTIVE_CONTRACT)

    assert contract["release"]["ready"] is False
    assert contract["bundle"]["interfaces"] == {
        "build": "infra/openclaw/build-image.sh",
        "verify_evidence": "infra/openclaw/verify-bundle-evidence.sh",
        "promote": "infra/openclaw/promote-bundle.sh",
    }
    with pytest.raises(provenance.ContractError, match="Boyle-owned"):
        provenance.require_release_ready(contract)


def test_contract_rejects_mcp_repository_or_mutable_trivy_database() -> None:
    contract = json.loads(ACTIVE_CONTRACT.read_text(encoding="utf-8"))
    contract["bundle"]["subjects"][0]["release_repository"] = "teamagent-mcp"
    with pytest.raises(provenance.ContractError, match="MCP repository"):
        provenance.validate_contract(contract)

    contract = json.loads(ACTIVE_CONTRACT.read_text(encoding="utf-8"))
    contract["tooling"]["trivy_db_repository"] = "aquasec/trivy-db:latest"
    with pytest.raises(provenance.ContractError, match="Trivy DB repositories"):
        provenance.validate_contract(contract)


def test_signed_source_manifest_binds_full_commit_tree_and_contract(tmp_path: Path) -> None:
    contract = _ready_contract(tmp_path)
    repo, commit = _source_repo(tmp_path)
    manifest = tmp_path / "source-manifest.json"
    provenance.write_source_manifest(repo, commit, contract, manifest)
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()

    provenance.verify_source_manifest(repo, manifest, contract, commit, manifest_sha256)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    assert value["source"]["commit"] == commit
    assert value["source"]["file_count"] == 2
    assert value["source"]["executable_paths"] == ["infra/openclaw/fixture.sh"]

    (repo / "README.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(provenance.ContractError, match="does not exactly match"):
        provenance.verify_source_manifest(repo, manifest, contract, commit, manifest_sha256)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["referrers"].pop(),
            "missing sbom referrer",
        ),
        (
            lambda value: value["referrers"].append(_referrer("9", "application/vnd.unknown+json")),
            "unknown OCI referrer",
        ),
        (
            lambda value: value.update({"nextToken": "truncated"}),
            "truncated",
        ),
        (
            lambda value: value["referrers"][0].update({"artifactStatus": "ARCHIVED"}),
            "must be ACTIVE",
        ),
    ],
)
def test_subject_referrer_gate_fails_closed(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    contract = _ready_contract(tmp_path)
    value = _subject_response()
    mutation(value)
    response = tmp_path / "response.json"
    _write_json(response, value)

    with pytest.raises(provenance.ContractError, match=message):
        provenance.verify_subject_referrers(response, contract)


def test_subject_requires_child_signature_and_signed_sbom_and_provenance(tmp_path: Path) -> None:
    contract = _ready_contract(tmp_path)
    response = tmp_path / "subject.json"
    _write_json(response, _subject_response())

    assert provenance.verify_subject_referrers(response, contract) == [
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
    ]

    signatures = tmp_path / "signatures.json"
    _write_json(signatures, _signature_response("4"))
    provenance.verify_signature_referrers(signatures, contract)


def test_openclaw_candidate_must_be_single_linux_arm64_manifest_not_index(
    tmp_path: Path,
) -> None:
    contract = _ready_contract(tmp_path)
    commit = "c" * 40
    config_value = {
        "architecture": "arm64",
        "os": "linux",
        "config": {"Labels": {"org.opencontainers.image.revision": commit}},
    }
    config_raw = json.dumps(config_value, sort_keys=True, separators=(",", ":")).encode()
    config_path = tmp_path / "config.json"
    config_path.write_bytes(config_raw)
    config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": len(config_raw),
            "digest": config_digest,
        },
        "layers": [],
    }
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    image_digest = "sha256:" + hashlib.sha256(manifest_raw.encode()).hexdigest()
    response_value = {
        "images": [
            {
                "registryId": "718959508629",
                "repositoryName": "teamagent-openclaw-quarantine",
                "imageId": {"imageDigest": image_digest},
                "imageManifest": manifest_raw,
                "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json",
            }
        ],
        "failures": [],
    }
    response = tmp_path / "batch.json"
    _write_json(response, response_value)

    assert (
        provenance.arm64_config_digest(
            response,
            contract,
            image_digest,
            "teamagent-openclaw-quarantine",
            "718959508629",
        )
        == config_digest
    )
    provenance.verify_arm64_config(config_path, config_digest, commit)

    response_value["images"][0]["imageManifestMediaType"] = (
        "application/vnd.oci.image.index.v1+json"
    )
    _write_json(response, response_value)
    with pytest.raises(provenance.ContractError, match="not a single OCI image manifest"):
        provenance.arm64_config_digest(
            response,
            contract,
            image_digest,
            "teamagent-openclaw-quarantine",
            "718959508629",
        )

    invalid_manifest = deepcopy(manifest)
    invalid_manifest["config"]["mediaType"] = "application/vnd.docker.container.image.v1+json"
    invalid_manifest_raw = json.dumps(invalid_manifest, sort_keys=True, separators=(",", ":"))
    invalid_image_digest = "sha256:" + hashlib.sha256(invalid_manifest_raw.encode()).hexdigest()
    response_value["images"][0].update(
        {
            "imageId": {"imageDigest": invalid_image_digest},
            "imageManifest": invalid_manifest_raw,
            "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json",
        }
    )
    _write_json(response, response_value)
    with pytest.raises(provenance.ContractError, match="config descriptor media type"):
        provenance.arm64_config_digest(
            response,
            contract,
            invalid_image_digest,
            "teamagent-openclaw-quarantine",
            "718959508629",
        )

    config_value["architecture"] = "amd64"
    changed_raw = json.dumps(config_value, sort_keys=True, separators=(",", ":")).encode()
    config_path.write_bytes(changed_raw)
    changed_digest = "sha256:" + hashlib.sha256(changed_raw).hexdigest()
    with pytest.raises(provenance.ContractError, match="not linux/arm64"):
        provenance.verify_arm64_config(config_path, changed_digest, commit)


def test_bundle_receipt_requires_exact_core_media_arm64_digests_and_c_h_zero(
    tmp_path: Path,
) -> None:
    contract = _ready_contract(tmp_path)
    commit = "a" * 40
    contract_sha256 = provenance.contract_sha256(contract)
    receipt = {
        "schema_version": 1,
        "source_commit": commit,
        "bundle_contract_sha256": contract_sha256,
        "subjects": [],
    }
    for index, (name, quarantine, release) in enumerate(
        (
            ("core", "teamagent-openclaw-quarantine", "teamagent-openclaw"),
            (
                "media",
                "teamagent-openclaw-media-quarantine",
                "teamagent-openclaw-media",
            ),
        ),
        start=6,
    ):
        receipt["subjects"].append(
            {
                "name": name,
                "quarantine_repository": quarantine,
                "release_repository": release,
                "tag": f"git-{commit[:12]}-{name}",
                "index_digest": "sha256:" + str(index) * 64,
                "arm64_digest": "sha256:" + str(index + 1) * 64,
                "scan": {"critical": 0, "high": 0},
            }
        )
    path = tmp_path / "receipt.json"
    _write_json(path, receipt)

    subjects = provenance.verify_bundle_receipt(path, contract, commit, contract_sha256)
    assert [subject["name"] for subject in subjects] == ["core", "media"]

    receipt["subjects"][1]["scan"]["high"] = 1
    _write_json(path, receipt)
    with pytest.raises(provenance.ContractError, match="scan is not C/H0"):
        provenance.verify_bundle_receipt(path, contract, commit, contract_sha256)


def test_release_evidence_is_exact_content_addressed_and_digest_equal(tmp_path: Path) -> None:
    contract = _ready_contract(tmp_path)
    repo, commit = _source_repo(tmp_path)
    source_manifest = tmp_path / "source-manifest.json"
    provenance.write_source_manifest(repo, commit, contract, source_manifest)
    manifest_sha256 = hashlib.sha256(source_manifest.read_bytes()).hexdigest()
    manifest_key = f"source-manifests/{commit}/{manifest_sha256}.json"
    referrers = _referrer_directory(tmp_path)
    evidence = tmp_path / "release-evidence.json"
    build_id = "teamagent-dev-openclaw-provenance-builder:11111111-2222-3333-4444-555555555555"
    core_digest = "sha256:" + "a" * 64
    media_digest = "sha256:" + "b" * 64

    provenance.create_release_evidence(
        contract,
        source_manifest,
        manifest_key,
        "source-version-1",
        f"{manifest_key}.sig",
        "signature-version-1",
        build_id,
        commit,
        [f"core={core_digest}={core_digest}", f"media={media_digest}={media_digest}"],
        referrers,
        evidence,
    )
    evidence_sha256 = hashlib.sha256(evidence.read_bytes()).hexdigest()
    provenance.verify_release_evidence(
        evidence,
        contract,
        build_id,
        commit,
        evidence_sha256,
    )
    value = json.loads(evidence.read_text(encoding="utf-8"))
    assert value["source"]["manifest_key"] == manifest_key
    assert all(
        subject["release_digest"] == subject["quarantine_digest"] for subject in value["subjects"]
    )
    assert all(
        any(referrer["signatures"] for referrer in subject["referrers"])
        for subject in value["subjects"]
    )

    tampered = deepcopy(value)
    tampered["subjects"][0]["release_digest"] = "sha256:" + "f" * 64
    with pytest.raises(provenance.ContractError, match="differs from quarantine"):
        provenance.validate_release_evidence(tampered, contract)
