from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "source_provenance.py"
APP_HTML_VERSION_ID = "app-version-fixture"
APP_HTML_SHA256 = hashlib.sha256(b"versioned app fixture\n").hexdigest()
RUNTIME_CONTRACT = json.loads(
    (ROOT / "infra" / "codebuild" / "teamagent_runtime_contract.json").read_text(encoding="utf-8")
)


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("teamagent_source_provenance", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provenance = _load_module()
RUNTIME_ENV = provenance.runtime_environment(RUNTIME_CONTRACT)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_archive(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "fixture-branch")
    _git(repo, "config", "user.name", "CodeBuild Test")
    _git(repo, "config", "user.email", "codebuild-test@example.invalid")
    (repo / "README.md").write_text("tracked source\n", encoding="utf-8")
    script = repo / "bin" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env bash\necho verified\n", encoding="utf-8")
    os.chmod(script, 0o755)
    runtime_contract = repo / provenance.RUNTIME_CONTRACT_PATH
    runtime_contract.parent.mkdir(parents=True)
    runtime_contract.write_text(
        json.dumps(RUNTIME_CONTRACT, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "branch", "--show-current")

    manifest = tmp_path / provenance.MANIFEST_NAME
    provenance.create_manifest(
        repo,
        commit,
        branch,
        "true",
        APP_HTML_VERSION_ID,
        APP_HTML_SHA256,
        manifest,
    )
    archive = tmp_path / "source.zip"
    _git(
        repo,
        "archive",
        "--format=zip",
        f"--output={archive}",
        f"--add-file={manifest}",
        commit,
    )
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as source_zip:
        source_zip.extractall(extracted)
    return extracted, commit, branch


def test_git_archive_source_and_manifest_verify_exactly(tmp_path: Path) -> None:
    extracted, commit, branch = _source_archive(tmp_path)

    provenance.verify_source(
        extracted,
        extracted / provenance.MANIFEST_NAME,
        commit,
        branch,
        "true",
        APP_HTML_VERSION_ID,
        APP_HTML_SHA256,
        RUNTIME_ENV,
    )


def test_source_byte_tampering_is_rejected(tmp_path: Path) -> None:
    extracted, commit, branch = _source_archive(tmp_path)
    (extracted / "README.md").write_text("different bytes\n", encoding="utf-8")

    with pytest.raises(provenance.ProvenanceError, match="source tree mismatch"):
        provenance.verify_source(
            extracted,
            extracted / provenance.MANIFEST_NAME,
            commit,
            branch,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            RUNTIME_ENV,
        )


@pytest.mark.parametrize(
    ("change_commit", "branch", "with_scrape_tools", "message"),
    [
        (True, "fixture-branch", "true", "GIT_COMMIT mismatch"),
        (False, "other-branch", "true", "GIT_BRANCH mismatch"),
        (False, "fixture-branch", "false", "WITH_SCRAPE_TOOLS mismatch"),
    ],
)
def test_environment_must_match_manifest(
    tmp_path: Path,
    change_commit: bool,
    branch: str,
    with_scrape_tools: str,
    message: str,
) -> None:
    extracted, commit, expected_branch = _source_archive(tmp_path)
    different_commit = "0" * 40 if commit != "0" * 40 else "1" * 40
    expected_commit = different_commit if change_commit else commit

    with pytest.raises(provenance.ProvenanceError, match=message):
        provenance.verify_source(
            extracted,
            extracted / provenance.MANIFEST_NAME,
            expected_commit,
            branch or expected_branch,
            with_scrape_tools,
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            RUNTIME_ENV,
        )


@pytest.mark.parametrize(
    ("version_id", "sha256", "message"),
    [
        ("different-version", APP_HTML_SHA256, "APP_HTML_VERSION_ID mismatch"),
        (APP_HTML_VERSION_ID, "f" * 64, "APP_HTML_SHA256 mismatch"),
    ],
)
def test_app_html_environment_must_match_manifest(
    tmp_path: Path,
    version_id: str,
    sha256: str,
    message: str,
) -> None:
    extracted, commit, branch = _source_archive(tmp_path)

    with pytest.raises(provenance.ProvenanceError, match=message):
        provenance.verify_source(
            extracted,
            extracted / provenance.MANIFEST_NAME,
            commit,
            branch,
            "true",
            version_id,
            sha256,
            RUNTIME_ENV,
        )


def test_runtime_environment_must_match_committed_contract(tmp_path: Path) -> None:
    extracted, commit, branch = _source_archive(tmp_path)
    changed_runtime = dict(RUNTIME_ENV)
    changed_runtime["NODE_VERSION"] = "99.99.99"

    with pytest.raises(provenance.ProvenanceError, match="NODE_VERSION mismatch"):
        provenance.verify_source(
            extracted,
            extracted / provenance.MANIFEST_NAME,
            commit,
            branch,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            changed_runtime,
        )


def _oci_fixture(
    tmp_path: Path,
    commit: str,
    profile: str,
    app_html_sha256: str = APP_HTML_SHA256,
    app_html_version_id: str = APP_HTML_VERSION_ID,
) -> tuple[Path, str, Path, str]:
    runtime_labels = {
        label: RUNTIME_ENV[environment_name]
        for environment_name, _section, _field, label in provenance.RUNTIME_FIELDS
    }
    config = {
        "architecture": "arm64",
        "os": "linux",
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": commit,
                provenance.SCRAPE_TOOLS_LABEL: profile,
                provenance.APP_HTML_VERSION_ID_LABEL: app_html_version_id,
                provenance.APP_HTML_SHA256_LABEL: app_html_sha256,
                **runtime_labels,
            }
        },
    }
    config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_path = tmp_path / "config.json"
    config_path.write_bytes(config_bytes)
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
    response = {
        "images": [{"imageId": {"imageDigest": image_digest}, "imageManifest": manifest_raw}],
        "failures": [],
    }
    response_path = tmp_path / "batch.json"
    response_path.write_text(json.dumps(response), encoding="utf-8")
    return config_path, config_digest, response_path, image_digest


def test_remote_oci_revision_and_scrape_profile_are_digest_bound(tmp_path: Path) -> None:
    commit = "a" * 40
    config_path, config_digest, response_path, image_digest = _oci_fixture(tmp_path, commit, "true")

    assert provenance.ecr_config_digest(response_path, image_digest) == config_digest
    provenance.verify_oci_revision(
        config_path,
        config_digest,
        commit,
        "true",
        APP_HTML_VERSION_ID,
        APP_HTML_SHA256,
        RUNTIME_ENV,
    )


def test_remote_oci_scrape_profile_mismatch_is_separate_failure(tmp_path: Path) -> None:
    commit = "b" * 40
    config_path, config_digest, _response_path, _image_digest = _oci_fixture(
        tmp_path, commit, "false"
    )

    with pytest.raises(provenance.ProvenanceError, match="with-scrape-tools mismatch"):
        provenance.verify_oci_revision(
            config_path,
            config_digest,
            commit,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            RUNTIME_ENV,
        )


def test_remote_oci_app_html_sha_mismatch_fails_closed(tmp_path: Path) -> None:
    commit = "d" * 40
    config_path, config_digest, _response_path, _image_digest = _oci_fixture(
        tmp_path,
        commit,
        "true",
        "e" * 64,
    )

    with pytest.raises(provenance.ProvenanceError, match="app-html-sha256 mismatch"):
        provenance.verify_oci_revision(
            config_path,
            config_digest,
            commit,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            RUNTIME_ENV,
        )


def test_remote_oci_app_html_version_mismatch_fails_closed(tmp_path: Path) -> None:
    commit = "e" * 40
    config_path, config_digest, _response_path, _image_digest = _oci_fixture(
        tmp_path,
        commit,
        "true",
        app_html_version_id="different-app-version",
    )

    with pytest.raises(provenance.ProvenanceError, match="app-html-version-id mismatch"):
        provenance.verify_oci_revision(
            config_path,
            config_digest,
            commit,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            RUNTIME_ENV,
        )


def test_remote_oci_config_bytes_cannot_change_after_digest_resolution(tmp_path: Path) -> None:
    commit = "c" * 40
    config_path, config_digest, _response_path, _image_digest = _oci_fixture(
        tmp_path, commit, "true"
    )
    config_path.write_text("{}", encoding="utf-8")

    with pytest.raises(provenance.ProvenanceError, match="OCI config digest mismatch"):
        provenance.verify_oci_revision(
            config_path,
            config_digest,
            commit,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            RUNTIME_ENV,
        )


def test_remote_oci_platform_mismatch_fails_closed(tmp_path: Path) -> None:
    commit = "f" * 40
    config_path, _config_digest, _response_path, _image_digest = _oci_fixture(
        tmp_path, commit, "true"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["architecture"] = "amd64"
    config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_path.write_bytes(config_bytes)
    changed_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()

    with pytest.raises(provenance.ProvenanceError, match="OCI platform mismatch"):
        provenance.verify_oci_revision(
            config_path,
            changed_digest,
            commit,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            RUNTIME_ENV,
        )
