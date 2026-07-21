"""EC2 deployment script regressions that block the remote prepare phase."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy_to_ec2.sh"


def _tar_member_validator() -> str:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r'tar tzf "\$STAGING_RELEASE/app\.tar\.gz" \\\n'
        r"  \| awk '(?P<program>[^']+)'",
        source,
    )
    assert match is not None
    return match.group("program")


def _validate_tar_members(members: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["awk", _tar_member_validator()],
        input=members,
        capture_output=True,
        text=True,
        check=False,
    )


def test_remote_prepare_tar_member_validator_accepts_safe_members() -> None:
    result = _validate_tar_members("app/main.py\napp/static/site.css\nscripts/deploy.sh\n")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("member", ("../evil", "/etc/passwd", "a/../b"))
def test_remote_prepare_tar_member_validator_rejects_unsafe_member(member: str) -> None:
    result = _validate_tar_members(f"{member}\n")

    assert result.returncode == 1, result.stderr
