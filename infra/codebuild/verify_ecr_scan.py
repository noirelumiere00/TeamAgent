#!/usr/bin/env python3
"""Fail-closed CRITICAL/HIGH gate for TeamAgent ECR scan findings.

An exception is valid only when CVE, severity, package, and installed version
all match.  Expired, duplicate, malformed, or stale exceptions fail the build;
stale entries must be removed as soon as an image no longer contains a finding.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
GATED_SEVERITIES = frozenset({"CRITICAL", "HIGH"})
KNOWN_SEVERITIES = frozenset({"INFORMATIONAL", "LOW", "MEDIUM", "HIGH", "CRITICAL", "UNDEFINED"})
_CVE_RE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


class GateError(ValueError):
    """The scan or exception policy cannot be accepted safely."""


@dataclass(frozen=True, order=True)
class FindingKey:
    cve: str
    severity: str
    package: str
    version: str

    def display(self) -> str:
        return f"{self.cve} {self.severity} {self.package} {self.version}"


@dataclass(frozen=True)
class ExceptionRecord:
    key: FindingKey
    owner: str
    reason: str
    expires_on: date


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, *, label: str) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GateError(f"cannot read {label}: {path}: {exc}") from exc
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise GateError(f"invalid {label} JSON: {exc}") from exc


def _exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise GateError(f"{label} schema mismatch: {'; '.join(details)}")


def _required_text(value: Any, *, field: str, minimum: int = 1) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) < minimum:
        raise GateError(f"{field} must be a non-blank, trimmed string")
    if any(ord(char) < 32 for char in value):
        raise GateError(f"{field} must not contain control characters")
    return value


def load_exceptions(path: Path, *, today: date) -> dict[FindingKey, ExceptionRecord]:
    """Load the strict exception registry and reject invalid policy state."""

    payload = _load_json(path, label="ECR exception policy")
    if not isinstance(payload, dict):
        raise GateError("ECR exception policy must be a JSON object")
    _exact_keys(
        payload,
        {"schema_version", "stale_exception_policy", "exceptions"},
        label="ECR exception policy",
    )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise GateError(f"unsupported ECR exception schema: {payload['schema_version']!r}")
    if payload["stale_exception_policy"] != "fail":
        raise GateError("stale_exception_policy must be exactly 'fail'")
    raw_exceptions = payload["exceptions"]
    if not isinstance(raw_exceptions, list):
        raise GateError("exceptions must be an array")

    records: dict[FindingKey, ExceptionRecord] = {}
    errors: list[str] = []
    expected_fields = {
        "cve",
        "severity",
        "package",
        "version",
        "owner",
        "reason",
        "expires_on",
    }
    for index, raw_exception in enumerate(raw_exceptions):
        label = f"exceptions[{index}]"
        try:
            if not isinstance(raw_exception, dict):
                raise GateError(f"{label} must be an object")
            _exact_keys(raw_exception, expected_fields, label=label)
            cve = _required_text(raw_exception["cve"], field=f"{label}.cve")
            severity = _required_text(raw_exception["severity"], field=f"{label}.severity")
            package = _required_text(raw_exception["package"], field=f"{label}.package")
            version = _required_text(raw_exception["version"], field=f"{label}.version")
            owner = _required_text(raw_exception["owner"], field=f"{label}.owner")
            reason = _required_text(raw_exception["reason"], field=f"{label}.reason", minimum=20)
            expires_text = _required_text(raw_exception["expires_on"], field=f"{label}.expires_on")
            if not _CVE_RE.fullmatch(cve):
                raise GateError(f"{label}.cve is not a canonical CVE ID")
            if severity not in GATED_SEVERITIES:
                raise GateError(f"{label}.severity must be CRITICAL or HIGH")
            try:
                expires_on = date.fromisoformat(expires_text)
            except ValueError as exc:
                raise GateError(f"{label}.expires_on must be YYYY-MM-DD") from exc
            key = FindingKey(cve, severity, package, version)
            if key in records:
                raise GateError(f"duplicate exception tuple: {key.display()}")
            if expires_on < today:
                raise GateError(f"expired exception ({expires_on.isoformat()}): {key.display()}")
            records[key] = ExceptionRecord(key, owner, reason, expires_on)
        except GateError as exc:
            errors.append(str(exc))
    if errors:
        raise GateError("invalid ECR exception policy:\n  - " + "\n  - ".join(errors))
    return records


def _scan_attribute_map(attributes: Any, *, label: str) -> dict[str, str]:
    if not isinstance(attributes, list):
        raise GateError(f"{label}.attributes must be an array")
    result: dict[str, str] = {}
    for index, attribute in enumerate(attributes):
        if not isinstance(attribute, dict):
            raise GateError(f"{label}.attributes[{index}] must be an object")
        key = attribute.get("key")
        value = attribute.get("value")
        if not isinstance(key, str) or not isinstance(value, str) or not key or not value:
            raise GateError(f"{label}.attributes[{index}] must contain string key/value")
        if key in result:
            raise GateError(f"{label} has duplicate attribute key: {key}")
        result[key] = value
    return result


def _parse_basic_findings(findings: list[Any]) -> tuple[list[FindingKey], Counter[str]]:
    gated: list[FindingKey] = []
    severities: Counter[str] = Counter()
    for index, finding in enumerate(findings):
        label = f"imageScanFindings.findings[{index}]"
        if not isinstance(finding, dict):
            raise GateError(f"{label} must be an object")
        severity = finding.get("severity")
        if severity not in KNOWN_SEVERITIES:
            raise GateError(f"{label}.severity is missing or unsupported: {severity!r}")
        severities[severity] += 1
        if severity not in GATED_SEVERITIES:
            continue
        cve = finding.get("name")
        if not isinstance(cve, str) or not _CVE_RE.fullmatch(cve):
            raise GateError(f"{label}.name is not a canonical CVE ID")
        attributes = _scan_attribute_map(finding.get("attributes"), label=label)
        package = attributes.get("package_name")
        version = attributes.get("package_version")
        if not package or not version:
            raise GateError(f"{label} is missing package_name/package_version")
        gated.append(FindingKey(cve, severity, package, version))
    return gated, severities


def _parse_enhanced_findings(findings: list[Any]) -> tuple[list[FindingKey], Counter[str]]:
    gated: list[FindingKey] = []
    severities: Counter[str] = Counter()
    for index, finding in enumerate(findings):
        label = f"imageScanFindings.enhancedFindings[{index}]"
        if not isinstance(finding, dict):
            raise GateError(f"{label} must be an object")
        severity = finding.get("severity")
        if severity not in KNOWN_SEVERITIES:
            raise GateError(f"{label}.severity is missing or unsupported: {severity!r}")
        severities[severity] += 1
        if severity not in GATED_SEVERITIES:
            continue
        if finding.get("status") != "ACTIVE":
            raise GateError(f"{label}.status must be ACTIVE for a gated finding")
        details = finding.get("packageVulnerabilityDetails")
        if not isinstance(details, dict):
            raise GateError(f"{label}.packageVulnerabilityDetails is missing")
        cve = details.get("vulnerabilityId")
        packages = details.get("vulnerablePackages")
        if not isinstance(cve, str) or not _CVE_RE.fullmatch(cve):
            raise GateError(f"{label}.vulnerabilityId is not a canonical CVE ID")
        if not isinstance(packages, list) or not packages:
            raise GateError(f"{label}.vulnerablePackages must be a non-empty array")
        for package_index, raw_package in enumerate(packages):
            package_label = f"{label}.vulnerablePackages[{package_index}]"
            if not isinstance(raw_package, dict):
                raise GateError(f"{package_label} must be an object")
            package = raw_package.get("name")
            version = raw_package.get("version")
            if not isinstance(package, str) or not package:
                raise GateError(f"{package_label}.name is missing")
            if not isinstance(version, str) or not version:
                raise GateError(f"{package_label}.version is missing")
            gated.append(FindingKey(cve, severity, package, version))
    return gated, severities


def parse_scan(
    path: Path,
    *,
    expected_image_digest: str,
    expected_repository: str,
) -> set[FindingKey]:
    """Validate a complete, untruncated ECR response and extract gated tuples."""

    if not _DIGEST_RE.fullmatch(expected_image_digest):
        raise GateError("expected image digest is invalid")
    if not expected_repository:
        raise GateError("expected repository is empty")
    payload = _load_json(path, label="ECR scan response")
    if not isinstance(payload, dict):
        raise GateError("ECR scan response must be an object")
    required = {
        "registryId",
        "repositoryName",
        "imageId",
        "imageScanStatus",
        "imageScanFindings",
    }
    allowed = required | {"nextToken"}
    missing = sorted(required - payload.keys())
    unknown = sorted(payload.keys() - allowed)
    if missing or unknown:
        raise GateError(f"ECR scan response schema mismatch: missing={missing}; unknown={unknown}")
    if payload.get("nextToken") not in {None, ""}:
        raise GateError("ECR scan response is truncated (nextToken is present)")
    if payload["repositoryName"] != expected_repository:
        raise GateError("ECR scan response repository does not match the requested repository")
    image_id = payload["imageId"]
    if not isinstance(image_id, dict) or image_id.get("imageDigest") != expected_image_digest:
        raise GateError("ECR scan response digest does not match the pushed image")
    status = payload["imageScanStatus"]
    if not isinstance(status, dict) or status.get("status") != "COMPLETE":
        actual = status.get("status") if isinstance(status, dict) else None
        raise GateError(f"ECR image scan is not COMPLETE: {actual!r}")

    scan_findings = payload["imageScanFindings"]
    if not isinstance(scan_findings, dict):
        raise GateError("imageScanFindings must be an object")
    allowed_finding_keys = {
        "imageScanCompletedAt",
        "vulnerabilitySourceUpdatedAt",
        "findingSeverityCounts",
        "findings",
        "enhancedFindings",
    }
    unknown_finding_keys = sorted(scan_findings.keys() - allowed_finding_keys)
    if unknown_finding_keys:
        raise GateError(f"unknown imageScanFindings fields: {unknown_finding_keys}")
    counts = scan_findings.get("findingSeverityCounts")
    if not isinstance(counts, dict):
        raise GateError("findingSeverityCounts must be an object")
    for severity, count in counts.items():
        invalid_count = not isinstance(count, int) or isinstance(count, bool)
        if severity not in KNOWN_SEVERITIES or invalid_count:
            raise GateError(f"invalid findingSeverityCounts entry: {severity!r}={count!r}")

    basic = scan_findings.get("findings", [])
    enhanced = scan_findings.get("enhancedFindings", [])
    if not isinstance(basic, list) or not isinstance(enhanced, list):
        raise GateError("findings and enhancedFindings must be arrays when present")
    if basic and enhanced:
        raise GateError("scan response cannot mix basic and enhanced findings")
    if enhanced:
        gated, observed_counts = _parse_enhanced_findings(enhanced)
    else:
        gated, observed_counts = _parse_basic_findings(basic)
    for severity in GATED_SEVERITIES:
        if counts.get(severity, 0) != observed_counts.get(severity, 0):
            raise GateError(
                f"{severity} count mismatch: summary={counts.get(severity, 0)}, "
                f"entries={observed_counts.get(severity, 0)}"
            )
    if len(gated) != len(set(gated)):
        duplicates = sorted(key for key, count in Counter(gated).items() if count > 1)
        raise GateError(
            "duplicate gated scan finding tuple(s): "
            + ", ".join(key.display() for key in duplicates)
        )
    return set(gated)


def evaluate_gate(
    findings: set[FindingKey],
    exceptions: dict[FindingKey, ExceptionRecord],
) -> None:
    """Require exact set equality; stale exception policy is deliberately fail."""

    exception_keys = set(exceptions)
    unapproved = sorted(findings - exception_keys)
    stale = sorted(exception_keys - findings)
    if not unapproved and not stale:
        return

    messages: list[str] = []
    for finding in unapproved:
        same_component = sorted(
            exception
            for exception in exception_keys
            if (
                exception.cve,
                exception.severity,
                exception.package,
            )
            == (finding.cve, finding.severity, finding.package)
        )
        if same_component:
            expected_versions = ", ".join(item.version for item in same_component)
            messages.append(
                f"version mismatch: {finding.display()} (excepted version(s): {expected_versions})"
            )
        else:
            messages.append(f"unapproved finding: {finding.display()}")
    for exception in stale:
        messages.append(f"stale exception (finding absent): {exception.display()}")
    raise GateError("ECR vulnerability gate rejected the image:\n  - " + "\n  - ".join(messages))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--exceptions", type=Path, required=True)
    parser.add_argument("--expected-image-digest", required=True)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        help="UTC policy date override for deterministic tests (YYYY-MM-DD)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy_date = args.today or datetime.now(UTC).date()
    try:
        exceptions = load_exceptions(args.exceptions, today=policy_date)
        findings = parse_scan(
            args.scan,
            expected_image_digest=args.expected_image_digest,
            expected_repository=args.expected_repository,
        )
        evaluate_gate(findings, exceptions)
    except GateError as exc:
        print(f"FATAL ECR vulnerability gate failed: {exc}", file=sys.stderr)
        return 1
    print(
        "ECR vulnerability gate passed: "
        f"{len(findings)} CRITICAL/HIGH finding(s), all exactly excepted; 0 stale"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
