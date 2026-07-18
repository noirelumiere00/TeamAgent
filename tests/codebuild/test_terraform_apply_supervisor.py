from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

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


def test_successful_heartbeat_preserves_the_terraform_exit_status() -> None:
    status = SUPERVISOR.run_supervised(
        [sys.executable, "-c", "import time; time.sleep(0.1); raise SystemExit(7)"],
        [sys.executable, "-c", "raise SystemExit(0)"],
        heartbeat_interval_seconds=0.02,
        heartbeat_timeout_seconds=1,
        termination_grace_seconds=1,
    )

    assert status == 7
