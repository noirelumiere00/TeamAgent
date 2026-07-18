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
ACTIVE_CONTRACT_PATH = ROOT / "infra" / "codebuild" / "teamagent_runtime_contract.json"
TEAMAGENT_DOCKERFILE = ROOT / "infra" / "docker" / "Dockerfile.teamagent-mcp"
READY_CONTRACT_PATH = (
    ROOT / "tests" / "codebuild" / "fixtures" / "teamagent_runtime_contract.ready.json"
)
APP_HTML_VERSION_ID = "app-version-fixture"
APP_HTML_SHA256 = hashlib.sha256(b"versioned app fixture\n").hexdigest()


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("teamagent_source_provenance", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provenance = _load_module()


def _ready_contract() -> dict[str, object]:
    return json.loads(READY_CONTRACT_PATH.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_archive(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "fixture-branch")
    _git(repo, "config", "user.name", "CodeBuild Test")
    _git(repo, "config", "user.email", "codebuild-test@example.invalid")
    (repo / "README.md").write_text("tracked source\n", encoding="utf-8")
    script = repo / "bin" / "nested" / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\necho verified\n", encoding="utf-8")
    os.chmod(script, 0o755)
    contract_path = repo / provenance.RUNTIME_CONTRACT_PATH
    contract_path.parent.mkdir(parents=True)
    contract_path.write_bytes(READY_CONTRACT_PATH.read_bytes())
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "branch", "--show-current")
    contract_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()

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
    return extracted, commit, branch, contract_sha256


def _synthetic_dockerfile(contract: dict[str, object]) -> str:
    entries = contract["receipt"]["entries"]
    fixed_args = "\n".join(f"ARG {entry['build_arg']}={entry['value']}" for entry in entries)
    redeclared_args = "\n".join(f"ARG {entry['build_arg']}" for entry in entries)
    uses = "\n".join(f"RUN {use}" for entry in entries for use in entry["dockerfile_uses"])
    labels = [f'{entry["oci_label"]}="${{{entry["build_arg"]}}}"' for entry in entries]
    labels.extend(
        [
            'io.teamagent.build.runtime-contract-sha256="${RUNTIME_CONTRACT_SHA256}"',
            'io.teamagent.build.runtime-receipt="${RUNTIME_RECEIPT_B64}"',
            'io.teamagent.build.runtime-receipt-sha256="${RUNTIME_RECEIPT_SHA256}"',
        ]
    )
    label_instruction = "LABEL " + " \\\n+      ".join(labels)
    return f"""{fixed_args}
FROM cgr.dev/chainguard/python@${{NODE_IMAGE_DIGEST}} AS builder
FROM cgr.dev/chainguard/wolfi-base@${{WOLFI_RUNTIME_IMAGE_DIGEST}} AS runtime
{redeclared_args}
ARG RUNTIME_CONTRACT_SHA256
ARG RUNTIME_RECEIPT_B64
ARG RUNTIME_RECEIPT_SHA256
COPY --from=builder /opt/app /opt/app
{uses}
RUN python3 - <<'PY'
import base64
import hashlib
receipt = base64.b64decode(RUNTIME_RECEIPT_B64)
assert hashlib.sha256(receipt).hexdigest() == RUNTIME_RECEIPT_SHA256
PY
{label_instruction}
"""


def _oci_fixture(
    tmp_path: Path,
    commit: str,
    *,
    profile: str = "true",
    mutate_labels: dict[str, str] | None = None,
) -> tuple[Path, str, Path, str]:
    contract = _ready_contract()
    contract_sha256 = hashlib.sha256(READY_CONTRACT_PATH.read_bytes()).hexdigest()
    labels = {
        "org.opencontainers.image.revision": commit,
        provenance.SCRAPE_TOOLS_LABEL: profile,
        provenance.APP_HTML_VERSION_ID_LABEL: APP_HTML_VERSION_ID,
        provenance.APP_HTML_SHA256_LABEL: APP_HTML_SHA256,
        **provenance.runtime_expected_labels(contract, contract_sha256),
    }
    labels.update(mutate_labels or {})
    config = {
        "architecture": "arm64",
        "os": "linux",
        "config": {"Labels": labels},
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


def test_active_wolfi_contract_is_fail_closed_until_missing_evidence_lands() -> None:
    contract = provenance.load_runtime_contract(ACTIVE_CONTRACT_PATH)

    assert contract["release"]["ready"] is False
    with pytest.raises(
        provenance.ProvenanceError,
        match=r"Debian 13.*CRITICAL=4/HIGH=49",
    ):
        provenance.require_release_ready(contract)
    assert all(
        "playwright" not in entry["key"] and "archive" not in entry["key"]
        for entry in contract["receipt"]["entries"]
    )


def test_ready_contract_requires_exact_wolfi_evidence_and_node_image_digest_binding() -> None:
    contract = provenance.load_runtime_contract(READY_CONTRACT_PATH)
    provenance.require_release_ready(contract)
    arguments = provenance.runtime_build_arguments(
        contract,
        provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
    )

    assert arguments["NODE_IMAGE_DIGEST"].startswith("sha256:")
    assert set(arguments) == {entry["build_arg"] for entry in contract["receipt"]["entries"]} | {
        provenance.RUNTIME_CONTRACT_SHA256_ARG,
        provenance.RUNTIME_RECEIPT_B64_ARG,
        provenance.RUNTIME_RECEIPT_SHA256_ARG,
    }


def test_release_ready_rejects_missing_or_rebound_builder_digest() -> None:
    contract = _ready_contract()
    contract["receipt"]["entries"] = [
        entry
        for entry in contract["receipt"]["entries"]
        if entry["key"] != "base.builder.arm64.digest"
    ]
    with pytest.raises(provenance.ProvenanceError, match=r"base\.builder\.arm64\.digest"):
        provenance.validate_runtime_contract(contract)

    contract = _ready_contract()
    contract["receipt"]["entries"][0]["build_arg"] = "MUTABLE_BUILDER_TAG"
    contract["receipt"]["entries"][0]["dockerfile_uses"] = [
        "cgr.dev/chainguard/python@${MUTABLE_BUILDER_TAG}"
    ]
    with pytest.raises(provenance.ProvenanceError, match="bindings are invalid"):
        provenance.validate_runtime_contract(contract)


def test_synthetic_future_dockerfile_implements_every_contract_arg_label_and_use(
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(_synthetic_dockerfile(_ready_contract()), encoding="utf-8")

    provenance.verify_dockerfile_contract(READY_CONTRACT_PATH, dockerfile)


@pytest.mark.parametrize("external_image", ["ubuntu", "alpine"])
def test_dockerfile_contract_treats_bare_from_names_as_external_images(
    tmp_path: Path,
    external_image: str,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        f"FROM {external_image} AS untrusted\n" + _synthetic_dockerfile(_ready_contract()),
        encoding="utf-8",
    )

    with pytest.raises(
        provenance.ProvenanceError,
        match=rf"external image is not digest pinned: {external_image}",
    ):
        provenance.verify_dockerfile_contract(READY_CONTRACT_PATH, dockerfile)


def test_dockerfile_contract_rejects_forward_stage_alias_as_unpinned_external(
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM future AS premature\n"
        + _synthetic_dockerfile(_ready_contract())
        + "\nFROM cgr.dev/chainguard/python@${NODE_IMAGE_DIGEST} AS future\n",
        encoding="utf-8",
    )

    with pytest.raises(
        provenance.ProvenanceError,
        match="external image is not digest pinned: future",
    ):
        provenance.verify_dockerfile_contract(READY_CONTRACT_PATH, dockerfile)


def test_checked_in_dockerfile_must_implement_active_contract_before_activation() -> None:
    """Turn the cross-branch schema into a hard merge gate once it is activated."""

    contract = provenance.load_runtime_contract(ACTIVE_CONTRACT_PATH)
    checked_in_dockerfile = TEAMAGENT_DOCKERFILE.read_text(encoding="utf-8")
    assert checked_in_dockerfile
    if not contract["release"]["ready"]:
        with pytest.raises(provenance.ProvenanceError, match="release is blocked"):
            provenance.require_release_ready(contract)
        return

    provenance.verify_dockerfile_contract(ACTIVE_CONTRACT_PATH, TEAMAGENT_DOCKERFILE)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda body: body.replace("@${NODE_IMAGE_DIGEST}", ":latest-dev", 1), "not digest pinned"),
        (
            lambda body: body.replace(
                'io.teamagent.build.binary-node-sha256="${NODE_BINARY_SHA256}"',
                'io.teamagent.build.binary-node-sha256="wrong"',
            ),
            "does not bind",
        ),
        (
            lambda body: body.replace(
                'echo "${PYTHON_BINARY_SHA256}  /usr/bin/python3.14" | sha256sum -c -',
                "true",
            ),
            "does not implement",
        ),
    ],
)
def test_dockerfile_contract_rejects_mutable_or_incomplete_implementation(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(mutation(_synthetic_dockerfile(_ready_contract())), encoding="utf-8")

    with pytest.raises(provenance.ProvenanceError, match=message):
        provenance.verify_dockerfile_contract(READY_CONTRACT_PATH, dockerfile)


def test_git_archive_source_and_manifest_verify_nested_tree_exactly(tmp_path: Path) -> None:
    extracted, commit, branch, contract_sha256 = _source_archive(tmp_path)

    provenance.verify_source(
        extracted,
        extracted / provenance.MANIFEST_NAME,
        commit,
        branch,
        "true",
        APP_HTML_VERSION_ID,
        APP_HTML_SHA256,
        contract_sha256,
    )


def test_source_byte_or_runtime_contract_tampering_is_rejected(tmp_path: Path) -> None:
    extracted, commit, branch, contract_sha256 = _source_archive(tmp_path)
    (extracted / "bin" / "nested" / "run.sh").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(provenance.ProvenanceError, match="source tree mismatch"):
        provenance.verify_source(
            extracted,
            extracted / provenance.MANIFEST_NAME,
            commit,
            branch,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            contract_sha256,
        )


@pytest.mark.parametrize(
    ("commit", "branch", "profile", "version_id", "sha256", "contract_hash", "message"),
    [
        ("0" * 40, None, None, None, None, None, "GIT_COMMIT mismatch"),
        (None, "other", None, None, None, None, "GIT_BRANCH mismatch"),
        (None, None, "false", None, None, None, "WITH_SCRAPE_TOOLS mismatch"),
        (None, None, None, "other-version", None, None, "APP_HTML_VERSION_ID mismatch"),
        (None, None, None, None, "f" * 64, None, "APP_HTML_SHA256 mismatch"),
        (None, None, None, None, None, "f" * 64, "RUNTIME_CONTRACT_SHA256 mismatch"),
    ],
)
def test_source_environment_must_match_manifest(
    tmp_path: Path,
    commit: str | None,
    branch: str | None,
    profile: str | None,
    version_id: str | None,
    sha256: str | None,
    contract_hash: str | None,
    message: str,
) -> None:
    extracted, actual_commit, actual_branch, actual_contract_hash = _source_archive(tmp_path)
    with pytest.raises(provenance.ProvenanceError, match=message):
        provenance.verify_source(
            extracted,
            extracted / provenance.MANIFEST_NAME,
            commit or actual_commit,
            branch or actual_branch,
            profile or "true",
            version_id or APP_HTML_VERSION_ID,
            sha256 or APP_HTML_SHA256,
            contract_hash or actual_contract_hash,
        )


def test_remote_oci_exact_runtime_receipt_and_labels_are_digest_bound(tmp_path: Path) -> None:
    commit = "a" * 40
    config, config_digest, response, image_digest = _oci_fixture(tmp_path, commit)

    assert provenance.ecr_config_digest(response, image_digest) == config_digest
    provenance.verify_oci_revision(
        config,
        config_digest,
        commit,
        "true",
        APP_HTML_VERSION_ID,
        APP_HTML_SHA256,
        READY_CONTRACT_PATH,
        provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        ({provenance.SCRAPE_TOOLS_LABEL: "false"}, "with-scrape-tools mismatch"),
        ({provenance.APP_HTML_SHA256_LABEL: "f" * 64}, "app-html-sha256 mismatch"),
        ({"io.teamagent.build.unexpected": "attacker"}, "allowlist mismatch"),
        ({"io.teamagent.build.binary-node-sha256": "0" * 64}, "binary-node-sha256 mismatch"),
    ],
)
def test_remote_oci_hostile_or_stale_labels_fail_closed(
    tmp_path: Path,
    mutate: dict[str, str],
    message: str,
) -> None:
    commit = "b" * 40
    config, config_digest, _response, _image_digest = _oci_fixture(
        tmp_path,
        commit,
        mutate_labels=mutate,
    )

    with pytest.raises(provenance.ProvenanceError, match=message):
        provenance.verify_oci_revision(
            config,
            config_digest,
            commit,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            READY_CONTRACT_PATH,
            provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
        )


def test_remote_oci_config_bytes_cannot_change_after_digest_resolution(tmp_path: Path) -> None:
    commit = "c" * 40
    config, config_digest, _response, _image_digest = _oci_fixture(tmp_path, commit)
    config.write_text("{}", encoding="utf-8")

    with pytest.raises(provenance.ProvenanceError, match="OCI config digest mismatch"):
        provenance.verify_oci_revision(
            config,
            config_digest,
            commit,
            "true",
            APP_HTML_VERSION_ID,
            APP_HTML_SHA256,
            READY_CONTRACT_PATH,
            provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
        )
