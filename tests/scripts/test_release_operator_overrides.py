"""Operator overrides preserve the release scripts' existing defaults."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts/aws/release_mcp.sh"
FIRE = ROOT / "scripts/aws/release/fire_and_authorize.sh"
MFA = ROOT / "scripts/aws/release/mfa_refresh.sh"


@pytest.mark.parametrize("path", (LAUNCHER, FIRE, MFA), ids=lambda path: path.name)
def test_shell_syntax(path: Path) -> None:
    subprocess.run(["bash", "-n", str(path)], check=True, capture_output=True, timeout=10)


def test_operator_defaults_are_unchanged() -> None:
    launcher = LAUNCHER.read_text()
    fire = FIRE.read_text()
    mfa = MFA.read_text()
    assert 'PROFILE="${RELEASE_MCP_PROFILE:-aiiadev}"' in launcher
    assert 'MFA_ARN="${RELEASE_MCP_MFA_ARN:-arn:aws:iam::${ACCOUNT}:mfa/N2}"' in launcher
    for body in (fire, mfa):
        assert 'RELEASE_PROFILE="${RELEASE_PROFILE:-aiiadev-mfa}"' in body
    for assignment in (
        'S="${RELEASE_TOOLS_DIR:-/Users/s-komata/Documents/Claude/Scheduled/aico-release-tools}"',
        'PAD="${RELEASE_PAD:-/Users/s-komata/dev/worktrees/teamagent-build}"',
        'FRJ_FILE="${RELEASE_FRJ_FILE:-/Users/s-komata/Documents/Claude/Artifacts/'
        'aico-inputs-20260911/frj_exemption_gate_20261222.json}"',
        "${RELEASE_INPUTS_DIR:-~/Documents/Claude/Artifacts/aico-inputs-20260911}/apply_mcp_tds.sh",
    ):
        assert assignment in fire


def test_fire_uses_the_selected_profile_everywhere() -> None:
    body = FIRE.read_text()
    assert body.count("aiiadev-mfa") == 1
    assert 'export AWS_PROFILE="${RELEASE_PROFILE}"' in body
    assert 'RELEASE_MCP_PROFILE="${RELEASE_PROFILE}" bash "$PATCHED"' in body


@pytest.mark.parametrize("profile", ("aiiadev-mfa", "engineer arima's session"))
def test_session_patcher_anchors_and_selected_profile(tmp_path: Path, profile: str) -> None:
    fire = FIRE.read_text()
    launcher = LAUNCHER.read_text()
    patcher = fire.split("\"$RELEASE_PROFILE\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    # These are the patcher's start/end, repository-root and tail-split anchors.
    for anchor in (
        'read -r -s -p "MFAコード',
        'BASE_TK="$(echo "$BASE" | cut -f3)"\n',
        'REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"',
        "assume() {",
    ):
        assert anchor in launcher
        assert repr(anchor) in patcher
    frj_anchor = (
        '[ -n "$PREV_FRJ" ] && [ "$PREV_FRJ" != "None" ] && '
        'FORCED_ROLLBACK_EVIDENCE_JSON="$PREV_FRJ"'
    )
    assert frj_anchor in launcher
    assert repr(frj_anchor) in fire
    assert "assert 'get-session-token' not in tail" in patcher
    patched = tmp_path / "patched.sh"
    subprocess.run(
        ["python3", "-", str(LAUNCHER), str(patched), str(ROOT), profile],
        input=patcher,
        text=True,
        check=True,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(["bash", "-n", str(patched)], check=True, capture_output=True, timeout=10)
    session = patched.read_text().split("RELEASE_PROFILE=", 1)[1].split("assume() {", 1)[0]
    assert "get-session-token" not in session
    # Evaluate only the inserted assignment: profile values must survive shell quoting.
    assignment = "RELEASE_PROFILE=" + session.splitlines()[0]
    result = subprocess.run(
        ["bash", "-c", assignment + '; printf "%s" "$RELEASE_PROFILE"'],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout == profile


@pytest.mark.parametrize(
    ("base", "serial"), ((None, None), ("operator", None), (None, "device"), ("operator", "device"))
)
def test_mfa_settings_bypass_local_script_only_when_both_are_given(
    tmp_path: Path, base: str | None, serial: str | None
) -> None:
    env = os.environ.copy()
    for name in ("MFA_BASE_PROFILE", "MFA_SERIAL"):
        env.pop(name, None)
    env.update(HOME=str(tmp_path), AWS_CA_BUNDLE="unused-test-bundle", LC_ALL="C")
    if base is not None:
        env["MFA_BASE_PROFILE"] = base
    if serial is not None:
        env["MFA_SERIAL"] = serial
    if not (base and serial):
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin/teamagent-build-mfa.sh").write_text(
            "PROFILE=legacy-base\nMFA_SERIAL=legacy-device\n"
        )
    setup = MFA.read_text().split('read -r -p "MFA コード', 1)[0]
    result = subprocess.run(
        ["bash", "-c", setup + '\nprintf "%s\\n%s" "$PROFILE" "$MFA_SERIAL"'],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stderr == ""
    assert result.stdout == (
        "operator\ndevice" if base and serial else "legacy-base\nlegacy-device"
    )


def test_mfa_writes_and_checks_the_selected_profile() -> None:
    body = MFA.read_text()
    assert 'python3 - "$J" "$RELEASE_PROFILE"' in body
    assert '"--profile", sys.argv[2]' in body
    assert 'aws sts get-caller-identity --profile "$RELEASE_PROFILE"' in body
