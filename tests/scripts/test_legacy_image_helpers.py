"""Regression guards for retired unsafe root-level image helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_HELPER = ROOT / "build_mcp_image.sh"
OPENCLAW_HELPER = ROOT / "build_openclaw_image.sh"
TIKTOK_HELPER = ROOT / "build_tiktok_image.sh"
HELPERS = (MCP_HELPER, OPENCLAW_HELPER, TIKTOK_HELPER)
RETIRED_DEPLOYERS = (
    ROOT / "infra" / "deploy" / "deploy_connectweb_unified.sh",
    ROOT / "deploy_digest_test.sh",
)


def _run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(path), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("path", HELPERS)
def test_helper_shell_syntax(path: Path) -> None:
    completed = subprocess.run(
        ["bash", "-n", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_mcp_helper_only_delegates_to_the_safe_launcher() -> None:
    body = MCP_HELPER.read_text(encoding="utf-8")

    assert "infra/deploy/build_teamagent_image.sh" in body
    assert 'exec "$SAFE_LAUNCHER" "$@"' in body
    completed = _run(MCP_HELPER, "--help")
    assert completed.returncode == 0
    assert "does not" in completed.stdout
    assert "accept an image tag or source path" in completed.stdout


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (OPENCLAW_HELPER, "Shared/legacy image-only OpenClaw builds are forbidden"),
        (TIKTOK_HELPER, "tiktok-data-service/scripts/build_acquire_image.sh"),
    ],
)
def test_deprecated_helpers_are_fail_loud(path: Path, message: str) -> None:
    completed = _run(path)

    assert completed.returncode == 64
    assert message in completed.stderr


def test_no_legacy_helper_contains_an_unsafe_build_path() -> None:
    bodies = {path.name: path.read_text(encoding="utf-8").lower() for path in HELPERS}
    forbidden = (
        "git ls-files",
        "zip -q",
        "aws s3",
        "start-build",
        "buildspec-override",
        "source-type-override",
        "source-location-override",
        "~/",
        "$home",
    )

    for name, body in bodies.items():
        for pattern in forbidden:
            assert pattern not in body, f"{name} still contains {pattern!r}"


@pytest.mark.parametrize("path", RETIRED_DEPLOYERS, ids=lambda path: path.name)
def test_unsafe_combined_deployers_are_fail_loud_stubs(path: Path) -> None:
    body = path.read_text(encoding="utf-8")
    completed = _run(path)

    assert completed.returncode == 64
    assert "permanently disabled" in completed.stderr
    assert "build_teamagent_image.sh" in completed.stderr
    assert "terraform/README.md" in completed.stderr
    for forbidden in (
        "aws ",
        "start-build",
        "source.zip",
        "register-task-definition",
        "update-service",
        "put-targets",
    ):
        assert forbidden not in body.lower()


def test_all_shell_start_build_and_source_zip_paths_are_allowlisted() -> None:
    safe_launchers = {
        ROOT / "infra" / "deploy" / "authorize_image_release.sh",
        ROOT / "infra" / "deploy" / "build_teamagent_image.sh",
        ROOT / "infra" / "deploy" / "build_openclaw_image.sh",
        ROOT / "infra" / "deploy" / "build_tiktok_image.sh",
    }
    shell_files = sorted(
        path
        for path in ROOT.rglob("*.sh")
        if ".git" not in path.parts and ".venv" not in path.parts
    )

    for path in shell_files:
        body = path.read_text(encoding="utf-8").lower()
        if "start-build" in body:
            assert path in safe_launchers, f"unapproved StartBuild path: {path}"
        if "source.zip" in body:
            pytest.fail(f"shell launchers must not create source.zip: {path}")


def test_openclaw_safe_launcher_is_not_reachable_through_legacy_helper() -> None:
    body = OPENCLAW_HELPER.read_text(encoding="utf-8")

    assert "infra/deploy/build_openclaw_image.sh" in body
    assert "exec " not in body
