from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "teamagent_bundle_provenance.py"
CONTRACT_PATH = ROOT / "infra" / "codebuild" / "teamagent_core_media_release_contract.json"
DEPLOY_LOG = ROOT / "infra" / "deploy_log.md"


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


def _deploy_log(record: dict[str, Any]) -> str:
    anchors = " / ".join(str(record[key]) for key in sorted(PROVENANCE.PRODUCTION_KEYS))
    return (
        "## 2026-07-17 /app 本番\n\n"
        f"{anchors}\n"
        f"<!-- PRODUCTION_APP_PROVENANCE={json.dumps(record, separators=(',', ':'))} -->\n"
        "\n---\n"
    )


def test_current_contract_and_latest_record_preserve_all_four_production_anchors() -> None:
    contract = PROVENANCE.load_contract(CONTRACT_PATH)
    record = PROVENANCE.verify_production_record(contract, DEPLOY_LOG)

    assert record == {
        "schema_version": 1,
        "app_html_s3_version_id": "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee",
        "app_html_sha256": ("03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c"),
        "vault_manifest_sha256": (
            "aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e"
        ),
        "build_inputs_sha256": ("6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf"),
    }
    assert len(PROVENANCE.application_provenance_sha256(contract, record)) == 64
    with pytest.raises(PROVENANCE.ProvenanceError, match="release is blocked"):
        PROVENANCE.require_release_ready(contract)


def test_malformed_newest_production_entry_cannot_fall_back_to_an_older_record(
    tmp_path: Path,
) -> None:
    valid = PROVENANCE.latest_production_record(DEPLOY_LOG)
    log = tmp_path / "deploy_log.md"
    log.write_text(
        "## 2026-07-18 /app 本番\n\nmalformed newest record\n\n---\n" + _deploy_log(valid),
        encoding="utf-8",
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="exactly one provenance record"):
        PROVENANCE.latest_production_record(log)


def test_source_interface_rejects_unknown_provenance_arg_defaults(
    tmp_path: Path,
) -> None:
    contract = copy.deepcopy(PROVENANCE.load_contract(CONTRACT_PATH))
    contract["release"] = {"ready": True, "blocked_reason": ""}
    contract["app_html"]["baked_fallback"]["s3_version_id"] = "fallback-version-1"
    runtime_bytes = b'{"schema_version":1}\n'
    contract["source_runtime_contract"]["sha256"] = hashlib.sha256(runtime_bytes).hexdigest()
    fallback_bytes = b"approved baked fallback"
    contract["app_html"]["baked_fallback"]["sha256"] = hashlib.sha256(fallback_bytes).hexdigest()

    contract_path = tmp_path / "contract.json"
    _write_json(contract_path, contract)
    runtime_path = tmp_path / "infra" / "codebuild" / "teamagent_runtime_contract.json"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_bytes(runtime_bytes)
    fallback_path = tmp_path / "external-baked-app.html"
    fallback_path.write_bytes(fallback_bytes)
    record = {
        "schema_version": 1,
        **contract["app_html"]["production"],
    }
    deploy_log = tmp_path / "infra" / "deploy_log.md"
    deploy_log.parent.mkdir(parents=True, exist_ok=True)
    deploy_log.write_text(_deploy_log(record), encoding="utf-8")

    for subject in contract["subjects"]:
        arguments = [
            f"ARG {argument}{'=unknown' if argument == 'GIT_COMMIT' else ''}"
            for argument in subject["required_build_args"]
        ]
        labels = []
        for label, binding in subject["required_label_bindings"].items():
            value = binding if binding == subject["runtime_kind"] else f"${{{binding}}}"
            labels.append(f'{label}="{value}"')
        dockerfile = tmp_path / subject["dockerfile"]
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text(
            "\n".join([*arguments, "FROM scratch", f"LABEL {' '.join(labels)}"]) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(
        PROVENANCE.ProvenanceError,
        match="must declare GIT_COMMIT without a default",
    ):
        PROVENANCE.verify_source_interface(
            tmp_path,
            contract_path,
            deploy_log,
            fallback_path,
        )


def test_oci_config_rejects_unknown_labels_before_they_can_be_receipted(
    tmp_path: Path,
) -> None:
    contract = PROVENANCE.load_contract(CONTRACT_PATH)
    record = {"schema_version": 1, **contract["app_html"]["production"]}
    labels = {
        "org.opencontainers.image.revision": "1" * 40,
        "org.opencontainers.image.ref.name": "unknown",
        "io.teamagent.runtime.kind": "core",
        "io.teamagent.build.release-contract-sha256": (PROVENANCE.contract_sha256(CONTRACT_PATH)),
        "io.teamagent.build.app-provenance-sha256": (
            PROVENANCE.application_provenance_sha256(contract, record)
        ),
    }
    config = tmp_path / "config.json"
    _write_json(
        config,
        {
            "os": "linux",
            "architecture": "arm64",
            "config": {"Labels": labels},
        },
    )

    with pytest.raises(PROVENANCE.ProvenanceError, match="untrusted placeholder"):
        PROVENANCE.verify_oci_config(
            config,
            subject_name="core",
            commit="1" * 40,
            expected_config_digest=("sha256:" + hashlib.sha256(config.read_bytes()).hexdigest()),
            contract_path=CONTRACT_PATH,
            expected_contract_sha256=PROVENANCE.contract_sha256(CONTRACT_PATH),
        )


def test_oci_config_binds_manifest_digest_and_rejects_unknown_teamagent_labels(
    tmp_path: Path,
) -> None:
    contract = copy.deepcopy(PROVENANCE.load_contract(CONTRACT_PATH))
    contract["release"] = {"ready": True, "blocked_reason": ""}
    contract["app_html"]["baked_fallback"]["s3_version_id"] = "fallback-version-1"
    contract_path = tmp_path / "contract.json"
    _write_json(contract_path, contract)
    contract_sha256 = PROVENANCE.contract_sha256(contract_path)
    record = {"schema_version": 1, **contract["app_html"]["production"]}
    labels = {
        "org.opencontainers.image.revision": "1" * 40,
        "org.opencontainers.image.ref.name": "dev",
        "io.teamagent.runtime.kind": "core",
        "io.teamagent.build.release-contract-sha256": contract_sha256,
        "io.teamagent.build.app-provenance-sha256": (
            PROVENANCE.application_provenance_sha256(contract, record)
        ),
        "io.teamagent.contract.app-html-source": "s3",
        "io.teamagent.contract.baked-app-html-sha256": (
            contract["app_html"]["baked_fallback"]["sha256"]
        ),
        "io.teamagent.contract.baked-app-html-version-id": "fallback-version-1",
        "io.teamagent.contract.app-html-version-id": (record["app_html_s3_version_id"]),
        "io.teamagent.contract.app-html-sha256": record["app_html_sha256"],
        "io.teamagent.contract.app-html-manifest-sha256": (record["vault_manifest_sha256"]),
        "io.teamagent.contract.app-html-build-inputs-sha256": (record["build_inputs_sha256"]),
    }

    def write_config(extra_labels: dict[str, str] | None = None) -> tuple[Path, str]:
        config = tmp_path / "config.json"
        _write_json(
            config,
            {
                "os": "linux",
                "architecture": "arm64",
                "config": {"Labels": {**labels, **(extra_labels or {})}},
            },
        )
        return config, "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest()

    config, digest = write_config()
    verified = PROVENANCE.verify_oci_config(
        config,
        subject_name="core",
        commit="1" * 40,
        expected_config_digest=digest,
        contract_path=contract_path,
        expected_contract_sha256=contract_sha256,
    )
    assert verified["io.teamagent.contract.baked-app-html-version-id"] == ("fallback-version-1")

    with pytest.raises(PROVENANCE.ProvenanceError, match="manifest digest"):
        PROVENANCE.verify_oci_config(
            config,
            subject_name="core",
            commit="1" * 40,
            expected_config_digest="sha256:" + "0" * 64,
            contract_path=contract_path,
            expected_contract_sha256=contract_sha256,
        )

    config, digest = write_config({"io.teamagent.build.unreviewed": "unreviewed-value"})
    with pytest.raises(PROVENANCE.ProvenanceError, match="label allowlist"):
        PROVENANCE.verify_oci_config(
            config,
            subject_name="core",
            commit="1" * 40,
            expected_config_digest=digest,
            contract_path=contract_path,
            expected_contract_sha256=contract_sha256,
        )
