from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "teamagent_bundle_provenance.py"
CONTRACT_PATH = ROOT / "infra" / "codebuild" / "teamagent_core_media_release_contract.json"
RUNTIME_CONTRACT_PATH = ROOT / "infra" / "codebuild" / "teamagent_runtime_contract.json"
DEPLOY_LOG = ROOT / "infra" / "deploy_log.md"
CORE_DOCKERFILE = Path("infra/docker/Dockerfile.teamagent-mcp")
MEDIA_DOCKERFILE = Path("infra/docker/Dockerfile.teamagent-media-worker")
BUILD_CONTEXT_SHA256 = "a" * 64
COMMIT = "1" * 40
OTHER_COMMIT = "2" * 40


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "teamagent_bundle_provenance_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROVENANCE = _load_module()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _deploy_log(record: dict[str, Any]) -> str:
    anchors = " / ".join(str(record[key]) for key in sorted(PROVENANCE.PRODUCTION_KEYS))
    return (
        "## 2026-07-17 /app 本番\n\n"
        f"{anchors}\n"
        f"<!-- PRODUCTION_APP_PROVENANCE={json.dumps(record, separators=(',', ':'))} -->\n"
        "\n---\n"
    )


def _copy_pair(tmp_path: Path) -> tuple[Path, Path]:
    for relative in (
        Path("infra/codebuild/teamagent_runtime_contract.json"),
        Path("infra/codebuild/teamagent_core_media_release_contract.json"),
        CORE_DOCKERFILE,
        MEDIA_DOCKERFILE,
        Path("infra/deploy_log.md"),
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return (
        tmp_path / "infra/codebuild/teamagent_runtime_contract.json",
        tmp_path / "infra/codebuild/teamagent_core_media_release_contract.json",
    )


def _sync_outer_pin(runtime_path: Path, contract_path: Path) -> None:
    contract = _read_json(contract_path)
    contract["source_runtime_contract"]["sha256"] = hashlib.sha256(
        runtime_path.read_bytes()
    ).hexdigest()
    _write_json(contract_path, contract)


def _approval_record(values: dict[str, str]) -> dict[str, Any]:
    return {
        "approved_at_utc": "2026-07-24T00:00:00Z",
        "approved_by": "test-approval-authority",
        "source_commit": COMMIT,
        "observations": [
            {
                "key": key,
                "value": value,
                "observed_at_utc": "2026-07-24T00:00:00Z",
                "source": f"immutable-test-receipt:{key}",
            }
            for key, value in sorted(values.items())
        ],
        "decision": "APPROVED: exact test evidence matched",
    }


def _activate_pair(runtime_path: Path, contract_path: Path) -> None:
    runtime = _read_json(runtime_path)
    runtime_entries = {
        entry["key"]: entry["value"] for entry in runtime["receipt"]["entries"]
    }
    runtime["release"] = {"ready": True, "blocked_reason": ""}
    runtime["approval_record"] = _approval_record(
        {
            "base.builder.arm64.digest": runtime_entries["base.builder.arm64.digest"],
            "binary.python.sha256": runtime_entries["binary.python.sha256"],
        }
    )
    _write_json(runtime_path, runtime)

    contract = _read_json(contract_path)
    contract["source_runtime_contract"]["sha256"] = hashlib.sha256(
        runtime_path.read_bytes()
    ).hexdigest()
    probes = {
        subject["name"]: {
            probe["key"]: probe["sha256"] for probe in subject["binary_probes"]
        }
        for subject in contract["subjects"]
    }
    contract["release"] = {"ready": True, "blocked_reason": ""}
    contract["approval_record"] = _approval_record(
        {
            "core.base.builder.arm64.digest": runtime_entries[
                "base.builder.arm64.digest"
            ],
            "core.binary.python.sha256": runtime_entries["binary.python.sha256"],
            "media.binary.chromium.sha256": probes["media"][
                "binary.chromium.sha256"
            ],
            "media.binary.ffmpeg.sha256": probes["media"]["binary.ffmpeg.sha256"],
            "media.binary.node.sha256": probes["media"]["binary.node.sha256"],
            "media.binary.python.sha256": probes["media"]["binary.python.sha256"],
        }
    )
    _write_json(contract_path, contract)


def _subject(contract: dict[str, Any], name: str) -> dict[str, Any]:
    return next(subject for subject in contract["subjects"] if subject["name"] == name)


def _expected_oci_labels(
    contract: dict[str, Any],
    runtime: dict[str, Any],
    *,
    subject_name: str,
    contract_sha256: str,
) -> dict[str, str]:
    subject = _subject(contract, subject_name)
    production = contract["app_html"]["production"]
    record = {"schema_version": 1, **production}
    fallback = contract["app_html"]["baked_fallback"]
    values = {
        "GIT_COMMIT": COMMIT,
        "GIT_BRANCH": "dev",
        "BUILD_CONTEXT_SHA256": BUILD_CONTEXT_SHA256,
        "RELEASE_CONTRACT_SHA256": contract_sha256,
        "APP_PROVENANCE_SHA256": PROVENANCE.application_provenance_sha256(
            contract,
            record,
        ),
        "APP_HTML_SOURCE": "s3",
        "APP_HTML_SHA256": record["app_html_sha256"],
        "APP_HTML_VERSION_ID": record["app_html_s3_version_id"],
        "APP_HTML_MANIFEST_SHA256": record["vault_manifest_sha256"],
        "APP_HTML_BUILD_INPUTS_SHA256": record["build_inputs_sha256"],
        "BAKED_APP_HTML_SHA256": fallback["sha256"],
        "BAKED_APP_HTML_VERSION_ID": fallback["s3_version_id"],
    }
    labels = dict(PROVENANCE.COMMON_STATIC_TEAMAGENT_LABELS)
    for label_name, binding in subject["required_label_bindings"].items():
        labels[label_name] = (
            subject["runtime_kind"]
            if binding == subject["runtime_kind"]
            else values[binding]
        )
    labels.update(
        {
            assertion["oci_label"]: assertion["value"]
            for assertion in subject["source_assertions"]
        }
    )
    if subject_name == "core":
        labels.update(
            PROVENANCE._runtime_expected_labels(
                runtime,
                contract["source_runtime_contract"]["sha256"],
            )
        )
    labels["org.opencontainers.image.revision"] = COMMIT
    labels["org.opencontainers.image.ref.name"] = "dev"
    return labels


def _write_oci_config(path: Path, labels: dict[str, str]) -> str:
    _write_json(
        path,
        {
            "os": "linux",
            "architecture": "arm64",
            "config": {"Labels": labels},
        },
    )
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_current_contract_pair_and_latest_record_are_valid() -> None:
    contract, runtime = PROVENANCE.validate_contract_pair(
        RUNTIME_CONTRACT_PATH,
        CONTRACT_PATH,
        ROOT,
        OTHER_COMMIT,
    )
    record = PROVENANCE.verify_production_record(contract, DEPLOY_LOG)

    assert contract["schema_version"] == 2
    assert contract["approval_record"] is None
    assert runtime["schema_version"] == 4
    assert runtime["receipt"]["schema_version"] == 2
    assert runtime["receipt"]["subject"] == "core"
    assert record["app_html_s3_version_id"] == "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee"
    assert len(PROVENANCE.application_provenance_sha256(contract, record)) == 64
    with pytest.raises(PROVENANCE.ProvenanceError, match="release is blocked"):
        PROVENANCE.require_release_ready(contract)


def test_binary_probe_keys_are_subject_scoped_and_cli_order_stays_path_sorted() -> None:
    contract = PROVENANCE.load_contract(CONTRACT_PATH)
    core = PROVENANCE.binary_probes(contract, "core")
    media = PROVENANCE.binary_probes(contract, "media")

    assert {probe["key"] for probe in core} == {
        "app.baked-fallback.sha256",
        "binary.python.sha256",
    }
    assert {probe["key"] for probe in media} == {
        "binary.chromium.sha256",
        "binary.ffmpeg.sha256",
        "binary.node.sha256",
        "binary.python.sha256",
    }
    assert [probe["path"] for probe in core] == sorted(probe["path"] for probe in core)
    assert [probe["path"] for probe in media] == sorted(probe["path"] for probe in media)


def test_release_contract_is_satisfiable_after_both_approvals(
    tmp_path: Path,
) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    _activate_pair(runtime_path, contract_path)

    contract, runtime = PROVENANCE.validate_contract_pair(
        runtime_path,
        contract_path,
        tmp_path,
        COMMIT,
    )
    PROVENANCE.require_release_ready(contract)
    assert runtime["release"]["ready"] is True


@pytest.mark.parametrize(
    ("inner_commit", "outer_commit"),
    [
        pytest.param(OTHER_COMMIT, COMMIT, id="inner-mismatch"),
        pytest.param(COMMIT, OTHER_COMMIT, id="outer-mismatch"),
        pytest.param(OTHER_COMMIT, OTHER_COMMIT, id="matching-stale-approvals"),
    ],
)
def test_pair_rejects_approval_commit_not_bound_to_expected_build(
    tmp_path: Path,
    inner_commit: str,
    outer_commit: str,
) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    _activate_pair(runtime_path, contract_path)

    runtime = _read_json(runtime_path)
    runtime["approval_record"]["source_commit"] = inner_commit
    _write_json(runtime_path, runtime)
    _sync_outer_pin(runtime_path, contract_path)

    contract = _read_json(contract_path)
    contract["approval_record"]["source_commit"] = outer_commit
    _write_json(contract_path, contract)

    with pytest.raises(
        PROVENANCE.ProvenanceError,
        match="approval source_commit does not bind the expected build commit",
    ):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_pair_validates_expected_commit_before_reading_contracts(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(
        PROVENANCE.ProvenanceError,
        match="expected commit must be a full lowercase Git SHA",
    ):
        PROVENANCE.validate_contract_pair(
            missing,
            missing,
            tmp_path,
            "not-a-commit",
        )


def test_outer_release_ready_cli_binds_approval_to_expected_commit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    _activate_pair(runtime_path, contract_path)

    assert (
        PROVENANCE.main(
            [
                "assert-release-ready",
                "--contract",
                str(contract_path),
                "--expected-commit",
                COMMIT,
            ]
        )
        == 0
    )
    assert (
        PROVENANCE.main(
            [
                "assert-release-ready",
                "--contract",
                str(contract_path),
                "--expected-commit",
                OTHER_COMMIT,
            ]
        )
        == 2
    )
    assert (
        "approval source_commit does not bind the expected build commit"
        in capsys.readouterr().err
    )
    assert runtime_path.exists()


def test_outer_release_ready_cli_keeps_blocked_contract_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        PROVENANCE.main(
            [
                "assert-release-ready",
                "--contract",
                str(CONTRACT_PATH),
                "--expected-commit",
                COMMIT,
            ]
        )
        == 2
    )
    assert "release is blocked" in capsys.readouterr().err


def test_malformed_newest_production_entry_cannot_fall_back_to_an_older_record(
    tmp_path: Path,
) -> None:
    valid = PROVENANCE.latest_production_record(DEPLOY_LOG)
    log = tmp_path / "deploy_log.md"
    log.write_text(
        "## 2026-07-18 /app 本番\n\nmalformed newest record\n\n---\n"
        + _deploy_log(valid),
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="exactly one provenance record"):
        PROVENANCE.latest_production_record(log)


def test_pair_rejects_stale_outer_runtime_pin(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    contract = _read_json(contract_path)
    contract["source_runtime_contract"]["sha256"] = "0" * 64
    _write_json(contract_path, contract)

    with pytest.raises(PROVENANCE.ProvenanceError, match="inner raw bytes"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_pair_rejects_core_and_media_python_value_exchange(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    contract = _read_json(contract_path)
    core_probe = next(
        probe
        for probe in _subject(contract, "core")["binary_probes"]
        if probe["key"] == "binary.python.sha256"
    )
    media_probe = next(
        probe
        for probe in _subject(contract, "media")["binary_probes"]
        if probe["key"] == "binary.python.sha256"
    )
    core_probe["sha256"], media_probe["sha256"] = (
        media_probe["sha256"],
        core_probe["sha256"],
    )
    _write_json(contract_path, contract)

    with pytest.raises(PROVENANCE.ProvenanceError, match="core Python probe path/value"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_contract_rejects_duplicate_subject_key_probe_identity(tmp_path: Path) -> None:
    _, contract_path = _copy_pair(tmp_path)
    contract = _read_json(contract_path)
    media = _subject(contract, "media")
    media["binary_probes"].append(copy.deepcopy(media["binary_probes"][-1]))
    _write_json(contract_path, contract)

    with pytest.raises(PROVENANCE.ProvenanceError, match=r"\(subject, key\) identities"):
        PROVENANCE.load_contract(contract_path)


def test_contract_rejects_chromium_old_path(tmp_path: Path) -> None:
    _, contract_path = _copy_pair(tmp_path)
    contract = _read_json(contract_path)
    chromium = next(
        probe
        for probe in _subject(contract, "media")["binary_probes"]
        if probe["key"] == "binary.chromium.sha256"
    )
    chromium["path"] = "/usr/bin/chromium-browser"
    _write_json(contract_path, contract)

    with pytest.raises(PROVENANCE.ProvenanceError, match="key/path interface"):
        PROVENANCE.load_contract(contract_path)


def test_pair_rejects_final_chromium_env_old_path(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    dockerfile = tmp_path / MEDIA_DOCKERFILE
    body = dockerfile.read_text(encoding="utf-8")
    assert "CHROMIUM_PATH=/usr/lib/chromium/chromium" in body
    dockerfile.write_text(
        body.replace(
            "CHROMIUM_PATH=/usr/lib/chromium/chromium",
            "CHROMIUM_PATH=/usr/bin/chromium-browser",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="does not implement"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


@pytest.mark.parametrize("legacy_argument", ["NODE_IMAGE_DIGEST", "WOLFI_PYTHON_VERSION"])
def test_pair_rejects_legacy_argument_reintroduction(
    tmp_path: Path,
    legacy_argument: str,
) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    dockerfile = tmp_path / MEDIA_DOCKERFILE
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8")
        + f"\nARG {legacy_argument}=sha256:{'1' * 64}\n",
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="reintroduces legacy ARG"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_pair_rejects_mutable_external_image(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    dockerfile = tmp_path / MEDIA_DOCKERFILE
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8") + "\nFROM ubuntu:latest AS future\n",
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="not digest pinned"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_pair_rejects_mutable_external_copy_image(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    dockerfile = tmp_path / MEDIA_DOCKERFILE
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8")
        + "\nCOPY --from=public.ecr.aws/docker/library/node:latest "
        "/usr/bin/node /tmp/untrusted-node\n",
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="not digest pinned"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


@pytest.mark.parametrize("reference", ["final", "4", "99"])
def test_pair_rejects_self_or_invalid_numeric_copy_stage(
    tmp_path: Path,
    reference: str,
) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    dockerfile = tmp_path / MEDIA_DOCKERFILE
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8")
        + f"\nCOPY --from={reference} /tmp/source /tmp/destination\n",
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="not digest pinned"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_pair_rejects_unknown_teamagent_dockerfile_label(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    dockerfile = tmp_path / MEDIA_DOCKERFILE
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8")
        + '\nLABEL io.teamagent.build.unreviewed="unreviewed-value"\n',
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match=r"unknown=.*unreviewed"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_pair_rejects_missing_media_package_assertion(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    contract = _read_json(contract_path)
    media = _subject(contract, "media")
    media["source_assertions"] = [
        assertion
        for assertion in media["source_assertions"]
        if assertion["key"] != "package.chromium.version"
    ]
    _write_json(contract_path, contract)

    with pytest.raises(PROVENANCE.ProvenanceError, match=r"unknown=.*package-version"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_pair_rejects_source_assertion_default_drift(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    dockerfile = tmp_path / MEDIA_DOCKERFILE
    body = dockerfile.read_text(encoding="utf-8")
    expected = "ARG CHROMIUM_PACKAGE_VERSION=150.0.7871.114-r0"
    assert expected in body
    dockerfile.write_text(
        body.replace(expected, "ARG CHROMIUM_PACKAGE_VERSION=150.0.7871.113-r0", 1),
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="does not fix"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_pair_rejects_missing_core_receipt_arg(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    dockerfile = tmp_path / CORE_DOCKERFILE
    body = dockerfile.read_text(encoding="utf-8")
    assert body.count("ARG RUNTIME_RECEIPT_SHA256\n") == 2
    dockerfile.write_text(
        body.replace("ARG RUNTIME_RECEIPT_SHA256\n", "", 1),
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="builder/final"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


def test_ready_and_approval_relationship_is_fail_closed(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    contract = _read_json(contract_path)
    contract["release"] = {"ready": True, "blocked_reason": ""}
    assert contract["approval_record"] is None
    _write_json(contract_path, contract)

    with pytest.raises(PROVENANCE.ProvenanceError, match="approval_record"):
        PROVENANCE.load_contract(contract_path)

    contract = _read_json(CONTRACT_PATH)
    contract["approval_record"] = _approval_record(
        {key: "x" for key in PROVENANCE.APPROVAL_OBSERVATION_KEYS}
    )
    _write_json(contract_path, contract)
    with pytest.raises(PROVENANCE.ProvenanceError, match="must be null"):
        PROVENANCE.load_contract(contract_path)
    assert runtime_path.exists()


def test_outer_ready_cannot_precede_inner_ready(tmp_path: Path) -> None:
    runtime_path, contract_path = _copy_pair(tmp_path)
    _activate_pair(runtime_path, contract_path)
    runtime = _read_json(runtime_path)
    runtime["release"] = {
        "ready": False,
        "blocked_reason": "test keeps the inner contract blocked until evidence is signed",
    }
    runtime["approval_record"] = None
    _write_json(runtime_path, runtime)
    _sync_outer_pin(runtime_path, contract_path)

    with pytest.raises(PROVENANCE.ProvenanceError, match="inner release is blocked"):
        PROVENANCE.validate_contract_pair(
            runtime_path,
            contract_path,
            tmp_path,
            COMMIT,
        )


@pytest.mark.parametrize("subject_name", ["core", "media"])
def test_oci_config_requires_complete_exact_teamagent_label_set(
    tmp_path: Path,
    subject_name: str,
) -> None:
    contract = PROVENANCE.load_contract(CONTRACT_PATH)
    runtime = PROVENANCE._load_runtime_contract(RUNTIME_CONTRACT_PATH)
    contract_sha256 = PROVENANCE.contract_sha256(CONTRACT_PATH)
    labels = _expected_oci_labels(
        contract,
        runtime,
        subject_name=subject_name,
        contract_sha256=contract_sha256,
    )
    config = tmp_path / f"{subject_name}.json"
    digest = _write_oci_config(config, labels)

    verified = PROVENANCE.verify_oci_config(
        config,
        subject_name=subject_name,
        commit=COMMIT,
        expected_config_digest=digest,
        contract_path=CONTRACT_PATH,
        expected_contract_sha256=contract_sha256,
        runtime_contract_path=RUNTIME_CONTRACT_PATH,
        expected_build_context_sha256=BUILD_CONTEXT_SHA256,
    )
    assert verified["io.teamagent.build.context-sha256"] == BUILD_CONTEXT_SHA256

    labels["io.teamagent.build.unreviewed"] = "unreviewed-value"
    digest = _write_oci_config(config, labels)
    with pytest.raises(PROVENANCE.ProvenanceError, match="label allowlist"):
        PROVENANCE.verify_oci_config(
            config,
            subject_name=subject_name,
            commit=COMMIT,
            expected_config_digest=digest,
            contract_path=CONTRACT_PATH,
            expected_contract_sha256=contract_sha256,
            runtime_contract_path=RUNTIME_CONTRACT_PATH,
            expected_build_context_sha256=BUILD_CONTEXT_SHA256,
        )


def test_oci_config_rejects_wrong_context_and_inner_receipt_value(
    tmp_path: Path,
) -> None:
    contract = PROVENANCE.load_contract(CONTRACT_PATH)
    runtime = PROVENANCE._load_runtime_contract(RUNTIME_CONTRACT_PATH)
    contract_sha256 = PROVENANCE.contract_sha256(CONTRACT_PATH)
    labels = _expected_oci_labels(
        contract,
        runtime,
        subject_name="core",
        contract_sha256=contract_sha256,
    )
    config = tmp_path / "core.json"
    digest = _write_oci_config(config, labels)

    with pytest.raises(PROVENANCE.ProvenanceError, match="context-sha256"):
        PROVENANCE.verify_oci_config(
            config,
            subject_name="core",
            commit=COMMIT,
            expected_config_digest=digest,
            contract_path=CONTRACT_PATH,
            expected_contract_sha256=contract_sha256,
            runtime_contract_path=RUNTIME_CONTRACT_PATH,
            expected_build_context_sha256="b" * 64,
        )

    labels["io.teamagent.contract.python-binary-sha256"] = "0" * 64
    digest = _write_oci_config(config, labels)
    with pytest.raises(PROVENANCE.ProvenanceError, match="python-binary-sha256"):
        PROVENANCE.verify_oci_config(
            config,
            subject_name="core",
            commit=COMMIT,
            expected_config_digest=digest,
            contract_path=CONTRACT_PATH,
            expected_contract_sha256=contract_sha256,
            runtime_contract_path=RUNTIME_CONTRACT_PATH,
            expected_build_context_sha256=BUILD_CONTEXT_SHA256,
        )


def test_oci_config_rejects_placeholder_before_it_can_be_receipted(
    tmp_path: Path,
) -> None:
    contract = PROVENANCE.load_contract(CONTRACT_PATH)
    runtime = PROVENANCE._load_runtime_contract(RUNTIME_CONTRACT_PATH)
    contract_sha256 = PROVENANCE.contract_sha256(CONTRACT_PATH)
    labels = _expected_oci_labels(
        contract,
        runtime,
        subject_name="core",
        contract_sha256=contract_sha256,
    )
    labels["org.opencontainers.image.ref.name"] = "unknown"
    config = tmp_path / "core.json"
    digest = _write_oci_config(config, labels)

    with pytest.raises(PROVENANCE.ProvenanceError, match="untrusted placeholder"):
        PROVENANCE.verify_oci_config(
            config,
            subject_name="core",
            commit=COMMIT,
            expected_config_digest=digest,
            contract_path=CONTRACT_PATH,
            expected_contract_sha256=contract_sha256,
            runtime_contract_path=RUNTIME_CONTRACT_PATH,
            expected_build_context_sha256=BUILD_CONTEXT_SHA256,
        )
