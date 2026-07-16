from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "verify_ecr_scan.py"
EXCEPTIONS_PATH = ROOT / "infra" / "codebuild" / "ecr_scan_exceptions.json"
DIGEST = "sha256:" + "d" * 64
REPOSITORY = "teamagent-mcp"
TODAY = date(2026, 7, 16)

EXPECTED_EXCEPTIONS = {
    ("CVE-2026-5450", "CRITICAL", "glibc", "2.41-12+deb13u3"),
    ("CVE-2026-5928", "HIGH", "glibc", "2.41-12+deb13u3"),
    ("CVE-2026-11824", "HIGH", "sqlite3", "3.46.1-7+deb13u1"),
    ("CVE-2026-11822", "HIGH", "sqlite3", "3.46.1-7+deb13u1"),
}


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("teamagent_ecr_scan_gate", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()


def _finding(key: tuple[str, str, str, str]) -> dict[str, Any]:
    cve, severity, package, version = key
    return {
        "name": cve,
        "severity": severity,
        "attributes": [
            {"key": "package_name", "value": package},
            {"key": "package_version", "value": version},
        ],
    }


def _scan_payload(
    keys: set[tuple[str, str, str, str]],
    *,
    status: str = "COMPLETE",
) -> dict[str, Any]:
    findings = [_finding(key) for key in sorted(keys)]
    counts = Counter(finding["severity"] for finding in findings)
    return {
        "registryId": "123456789012",
        "repositoryName": REPOSITORY,
        "imageId": {"imageDigest": DIGEST},
        "imageScanStatus": {"status": status, "description": "fixture"},
        "imageScanFindings": {
            "findingSeverityCounts": dict(counts),
            "findings": findings,
        },
    }


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _evaluate(tmp_path: Path, keys: set[tuple[str, str, str, str]]) -> None:
    scan_path = _write_json(tmp_path / "scan.json", _scan_payload(keys))
    exceptions = gate.load_exceptions(EXCEPTIONS_PATH, today=TODAY)
    findings = gate.parse_scan(
        scan_path,
        expected_image_digest=DIGEST,
        expected_repository=REPOSITORY,
    )
    gate.evaluate_gate(findings, exceptions)


def test_initial_registry_contains_only_the_four_approved_findings() -> None:
    payload = json.loads(EXCEPTIONS_PATH.read_text(encoding="utf-8"))
    actual = {
        (item["cve"], item["severity"], item["package"], item["version"])
        for item in payload["exceptions"]
    }

    assert payload["stale_exception_policy"] == "fail"
    assert actual == EXPECTED_EXCEPTIONS
    assert len(payload["exceptions"]) == 4
    assert {item["owner"] for item in payload["exceptions"]} == {"teamagent-maintainers"}
    assert {item["expires_on"] for item in payload["exceptions"]} == {"2026-08-16"}


def test_exact_complete_true_image_findings_pass(tmp_path: Path) -> None:
    _evaluate(tmp_path, EXPECTED_EXCEPTIONS)


def test_new_high_finding_fails_instead_of_being_preemptively_excepted(tmp_path: Path) -> None:
    new_finding = ("CVE-2026-99999", "HIGH", "chromium", "1.2.3")
    scan_path = _write_json(
        tmp_path / "scan.json", _scan_payload(EXPECTED_EXCEPTIONS | {new_finding})
    )
    exceptions = gate.load_exceptions(EXCEPTIONS_PATH, today=TODAY)
    findings = gate.parse_scan(
        scan_path,
        expected_image_digest=DIGEST,
        expected_repository=REPOSITORY,
    )

    with pytest.raises(gate.GateError, match="unapproved finding: CVE-2026-99999"):
        gate.evaluate_gate(findings, exceptions)


def test_package_version_change_is_not_an_exception_match(tmp_path: Path) -> None:
    changed = set(EXPECTED_EXCEPTIONS)
    changed.remove(("CVE-2026-5928", "HIGH", "glibc", "2.41-12+deb13u3"))
    changed.add(("CVE-2026-5928", "HIGH", "glibc", "2.41-12+deb13u4"))
    scan_path = _write_json(tmp_path / "scan.json", _scan_payload(changed))
    exceptions = gate.load_exceptions(EXCEPTIONS_PATH, today=TODAY)
    findings = gate.parse_scan(
        scan_path,
        expected_image_digest=DIGEST,
        expected_repository=REPOSITORY,
    )

    with pytest.raises(gate.GateError, match="version mismatch: CVE-2026-5928"):
        gate.evaluate_gate(findings, exceptions)


def test_disappeared_finding_makes_exception_stale_and_fails(tmp_path: Path) -> None:
    reduced = set(EXPECTED_EXCEPTIONS)
    removed = ("CVE-2026-11822", "HIGH", "sqlite3", "3.46.1-7+deb13u1")
    reduced.remove(removed)
    scan_path = _write_json(tmp_path / "scan.json", _scan_payload(reduced))
    exceptions = gate.load_exceptions(EXCEPTIONS_PATH, today=TODAY)
    findings = gate.parse_scan(
        scan_path,
        expected_image_digest=DIGEST,
        expected_repository=REPOSITORY,
    )

    with pytest.raises(gate.GateError, match=r"stale exception \(finding absent\)"):
        gate.evaluate_gate(findings, exceptions)


def test_expired_exception_registry_fails_even_before_matching() -> None:
    with pytest.raises(gate.GateError, match="expired exception"):
        gate.load_exceptions(EXCEPTIONS_PATH, today=date(2026, 8, 17))


def test_duplicate_exception_tuple_is_invalid(tmp_path: Path) -> None:
    payload = json.loads(EXCEPTIONS_PATH.read_text(encoding="utf-8"))
    payload["exceptions"].append(dict(payload["exceptions"][0]))
    path = _write_json(tmp_path / "exceptions.json", payload)

    with pytest.raises(gate.GateError, match="duplicate exception tuple"):
        gate.load_exceptions(path, today=TODAY)


@pytest.mark.parametrize("missing_field", ["owner", "reason", "expires_on"])
def test_required_exception_metadata_cannot_be_omitted(tmp_path: Path, missing_field: str) -> None:
    payload = json.loads(EXCEPTIONS_PATH.read_text(encoding="utf-8"))
    del payload["exceptions"][0][missing_field]
    path = _write_json(tmp_path / "exceptions.json", payload)

    with pytest.raises(gate.GateError, match="schema mismatch"):
        gate.load_exceptions(path, today=TODAY)


def test_unknown_exception_schema_field_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(EXCEPTIONS_PATH.read_text(encoding="utf-8"))
    payload["exceptions"][0]["ticket"] = "not-in-schema"
    path = _write_json(tmp_path / "exceptions.json", payload)

    with pytest.raises(gate.GateError, match=r"unknown=\['ticket'\]"):
        gate.load_exceptions(path, today=TODAY)


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "exceptions.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,"stale_exception_policy":"fail","exceptions":[]}',
        encoding="utf-8",
    )

    with pytest.raises(gate.GateError, match="duplicate JSON key"):
        gate.load_exceptions(path, today=TODAY)


def test_non_complete_scan_cannot_pass(tmp_path: Path) -> None:
    scan_path = _write_json(
        tmp_path / "scan.json", _scan_payload(EXPECTED_EXCEPTIONS, status="IN_PROGRESS")
    )

    with pytest.raises(gate.GateError, match="not COMPLETE"):
        gate.parse_scan(
            scan_path,
            expected_image_digest=DIGEST,
            expected_repository=REPOSITORY,
        )


def test_truncated_scan_cannot_pass(tmp_path: Path) -> None:
    payload = _scan_payload(EXPECTED_EXCEPTIONS)
    payload["nextToken"] = "more-findings"
    scan_path = _write_json(tmp_path / "scan.json", payload)

    with pytest.raises(gate.GateError, match="truncated"):
        gate.parse_scan(
            scan_path,
            expected_image_digest=DIGEST,
            expected_repository=REPOSITORY,
        )


def test_high_finding_without_exact_package_metadata_cannot_pass(tmp_path: Path) -> None:
    payload = _scan_payload(EXPECTED_EXCEPTIONS)
    payload["imageScanFindings"]["findings"][0]["attributes"] = []
    scan_path = _write_json(tmp_path / "scan.json", payload)

    with pytest.raises(gate.GateError, match="package_name/package_version"):
        gate.parse_scan(
            scan_path,
            expected_image_digest=DIGEST,
            expected_repository=REPOSITORY,
        )
