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
# 2026-09-14 に core へ登録した glibc 2.44-r4 の例外（3 サブパッケージ共通の CVE）。
GLIBC_2_44_CVE = "CVE-2026-18374"
ZLIB_1_3_2_CVE = "CVE-2026-85091"
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

    # 2026-08-19: CVE-2026-54876(openssl HIGH)を機にchainguardベースをバンプ。
    # python 3.14.6-r4→3.14.7系で上記2件のfindingが消えるため、08-14のmediaと同じく
    # 例外は同時撤去し core も空へ復帰（残すと stale=fail が発火する）。
    # 2026-09-03: r16 image-builder 段3 で CVE-2026-15806（MEDIUM・python-3.14 3.14.7-r1）が
    # 新規検出。08-13 と同型の期限つき例外（stale=fail・expires 2026-09-17）で通した（#378）。
    # 2026-09-04: Wolfi が 3.14.7-r5 で CVE-2026-15806 の upstream fix を backport
    # （wolfi-dev/os 95cf7b435 — cpython a0d023fb「Scope HTTPPasswordMgr credentials by
    # URL scheme」の cherry-pick）。現在の chainguard python:latest / latest-dev は
    # 3.14.7-r6 で、本 PR が base digest をバンプしたため finding は消える。例外を残すと
    # stale=fail が発火するので、期限（09-17）を待たず core を空へ復帰させる。
    # 2026-09-14: mcp 便 r22 image-builder 段3 で CVE-2026-18374（MEDIUM・glibc 2.44-r4 の
    # 3 サブパッケージ: glibc-2.44 / ld-linux-2.44 / glibc-2.44-locale-posix）が新規検出。
    # 段4 Trivy は契約が Critical/High ゼロを固定しており MEDIUM は通るため、08-13 / 09-03 と
    # 同型の期限つき例外（stale=fail・expires 2026-09-28）で段3 だけを通す。
    # 同時に検出された CVE-2026-85091（HIGH・zlib 1.3.2-r5）は当初「HIGH は期限付き例外にせず
    # バンプで直す」規律（activation_freeze.json の 09-11 宣言）に従い例外へ載せなかったが、
    # 2026-09-14 夕方の裁定「期限付き例外で今日発射」で 1 件限りの例外として登録した。
    # 根拠: バンプ先が存在しない（Wolfi は 1.3.3-r0 を宣言のみ・upstream 未修正）／runtime の
    # ELF 73 本に脆弱関数 gzprintf/gzvprintf への経路が無い／段4 Trivy は MEDIUM 判定で通る。
    # 失効日は glibc と同じ 09-28（同じ base digest バンプで一括撤去・stale=fail が強制）。
    # 2026-09-17: Chainguard python:latest arm64（2026-09-16T20:13Z 生成）は glibc 2.44-r6 と
    # zlib 1.3.2.1_rc20260601-r0（Wolfi secdb が CVE-2026-85091 の修正版と宣言し直した）を同梱し、
    # 09-14 の 4 件（glibc MEDIUM ×3・zlib HIGH ×1）はすべて finding が消える。例外を残すと
    # stale=fail が発火するため、base digest バンプと同じ PR で core を空へ復帰させる（#386 と同型）。
    # HIGH の例外は 09-14 の zlib 1 件限りで、期限（09-28）を待たずバンプで撤去した。
    # 2026-10-08: mcp 便 r51 の image-builder 段3 で、OpenClaw（#568）と同じ glibc 2.44-r7 の
    # MEDIUM 3 件・LOW 1 件が core（Chainguard python:latest）にも新規検出（同日朝の r50 では未検出）。
    # #408・#568 と同型の期限つき例外（stale=fail・expires 2026-10-22）で段3 だけを通す。
    # python ベースは libcrypt1 を同梱しないため 3 パッケージ × 4 件＝12 件（OpenClaw は 16 件）。
    # 恒久対応は python ベースの glibc 2.44-r8 へのバンプで、その PR で core を空へ戻す。
    core_payload = json.loads(core.read_text(encoding="utf-8"))
    assert core_payload["schema_version"] == 1
    assert core_payload["stale_exception_policy"] == "fail"
    assert sorted(
        (e["cve"], e["severity"], e["package"], e["version"], e["expires_on"])
        for e in core_payload["exceptions"]
    ) == sorted(
        (cve, severity, package, "2.44-r7", "2026-10-22")
        for cve, severity in OC_GLIBC_2_44_R7_CVES
        for package in CORE_GLIBC_2_44_PACKAGES
    )
    for entry in core_payload["exceptions"]:
        assert entry["owner"] == "s-komata@vectorinc.co.jp"
        assert "2026-10-08" in entry["reason"]
    assert len(gate.load_exceptions(core, today=date(2026, 10, 8))) == 12

    # 2026-08-14: media の CVE-2026-7210（python3 3.14.5-r2）は一時的に例外登録したが、
    # Trivy 側では HIGH 判定（4サブパッケージに計上）で attestor の C/H ゼロゲートを
    # 通せないと実測で確定。fix 版 3.14.7-r0 が存在したため python バンプで根治し、
    # 例外は同時に撤去した（残すと finding 消滅で stale=fail が発火する）。media は空へ復帰。
    # 2026-10-08: r51 の撃ち直しで media に CVE-2026-85091（ECR は HIGH・Trivy は MEDIUM・
    # Alpine v3.24 base 同梱の zlib 1.3.2-r0・修正版 1.3.2-r1）。9/14（#411）と同じく小俣さんの裁定で
    # 1 件限りの期限つき例外（stale=fail・expires 2026-10-22）。恒久対応は zlib の明示 pin（#518 型）で、
    # その PR で media を空へ戻す。HIGH を黙って増やす・期限を延ばす変更はここで赤になる。
    media_payload = json.loads(media.read_text(encoding="utf-8"))
    assert media_payload["schema_version"] == 1
    assert media_payload["stale_exception_policy"] == "fail"
    assert [
        (e["cve"], e["severity"], e["package"], e["version"], e["expires_on"], e["owner"])
        for e in media_payload["exceptions"]
    ] == [
        (
            "CVE-2026-85091",
            "HIGH",
            "zlib",
            "1.3.2-r0",
            "2026-10-22",
            "s-komata@vectorinc.co.jp",
        )
    ]
    assert "2026-10-08" in media_payload["exceptions"][0]["reason"]
    assert len(gate.load_exceptions(media, today=date(2026, 10, 8))) == 1


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


# 2026-10-08 に OpenClaw へ登録した glibc 2.44-r7 の例外（4 サブパッケージ共通の CVE 4 件）。
# gitleaks(generic-api-key) の誤検知を避けるため、CVE ID は定数へ逃がし識別子 key を使わない。
OC_GLIBC_2_44_R7_CVES = (
    ("CVE-2026-8674", "MEDIUM"),
    ("CVE-2026-86805", "MEDIUM"),
    ("CVE-2026-89092", "MEDIUM"),
    ("CVE-2026-95818", "LOW"),
)
OC_GLIBC_2_44_PACKAGES = (
    "glibc-2.44",
    "glibc-2.44-locale-posix",
    "ld-linux-2.44",
    "libcrypt1-2.44",
)
CORE_GLIBC_2_44_PACKAGES = (
    "glibc-2.44",
    "glibc-2.44-locale-posix",
    "ld-linux-2.44",
)


def test_openclaw_registry_is_exactly_the_2026_10_08_glibc_exceptions() -> None:
    """OpenClaw の例外レジストリを 10-08 裁定の集合そのものに固定する。

    2026-10-08: oc17 の provenance builder 段で Chainguard node:latest 同梱の glibc 2.44-r7 に
    MEDIUM 3 件・LOW 1 件が新規検出（10-07 19:39 のビルドでは未検出）。Wolfi には 2.44-r8 が
    あるが、runtime ベースのバンプは契約の node probe と世代の再導出を伴うため、9/14（#408）と
    同じく MEDIUM/LOW は期限つき例外（stale=fail・expires 2026-10-22）で段を通す。HIGH/CRITICAL
    を黙って足す変更、パッケージや版をずらす変更、期限を延ばす変更はすべてここで赤になる。
    """
    path = ROOT / "infra/codebuild/ecr_scan_exceptions_openclaw.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["stale_exception_policy"] == "fail"
    expected = sorted(
        (cve, severity, package, "2.44-r7", "2026-10-22")
        for cve, severity in OC_GLIBC_2_44_R7_CVES
        for package in OC_GLIBC_2_44_PACKAGES
    )
    actual = sorted(
        (e["cve"], e["severity"], e["package"], e["version"], e["expires_on"])
        for e in payload["exceptions"]
    )
    assert actual == expected
    for entry in payload["exceptions"]:
        assert entry["owner"] == "s-komata@vectorinc.co.jp"
        assert "2026-10-08" in entry["reason"]
        assert entry["severity"] in {"MEDIUM", "LOW"}
    loaded = gate.load_exceptions(path, today=date(2026, 10, 8))
    assert len(loaded) == 16
