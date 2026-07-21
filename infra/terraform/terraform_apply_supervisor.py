#!/usr/bin/env python3
"""Run Terraform in its own process group while the release lock is heartbeated."""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

HEARTBEAT_FAILURE_EXIT = 75


class SupervisorError(RuntimeError):
    """Terraform supervision could not preserve the release-lock invariant."""


class _SupervisorInterruptedError(Exception):
    def __init__(self, signal_number: int) -> None:
        self.signal_number = signal_number


def _measure_descriptor(descriptor: int) -> tuple[str, str]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise SupervisorError("saved plan descriptor is not a regular file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), f"{metadata.st_dev}:{metadata.st_ino}"


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin reports EPERM for a group whose same-UID members have all
        # exited but have not yet disappeared from the process table. Treat
        # that group as present until the kernel reports ESRCH so lock release
        # cannot race any unsignalable descendant either.
        return True
    return True


def _terminate_and_wait(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
) -> None:
    """Terminate every Terraform group member and wait until none remain."""

    process_group_id = process.pid
    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.monotonic() + grace_seconds
    while _process_group_exists(process_group_id) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)

    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    process.wait()
    # Do not return control to the lock-owning shell until every descendant in
    # Terraform's isolated process group has exited and been reaped.
    while _process_group_exists(process_group_id):
        time.sleep(0.05)


def run_supervised(
    terraform_command: Sequence[str],
    heartbeat_command: Sequence[str],
    *,
    heartbeat_interval_seconds: float,
    heartbeat_timeout_seconds: float,
    termination_grace_seconds: float,
) -> int:
    if not terraform_command or not heartbeat_command:
        raise SupervisorError("Terraform and heartbeat commands are required")
    if (
        heartbeat_interval_seconds <= 0
        or heartbeat_timeout_seconds <= 0
        or termination_grace_seconds <= 0
    ):
        raise SupervisorError("supervisor timing values must be positive")

    process = subprocess.Popen(
        list(terraform_command),
        start_new_session=True,
    )

    def interrupt(signal_number: int, _frame: object) -> None:
        raise _SupervisorInterruptedError(signal_number)

    previous_handlers = {
        signal_number: signal.signal(signal_number, interrupt)
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        while True:
            try:
                terraform_status = process.wait(
                    timeout=heartbeat_interval_seconds,
                )
            except subprocess.TimeoutExpired:
                pass
            else:
                _terminate_and_wait(
                    process,
                    grace_seconds=termination_grace_seconds,
                )
                return terraform_status
            try:
                heartbeat = subprocess.run(
                    list(heartbeat_command),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    timeout=heartbeat_timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(
                    f"FATAL: shared image/Terraform automation lock heartbeat failed: {exc}",
                    file=sys.stderr,
                )
                _terminate_and_wait(
                    process,
                    grace_seconds=termination_grace_seconds,
                )
                return HEARTBEAT_FAILURE_EXIT
            if heartbeat.returncode != 0:
                print(
                    "FATAL: shared image/Terraform automation lock heartbeat failed",
                    file=sys.stderr,
                )
                _terminate_and_wait(
                    process,
                    grace_seconds=termination_grace_seconds,
                )
                return HEARTBEAT_FAILURE_EXIT
    except _SupervisorInterruptedError as exc:
        _terminate_and_wait(
            process,
            grace_seconds=termination_grace_seconds,
        )
        return 128 + exc.signal_number
    except BaseException:
        _terminate_and_wait(
            process,
            grace_seconds=termination_grace_seconds,
        )
        raise
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform-bin", type=Path, required=True)
    parser.add_argument("--gate-runner", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--plan-identity", required=True)
    parser.add_argument("--apply-attempt-id", required=True)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=30.0)
    parser.add_argument("--heartbeat-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--termination-grace-seconds", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    terraform_bin = args.terraform_bin.resolve()
    gate_runner = args.gate_runner.resolve()
    plan = args.plan
    if not terraform_bin.is_file() or not os.access(terraform_bin, os.X_OK):
        print("FATAL: Terraform executable is unavailable", file=sys.stderr)
        return 2
    if not gate_runner.is_file():
        print("FATAL: release gate runner or saved plan is unavailable", file=sys.stderr)
        return 2

    plan_descriptor = -1
    try:
        plan_descriptor = os.open(plan, os.O_RDONLY)
        measured_digest, measured_identity = _measure_descriptor(plan_descriptor)
        if measured_digest != args.plan_sha256 or measured_identity != args.plan_identity:
            raise SupervisorError("saved plan descriptor binding differs")
        held_plan = f"/proc/{os.getpid()}/fd/{plan_descriptor}"
        os.environ["TEAMAGENT_SAVED_PLAN_PATH"] = held_plan
        os.environ["TEAMAGENT_SAVED_PLAN_SHA256"] = measured_digest
        os.environ["TEAMAGENT_SAVED_PLAN_IDENTITY"] = measured_identity
        status = run_supervised(
            [
                str(terraform_bin),
                "apply",
                "-input=false",
                "-lock=true",
                "-lock-timeout=5m",
                held_plan,
            ],
            [
                "bash",
                str(gate_runner),
                "heartbeat-deployment-lock",
                "--plan",
                held_plan,
                "--apply-attempt-id",
                args.apply_attempt_id,
            ],
            heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            heartbeat_timeout_seconds=args.heartbeat_timeout_seconds,
            termination_grace_seconds=args.termination_grace_seconds,
        )
        final_digest, final_identity = _measure_descriptor(plan_descriptor)
        if final_digest != measured_digest or final_identity != measured_identity:
            raise SupervisorError("saved plan inode changed during apply")
        return status
    except SupervisorError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"FATAL: saved plan descriptor failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if plan_descriptor >= 0:
            os.close(plan_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
