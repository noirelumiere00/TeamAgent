from __future__ import annotations

import hashlib
import importlib.util
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "terraform" / "terraform_apply_supervisor.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "terraform_apply_supervisor_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SUPERVISOR = _load_module()

_SAVED_PLAN_ENVIRONMENT_KEYS = (
    "TEAMAGENT_SAVED_PLAN_PATH",
    "TEAMAGENT_SAVED_PLAN_SHA256",
    "TEAMAGENT_SAVED_PLAN_IDENTITY",
)


@pytest.fixture(autouse=True)
def restore_saved_plan_environment() -> Iterator[None]:
    saved = {key: os.environ.get(key) for key in _SAVED_PLAN_ENVIRONMENT_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_heartbeat_failure_terminates_and_waits_for_the_terraform_process_group(
    tmp_path: Path,
) -> None:
    leader_pid_path = tmp_path / "leader.pid"
    child_pid_path = tmp_path / "child.pid"
    terraform = tmp_path / "fake_terraform.py"
    terraform.write_text(
        "\n".join(
            [
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                f"open({str(leader_pid_path)!r}, 'w').write(str(os.getpid()))",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                f"open({str(child_pid_path)!r}, 'w').write(str(child.pid))",
                "time.sleep(60)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    status = SUPERVISOR.run_supervised(
        [sys.executable, str(terraform)],
        [sys.executable, "-c", "raise SystemExit(1)"],
        heartbeat_interval_seconds=0.2,
        heartbeat_timeout_seconds=1,
        termination_grace_seconds=1,
    )

    assert status == SUPERVISOR.HEARTBEAT_FAILURE_EXIT
    leader_pid = int(leader_pid_path.read_text(encoding="utf-8"))
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert not _pid_exists(leader_pid)
    assert not _pid_exists(child_pid)


@pytest.mark.parametrize("leader_exit_status", [0, 7])
def test_leader_exit_drains_a_surviving_process_group_child_before_returning(
    tmp_path: Path,
    leader_exit_status: int,
) -> None:
    leader_state_path = tmp_path / "leader.state"
    child_state_path = tmp_path / "child.state"
    terraform = tmp_path / "fake_terraform.py"
    child_program = (
        "import os, time; "
        f"open({str(child_state_path)!r}, 'w').write("
        "str(os.getpid()) + ':' + str(os.getpgrp())); "
        "time.sleep(60)"
    )
    terraform.write_text(
        "\n".join(
            [
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                f"open({str(leader_state_path)!r}, 'w').write("
                "str(os.getpid()) + ':' + str(os.getpgrp()))",
                f"subprocess.Popen([sys.executable, '-c', {child_program!r}])",
                f"while not os.path.exists({str(child_state_path)!r}):",
                "    time.sleep(0.01)",
                f"raise SystemExit({leader_exit_status})",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    child_pid: int | None = None
    try:
        status = SUPERVISOR.run_supervised(
            [sys.executable, str(terraform)],
            [sys.executable, "-c", "raise SystemExit(0)"],
            heartbeat_interval_seconds=0.5,
            heartbeat_timeout_seconds=1,
            termination_grace_seconds=1,
        )

        leader_pid, leader_process_group = (
            int(value) for value in leader_state_path.read_text(encoding="utf-8").split(":")
        )
        child_pid, child_process_group = (
            int(value) for value in child_state_path.read_text(encoding="utf-8").split(":")
        )
        assert status == leader_exit_status
        assert leader_pid == leader_process_group == child_process_group
        assert not _pid_exists(leader_pid)
        assert not _pid_exists(child_pid)
    finally:
        if child_pid is not None and _pid_exists(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_successful_heartbeat_preserves_the_terraform_exit_status() -> None:
    assert all(key not in os.environ for key in _SAVED_PLAN_ENVIRONMENT_KEYS)

    status = SUPERVISOR.run_supervised(
        [sys.executable, "-c", "import time; time.sleep(0.1); raise SystemExit(7)"],
        [sys.executable, "-c", "raise SystemExit(0)"],
        heartbeat_interval_seconds=0.02,
        heartbeat_timeout_seconds=1,
        termination_grace_seconds=1,
    )

    assert status == 7


@pytest.mark.skipif(
    not Path("/proc/self/fd").exists(),
    reason="the production apply runner requires Linux procfs",
)
def test_main_holds_one_plan_inode_for_apply_heartbeat_and_provisioners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terraform = tmp_path / "terraform"
    gate = tmp_path / "gate.sh"
    plan = tmp_path / "saved.tfplan"
    replacement = tmp_path / "replacement.tfplan"
    terraform.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    terraform.chmod(0o755)
    gate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    original = b"reviewed opaque plan"
    plan.write_bytes(original)
    replacement.write_bytes(b"replaced path payload")
    metadata = plan.stat()
    observed: dict[str, object] = {}

    def run_supervised(
        terraform_command: list[str],
        heartbeat_command: list[str],
        **_kwargs: object,
    ) -> int:
        held_path = Path(terraform_command[-1])
        observed["terraform"] = held_path.read_bytes()
        observed["heartbeat_path"] = heartbeat_command[heartbeat_command.index("--plan") + 1]
        observed["environment_path"] = os.environ["TEAMAGENT_SAVED_PLAN_PATH"]
        os.replace(replacement, plan)
        observed["held_after_replacement"] = held_path.read_bytes()
        observed["path_after_replacement"] = plan.read_bytes()
        return 0

    monkeypatch.setattr(SUPERVISOR, "run_supervised", run_supervised)
    digest = hashlib.sha256(original).hexdigest()

    status = SUPERVISOR.main(
        [
            "--terraform-bin",
            str(terraform),
            "--gate-runner",
            str(gate),
            "--plan",
            str(plan),
            "--plan-sha256",
            digest,
            "--plan-identity",
            f"{metadata.st_dev}:{metadata.st_ino}",
            "--apply-attempt-id",
            "12345678-1234-4123-8123-123456789abc",
        ]
    )

    assert status == 0
    assert observed == {
        "terraform": original,
        "heartbeat_path": observed["environment_path"],
        "environment_path": observed["heartbeat_path"],
        "held_after_replacement": original,
        "path_after_replacement": b"replaced path payload",
    }
