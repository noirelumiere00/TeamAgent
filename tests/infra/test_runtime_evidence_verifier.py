"""Tamper tests for immutable runtime scan/SBOM receipts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERIFIER_PATH = ROOT / "infra/docker/verify_runtime_evidence.py"
BUILD_SCRIPT = ROOT / "infra/docker/build_local_runtime_evidence.sh"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_evidence_verifier", VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFIER = _load_verifier()
EvidenceError = VERIFIER.EvidenceError
IMAGE_ID = "sha256:" + "a" * 64
ARTIFACT_ID = "sha256:" + "b" * 64
NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


def _report(kind: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "Target": "exact image (alpine 3.24)",
        "Class": "os-pkgs",
        "Type": "alpine",
        "Packages": [{"Name": "musl", "Version": "1.2.5"}],
    }
    if kind == "vulnerability":
        result["Vulnerabilities"] = []
    else:
        result["Secrets"] = []
    return {
        "SchemaVersion": 2,
        "ArtifactName": IMAGE_ID,
        "ArtifactType": "container_image",
        "ArtifactID": ARTIFACT_ID,
        "CreatedAt": NOW.isoformat(),
        "Metadata": {
            "ImageID": IMAGE_ID,
            "RepoDigests": [f"teamagent@example.invalid/runtime@{IMAGE_ID}"],
        },
        "Results": [result],
    }


def _verify_pair(
    vulnerability: dict[str, Any] | None = None,
    secret: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return VERIFIER.verify_trivy_pair(
        vulnerability or _report("vulnerability"),
        secret or _report("secret"),
        expected_image_id=IMAGE_ID,
        expected_artifact_name=IMAGE_ID,
        scan_started_at=NOW - timedelta(minutes=1),
        scan_finished_at=NOW + timedelta(minutes=1),
    )


def test_trivy_pair_accepts_only_meaningful_exact_zero_reports() -> None:
    summary = _verify_pair()
    assert summary["critical"] == summary["high"] == summary["secrets"] == 0
    assert summary["artifact_id"] == ARTIFACT_ID
    assert set(summary["explicitly_absent_live_cves"]) == {
        "CVE-2026-5450",
        "CVE-2026-13221",
        "CVE-2026-12087",
        "CVE-2026-57433",
    }


def test_trivy_pair_rejects_results_only_empty_report() -> None:
    vulnerability = _report("vulnerability")
    vulnerability["Results"] = []
    with pytest.raises(EvidenceError, match="empty Results"):
        _verify_pair(vulnerability=vulnerability)


@pytest.mark.parametrize("field", ("ArtifactName", "ImageID", "ArtifactID"))
def test_trivy_pair_rejects_swapped_or_mismatched_subject(field: str) -> None:
    secret = _report("secret")
    if field == "ImageID":
        secret["Metadata"]["ImageID"] = "sha256:" + "c" * 64
    else:
        secret[field] = "sha256:" + "c" * 64
    with pytest.raises(EvidenceError):
        _verify_pair(secret=secret)


def test_trivy_pair_rejects_named_live_critical_cve() -> None:
    vulnerability = _report("vulnerability")
    vulnerability["Results"][0]["Vulnerabilities"] = [
        {"VulnerabilityID": "CVE-2026-5450", "Severity": "CRITICAL"}
    ]
    with pytest.raises(EvidenceError, match="zero gate"):
        _verify_pair(vulnerability=vulnerability)


def _scanner(*, next_update: datetime) -> dict[str, Any]:
    return {
        "Version": "0.72.0",
        "VulnerabilityDB": {
            "Version": 2,
            "UpdatedAt": (NOW - timedelta(hours=2)).isoformat(),
            "DownloadedAt": (NOW - timedelta(hours=1)).isoformat(),
            "NextUpdate": next_update.isoformat(),
        },
        "CheckBundle": {
            "Digest": "sha256:" + "d" * 64,
            "DownloadedAt": (NOW - timedelta(hours=1)).isoformat(),
        },
    }


def test_scanner_snapshot_binds_version_database_and_timestamp() -> None:
    summary = VERIFIER.verify_scanner_snapshot(
        _scanner(next_update=NOW + timedelta(hours=8)),
        scan_started_at=NOW,
        scan_finished_at=NOW + timedelta(minutes=5),
    )
    assert summary["version"] == "0.72.0"
    assert summary["vulnerability_db_version"] == 2


def test_scanner_snapshot_rejects_stale_database() -> None:
    with pytest.raises(EvidenceError, match="stale"):
        VERIFIER.verify_scanner_snapshot(
            _scanner(next_update=NOW - timedelta(seconds=1)),
            scan_started_at=NOW,
            scan_finished_at=NOW + timedelta(minutes=5),
        )


def _inspect() -> dict[str, Any]:
    return {"RootFS": {"Layers": ["sha256:" + "1" * 64, "sha256:" + "2" * 64]}}


def _sbom() -> dict[str, Any]:
    properties = [
        {"name": "aquasecurity:trivy:ImageID", "value": IMAGE_ID},
        {"name": "aquasecurity:trivy:DiffID", "value": "sha256:" + "1" * 64},
        {"name": "aquasecurity:trivy:DiffID", "value": "sha256:" + "2" * 64},
    ]
    return {
        "bomFormat": "CycloneDX",
        "components": [{"type": "library", "name": "musl"}],
        "metadata": {
            "timestamp": NOW.isoformat(),
            "tools": {
                "components": [{"type": "application", "name": "trivy", "version": "0.72.0"}]
            },
            "component": {
                "type": "container",
                "purl": f"pkg:oci/runtime@{IMAGE_ID}",
                "bom-ref": f"pkg:oci/runtime@{IMAGE_ID}",
                "properties": properties,
            },
        },
    }


def test_sbom_binds_exact_image_and_filesystem_layers() -> None:
    assert VERIFIER.verify_sbom(
        _sbom(), _inspect(), expected_image_id=IMAGE_ID, scanner_version="0.72.0"
    ) == {"components": 1, "layers": 2}


def test_sbom_rejects_swapped_image_or_filesystem() -> None:
    sbom = _sbom()
    sbom["metadata"]["component"]["properties"][-1]["value"] = "sha256:" + "3" * 64
    with pytest.raises(EvidenceError, match="DiffIDs"):
        VERIFIER.verify_sbom(
            sbom,
            _inspect(),
            expected_image_id=IMAGE_ID,
            scanner_version="0.72.0",
        )


def test_checksum_manifest_rejects_post_scan_swap(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    payload = evidence / "core-trivy-vulnerability.json"
    payload.write_text("original", encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    (evidence / "SHA256SUMS").write_text(
        f"{digest}  ./core-trivy-vulnerability.json\n",
        encoding="utf-8",
    )
    VERIFIER._verify_checksums(evidence)
    payload.write_text("swapped", encoding="utf-8")
    with pytest.raises(EvidenceError, match="mismatch"):
        VERIFIER._verify_checksums(evidence)


def test_full_verifier_rejects_stale_head_before_using_artifacts(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "receipt.json").write_text(
        json.dumps({"schema_version": "1", "git": {"head": "b" * 40}}),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="stale"):
        VERIFIER.verify_evidence(
            evidence,
            expected_head="a" * 40,
            repo_root=ROOT,
            verify_checksums=False,
        )


def test_evidence_builder_canonicalizes_source_scan_subject() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    created = 'TRACKED_SOURCE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/'
    canonical = 'TRACKED_SOURCE_DIR=$(CDPATH= cd -- "$TRACKED_SOURCE_DIR" && pwd -P)'
    assert created in script
    assert canonical in script
    assert script.index(created) < script.index(canonical)
