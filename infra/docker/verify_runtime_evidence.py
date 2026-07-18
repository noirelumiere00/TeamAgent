#!/usr/bin/env python3
"""Verify that a runtime evidence directory is fresh and bound to exact images."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_LIVE_CRITICAL_CVES = (
    "CVE-2026-5450",
    "CVE-2026-13221",
    "CVE-2026-12087",
    "CVE-2026-57433",
)


class EvidenceError(ValueError):
    """Evidence is incomplete, stale, swapped, or subject-mismatched."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{path.name}: invalid JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{path.name}: expected JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{field}: missing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{field}: invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{field}: timestamp must have timezone")
    return parsed.astimezone(UTC)


def _assert_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvidenceError(f"{field}: expected sha256 digest")
    return value


def _meaningful_results(report: dict[str, Any], *, field: str) -> list[dict[str, Any]]:
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise EvidenceError(f"{field}: empty Results cannot prove a scan")
    meaningful: list[dict[str, Any]] = []
    package_count = 0
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise EvidenceError(f"{field}.Results[{index}]: expected object")
        if not all(
            isinstance(result.get(name), str) and result[name]
            for name in ("Target", "Class", "Type")
        ):
            raise EvidenceError(f"{field}.Results[{index}]: missing Target/Class/Type")
        packages = result.get("Packages")
        if not isinstance(packages, list):
            raise EvidenceError(f"{field}.Results[{index}]: package inventory missing")
        package_count += len(packages)
        meaningful.append(result)
    if package_count == 0:
        raise EvidenceError(f"{field}: zero-package Results cannot bind an image filesystem")
    return meaningful


def _verify_scan_time(
    report: dict[str, Any],
    *,
    field: str,
    scan_started_at: datetime,
    scan_finished_at: datetime,
) -> str:
    created = _timestamp(report.get("CreatedAt"), field=f"{field}.CreatedAt")
    tolerance = timedelta(seconds=5)
    if created < scan_started_at - tolerance or created > scan_finished_at + tolerance:
        raise EvidenceError(f"{field}: scan timestamp is outside this receipt window")
    return created.isoformat().replace("+00:00", "Z")


def verify_trivy_pair(
    vulnerability: dict[str, Any],
    secret: dict[str, Any],
    *,
    expected_image_id: str,
    expected_artifact_name: str,
    scan_started_at: datetime,
    scan_finished_at: datetime,
) -> dict[str, Any]:
    """Verify two Trivy reports are non-empty scans of one immutable image."""

    _assert_sha256(expected_image_id, field="expected_image_id")
    artifact_ids: set[str] = set()
    all_vulnerability_ids: set[str] = set()
    counts = {"critical": 0, "high": 0, "secrets": 0}
    created_at: dict[str, str] = {}

    for kind, report in (("vulnerability", vulnerability), ("secret", secret)):
        if report.get("SchemaVersion") != 2 or report.get("ArtifactType") != "container_image":
            raise EvidenceError(f"{kind}: not a Trivy container image report")
        if report.get("ArtifactName") != expected_artifact_name:
            raise EvidenceError(f"{kind}: ArtifactName does not match immutable scan subject")
        metadata = report.get("Metadata")
        if not isinstance(metadata, dict) or metadata.get("ImageID") != expected_image_id:
            raise EvidenceError(f"{kind}: Metadata.ImageID mismatch")
        repo_digests = metadata.get("RepoDigests")
        if not isinstance(repo_digests, list) or not any(
            isinstance(item, str) and item.endswith(f"@{expected_image_id}")
            for item in repo_digests
        ):
            raise EvidenceError(f"{kind}: immutable RepoDigest is missing")
        artifact_ids.add(_assert_sha256(report.get("ArtifactID"), field=f"{kind}.ArtifactID"))
        results = _meaningful_results(report, field=kind)
        created_at[kind] = _verify_scan_time(
            report,
            field=kind,
            scan_started_at=scan_started_at,
            scan_finished_at=scan_finished_at,
        )
        for result in results:
            for finding in result.get("Vulnerabilities") or []:
                if not isinstance(finding, dict):
                    raise EvidenceError(f"{kind}: malformed vulnerability")
                vulnerability_id = str(finding.get("VulnerabilityID") or "")
                if vulnerability_id:
                    all_vulnerability_ids.add(vulnerability_id)
                severity = str(finding.get("Severity") or "").upper()
                if severity == "CRITICAL":
                    counts["critical"] += 1
                elif severity == "HIGH":
                    counts["high"] += 1
            secrets = result.get("Secrets") or []
            if not isinstance(secrets, list):
                raise EvidenceError(f"{kind}: malformed secret findings")
            counts["secrets"] += len(secrets)

    if len(artifact_ids) != 1:
        raise EvidenceError("vulnerability and secret reports describe different artifacts")
    if counts != {"critical": 0, "high": 0, "secrets": 0}:
        raise EvidenceError(f"Trivy zero gate failed: {counts}")
    present_live_cves = sorted(set(_LIVE_CRITICAL_CVES) & all_vulnerability_ids)
    if present_live_cves:
        raise EvidenceError(f"live-image CVEs remain present: {present_live_cves}")
    return {
        **counts,
        "artifact_id": next(iter(artifact_ids)),
        "created_at": created_at,
        "explicitly_absent_live_cves": list(_LIVE_CRITICAL_CVES),
    }


def verify_scanner_snapshot(
    scanner: dict[str, Any],
    *,
    scan_started_at: datetime,
    scan_finished_at: datetime,
) -> dict[str, Any]:
    version = scanner.get("Version")
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise EvidenceError("scanner: invalid Trivy version")
    database = scanner.get("VulnerabilityDB")
    if not isinstance(database, dict) or not isinstance(database.get("Version"), int):
        raise EvidenceError("scanner: vulnerability DB status is missing")
    updated_at = _timestamp(database.get("UpdatedAt"), field="scanner.VulnerabilityDB.UpdatedAt")
    downloaded_at = _timestamp(
        database.get("DownloadedAt"),
        field="scanner.VulnerabilityDB.DownloadedAt",
    )
    next_update = _timestamp(
        database.get("NextUpdate"),
        field="scanner.VulnerabilityDB.NextUpdate",
    )
    if updated_at > scan_started_at or downloaded_at > scan_finished_at:
        raise EvidenceError("scanner: vulnerability DB timestamp is in the future")
    if next_update <= scan_finished_at:
        raise EvidenceError("scanner: vulnerability DB was stale before scans completed")
    checks = scanner.get("CheckBundle")
    if not isinstance(checks, dict):
        raise EvidenceError("scanner: secret check bundle status is missing")
    _assert_sha256(checks.get("Digest"), field="scanner.CheckBundle.Digest")
    _timestamp(checks.get("DownloadedAt"), field="scanner.CheckBundle.DownloadedAt")
    return {
        "version": version,
        "vulnerability_db_version": database["Version"],
        "vulnerability_db_updated_at": updated_at.isoformat().replace("+00:00", "Z"),
        "vulnerability_db_downloaded_at": downloaded_at.isoformat().replace("+00:00", "Z"),
        "vulnerability_db_next_update": next_update.isoformat().replace("+00:00", "Z"),
        "check_bundle_digest": checks["Digest"],
        "check_bundle_downloaded_at": checks["DownloadedAt"],
    }


def verify_sbom(
    sbom: dict[str, Any],
    inspect_record: dict[str, Any],
    *,
    expected_image_id: str,
    scanner_version: str,
) -> dict[str, Any]:
    if sbom.get("bomFormat") != "CycloneDX" or not isinstance(sbom.get("components"), list):
        raise EvidenceError("SBOM: invalid CycloneDX document")
    if not sbom["components"]:
        raise EvidenceError("SBOM: component inventory is empty")
    metadata = sbom.get("metadata")
    if not isinstance(metadata, dict):
        raise EvidenceError("SBOM: metadata is missing")
    _timestamp(metadata.get("timestamp"), field="SBOM.metadata.timestamp")
    tools = metadata.get("tools")
    tool_components = tools.get("components") if isinstance(tools, dict) else None
    if not isinstance(tool_components, list) or not any(
        isinstance(tool, dict)
        and tool.get("name") == "trivy"
        and tool.get("version") == scanner_version
        for tool in tool_components
    ):
        raise EvidenceError("SBOM: scanner version is not bound")
    subject = metadata.get("component")
    if not isinstance(subject, dict) or subject.get("type") != "container":
        raise EvidenceError("SBOM: container subject is missing")
    if expected_image_id not in str(subject.get("purl") or ""):
        raise EvidenceError("SBOM: purl does not bind the image ID")
    if expected_image_id not in str(subject.get("bom-ref") or ""):
        raise EvidenceError("SBOM: bom-ref does not bind the image ID")
    properties = subject.get("properties")
    if not isinstance(properties, list):
        raise EvidenceError("SBOM: subject properties are missing")
    image_ids = {
        item.get("value")
        for item in properties
        if isinstance(item, dict) and item.get("name") == "aquasecurity:trivy:ImageID"
    }
    if image_ids != {expected_image_id}:
        raise EvidenceError("SBOM: ImageID property mismatch")
    sbom_layers = sorted(
        str(item["value"])
        for item in properties
        if isinstance(item, dict)
        and item.get("name") == "aquasecurity:trivy:DiffID"
        and "value" in item
    )
    rootfs = inspect_record.get("RootFS")
    inspect_layers = sorted(rootfs.get("Layers") or []) if isinstance(rootfs, dict) else []
    if not inspect_layers or sbom_layers != inspect_layers:
        raise EvidenceError("SBOM: filesystem DiffIDs do not match image inspect")
    return {"components": len(sbom["components"]), "layers": len(inspect_layers)}


def _verify_checksums(evidence_dir: Path) -> None:
    manifest = evidence_dir / "SHA256SUMS"
    if not manifest.is_file():
        raise EvidenceError("SHA256SUMS is missing")
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  \./([^/]+)", line)
        if match is None or match.group(2) == "SHA256SUMS":
            raise EvidenceError("SHA256SUMS contains an invalid entry")
        if match.group(2) in expected:
            raise EvidenceError("SHA256SUMS contains a duplicate entry")
        expected[match.group(2)] = match.group(1)
    actual_files = {
        path.name for path in evidence_dir.iterdir() if path.is_file() and path.name != "SHA256SUMS"
    }
    if set(expected) != actual_files:
        raise EvidenceError("SHA256SUMS file set does not match evidence directory")
    for name, digest in expected.items():
        if _sha256(evidence_dir / name) != digest:
            raise EvidenceError(f"SHA256SUMS mismatch: {name}")


def _verify_source(
    evidence_dir: Path,
    receipt: dict[str, Any],
    *,
    expected_head: str,
    repo_root: Path,
    scan_started_at: datetime,
    scan_finished_at: datetime,
) -> dict[str, Any]:
    source = receipt.get("source")
    if not isinstance(source, dict):
        raise EvidenceError("receipt.source is missing")
    archive = evidence_dir / "source-tracked.tar"
    if _sha256(archive) != source.get("archive_sha256"):
        raise EvidenceError("source archive hash mismatch")
    with tarfile.open(archive, "r:") as bundle:
        members = bundle.getmembers()
        if bundle.pax_headers.get("comment") != expected_head:
            raise EvidenceError("source archive is not bound to expected HEAD")
        if len(members) != source.get("tracked_members") or not members:
            raise EvidenceError("source archive member count mismatch")
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise EvidenceError("source archive contains an unsafe path")
    report = _load_json(evidence_dir / "source-tracked-trivy-secret.json")
    if report.get("ArtifactType") != "filesystem":
        raise EvidenceError("source secret report is not a filesystem scan")
    if report.get("ArtifactName") != source.get("scan_artifact_name"):
        raise EvidenceError("source secret report subject mismatch")
    results = _meaningful_results(report, field="source-secret")
    _verify_scan_time(
        report,
        field="source-secret",
        scan_started_at=scan_started_at,
        scan_finished_at=scan_finished_at,
    )
    secret_count = sum(len(result.get("Secrets") or []) for result in results)
    if secret_count:
        raise EvidenceError(f"tracked source secret gate failed: {secret_count}")

    git_files = (evidence_dir / "git-files.txt").read_text(encoding="utf-8")
    if not git_files.strip():
        raise EvidenceError("git-files.txt is empty")
    command = [
        "git",
        "-C",
        str(repo_root),
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--name-status",
        "-r",
        "--first-parent",
        expected_head,
    ]
    expected_git_files = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if git_files != expected_git_files or not expected_git_files.strip():
        raise EvidenceError("git-files.txt is stale or not merge-safe")
    tree = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", f"{expected_head}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if source.get("git_tree") != tree:
        raise EvidenceError("source git tree mismatch")
    return {"tracked_members": len(members), "secrets": 0, "git_tree": tree}


def _verify_image(
    evidence_dir: Path,
    receipt: dict[str, Any],
    *,
    name: str,
    expected_head: str,
    scanner_version: str,
    scan_started_at: datetime,
    scan_finished_at: datetime,
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    image_receipt = receipt.get("images", {}).get(name)
    if not isinstance(image_receipt, dict):
        raise EvidenceError(f"receipt.images.{name} is missing")
    image_id = _assert_sha256(image_receipt.get("image_id"), field=f"{name}.image_id")
    if image_receipt.get("subject_kind") != "local-oci-digest-not-registry-pushed":
        raise EvidenceError(f"{name}: local/registry subject kind is ambiguous")
    inspect_value = json.loads((evidence_dir / f"{name}-image-inspect.json").read_text())
    if not isinstance(inspect_value, list) or len(inspect_value) != 1:
        raise EvidenceError(f"{name}: image inspect must contain exactly one image")
    inspect_record = inspect_value[0]
    if inspect_record.get("Id") != image_id:
        raise EvidenceError(f"{name}: image inspect ID mismatch")
    if inspect_record.get("Architecture") != "arm64" or inspect_record.get("Os") != "linux":
        raise EvidenceError(f"{name}: image is not linux/arm64")
    config = inspect_record.get("Config")
    if not isinstance(config, dict):
        raise EvidenceError(f"{name}: image config is missing")
    labels = config.get("Labels")
    if (
        not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != expected_head
    ):
        raise EvidenceError(f"{name}: OCI revision mismatch")
    expected_kind = "core" if name == "core" else "media-worker"
    if labels.get("io.teamagent.runtime.kind") != expected_kind:
        raise EvidenceError(f"{name}: runtime kind mismatch")
    if config.get("User") != "10001:10001" or config.get("Volumes") != {"/tmp": {}}:
        raise EvidenceError(f"{name}: user or writable volume contract mismatch")
    repo_digests = inspect_record.get("RepoDigests")
    if not isinstance(repo_digests, list) or not any(
        isinstance(item, str) and item.endswith(f"@{image_id}") for item in repo_digests
    ):
        raise EvidenceError(f"{name}: immutable local repo digest missing")
    if sorted(repo_digests) != sorted(image_receipt.get("local_repo_digests") or []):
        raise EvidenceError(f"{name}: receipt repo digests mismatch")

    scan = verify_trivy_pair(
        _load_json(evidence_dir / f"{name}-trivy-vulnerability.json"),
        _load_json(evidence_dir / f"{name}-trivy-secret.json"),
        expected_image_id=image_id,
        expected_artifact_name=image_id,
        scan_started_at=scan_started_at,
        scan_finished_at=scan_finished_at,
    )
    if scan["artifact_id"] != image_receipt.get("artifact_id"):
        raise EvidenceError(f"{name}: receipt ArtifactID mismatch")
    if image_receipt.get("trivy_zero") != {
        "critical": scan["critical"],
        "high": scan["high"],
        "secrets": scan["secrets"],
    }:
        raise EvidenceError(f"{name}: receipt Trivy counts mismatch")
    if image_receipt.get("explicitly_absent_live_cves") != scan["explicitly_absent_live_cves"]:
        raise EvidenceError(f"{name}: receipt live-CVE assertion mismatch")
    sbom = verify_sbom(
        _load_json(evidence_dir / f"{name}-sbom.cdx.json"),
        inspect_record,
        expected_image_id=image_id,
        scanner_version=scanner_version,
    )
    metadata = _load_json(evidence_dir / f"{name}-build-metadata.json")
    if metadata.get("containerimage.digest") != image_id:
        raise EvidenceError(f"{name}: build output digest mismatch")
    descriptor = metadata.get("containerimage.descriptor")
    if not isinstance(descriptor, dict) or descriptor.get("digest") != image_id:
        raise EvidenceError(f"{name}: build descriptor digest mismatch")
    provenance = metadata.get("buildx.build.provenance")
    invocation = provenance.get("invocation") if isinstance(provenance, dict) else None
    parameters = invocation.get("parameters") if isinstance(invocation, dict) else None
    arguments = parameters.get("args") if isinstance(parameters, dict) else None
    environment = invocation.get("environment") if isinstance(invocation, dict) else None
    if not isinstance(arguments, dict) or arguments.get("build-arg:GIT_COMMIT") != expected_head:
        raise EvidenceError(f"{name}: provenance revision mismatch")
    if not isinstance(environment, dict) or environment.get("platform") != "linux/arm64":
        raise EvidenceError(f"{name}: provenance platform mismatch")
    expected_dockerfile = (
        "Dockerfile.teamagent-mcp" if name == "core" else "Dockerfile.teamagent-media-worker"
    )
    config_source = invocation.get("configSource") if isinstance(invocation, dict) else None
    if (
        not isinstance(config_source, dict)
        or config_source.get("entryPoint") != expected_dockerfile
    ):
        raise EvidenceError(f"{name}: provenance Dockerfile mismatch")
    materials = provenance.get("materials") if isinstance(provenance, dict) else None
    material_digests = {
        item.get("digest", {}).get("sha256")
        for item in materials or []
        if isinstance(item, dict) and isinstance(item.get("digest"), dict)
    }
    parent_digests = image_receipt.get("parent_registry_child_digests")
    if not isinstance(parent_digests, dict) or not parent_digests:
        raise EvidenceError(f"{name}: parent registry child digests are missing")
    actual_parent_digests = {
        key: value
        for key, value in labels.items()
        if key.startswith("io.teamagent.contract.") and key.endswith("-arm64-digest")
    }
    if parent_digests != actual_parent_digests:
        raise EvidenceError(f"{name}: parent digest receipt does not match image labels")
    for digest in parent_digests.values():
        normalized = _assert_sha256(digest, field=f"{name}.parent_digest").removeprefix("sha256:")
        if normalized not in material_digests:
            raise EvidenceError(f"{name}: parent digest is not in provenance materials")

    if name == "core":
        app_contract = runtime_contract["tasks"]["teamagent-mcp-core"]["app_html_contract"]
        label_mapping = {
            "production_source": "io.teamagent.contract.app-html-source",
            "production_sha256": "io.teamagent.contract.app-html-sha256",
            "production_s3_version_id": "io.teamagent.contract.app-html-version-id",
            "production_manifest_sha256": "io.teamagent.contract.app-html-manifest-sha256",
            "production_build_inputs_sha256": "io.teamagent.contract.app-html-build-inputs-sha256",
            "baked_fallback_sha256": "io.teamagent.contract.baked-app-html-sha256",
        }
        for contract_name, label_name in label_mapping.items():
            if labels.get(label_name) != app_contract[contract_name]:
                raise EvidenceError(f"core: app contract mismatch for {contract_name}")

    return {
        "image_id": image_id,
        "local_repo_digests": sorted(repo_digests),
        "trivy": scan,
        "sbom": sbom,
        "parent_registry_child_digests": parent_digests,
    }


def verify_evidence(
    evidence_dir: Path,
    *,
    expected_head: str,
    repo_root: Path,
    verify_checksums: bool = True,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        raise EvidenceError("expected HEAD must be full 40-hex")
    if verify_checksums:
        _verify_checksums(evidence_dir)
    receipt = _load_json(evidence_dir / "receipt.json")
    if receipt.get("schema_version") != "1" or receipt.get("git", {}).get("head") != expected_head:
        raise EvidenceError("receipt is stale for expected HEAD")
    current_head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if current_head != expected_head:
        raise EvidenceError("repository HEAD differs from receipt")
    if subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout:
        raise EvidenceError("repository must be clean when evidence is verified")
    window = receipt.get("scan_window")
    if not isinstance(window, dict):
        raise EvidenceError("receipt.scan_window is missing")
    started = _timestamp(window.get("started_at"), field="scan_window.started_at")
    finished = _timestamp(window.get("finished_at"), field="scan_window.finished_at")
    generated = _timestamp(receipt.get("generated_at"), field="receipt.generated_at")
    if not started < finished <= generated:
        raise EvidenceError("receipt scan timestamps are inconsistent")
    now = datetime.now(UTC)
    if generated > now + timedelta(minutes=5) or now - generated > timedelta(hours=24):
        raise EvidenceError("receipt is outside the accepted freshness window")
    scanner = verify_scanner_snapshot(
        _load_json(evidence_dir / "trivy-version.json"),
        scan_started_at=started,
        scan_finished_at=finished,
    )
    if scanner != receipt.get("scanner"):
        raise EvidenceError("receipt scanner status mismatch")
    runtime_contract = _load_json(repo_root / "infra/docker/runtime-contract.json")
    required_live_cves = (
        runtime_contract.get("security_gates", {}).get("trivy", {}).get("required_absent_live_cves")
    )
    if required_live_cves != list(_LIVE_CRITICAL_CVES):
        raise EvidenceError("runtime contract live-CVE gate differs from verifier")
    expected_external_gates = {
        "ecr_basic_scan": "NOT_RUN_LOCAL_PUSH_PROHIBITED",
        "fargate_runtime_smoke": "NOT_RUN_LOCAL_AWS_ACCESS_PROHIBITED",
    }
    if receipt.get("external_gates") != expected_external_gates:
        raise EvidenceError("receipt external gate status is ambiguous")
    source = _verify_source(
        evidence_dir,
        receipt,
        expected_head=expected_head,
        repo_root=repo_root,
        scan_started_at=started,
        scan_finished_at=finished,
    )
    images = {
        name: _verify_image(
            evidence_dir,
            receipt,
            name=name,
            expected_head=expected_head,
            scanner_version=scanner["version"],
            scan_started_at=started,
            scan_finished_at=finished,
            runtime_contract=runtime_contract,
        )
        for name in ("core", "media")
    }
    smoke = (evidence_dir / "runtime-smokes.log").read_text(encoding="utf-8")
    required_smokes = {
        value["dynamic_service"]
        for group in ("core_image_consumers", "media_image_consumers")
        for value in _load_json(repo_root / "infra/docker/runtime-consumers.json")[group].values()
    }
    missing_smokes = sorted(
        service
        for service in required_smokes
        if f"runtime_composition service={service} " not in smoke
    )
    if missing_smokes:
        raise EvidenceError(f"runtime composition smokes missing: {missing_smokes}")
    return {
        "head": expected_head,
        "scanner": scanner,
        "source": source,
        "images": images,
        "runtime_composition_services": sorted(required_smokes),
        "ecr_basic_scan": "NOT_RUN_LOCAL_PUSH_PROHIBITED",
        "fargate_smoke": "NOT_RUN_LOCAL_AWS_ACCESS_PROHIBITED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence_dir", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--skip-checksums", action="store_true")
    args = parser.parse_args()
    try:
        summary = verify_evidence(
            args.evidence_dir.resolve(),
            expected_head=args.expected_head,
            repo_root=args.repo_root.resolve(),
            verify_checksums=not args.skip_checksums,
        )
    except (EvidenceError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
