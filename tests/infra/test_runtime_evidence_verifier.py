"""Tamper tests for immutable runtime scan/SBOM receipts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tarfile
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
        "Packages": [
            {
                "Name": "musl",
                "Version": "1.2.5",
                "Identifier": {"PURL": "pkg:apk/musl@1.2.5"},
            }
        ],
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
    assert all(
        summary[key] == 0 for key in ("unknown", "low", "medium", "high", "critical", "secrets")
    )
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


@pytest.mark.parametrize("severity", ("LOW", "MEDIUM", "UNKNOWN"))
def test_trivy_pair_rejects_nonzero_lower_or_unknown_severity(severity: str) -> None:
    vulnerability = _report("vulnerability")
    vulnerability["Results"][0]["Vulnerabilities"] = [
        {"VulnerabilityID": "CVE-2099-12345", "Severity": severity}
    ]
    with pytest.raises(EvidenceError, match="zero gate"):
        _verify_pair(vulnerability=vulnerability)


def test_retained_scan_summary_rejects_mutated_counts_or_subject() -> None:
    recomputed = _verify_pair()
    retained = dict(recomputed)
    retained["critical"] = 1
    with pytest.raises(EvidenceError, match="differs from recomputed"):
        VERIFIER.verify_retained_scan_summary(retained, recomputed, name="core")


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


def _grype_database() -> dict[str, Any]:
    return {
        "schemaVersion": "v6.1.9",
        "from": "manual import",
        "built": (NOW - timedelta(days=1)).isoformat(),
        "path": "/private/tmp/grype/vulnerability.db",
        "valid": True,
    }


def _grype_version() -> dict[str, Any]:
    return {
        "application": "grype",
        "buildDate": (NOW - timedelta(days=60)).isoformat(),
        "gitCommit": "2" * 40,
        "platform": "darwin/arm64",
        "supportedDbSchema": 6,
        "syftVersion": "v1.44.0",
        "version": "0.112.0",
    }


def _grype_scanner() -> dict[str, Any]:
    return VERIFIER.verify_grype_scanner_snapshot(
        _grype_version(),
        _grype_database(),
        binary_sha256="3" * 64,
        scan_finished_at=NOW + timedelta(minutes=1),
    )


def _grype_report() -> dict[str, Any]:
    return {
        "descriptor": {
            "name": "grype",
            "version": "0.112.0",
            "timestamp": NOW.isoformat(),
            "configuration": {
                "show-suppressed": True,
                "db": {
                    "auto-update": False,
                    "validate-by-hash-on-start": True,
                    "validate-age": True,
                },
            },
            "db": {"status": _grype_database()},
        },
        "source": {
            "type": "image",
            "target": {
                "userInput": IMAGE_ID,
                "repoDigests": [f"teamagent@example.invalid/runtime@{IMAGE_ID}"],
                "architecture": "arm64",
                "os": "linux",
                "labels": {"org.opencontainers.image.revision": "4" * 40},
            },
        },
        "matches": [
            {
                "vulnerability": {
                    "id": "CVE-2099-12345",
                    "namespace": "github:language:python",
                    "dataSource": "https://github.com/advisories/CVE-2099-12345",
                    "severity": "Medium",
                    "fix": {"versions": ["1.2.6"], "state": "fixed"},
                },
                "artifact": {
                    "name": "fixture",
                    "version": "1.2.5",
                    "purl": "pkg:pypi/fixture@1.2.5",
                },
            }
        ],
        "ignoredMatches": [],
    }


def test_grype_report_binds_scanner_database_subject_and_fix_availability() -> None:
    summary = VERIFIER.verify_grype_report(
        _grype_report(),
        expected_image_id=IMAGE_ID,
        expected_head="4" * 40,
        scanner=_grype_scanner(),
        scan_started_at=NOW - timedelta(minutes=1),
        scan_finished_at=NOW + timedelta(minutes=1),
    )
    assert summary["total"] == summary["medium"] == summary["fixed_available"] == 1
    assert summary["suppressed"] == 0


@pytest.mark.parametrize("mutation", ("subject", "suppressed", "purl", "database"))
def test_grype_report_rejects_mutated_or_suppressed_evidence(mutation: str) -> None:
    report = _grype_report()
    if mutation == "subject":
        report["source"]["target"]["userInput"] = "sha256:" + "9" * 64
    elif mutation == "suppressed":
        report["ignoredMatches"] = [report["matches"][0]]
    elif mutation == "purl":
        report["matches"][0]["artifact"]["purl"] = "pkg:pypi/fixture@9.9.9"
    else:
        report["descriptor"]["db"]["status"]["built"] = (NOW - timedelta(days=6)).isoformat()
    with pytest.raises(EvidenceError):
        VERIFIER.verify_grype_report(
            report,
            expected_image_id=IMAGE_ID,
            expected_head="4" * 40,
            scanner=_grype_scanner(),
            scan_started_at=NOW - timedelta(minutes=1),
            scan_finished_at=NOW + timedelta(minutes=1),
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
        "components": [
            {
                "type": "library",
                "name": "musl",
                "version": "1.2.5",
                "purl": "pkg:apk/musl@1.2.5",
            }
        ],
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
        _sbom(),
        _inspect(),
        _report("vulnerability"),
        expected_image_id=IMAGE_ID,
        scanner_version="0.72.0",
    ) == {"components": 1, "layers": 2, "packages_reconciled": 1}


def test_sbom_rejects_swapped_image_or_filesystem() -> None:
    sbom = _sbom()
    sbom["metadata"]["component"]["properties"][-1]["value"] = "sha256:" + "3" * 64
    with pytest.raises(EvidenceError, match="DiffIDs"):
        VERIFIER.verify_sbom(
            sbom,
            _inspect(),
            _report("vulnerability"),
            expected_image_id=IMAGE_ID,
            scanner_version="0.72.0",
        )


@pytest.mark.parametrize("mutation", ("version", "purl", "extra", "duplicate"))
def test_sbom_rejects_mutated_package_inventory(mutation: str) -> None:
    sbom = _sbom()
    if mutation == "version":
        sbom["components"][0]["version"] = "9.9.9"
    elif mutation == "purl":
        sbom["components"][0]["purl"] = "pkg:apk/musl@9.9.9"
    elif mutation == "extra":
        sbom["components"].append({"type": "library", "name": "injected", "version": "1.0"})
    else:
        sbom["components"].append(dict(sbom["components"][0]))
    with pytest.raises(EvidenceError, match=r"package inventory|package purl"):
        VERIFIER.verify_sbom(
            sbom,
            _inspect(),
            _report("vulnerability"),
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
    assert (
        VERIFIER._verify_checksums(evidence)
        == hashlib.sha256((evidence / "SHA256SUMS").read_bytes()).hexdigest()
    )
    payload.write_text("swapped", encoding="utf-8")
    with pytest.raises(EvidenceError, match="mismatch"):
        VERIFIER._verify_checksums(evidence)


def test_checksum_manifest_ignores_only_retained_final_verification(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    payload = evidence / "receipt.json"
    payload.write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    (evidence / "SHA256SUMS").write_text(
        f"{digest}  ./receipt.json\n",
        encoding="utf-8",
    )
    (evidence / "FINAL_VERIFICATION.json").write_text(
        json.dumps({"ok": True, "sha256sums_sha256": "a" * 64}),
        encoding="utf-8",
    )

    assert (
        VERIFIER._verify_checksums(evidence)
        == hashlib.sha256((evidence / "SHA256SUMS").read_bytes()).hexdigest()
    )
    (evidence / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceError, match="file set"):
        VERIFIER._verify_checksums(evidence)


def test_context_tree_digest_reconciles_retained_archive_and_detects_mutation(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    regular = context / "regular.txt"
    regular.write_text("exact source\n", encoding="utf-8")
    executable = context / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    os.symlink("regular.txt", context / "link")
    archive = tmp_path / "context.tar"
    with tarfile.open(archive, "w:") as bundle:
        bundle.add(context, arcname=".")

    filesystem_digest, filesystem_files = VERIFIER._tree_digest(context)
    archive_digest, inventory, _members = VERIFIER._archive_inventory(archive)
    assert archive_digest == filesystem_digest
    assert filesystem_files == len(inventory) == 3

    regular.write_text("mutated source\n", encoding="utf-8")
    mutated_digest, _ = VERIFIER._tree_digest(context)
    assert mutated_digest != archive_digest


def test_retained_source_archive_git_tree_oid_detects_mutation(tmp_path: Path) -> None:
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
    (repository / "source.txt").write_text("exact\n", encoding="utf-8")
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
    assert VERIFIER._archive_git_tree_oid(archive) == expected_tree

    mutated = tmp_path / "mutated.tar"
    with tarfile.open(mutated, "w:") as bundle:
        payload = tmp_path / "source.txt"
        payload.write_text("mutated\n", encoding="utf-8")
        bundle.add(payload, arcname="source.txt")
    assert VERIFIER._archive_git_tree_oid(mutated) != expected_tree


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


def test_local_receipt_is_explicitly_not_a_production_release_credential() -> None:
    generator = (ROOT / "infra/docker/generate_runtime_receipt.py").read_text(encoding="utf-8")
    verifier = VERIFIER_PATH.read_text(encoding="utf-8")
    scope = "local-source-validation-only-not-release-credential"
    assert scope in generator
    assert scope in verifier
