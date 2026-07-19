#!/usr/bin/env python3
"""Verify that a runtime evidence directory is fresh and bound to exact images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_LIVE_CRITICAL_CVES = (
    "CVE-2026-5450",
    "CVE-2026-13221",
    "CVE-2026-12087",
    "CVE-2026-57433",
)
_GRYPE_VERSION = "0.112.0"
_GRYPE_SEVERITIES = ("unknown", "negligible", "low", "medium", "high", "critical")


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


def _tree_entry(mode: str, relative: str, body: bytes) -> bytes:
    content_digest = hashlib.sha256(body).hexdigest()
    return (
        f"{mode}\0{len(relative.encode('utf-8'))}\0{relative}\0{len(body)}\0{content_digest}\n"
    ).encode()


def _tree_digest(root: Path) -> tuple[str, int]:
    """Hash the exact regular-file/symlink build context with Git-like modes."""

    root = root.resolve()
    digest = hashlib.sha256()
    count = 0
    paths = sorted(
        (path for path in root.rglob("*") if not stat.S_ISDIR(path.lstat().st_mode)),
        key=lambda path: path.relative_to(root).as_posix().encode(),
    )
    for path in paths:
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            mode = "120000"
            body = os.readlink(path).encode()
        elif stat.S_ISREG(metadata.st_mode):
            mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
            body = path.read_bytes()
        else:
            raise EvidenceError(f"source context has unsupported entry: {relative}")
        digest.update(_tree_entry(mode, relative, body))
        count += 1
    if count == 0:
        raise EvidenceError("source context is empty")
    return digest.hexdigest(), count


def _archive_inventory(
    archive_path: Path,
) -> tuple[str, dict[str, tuple[str, int, str]], int]:
    """Return a canonical tree digest and inventory for a retained tar context."""

    inventory: dict[str, tuple[str, int, str]] = {}
    member_count = 0
    with tarfile.open(archive_path, "r:") as bundle:
        for member in bundle.getmembers():
            member_count += 1
            relative = member.name.removeprefix("./").rstrip("/")
            if not relative:
                if not member.isdir():
                    raise EvidenceError(f"{archive_path.name}: invalid root member")
                continue
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise EvidenceError(f"{archive_path.name}: unsafe archive path")
            if member.isdir():
                continue
            if relative in inventory:
                raise EvidenceError(f"{archive_path.name}: duplicate archive path")
            if member.issym():
                target = member.linkname
                target_path = Path(target)
                if target_path.is_absolute() or ".." in target_path.parts:
                    raise EvidenceError(f"{archive_path.name}: unsafe symlink target")
                mode = "120000"
                body = target.encode()
            elif member.isfile():
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise EvidenceError(f"{archive_path.name}: unreadable regular file")
                body = extracted.read()
                mode = "100755" if member.mode & stat.S_IXUSR else "100644"
            else:
                raise EvidenceError(f"{archive_path.name}: unsupported archive member")
            inventory[relative] = (mode, len(body), hashlib.sha256(body).hexdigest())
    if not inventory:
        raise EvidenceError(f"{archive_path.name}: archive has no files")
    digest = hashlib.sha256()
    for relative in sorted(inventory, key=lambda value: value.encode()):
        mode, size, content_digest = inventory[relative]
        digest.update(
            (
                f"{mode}\0{len(relative.encode('utf-8'))}\0{relative}\0{size}\0{content_digest}\n"
            ).encode()
        )
    return digest.hexdigest(), inventory, member_count


def _verify_canonical_context_archive(archive_path: Path) -> None:
    """Reject host metadata, xattrs, unstable order, and noncanonical modes."""

    with tarfile.open(archive_path, "r:") as bundle:
        if bundle.pax_headers:
            raise EvidenceError(f"{archive_path.name}: global PAX metadata is forbidden")
        members = bundle.getmembers()
    names = [member.name for member in members]
    if names != sorted(names, key=lambda value: value.encode("utf-8")):
        raise EvidenceError(f"{archive_path.name}: members are not bytewise ordered")
    if len(names) != len(set(names)):
        raise EvidenceError(f"{archive_path.name}: duplicate canonical member")
    for member in members:
        if (
            member.uid != 0
            or member.gid != 0
            or member.uname != "root"
            or member.gname != "root"
            or member.mtime != 0
            or member.pax_headers
        ):
            raise EvidenceError(f"{archive_path.name}: noncanonical member metadata")
        if member.isdir():
            expected_mode = 0o755
        elif member.issym():
            expected_mode = 0o777
        elif member.isfile():
            expected_mode = 0o755 if member.mode & stat.S_IXUSR else 0o644
        else:
            raise EvidenceError(f"{archive_path.name}: unsupported canonical member")
        if member.mode != expected_mode:
            raise EvidenceError(f"{archive_path.name}: noncanonical member mode")


def _git_object_id(kind: str, body: bytes) -> bytes:
    framed = f"{kind} {len(body)}\0".encode() + body
    return hashlib.sha1(framed, usedforsecurity=False).digest()


def _archive_git_tree_oid(archive_path: Path) -> str:
    """Recompute the Git tree OID represented by a retained source archive."""

    root: dict[str, Any] = {}
    with tarfile.open(archive_path, "r:") as bundle:
        for member in bundle.getmembers():
            relative = member.name.removeprefix("./").rstrip("/")
            if not relative or member.isdir():
                continue
            parts = relative.split("/")
            if (
                relative.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
                or member.islnk()
            ):
                raise EvidenceError(f"{archive_path.name}: unsafe Git tree member")
            node = root
            for part in parts[:-1]:
                existing = node.setdefault(part, {})
                if not isinstance(existing, dict):
                    raise EvidenceError(f"{archive_path.name}: file/directory path conflict")
                node = existing
            name = parts[-1]
            if name in node:
                raise EvidenceError(f"{archive_path.name}: duplicate Git tree path")
            if member.issym():
                mode = b"120000"
                body = member.linkname.encode()
            elif member.isfile():
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise EvidenceError(f"{archive_path.name}: unreadable Git tree member")
                body = extracted.read()
                mode = b"100755" if member.mode & stat.S_IXUSR else b"100644"
            else:
                raise EvidenceError(f"{archive_path.name}: unsupported Git tree member")
            node[name] = (mode, _git_object_id("blob", body))

    def tree_id(node: dict[str, Any]) -> bytes:
        entries: list[tuple[bytes, bool, bytes, bytes]] = []
        for name, value in node.items():
            encoded_name = name.encode()
            if isinstance(value, dict):
                entries.append((encoded_name, True, b"40000", tree_id(value)))
            else:
                mode, object_id = value
                entries.append((encoded_name, False, mode, object_id))
        entries.sort(key=lambda item: item[0] + (b"/" if item[1] else b"\0"))
        body = b"".join(
            mode + b" " + name + b"\0" + object_id
            for name, _is_directory, mode, object_id in entries
        )
        return _git_object_id("tree", body)

    if not root:
        raise EvidenceError(f"{archive_path.name}: archive has no Git tree entries")
    return tree_id(root).hex()


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
    counts = {
        "unknown": 0,
        "low": 0,
        "medium": 0,
        "high": 0,
        "critical": 0,
        "secrets": 0,
    }
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
                severity_key = severity.lower()
                if severity_key not in {"unknown", "low", "medium", "high", "critical"}:
                    raise EvidenceError(f"{kind}: unsupported vulnerability severity")
                counts[severity_key] += 1
            secrets = result.get("Secrets") or []
            if not isinstance(secrets, list):
                raise EvidenceError(f"{kind}: malformed secret findings")
            counts["secrets"] += len(secrets)

    if len(artifact_ids) != 1:
        raise EvidenceError("vulnerability and secret reports describe different artifacts")
    if any(counts.values()):
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


def verify_retained_scan_summary(
    retained: dict[str, Any],
    recomputed: dict[str, Any],
    *,
    name: str,
) -> None:
    if retained != recomputed:
        raise EvidenceError(f"{name}: retained Trivy summary differs from recomputed scan")


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


def _grype_database_summary(
    status: dict[str, Any],
    *,
    scan_finished_at: datetime,
) -> dict[str, Any]:
    schema = status.get("schemaVersion")
    if not isinstance(schema, str) or re.fullmatch(r"v6\.\d+\.\d+", schema) is None:
        raise EvidenceError("Grype database schema is invalid")
    if status.get("valid") is not True:
        raise EvidenceError("Grype database is not valid")
    source = status.get("from")
    path = status.get("path")
    if not isinstance(source, str) or not source or not isinstance(path, str) or not path:
        raise EvidenceError("Grype database source/path is missing")
    built = _timestamp(status.get("built"), field="Grype database built")
    if built > scan_finished_at + timedelta(seconds=5):
        raise EvidenceError("Grype database build time is in the future")
    if scan_finished_at - built > timedelta(days=5):
        raise EvidenceError("Grype database is older than the accepted five-day window")
    return {
        "schema_version": schema,
        "built_at": built.isoformat().replace("+00:00", "Z"),
        "source": source,
        "valid": True,
    }


def verify_grype_scanner_snapshot(
    version_report: dict[str, Any],
    database_status: dict[str, Any],
    *,
    binary_sha256: str,
    scan_finished_at: datetime,
) -> dict[str, Any]:
    if (
        version_report.get("application") != "grype"
        or version_report.get("version") != _GRYPE_VERSION
    ):
        raise EvidenceError(f"Grype scanner must be exactly {_GRYPE_VERSION}")
    platform = version_report.get("platform")
    if not isinstance(platform, str) or re.fullmatch(r"(?:darwin|linux)/arm64", platform) is None:
        raise EvidenceError("Grype scanner is not an ARM64 binary")
    git_commit = version_report.get("gitCommit")
    if not isinstance(git_commit, str) or re.fullmatch(r"[0-9a-f]{40}", git_commit) is None:
        raise EvidenceError("Grype scanner Git commit is invalid")
    syft_version = version_report.get("syftVersion")
    if not isinstance(syft_version, str) or re.fullmatch(r"v\d+\.\d+\.\d+", syft_version) is None:
        raise EvidenceError("Grype embedded Syft version is invalid")
    if version_report.get("supportedDbSchema") != 6:
        raise EvidenceError("Grype supported database schema is unexpected")
    _timestamp(version_report.get("buildDate"), field="Grype build date")
    if re.fullmatch(r"[0-9a-f]{64}", binary_sha256) is None:
        raise EvidenceError("Grype binary SHA-256 is invalid")
    return {
        "version": _GRYPE_VERSION,
        "git_commit": git_commit,
        "platform": platform,
        "syft_version": syft_version,
        "supported_db_schema": 6,
        "binary_sha256": binary_sha256,
        "database": _grype_database_summary(
            database_status,
            scan_finished_at=scan_finished_at,
        ),
    }


def verify_grype_report(
    report: dict[str, Any],
    *,
    expected_image_id: str,
    expected_head: str,
    scanner: dict[str, Any],
    scan_started_at: datetime,
    scan_finished_at: datetime,
) -> dict[str, Any]:
    """Bind a complete unsuppressed Grype report to one immutable local image."""

    _assert_sha256(expected_image_id, field="Grype expected image ID")
    descriptor = report.get("descriptor")
    if not isinstance(descriptor, dict):
        raise EvidenceError("Grype descriptor is missing")
    if descriptor.get("name") != "grype" or descriptor.get("version") != scanner["version"]:
        raise EvidenceError("Grype report scanner version mismatch")
    created = _timestamp(descriptor.get("timestamp"), field="Grype descriptor timestamp")
    tolerance = timedelta(seconds=5)
    if created < scan_started_at - tolerance or created > scan_finished_at + tolerance:
        raise EvidenceError("Grype report timestamp is outside this receipt window")
    configuration = descriptor.get("configuration")
    if not isinstance(configuration, dict):
        raise EvidenceError("Grype report configuration is missing")
    database_config = configuration.get("db")
    if (
        configuration.get("show-suppressed") is not True
        or not isinstance(database_config, dict)
        or database_config.get("auto-update") is not False
        or database_config.get("validate-by-hash-on-start") is not True
        or database_config.get("validate-age") is not True
    ):
        raise EvidenceError("Grype report was not produced with the fail-closed scan config")
    descriptor_database = descriptor.get("db")
    descriptor_status = (
        descriptor_database.get("status") if isinstance(descriptor_database, dict) else None
    )
    if not isinstance(descriptor_status, dict):
        raise EvidenceError("Grype report database status is missing")
    if (
        _grype_database_summary(
            descriptor_status,
            scan_finished_at=scan_finished_at,
        )
        != scanner["database"]
    ):
        raise EvidenceError("Grype report database differs from the retained scanner snapshot")

    source = report.get("source")
    target = source.get("target") if isinstance(source, dict) else None
    if not isinstance(target, dict) or source.get("type") != "image":
        raise EvidenceError("Grype report source is not a container image")
    if target.get("userInput") != expected_image_id:
        raise EvidenceError("Grype report input is not the immutable image ID")
    repo_digests = target.get("repoDigests")
    if not isinstance(repo_digests, list) or not any(
        isinstance(value, str) and value.endswith(f"@{expected_image_id}") for value in repo_digests
    ):
        raise EvidenceError("Grype report repository digest does not bind the image ID")
    if target.get("architecture") != "arm64" or target.get("os") != "linux":
        raise EvidenceError("Grype report target is not linux/arm64")
    labels = target.get("labels")
    if (
        not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != expected_head
    ):
        raise EvidenceError("Grype report target revision mismatch")

    ignored = report.get("ignoredMatches")
    if ignored is not None and (not isinstance(ignored, list) or ignored):
        raise EvidenceError("Grype report contains suppressed findings")
    matches = report.get("matches")
    if not isinstance(matches, list):
        raise EvidenceError("Grype report matches are missing")
    counts = {severity: 0 for severity in _GRYPE_SEVERITIES}
    fixed_available = 0
    identities: set[tuple[str, str, str]] = set()
    for index, match in enumerate(matches):
        if not isinstance(match, dict):
            raise EvidenceError(f"Grype match {index} is malformed")
        vulnerability = match.get("vulnerability")
        artifact = match.get("artifact")
        if not isinstance(vulnerability, dict) or not isinstance(artifact, dict):
            raise EvidenceError(f"Grype match {index} lacks vulnerability/package data")
        advisory = vulnerability.get("id")
        namespace = vulnerability.get("namespace")
        data_source = vulnerability.get("dataSource")
        if (
            not isinstance(advisory, str)
            or not advisory
            or not isinstance(namespace, str)
            or not namespace
            or not isinstance(data_source, str)
            or not data_source.startswith("https://")
        ):
            raise EvidenceError(f"Grype match {index} lacks advisory provenance")
        severity = str(vulnerability.get("severity") or "").lower()
        if severity not in counts:
            raise EvidenceError(f"Grype match {index} has unsupported severity")
        name = artifact.get("name")
        version = artifact.get("version")
        purl = artifact.get("purl")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
            or not isinstance(purl, str)
            or not purl
        ):
            raise EvidenceError(f"Grype match {index} lacks exact package identity")
        _verify_purl_version(purl, version, field=f"Grype match {index}")
        identity = (advisory, namespace, purl)
        if identity in identities:
            raise EvidenceError(f"Grype report contains duplicate match identity: {identity}")
        identities.add(identity)
        fix = vulnerability.get("fix")
        if not isinstance(fix, dict):
            raise EvidenceError(f"Grype match {index} lacks fix availability")
        fix_versions = fix.get("versions")
        fix_state = fix.get("state")
        if not isinstance(fix_versions, list) or not all(
            isinstance(value, str) and value for value in fix_versions
        ):
            raise EvidenceError(f"Grype match {index} has malformed fix versions")
        if not isinstance(fix_state, str):
            raise EvidenceError(f"Grype match {index} has malformed fix state")
        if fix_versions:
            fixed_available += 1
        counts[severity] += 1
    return {
        "total": len(matches),
        **counts,
        "fixed_available": fixed_available,
        "suppressed": 0,
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }


def _scan_package_inventory(
    report: dict[str, Any],
) -> Counter[tuple[str, ...]]:
    identities: Counter[tuple[str, ...]] = Counter()
    for result in _meaningful_results(report, field="SBOM-scan"):
        for index, package in enumerate(result["Packages"]):
            if not isinstance(package, dict):
                raise EvidenceError(f"SBOM-scan package {index}: expected object")
            name = package.get("Name")
            version = package.get("Version")
            if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
                raise EvidenceError("SBOM-scan package lacks name/version identity")
            identifier = package.get("Identifier")
            purl = identifier.get("PURL") if isinstance(identifier, dict) else None
            if isinstance(purl, str) and purl:
                _verify_purl_version(
                    purl,
                    version,
                    field=f"SBOM-scan package {index}",
                )
                identities[("purl", purl)] += 1
            else:
                identities[("name-version", name, version)] += 1
    if not identities:
        raise EvidenceError("SBOM-scan package inventory is empty")
    return identities


def _verify_purl_version(purl: str, version: str, *, field: str) -> None:
    """Reject internally inconsistent package records before comparing inventories."""
    package_path = purl.split("#", 1)[0].split("?", 1)[0]
    separator = package_path.rfind("@")
    if (
        not package_path.startswith("pkg:")
        or separator <= len("pkg:")
        or separator == len(package_path) - 1
    ):
        raise EvidenceError(f"{field}: package purl lacks a version")
    if unquote(package_path[separator + 1 :]) != version:
        raise EvidenceError(f"{field}: package purl version disagrees with package version")


def _sbom_package_inventory(
    components: list[Any],
) -> Counter[tuple[str, ...]]:
    identities: Counter[tuple[str, ...]] = Counter()
    excluded_types = {"container", "device", "file", "operating-system"}
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise EvidenceError(f"SBOM.components[{index}]: expected object")
        if component.get("type") in excluded_types:
            continue
        name = component.get("name")
        version = component.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise EvidenceError(f"SBOM.components[{index}]: package lacks name/version identity")
        purl = component.get("purl")
        if isinstance(purl, str) and purl:
            _verify_purl_version(
                purl,
                version,
                field=f"SBOM.components[{index}]",
            )
            identities[("purl", purl)] += 1
        else:
            identities[("name-version", name, version)] += 1
    if not identities:
        raise EvidenceError("SBOM: package inventory is empty")
    return identities


def verify_sbom(
    sbom: dict[str, Any],
    inspect_record: dict[str, Any],
    scan_report: dict[str, Any],
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
    scan_packages = _scan_package_inventory(scan_report)
    sbom_packages = _sbom_package_inventory(sbom["components"])
    missing = sorted((scan_packages - sbom_packages).elements())
    extra = sorted((sbom_packages - scan_packages).elements())
    if missing or extra:
        raise EvidenceError(
            f"SBOM: package inventory differs from scan (missing={missing[:3]}, extra={extra[:3]})"
        )
    return {
        "components": len(sbom["components"]),
        "layers": len(inspect_layers),
        "packages_reconciled": sum(scan_packages.values()),
    }


def _verify_checksums(evidence_dir: Path) -> str:
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
        path.name
        for path in evidence_dir.iterdir()
        if path.is_file() and path.name not in {"SHA256SUMS", "FINAL_VERIFICATION.json"}
    }
    if set(expected) != actual_files:
        raise EvidenceError("SHA256SUMS file set does not match evidence directory")
    for name, digest in expected.items():
        if _sha256(evidence_dir / name) != digest:
            raise EvidenceError(f"SHA256SUMS mismatch: {name}")
    return _sha256(manifest)


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
        if bundle.pax_headers.get("comment") != expected_head:
            raise EvidenceError("source archive is not bound to expected HEAD")
    source_tree_digest, source_inventory, source_members = _archive_inventory(archive)
    if source_tree_digest != source.get("source_tree_sha256") or source_members != source.get(
        "tracked_members"
    ):
        raise EvidenceError("source archive tree digest/member count mismatch")
    if _archive_git_tree_oid(archive) != source.get("git_tree"):
        raise EvidenceError("source archive does not represent the claimed Git tree")

    context = source.get("build_context")
    if not isinstance(context, dict):
        raise EvidenceError("receipt source build context is missing")
    context_archive = evidence_dir / "build-context.tar"
    if _sha256(context_archive) != context.get("archive_sha256"):
        raise EvidenceError("build context archive hash mismatch")
    _verify_canonical_context_archive(context_archive)
    context_tree_digest, context_inventory, context_members = _archive_inventory(context_archive)
    if context_tree_digest != context.get("tree_sha256") or context_members != context.get(
        "members"
    ):
        raise EvidenceError("build context tree digest/member count mismatch")
    fixture_path = "infra/docker/app-html-runtime-fixture.html"
    baked_path = "src/teamagent/connect_web/static/app.html"
    fixture = source_inventory.get(fixture_path)
    if fixture is None:
        raise EvidenceError("tracked app HTML fixture is missing")
    expected_context = dict(source_inventory)
    expected_context[baked_path] = fixture
    if context_inventory != expected_context:
        raise EvidenceError("build context differs from the exact tracked tree materialization")
    if context.get("materialized_baked_app_html") != {
        "source": fixture_path,
        "destination": baked_path,
        "sha256": fixture[2],
    }:
        raise EvidenceError("build context app HTML materialization receipt mismatch")

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

    git_receipt = receipt.get("git")
    if not isinstance(git_receipt, dict):
        raise EvidenceError("receipt.git is missing")
    review_base_ref = git_receipt.get("review_base_ref")
    review_base_oid = git_receipt.get("review_base_oid")
    merge_base_oid = git_receipt.get("merge_base_oid")
    if not isinstance(review_base_ref, str) or not review_base_ref:
        raise EvidenceError("review base ref is missing")
    for field, value in (
        ("review_base_oid", review_base_oid),
        ("merge_base_oid", merge_base_oid),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise EvidenceError(f"{field} is invalid")
    subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{review_base_oid}^{{commit}}"],
        check=True,
        capture_output=True,
    )
    actual_merge_base = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", review_base_oid, expected_head],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_merge_base != merge_base_oid:
        raise EvidenceError("reviewed merge-base OID mismatch")
    review_record = (evidence_dir / "git-review-base.txt").read_text(encoding="utf-8")
    expected_review_record = (
        f"review_base_ref={review_base_ref}\n"
        f"review_base_oid={review_base_oid}\n"
        f"merge_base_oid={merge_base_oid}\n"
    )
    if review_record != expected_review_record:
        raise EvidenceError("review base record differs from receipt")
    reviewed_changes = (evidence_dir / "git-base-head-files.txt").read_text(encoding="utf-8")
    expected_reviewed_changes = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--name-status",
            "--find-renames",
            f"{review_base_oid}...{expected_head}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if reviewed_changes != expected_reviewed_changes:
        raise EvidenceError("base...HEAD change list is stale or incomplete")

    tree = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", f"{expected_head}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if source.get("git_tree") != tree:
        raise EvidenceError("source git tree mismatch")
    return {
        "tracked_members": source_members,
        "source_tree_sha256": source_tree_digest,
        "context_tree_sha256": context_tree_digest,
        "secrets": 0,
        "git_tree": tree,
        "review_base_oid": review_base_oid,
        "merge_base_oid": merge_base_oid,
        "base_head_changes": len(reviewed_changes.splitlines()),
    }


def _verify_image(
    evidence_dir: Path,
    receipt: dict[str, Any],
    *,
    name: str,
    expected_head: str,
    expected_branch: str,
    scanner_version: str,
    grype_scanner: dict[str, Any],
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
    if labels.get("org.opencontainers.image.ref.name") != expected_branch:
        raise EvidenceError(f"{name}: OCI branch mismatch")
    context_sha256 = receipt.get("source", {}).get("build_context", {}).get("archive_sha256")
    if labels.get("io.teamagent.build.context-sha256") != context_sha256:
        raise EvidenceError(f"{name}: build context label mismatch")
    if image_receipt.get("build_context_sha256") != context_sha256:
        raise EvidenceError(f"{name}: build context receipt mismatch")
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

    vulnerability_report = _load_json(evidence_dir / f"{name}-trivy-vulnerability.json")
    secret_report = _load_json(evidence_dir / f"{name}-trivy-secret.json")
    scan = verify_trivy_pair(
        vulnerability_report,
        secret_report,
        expected_image_id=image_id,
        expected_artifact_name=image_id,
        scan_started_at=scan_started_at,
        scan_finished_at=scan_finished_at,
    )
    if scan["artifact_id"] != image_receipt.get("artifact_id"):
        raise EvidenceError(f"{name}: receipt ArtifactID mismatch")
    if image_receipt.get("trivy_zero") != {
        "unknown": scan["unknown"],
        "low": scan["low"],
        "medium": scan["medium"],
        "critical": scan["critical"],
        "high": scan["high"],
        "secrets": scan["secrets"],
    }:
        raise EvidenceError(f"{name}: receipt Trivy counts mismatch")
    if image_receipt.get("explicitly_absent_live_cves") != scan["explicitly_absent_live_cves"]:
        raise EvidenceError(f"{name}: receipt live-CVE assertion mismatch")
    retained_summary = _load_json(evidence_dir / f"{name}-trivy-summary.json")
    verify_retained_scan_summary(retained_summary, scan, name=name)
    grype = verify_grype_report(
        _load_json(evidence_dir / f"{name}-grype-vulnerability.json"),
        expected_image_id=image_id,
        expected_head=expected_head,
        scanner=grype_scanner,
        scan_started_at=scan_started_at,
        scan_finished_at=scan_finished_at,
    )
    if image_receipt.get("grype") != grype:
        raise EvidenceError(f"{name}: receipt Grype summary mismatch")
    sbom = verify_sbom(
        _load_json(evidence_dir / f"{name}-sbom.cdx.json"),
        inspect_record,
        vulnerability_report,
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
    if arguments.get("build-arg:GIT_BRANCH") != expected_branch:
        raise EvidenceError(f"{name}: provenance branch mismatch")
    if arguments.get("build-arg:BUILD_CONTEXT_SHA256") != context_sha256:
        raise EvidenceError(f"{name}: provenance context digest mismatch")
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
        "grype": grype,
        "sbom": sbom,
        "parent_registry_child_digests": parent_digests,
    }


def verify_evidence(
    evidence_dir: Path,
    *,
    expected_head: str,
    repo_root: Path,
    expected_branch: str | None = None,
    verify_checksums: bool = True,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        raise EvidenceError("expected HEAD must be full 40-hex")
    checksum_digest = _verify_checksums(evidence_dir) if verify_checksums else None
    receipt = _load_json(evidence_dir / "receipt.json")
    if receipt.get("schema_version") != "3" or receipt.get("git", {}).get("head") != expected_head:
        raise EvidenceError("receipt is stale for expected HEAD")
    if receipt.get("evidence_scope") != "local-source-validation-only-not-release-credential":
        raise EvidenceError("local evidence scope is ambiguous")
    current_head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if current_head != expected_head:
        raise EvidenceError("repository HEAD differs from receipt")
    current_branch = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    resolved_branch = expected_branch if expected_branch is not None else current_branch
    if not resolved_branch or current_branch != resolved_branch:
        raise EvidenceError("repository branch differs from expected branch")
    if receipt.get("git", {}).get("branch") != resolved_branch:
        raise EvidenceError("receipt branch differs from expected branch")
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
    trivy_scanner = verify_scanner_snapshot(
        _load_json(evidence_dir / "trivy-version.json"),
        scan_started_at=started,
        scan_finished_at=finished,
    )
    try:
        grype_binary_sha256 = (
            (evidence_dir / "grype-binary-sha256.txt").read_text(encoding="utf-8").strip()
        )
    except OSError as exc:
        raise EvidenceError("Grype binary SHA-256 evidence is missing") from exc
    grype_scanner = verify_grype_scanner_snapshot(
        _load_json(evidence_dir / "grype-version.json"),
        _load_json(evidence_dir / "grype-db-status.json"),
        binary_sha256=grype_binary_sha256,
        scan_finished_at=finished,
    )
    scanners = {"trivy": trivy_scanner, "grype": grype_scanner}
    if scanners != receipt.get("scanners"):
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
    expected_materials = [
        {
            "uri": "file:build-context.tar",
            "digest": {
                "sha256": receipt.get("source", {}).get("build_context", {}).get("archive_sha256")
            },
        },
        {
            "uri": "git:source-tree",
            "digest": {
                "sha1": receipt.get("git", {}).get("head"),
                "gitTree": receipt.get("source", {}).get("git_tree"),
            },
        },
    ]
    if receipt.get("materials") != expected_materials:
        raise EvidenceError("receipt materials do not bind source and build context")
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
            expected_branch=resolved_branch,
            scanner_version=trivy_scanner["version"],
            grype_scanner=grype_scanner,
            scan_started_at=started,
            scan_finished_at=finished,
            runtime_contract=runtime_contract,
        )
        for name in ("core", "media")
    }
    smoke = (evidence_dir / "runtime-smokes.log").read_text(encoding="utf-8")
    consumers = [
        value
        for group in ("core_image_consumers", "media_image_consumers")
        for value in _load_json(repo_root / "infra/docker/runtime-consumers.json")[group].values()
    ]
    missing_smokes = sorted(
        value["dynamic_service"]
        for value in consumers
        if (
            f"runtime_composition service={value['dynamic_service']} " not in smoke
            or (
                f"runtime_security service={value['dynamic_service']} readonly=true "
                f"memory={value['memory_mib']}MiB "
            )
            not in smoke
        )
    )
    if missing_smokes:
        raise EvidenceError(f"runtime composition smokes missing: {missing_smokes}")
    required_smokes = {value["dynamic_service"] for value in consumers}
    return {
        "head": expected_head,
        "branch": resolved_branch,
        "scanners": scanners,
        "source": source,
        "images": images,
        "runtime_composition_services": sorted(required_smokes),
        "ecr_basic_scan": "NOT_RUN_LOCAL_PUSH_PROHIBITED",
        "fargate_smoke": "NOT_RUN_LOCAL_AWS_ACCESS_PROHIBITED",
        **({"sha256sums_sha256": checksum_digest} if checksum_digest is not None else {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence_dir", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--skip-checksums", action="store_true")
    args = parser.parse_args()
    try:
        summary = verify_evidence(
            args.evidence_dir.resolve(),
            expected_head=args.expected_head,
            expected_branch=args.expected_branch,
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
