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
    completed = _run(MCP_HELPER)
    assert completed.returncode != 0
    assert "--image-tag is required" in completed.stderr


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (OPENCLAW_HELPER, "No provenance-pinned, vulnerability-gated OpenClaw builder"),
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
