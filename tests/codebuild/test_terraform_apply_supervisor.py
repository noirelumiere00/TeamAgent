from __future__ import annotations

import importlib.util
import os
import signal
import sys
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
    status = SUPERVISOR.run_supervised(
        [sys.executable, "-c", "import time; time.sleep(0.1); raise SystemExit(7)"],
        [sys.executable, "-c", "raise SystemExit(0)"],
        heartbeat_interval_seconds=0.02,
        heartbeat_timeout_seconds=1,
        termination_grace_seconds=1,
    )

    assert status == 7
