"""Offline checks: never execute the mirror CLI or real curl/AWS commands."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "mirror_build_tools", ROOT / "scripts/aws/release/mirror_build_tools.py"
)
assert SPEC is not None and SPEC.loader is not None
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)
KMS = json.loads((ROOT / "infra/deploy/buildspec_generation_inputs.json").read_text())["kms_keys"][
    "image_release_evidence"
]


def test_inventory_includes_literal_and_lock_pins() -> None:
    tools = mirror.inventory(ROOT)
    assert len(tools) == 6
    lock = json.loads((ROOT / "infra/openclaw/plugins-lock.json").read_text())["tooling"]
    assert any(tool["sha256"] == lock["buildx"]["linuxArm64BinarySha256"] for tool in tools)
    assert any(tool["sha256"] == lock["trivy"]["linuxArm64ArchiveSha256"] for tool in tools)
    assert all(tool["key"].split("/")[1] == tool["sha256"] for tool in tools)


def test_plan_is_completely_offline(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("plan must not invoke subprocesses")

    monkeypatch.setattr(mirror.subprocess, "run", forbidden)
    mirror.mirror(mirror.inventory(ROOT), KMS, apply=False)
    assert capsys.readouterr().out.count("PLAN verify") == 6


@pytest.mark.parametrize("value", ["$(touch /tmp/forbidden)", "$LOOP"])
def test_shell_expressions_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="unsupported or cyclic"):
        mirror.expand(value, {"LOOP": "$LOOP"})


@pytest.mark.parametrize("state", ["exists", "missing", "denied", "corrupt"])
def test_mirror_verifies_before_conditional_upload(monkeypatch: pytest.MonkeyPatch, state) -> None:
    payload = b"verified tool archive"
    sha256 = hashlib.sha256(payload).hexdigest()
    tool = {
        "url": "https://github.com/org/tool/releases/download/v1.0.0/tool.tgz",
        "sha256": sha256,
        "key": f"build-tools/{sha256}/tool.tgz",
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "head-object" in command:
            if state == "exists":
                return subprocess.CompletedProcess(command, 0, "{}", "")
            error = "(403)" if state == "denied" else "(404)"
            return subprocess.CompletedProcess(command, 1, "", error)
        if command[0] == "curl":
            Path(command[command.index("--output") + 1]).write_bytes(
                b"wrong bytes" if state == "corrupt" else payload
            )
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(mirror.subprocess, "run", fake_run)
    if state in {"denied", "corrupt"}:
        with pytest.raises((ValueError, RuntimeError)):
            mirror.mirror([tool], KMS, apply=True)
        assert not any("put-object" in command for command in calls)
    else:
        mirror.mirror([tool], KMS, apply=True)
        assert len(calls) == (1 if state == "exists" else 3)
        if state == "missing":
            upload = calls[-1]
            assert upload[upload.index("--if-none-match") + 1] == "*"
            assert upload[upload.index("--checksum-algorithm") + 1] == "SHA256"
            assert upload[upload.index("--expected-bucket-owner") + 1] == mirror.OWNER
            assert upload[upload.index("--ssekms-key-id") + 1] == KMS
            assert upload[upload.index("--object-lock-mode") + 1] == "GOVERNANCE"
