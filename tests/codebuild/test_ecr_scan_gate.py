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
# 例外レジストリは c101715 で subject 別（core / media）へ分割された。
# 単一ファイルへ戻る回帰を防ぐため、両方を明示して回す。
EXCEPTIONS_PATHS = (
    ROOT / "infra" / "codebuild" / "ecr_scan_exceptions_core.json",
    ROOT / "infra" / "codebuild" / "ecr_scan_exceptions_media.json",
)
DIGEST = "sha256:" + "d" * 64
REPOSITORY = "teamagent-mcp"
TODAY = date(2026, 7, 16)
SYNTHETIC_EXCEPTIONS = {
    ("CVE-2099-10001", "CRITICAL", "fixture-libc", "1.0.0"),
    ("CVE-2099-10002", "HIGH", "fixture-db", "2.0.0"),
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


def _exception_policy(
    keys: set[tuple[str, str, str, str]] = SYNTHETIC_EXCEPTIONS,
    *,
    expires_on: str = "2026-08-16",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "stale_exception_policy": "fail",
        "exceptions": [
            {
                "cve": cve,
                "severity": severity,
                "package": package,
                "version": version,
                "owner": "fixture-maintainers",
                "reason": "Synthetic unit-test exception with constrained reachability.",
                "expires_on": expires_on,
            }
            for cve, severity, package, version in sorted(keys)
        ],
    }


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _load_fixture_exceptions(tmp_path: Path) -> dict[Any, Any]:
    policy_path = _write_json(tmp_path / "exceptions.json", _exception_policy())
    return gate.load_exceptions(policy_path, today=TODAY)


def _parse_scan(tmp_path: Path, keys: set[tuple[str, str, str, str]]) -> set[Any]:
    scan_path = _write_json(tmp_path / "scan.json", _scan_payload(keys))
    return gate.parse_scan(
        scan_path,
        expected_image_digest=DIGEST,
        expected_repository=REPOSITORY,
    )


def test_registry_contents_are_exactly_the_adjudicated_exceptions() -> None:
    """例外レジストリの中身を「裁定済みの集合そのもの」に固定する。

    2026-08-13: chainguard python ベース（digest 固定）に公開後の新規 findings 2件
    （MEDIUM/LOW）が付き、0件ゲートが落ちた。ベース更新は契約リップルの再演になるため、
    設計どおり期限つき例外（stale=fail・expires 2026-09-22）で通す裁定をユーザーが実施。
    このテストは黙った追加・削除・改変をすべて赤にする（タプル完全一致）。
    """
    core, media = EXCEPTIONS_PATHS

    core_records = gate.load_exceptions(core, today=TODAY)
    assert {(k.cve, k.severity, k.package, k.version) for k in core_records} == {
        ("CVE-2025-15366", "MEDIUM", "python-3.14", "3.14.6-r4"),
        ("CVE-2026-6879", "LOW", "python-3.14", "3.14.6-r4"),
    }
    for record in core_records.values():
        assert record.owner == "s-komata@vectorinc.co.jp"
        assert record.expires_on.isoformat() == "2026-09-22"

    # 2026-08-14: media の Alpine python3 3.14.5-r2 にも公開後の新規 MEDIUM が付き
    # （ECR DB の日次更新）、空レジストリの deny-all ゲートが停止した。core と同じ
    # 期限つき例外（stale=fail・expires 2026-09-22）で通す裁定。
    media_records = gate.load_exceptions(media, today=TODAY)
    assert {(k.cve, k.severity, k.package, k.version) for k in media_records} == {
        ("CVE-2026-7210", "MEDIUM", "python3", "3.14.5-r2"),
    }
    for record in media_records.values():
        assert record.owner == "s-komata@vectorinc.co.jp"
        assert record.expires_on.isoformat() == "2026-09-22"


def test_exact_synthetic_exception_set_passes(tmp_path: Path) -> None:
    gate.evaluate_gate(
        _parse_scan(tmp_path, SYNTHETIC_EXCEPTIONS),
        _load_fixture_exceptions(tmp_path),
    )


def test_deny_all_mode_accepts_only_zero_gated_findings(tmp_path: Path) -> None:
    clean_scan = _write_json(tmp_path / "clean.json", _scan_payload(set()))
    common = [
        "--deny-all",
        "--expected-image-digest",
        DIGEST,
        "--expected-repository",
        REPOSITORY,
    ]
    assert gate.main(["--scan", str(clean_scan), *common]) == 0

    high_scan = _write_json(
        tmp_path / "high.json",
        _scan_payload({("CVE-2099-99999", "HIGH", "fixture-browser", "1.2.3")}),
    )
    assert gate.main(["--scan", str(high_scan), *common]) == 1

    for severity in ("MEDIUM", "LOW"):
        scan = _write_json(
            tmp_path / f"{severity.lower()}.json",
            _scan_payload(
                {(f"CVE-2099-{90000 + len(severity)}", severity, "fixture-lib", "1.2.3")}
            ),
        )
        assert gate.main(["--scan", str(scan), *common]) == 1


def test_new_high_finding_fails_instead_of_being_preemptively_excepted(tmp_path: Path) -> None:
    new_finding = ("CVE-2099-99999", "HIGH", "fixture-browser", "1.2.3")
    findings = _parse_scan(tmp_path, SYNTHETIC_EXCEPTIONS | {new_finding})

    with pytest.raises(gate.GateError, match="unapproved finding: CVE-2099-99999"):
        gate.evaluate_gate(findings, _load_fixture_exceptions(tmp_path))


def test_package_version_change_is_not_an_exception_match(tmp_path: Path) -> None:
    changed = set(SYNTHETIC_EXCEPTIONS)
    changed.remove(("CVE-2099-10001", "CRITICAL", "fixture-libc", "1.0.0"))
    changed.add(("CVE-2099-10001", "CRITICAL", "fixture-libc", "1.0.1"))

    with pytest.raises(gate.GateError, match="version mismatch: CVE-2099-10001"):
        gate.evaluate_gate(
            _parse_scan(tmp_path, changed),
            _load_fixture_exceptions(tmp_path),
        )


def test_disappeared_finding_makes_exception_stale_and_fails(tmp_path: Path) -> None:
    reduced = set(SYNTHETIC_EXCEPTIONS)
    reduced.remove(("CVE-2099-10002", "HIGH", "fixture-db", "2.0.0"))

    with pytest.raises(gate.GateError, match=r"stale exception \(finding absent\)"):
        gate.evaluate_gate(
            _parse_scan(tmp_path, reduced),
            _load_fixture_exceptions(tmp_path),
        )


def test_expired_exception_registry_fails_even_before_matching(tmp_path: Path) -> None:
    policy = _write_json(
        tmp_path / "exceptions.json",
        _exception_policy(expires_on="2026-07-15"),
    )

    with pytest.raises(gate.GateError, match="expired exception"):
        gate.load_exceptions(policy, today=TODAY)


def test_duplicate_exception_tuple_is_invalid(tmp_path: Path) -> None:
    payload = _exception_policy()
    payload["exceptions"].append(dict(payload["exceptions"][0]))
    path = _write_json(tmp_path / "exceptions.json", payload)

    with pytest.raises(gate.GateError, match="duplicate exception tuple"):
        gate.load_exceptions(path, today=TODAY)


@pytest.mark.parametrize("missing_field", ["owner", "reason", "expires_on"])
def test_required_exception_metadata_cannot_be_omitted(
    tmp_path: Path,
    missing_field: str,
) -> None:
    payload = _exception_policy()
    del payload["exceptions"][0][missing_field]
    path = _write_json(tmp_path / "exceptions.json", payload)

    with pytest.raises(gate.GateError, match="schema mismatch"):
        gate.load_exceptions(path, today=TODAY)


def test_unknown_exception_schema_field_is_rejected(tmp_path: Path) -> None:
    payload = _exception_policy()
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
        tmp_path / "scan.json",
        _scan_payload(SYNTHETIC_EXCEPTIONS, status="IN_PROGRESS"),
    )

    with pytest.raises(gate.GateError, match="not COMPLETE"):
        gate.parse_scan(
            scan_path,
            expected_image_digest=DIGEST,
            expected_repository=REPOSITORY,
        )


def test_truncated_scan_cannot_pass(tmp_path: Path) -> None:
    payload = _scan_payload(SYNTHETIC_EXCEPTIONS)
    payload["nextToken"] = "more-findings"
    scan_path = _write_json(tmp_path / "scan.json", payload)

    with pytest.raises(gate.GateError, match="truncated"):
        gate.parse_scan(
            scan_path,
            expected_image_digest=DIGEST,
            expected_repository=REPOSITORY,
        )


def test_high_finding_without_exact_package_metadata_cannot_pass(tmp_path: Path) -> None:
    payload = _scan_payload(SYNTHETIC_EXCEPTIONS)
    payload["imageScanFindings"]["findings"][0]["attributes"] = []
    scan_path = _write_json(tmp_path / "scan.json", payload)

    with pytest.raises(gate.GateError, match="package_name/package_version"):
        gate.parse_scan(
            scan_path,
            expected_image_digest=DIGEST,
            expected_repository=REPOSITORY,
        )
