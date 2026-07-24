from __future__ import annotations

import base64
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
COMMIT = "1" * 40
OTHER_COMMIT = "2" * 40


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
    label_instruction = "LABEL " + " \\\n      ".join(labels)
    return f"""{fixed_args}
FROM ghcr.io/astral-sh/uv@${{UV_ARM64_DIGEST}} AS uv
FROM cgr.dev/chainguard/python@${{PYTHON_BUILDER_ARM64_DIGEST}} AS builder
{redeclared_args}
ARG RUNTIME_CONTRACT_SHA256
ARG RUNTIME_RECEIPT_B64
ARG RUNTIME_RECEIPT_SHA256
COPY infra/codebuild/teamagent_runtime_contract.json /tmp/teamagent_runtime_contract.json
{uses}
RUN python -c "import base64, hashlib, json, os, pathlib; \\
raw = pathlib.Path('/tmp/teamagent_runtime_contract.json').read_bytes(); \\
assert hashlib.sha256(raw).hexdigest() == os.environ['RUNTIME_CONTRACT_SHA256']; \\
contract = json.loads(raw); \\
entries = contract['receipt']['entries']; \\
expected_values = {{entry['key']: entry['value'] for entry in entries}}; \\
expected_receipt = {{'schema_version': contract['receipt']['schema_version'], 'subject': contract['receipt']['subject'], 'values': expected_values}}; \\
encoded = os.environ['RUNTIME_RECEIPT_B64']; \\
receipt = base64.b64decode(encoded, validate=True); \\
assert base64.b64encode(receipt).decode('ascii') == encoded; \\
assert hashlib.sha256(receipt).hexdigest() == os.environ['RUNTIME_RECEIPT_SHA256']; \\
assert receipt == json.dumps(expected_receipt, sort_keys=True, separators=(',', ':')).encode('utf-8'); \\
parsed_receipt = json.loads(receipt); \\
assert parsed_receipt['subject'] == 'core'; \\
assert parsed_receipt['values'] == expected_values; \\
assert all(os.environ[entry['build_arg']] == entry['value'] for entry in entries)"
FROM cgr.dev/chainguard/python@${{PYTHON_RUNTIME_ARM64_DIGEST}} AS final
{redeclared_args}
ARG RUNTIME_CONTRACT_SHA256
ARG RUNTIME_RECEIPT_B64
ARG RUNTIME_RECEIPT_SHA256
COPY --from=builder /opt/app /opt/app
{label_instruction}
"""


def _oci_fixture(
    tmp_path: Path,
    commit: str,
    *,
    mutate_labels: dict[str, str] | None = None,
) -> tuple[Path, str, Path, str]:
    contract = _ready_contract()
    contract_sha256 = hashlib.sha256(READY_CONTRACT_PATH.read_bytes()).hexdigest()
    labels = {
        "org.opencontainers.image.revision": commit,
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


def test_active_core_contract_is_schema_aligned_but_release_remains_fail_closed() -> None:
    contract = provenance.load_runtime_contract(ACTIVE_CONTRACT_PATH)

    assert contract["release"]["ready"] is False
    assert contract["approval_record"] is None
    assert contract["receipt"]["subject"] == "core"
    assert {entry["key"] for entry in contract["receipt"]["entries"]} == {
        "artifact.torch.arm64-wheel.sha256",
        "base.builder.arm64.digest",
        "base.runtime.arm64.digest",
        "base.uv.arm64.digest",
        "binary.python.sha256",
        "binary.uv.sha256",
        "component.python.version",
        "component.torch.version",
        "component.uv.version",
        "model.e5.revision",
    }
    with pytest.raises(provenance.ProvenanceError, match="release is blocked"):
        provenance.require_release_ready(contract)
    assert all(
        not entry["build_arg"].startswith("WOLFI_")
        and entry["build_arg"] != "NODE_IMAGE_DIGEST"
        for entry in contract["receipt"]["entries"]
    )


def test_ready_contract_requires_exact_core_evidence_and_receipt_subject() -> None:
    contract = provenance.load_runtime_contract(READY_CONTRACT_PATH)
    provenance.require_release_ready(contract)
    arguments = provenance.runtime_build_arguments(
        contract,
        provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
    )

    assert arguments["PYTHON_BUILDER_ARM64_DIGEST"].startswith("sha256:")
    receipt = json.loads(provenance.runtime_receipt_bytes(contract))
    assert receipt["schema_version"] == 2
    assert receipt["subject"] == "core"
    assert set(arguments) == {entry["build_arg"] for entry in contract["receipt"]["entries"]} | {
        provenance.RUNTIME_CONTRACT_SHA256_ARG,
        provenance.RUNTIME_RECEIPT_B64_ARG,
        provenance.RUNTIME_RECEIPT_SHA256_ARG,
    }


def test_runtime_release_ready_cli_binds_approval_to_expected_commit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        provenance.main(
            [
                "assert-release-ready",
                "--contract",
                str(READY_CONTRACT_PATH),
                "--expected-commit",
                COMMIT,
            ]
        )
        == 0
    )
    assert (
        provenance.main(
            [
                "assert-release-ready",
                "--contract",
                str(READY_CONTRACT_PATH),
                "--expected-commit",
                OTHER_COMMIT,
            ]
        )
        == 1
    )
    assert (
        "approval source_commit does not bind the expected build commit"
        in capsys.readouterr().err
    )


def test_runtime_release_ready_cli_keeps_blocked_contract_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        provenance.main(
            [
                "assert-release-ready",
                "--contract",
                str(ACTIVE_CONTRACT_PATH),
                "--expected-commit",
                COMMIT,
            ]
        )
        == 1
    )
    assert "release is blocked" in capsys.readouterr().err


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
    builder_entry = next(
        entry
        for entry in contract["receipt"]["entries"]
        if entry["key"] == "base.builder.arm64.digest"
    )
    builder_entry["build_arg"] = "MUTABLE_BUILDER_TAG"
    builder_entry["dockerfile_uses"] = [
        "cgr.dev/chainguard/python@${MUTABLE_BUILDER_TAG}"
    ]
    with pytest.raises(provenance.ProvenanceError, match="bindings are invalid"):
        provenance.validate_runtime_contract(contract)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda contract: contract.update(schema_version=3), "unsupported runtime contract schema"),
        (
            lambda contract: contract["receipt"].update(schema_version=1),
            "receipt schema",
        ),
        (
            lambda contract: contract["receipt"].update(subject="media"),
            r"receipt\.subject must be 'core'",
        ),
        (
            lambda contract: contract.update(approval_record=None),
            "approval_record must be an object",
        ),
        (
            lambda contract: contract["approval_record"].update(source_commit="A" * 40),
            "full lowercase Git SHA-1",
        ),
        (
            lambda contract: contract["approval_record"].update(
                approved_at_utc="2026-07-24T00:00:00+00:00"
            ),
            "RFC3339 UTC",
        ),
        (
            lambda contract: contract["approval_record"].update(decision="looks good"),
            "must begin with 'APPROVED: '",
        ),
        (
            lambda contract: contract["approval_record"].update(unexpected="value"),
            "schema mismatch",
        ),
        (
            lambda contract: contract["approval_record"]["observations"].append(
                dict(contract["approval_record"]["observations"][0])
            ),
            "duplicate runtime contract approval_record observation key",
        ),
    ],
)
def test_runtime_contract_schema_and_approval_record_are_strict(
    mutation: object,
    message: str,
) -> None:
    contract = _ready_contract()
    mutation(contract)

    with pytest.raises(provenance.ProvenanceError, match=message):
        provenance.validate_runtime_contract(contract)


def test_blocked_contract_rejects_an_approval_record() -> None:
    contract = _ready_contract()
    contract["release"] = {
        "ready": False,
        "blocked_reason": "Schema alignment is complete, but release approval remains intentionally blocked.",
    }

    with pytest.raises(provenance.ProvenanceError, match="approval_record must be null"):
        provenance.validate_runtime_contract(contract)


@pytest.mark.parametrize(
    ("key", "replacement_arg"),
    [
        ("base.builder.arm64.digest", "NODE_IMAGE_DIGEST"),
        ("base.runtime.arm64.digest", "WOLFI_RUNTIME_IMAGE_DIGEST"),
    ],
)
def test_ready_contract_rejects_legacy_wolfi_or_node_digest_bindings(
    key: str,
    replacement_arg: str,
) -> None:
    contract = _ready_contract()
    entry = next(item for item in contract["receipt"]["entries"] if item["key"] == key)
    entry["dockerfile_uses"] = [
        use.replace(entry["build_arg"], replacement_arg)
        for use in entry["dockerfile_uses"]
    ]
    entry["build_arg"] = replacement_arg

    with pytest.raises(provenance.ProvenanceError, match="bindings are invalid"):
        provenance.validate_runtime_contract(contract)


def test_runtime_entry_rejects_legacy_build_label_namespace() -> None:
    contract = _ready_contract()
    contract["receipt"]["entries"][0]["oci_label"] = "io.teamagent.build.legacy-entry"

    with pytest.raises(
        provenance.ProvenanceError,
        match=r"must use io\.teamagent\.contract\.",
    ):
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
        + "\nFROM cgr.dev/chainguard/python@${PYTHON_BUILDER_ARM64_DIGEST} AS future\n",
        encoding="utf-8",
    )

    with pytest.raises(
        provenance.ProvenanceError,
        match="external image is not digest pinned: future",
    ):
        provenance.verify_dockerfile_contract(READY_CONTRACT_PATH, dockerfile)


@pytest.mark.parametrize(
    "instruction",
    [
        "FROM\tubuntu\tAS untrusted",
        "COPY\t--from=ubuntu\t/bin/tool /usr/local/bin/tool",
    ],
)
def test_dockerfile_contract_rejects_unpinned_external_images_with_tab_whitespace(
    tmp_path: Path,
    instruction: str,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        f"{instruction}\n" + _synthetic_dockerfile(_ready_contract()),
        encoding="utf-8",
    )

    with pytest.raises(
        provenance.ProvenanceError,
        match="external image is not digest pinned: ubuntu",
    ):
        provenance.verify_dockerfile_contract(READY_CONTRACT_PATH, dockerfile)


def test_dockerfile_contract_accepts_a_declared_numeric_copy_stage(
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        _synthetic_dockerfile(_ready_contract()).replace(
            "COPY --from=builder",
            "COPY\t--from=0",
        ),
        encoding="utf-8",
    )

    provenance.verify_dockerfile_contract(READY_CONTRACT_PATH, dockerfile)


@pytest.mark.parametrize("reference", ["2", "01"])
def test_dockerfile_contract_rejects_invalid_numeric_copy_stages(
    tmp_path: Path,
    reference: str,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        _synthetic_dockerfile(_ready_contract()).replace(
            "COPY --from=builder",
            f"COPY --from={reference}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        provenance.ProvenanceError,
        match=rf"external image is not digest pinned: {reference}",
    ):
        provenance.verify_dockerfile_contract(READY_CONTRACT_PATH, dockerfile)


def test_checked_in_dockerfile_implements_active_contract_while_release_is_blocked() -> None:
    contract = provenance.load_runtime_contract(ACTIVE_CONTRACT_PATH)
    assert contract["release"]["ready"] is False
    with pytest.raises(provenance.ProvenanceError, match="release is blocked"):
        provenance.require_release_ready(contract)
    provenance.verify_dockerfile_contract(ACTIVE_CONTRACT_PATH, TEAMAGENT_DOCKERFILE)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda body: body.replace(
                "@${PYTHON_BUILDER_ARM64_DIGEST}",
                ":latest-dev",
                1,
            ),
            "not digest pinned",
        ),
        (
            lambda body: body.replace(
                'io.teamagent.contract.python-binary-sha256="${PYTHON_BINARY_SHA256}"',
                'io.teamagent.contract.python-binary-sha256="wrong"',
            ),
            "does not bind",
        ),
        (
            lambda body: body.replace(
                "printf '%s  %s\\n' \"$PYTHON_BINARY_SHA256\" "
                "/usr/bin/python3.14 | sha256sum -c -",
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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda body: body.replace("ARG RUNTIME_RECEIPT_B64\n", "", 1),
            "receipt ARG must be declared exactly once",
        ),
        (
            lambda body: body.replace(
                "ARG RUNTIME_RECEIPT_SHA256\n",
                "ARG RUNTIME_RECEIPT_SHA256=0\n",
                1,
            ),
            "without a default",
        ),
        (
            lambda body: body.replace(
                "hashlib.sha256(raw).hexdigest() == "
                "os.environ['RUNTIME_CONTRACT_SHA256']",
                "True",
            ),
            "inner contract raw SHA-256",
        ),
        (
            lambda body: body.replace(
                "base64.b64encode(receipt).decode('ascii') == encoded",
                "True",
            ),
            "canonical receipt base64",
        ),
        (
            lambda body: body.replace(
                "parsed_receipt['subject'] == 'core'",
                "True",
            ),
            "core receipt subject",
        ),
        (
            lambda body: body.replace(
                'io.teamagent.build.runtime-receipt="${RUNTIME_RECEIPT_B64}"',
                "",
            ),
            "missing receipt label",
        ),
    ],
)
def test_dockerfile_contract_rejects_missing_or_weakened_receipt_proof(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    original = _synthetic_dockerfile(_ready_contract())
    mutated = mutation(original)
    assert mutated != original
    dockerfile.write_text(mutated, encoding="utf-8")

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
        READY_CONTRACT_PATH,
        provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            {provenance.RUNTIME_CONTRACT_SHA256_LABEL: "f" * 64},
            "runtime-contract-sha256 mismatch",
        ),
        (
            {provenance.RUNTIME_RECEIPT_SHA256_LABEL: "0" * 64},
            "runtime-receipt-sha256 mismatch",
        ),
        (
            {"io.teamagent.contract.python-binary-sha256": "0" * 64},
            "python-binary-sha256 mismatch",
        ),
    ],
)
def test_remote_oci_stale_core_receipt_labels_fail_closed(
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
            READY_CONTRACT_PATH,
            provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
        )


def test_remote_oci_unknown_teamagent_label_is_owned_by_bundle_verifier(
    tmp_path: Path,
) -> None:
    commit = "d" * 40
    config, config_digest, _response, _image_digest = _oci_fixture(
        tmp_path,
        commit,
        mutate_labels={"io.teamagent.build.bundle-owned": "checked-elsewhere"},
    )

    provenance.verify_oci_revision(
        config,
        config_digest,
        commit,
        READY_CONTRACT_PATH,
        provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
    )


def test_remote_oci_receipt_rejects_noncanonical_base64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_expected_labels = provenance.runtime_expected_labels

    def noncanonical_expected_labels(
        contract: dict[str, object],
        contract_sha256: str,
    ) -> dict[str, str]:
        labels = original_expected_labels(contract, contract_sha256)
        encoded = labels[provenance.RUNTIME_RECEIPT_LABEL]
        assert encoded.endswith("Q==")
        labels[provenance.RUNTIME_RECEIPT_LABEL] = encoded[:-3] + "R=="
        return labels

    monkeypatch.setattr(
        provenance,
        "runtime_expected_labels",
        noncanonical_expected_labels,
    )
    commit = "e" * 40
    config, config_digest, _response, _image_digest = _oci_fixture(tmp_path, commit)

    with pytest.raises(provenance.ProvenanceError, match="not canonical base64"):
        provenance.verify_oci_revision(
            config,
            config_digest,
            commit,
            READY_CONTRACT_PATH,
            provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
        )


def test_remote_oci_receipt_rejects_non_core_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_expected_labels = provenance.runtime_expected_labels

    def media_subject_expected_labels(
        contract: dict[str, object],
        contract_sha256: str,
    ) -> dict[str, str]:
        labels = original_expected_labels(contract, contract_sha256)
        receipt = json.loads(
            base64.b64decode(labels[provenance.RUNTIME_RECEIPT_LABEL], validate=True)
        )
        receipt["subject"] = "media"
        receipt_bytes = json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        labels[provenance.RUNTIME_RECEIPT_LABEL] = base64.b64encode(receipt_bytes).decode()
        labels[provenance.RUNTIME_RECEIPT_SHA256_LABEL] = hashlib.sha256(
            receipt_bytes
        ).hexdigest()
        return labels

    monkeypatch.setattr(
        provenance,
        "runtime_expected_labels",
        media_subject_expected_labels,
    )
    commit = "f" * 40
    config, config_digest, _response, _image_digest = _oci_fixture(tmp_path, commit)

    with pytest.raises(provenance.ProvenanceError, match="subject must be 'core'"):
        provenance.verify_oci_revision(
            config,
            config_digest,
            commit,
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
            READY_CONTRACT_PATH,
            provenance.runtime_contract_sha256(READY_CONTRACT_PATH),
        )
