from __future__ import annotations

import copy
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from scripts import release_preflight_vuln as preflight

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def report() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (Path(__file__).parent / "fixtures/release_preflight_trivy.json").read_text()
    )
    return payload


def policy(tmp_path: Path, findings: list[dict[str, str]]) -> Path:
    path = tmp_path / "exceptions.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stale_exception_policy": "fail",
                "exceptions": [
                    {
                        "cve": f["VulnerabilityID"],
                        "severity": f["Severity"],
                        "package": f["PkgName"],
                        "version": f["InstalledVersion"],
                        "owner": "fixture",
                        "reason": "Synthetic exception for preflight fixture tests.",
                        "expires_on": "2099-12-31",
                    }
                    for f in findings
                ],
            }
        )
    )
    return path


def test_core_medium_unfixed_and_exact_exceptions(report: dict[str, Any], tmp_path: Path) -> None:
    findings = report["Results"][0]["Vulnerabilities"]
    path = policy(tmp_path, [findings[0]])
    rows = preflight.evaluate_report(report, "core OS", "wolfi", policy=path)
    assert len(rows) == 2
    assert any("3.14.10-r0" in r.fix for r in rows)
    assert any("修正版未公開" in r.fix for r in rows)
    assert not any("LOW" in r.item or "10001" in r.item for r in rows)
    assert (
        preflight.evaluate_report(report, "core OS", "wolfi", policy=policy(tmp_path, findings))
        == []
    )


@pytest.mark.parametrize(
    "field,value", [("InstalledVersion", "26.2.0-r0"), ("Severity", "HIGH"), ("PkgName", "other")]
)
def test_mismatched_exception_is_both_unapproved_and_stale(
    report: dict[str, Any],
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    finding = copy.deepcopy(report["Results"][0]["Vulnerabilities"][0])
    finding[field] = value
    rows = preflight.evaluate_report(report, "core OS", "wolfi", policy=policy(tmp_path, [finding]))
    assert any(r.item.startswith("stale:") for r in rows)
    assert any("CVE-2099-10001 MEDIUM py3-pip-wheel 26.2.1-r0" == r.item for r in rows)


def test_stale_after_fix_and_invalid_policy(report: dict[str, Any], tmp_path: Path) -> None:
    path = policy(tmp_path, [report["Results"][0]["Vulnerabilities"][0]])
    report["Results"][0]["Vulnerabilities"] = []
    rows = preflight.evaluate_report(report, "core OS", "wolfi", policy=path)
    assert len(rows) == 1 and rows[0].item.startswith("stale:")
    payload = json.loads(path.read_text())
    payload["exceptions"][0]["expires_on"] = "2000-01-01"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="expired exception"):
        preflight.evaluate_report(report, "core OS", "wolfi", policy=path)
    payload["exceptions"][0]["expires_on"] = "2099-12-31"
    payload["exceptions"] *= 2
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="duplicate exception"):
        preflight.evaluate_report(report, "core OS", "wolfi", policy=path)


def test_ci_fixed_versions_and_npm_unfixed(report: dict[str, Any]) -> None:
    rows = preflight.evaluate_report(report, "uv.lock", "uv")
    assert len(rows) == 1
    assert rows[0].fix == "修正版候補: 6.0.1, 7.0.0"
    assert "pypdf" in rows[0].item
    rows = preflight.evaluate_report(report, "OpenClaw", "npm")
    assert len(rows) == 2
    assert any(r.fix == "修正版候補: 1.16.2" for r in rows)
    assert any("修正版未公開" in r.fix for r in rows)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"SchemaVersion": 2},
        {"SchemaVersion": 2, "Results": []},
        {"SchemaVersion": 2, "Results": [{"Type": "uv"}]},
    ],
)
def test_empty_scan_cannot_pass(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        preflight.evaluate_report(payload, "uv.lock", "uv")


def test_clean_inventory_passes(report: dict[str, Any]) -> None:
    for result in report["Results"]:
        result.pop("Vulnerabilities")
    assert preflight.evaluate_report(report, "uv.lock", "uv") == []


@pytest.mark.parametrize("malformed", ["not-an-array", [None], ["not-an-object"]])
def test_malformed_vulnerabilities_fail_closed(report: dict[str, Any], malformed: Any) -> None:
    report["Results"][1]["Vulnerabilities"] = malformed
    with pytest.raises(ValueError, match="malformed vulnerability list"):
        preflight.evaluate_report(report, "uv.lock", "uv")


def test_fixed_version_is_validated(report: dict[str, Any]) -> None:
    report["Results"][1]["Vulnerabilities"][0]["FixedVersion"] = 0
    with pytest.raises(ValueError, match="invalid FixedVersion"):
        preflight.evaluate_report(report, "uv.lock", "uv")


def test_missing_plugin_inventory_cannot_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "openclaw"
    (target / "slack").mkdir(parents=True)
    (target / "slack/package-lock.json").write_text("{}")

    def fake_run(*args: Any, **kwargs: Any) -> str:
        (tmp_path / "openclaw.json").write_text(
            json.dumps(
                {
                    "SchemaVersion": 2,
                    "Results": [{"Target": "patches/package-lock.json", "Type": "npm"}],
                }
            )
        )
        return ""

    monkeypatch.setattr(preflight, "run", fake_run)
    with pytest.raises(ValueError, match="missing lock inventories"):
        preflight.scan(
            target, "fs", "openclaw", env={"TRIVY_CACHE_DIR": str(tmp_path)}, work=tmp_path
        )


def test_core_build_reuses_real_replacements_without_app_stages() -> None:
    source = (ROOT / "infra/docker/Dockerfile.teamagent-mcp").read_text()
    recipe = preflight.core_dockerfile(source)
    assert "cgr.dev/chainguard/python:latest@${PYTHON_RUNTIME_ARM64_DIGEST}" in recipe
    for lock in (ROOT / "infra/docker").glob("core-*.lock.json"):
        assert f"infra/docker/{lock.name}" in recipe
    assert "--no-network --no-scripts" in recipe
    assert "replace_core_ca_bundle.py prepare" in recipe
    assert "COPY --from=builder /tmp/core-ca/root/ /" in recipe
    assert "uv sync" not in recipe and "s3://" not in recipe
    with pytest.raises(ValueError, match="structure changed"):
        preflight.core_dockerfile(source.replace("ENV UV_COMPILE_BYTECODE=", "ENV DIFFERENT="))


def test_plugin_replacements_follow_actual_paths_and_remove_old_private_tree() -> None:
    source = (ROOT / "infra/docker/Dockerfile.openclaw").read_text()
    specs = preflight.npm_patches(source)
    patches = {
        archive: {"name": name, "version": version} for archive, (name, version) in specs.items()
    }
    versions = dict(specs.values())
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "slack"},
            "node_modules/follow-redirects": {"version": "1.16.1"},
            "node_modules/body-parser/node_modules/old": {"version": "1.0.0"},
            "node_modules/other/node_modules/follow-redirects": {"version": "1.16.0"},
        },
    }
    packages = preflight.patch_plugin_lock(lock, "slack", source, patches)["packages"]
    assert packages["node_modules/follow-redirects"]["version"] == versions["follow-redirects"]
    assert packages["node_modules/body-parser/node_modules/qs"]["version"] == versions["qs"]
    assert "node_modules/body-parser/node_modules/old" not in packages
    assert packages["node_modules/other/node_modules/follow-redirects"]["version"] == "1.16.0"
    assert "node_modules/undici" not in packages
    with pytest.raises(ValueError, match="exact version"):
        preflight.npm_patches("    npm pack x@latest --ignore-scripts --pack-destination")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/a",
        "http://registry.npmjs.org/a",
        "https://registry.npmjs.org:443/a",
        "https://secret@registry.npmjs.org/a",
        "https://:secret@registry.npmjs.org/a",
    ],
)
def test_nonpublic_registry_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        preflight.check_registry_url(url)


def test_tarball_metadata_only_and_no_symlinks() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(ValueError):
        preflight.tar_member(buffer.getvalue(), "package.json")


def test_prefire_uses_worktree_without_shadow_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "prefire_apk_check.sh").write_text("""#!/bin/bash
REF="$(git -C "$PAD" rev-parse HEAD)"
python3 "$HERE/check_media_apk_pins.py" --repo "$PAD" --ref "$REF"
if docker info >/dev/null 2>&1; then
  bash "$HERE/shadow_build_apk.sh" "$REF"
else
  echo "WARN: Docker が起動していないので apk 段の実ビルドは省略（索引での確認は OK）。確定させたいなら open -a Docker のあと撃ち直す"
fi
""")
    (external / "check_media_apk_pins.py").write_text("")
    work = tmp_path / "work"
    work.mkdir()

    def fake_run(command: list[str], **kwargs: Any) -> str:
        script = Path(command[1]).read_text()
        assert '--ref "$REF"' not in script
        assert "if false; then" in script
        assert "if docker info" not in script
        kwargs["log"].write_text("DRIFT:\n  - pin ffmpeg=8.1.2-r3 が上流に無い（上流: 8.1.2-r4）\n")
        raise RuntimeError("exit 1")

    monkeypatch.setattr(preflight, "run", fake_run)
    rows = preflight.check_media(ROOT, external, work, {})
    assert len(rows) == 1 and "8.1.2-r4" in rows[0].item


@pytest.mark.parametrize(
    "blocked,unknown,code", [(False, False, 0), (True, False, 1), (False, True, 2), (True, True, 2)]
)
def test_main_exit_and_independent_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked: bool,
    unknown: bool,
    code: int,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "uv.lock").write_text("")
    monkeypatch.setattr(preflight, "run", lambda *a, **kw: "")
    monkeypatch.setattr(preflight, "prepare_core", lambda *a: repo)
    monkeypatch.setattr(preflight, "prepare_openclaw", lambda *a: repo)
    monkeypatch.setattr(preflight, "scan", lambda *a, **kw: {})
    visited = []

    def evaluate(report: Any, gate: str, *a: Any, **kw: Any) -> list[preflight.Blocker]:
        visited.append(gate)
        if unknown and gate == "core OS":
            raise ValueError("scan incomplete")
        return [preflight.Blocker(gate, "test finding", "fixed 2.0")] if blocked else []

    monkeypatch.setattr(preflight, "evaluate_report", evaluate)
    monkeypatch.setattr(preflight, "check_media", lambda *a: [])
    assert preflight.main(["--repo", str(repo), "--output-dir", str(tmp_path / "out")]) == code
    assert visited == ["core OS", "uv.lock", "OpenClaw"]
