#!/usr/bin/env python3
"""Run Terraform in its own process group while the release lock is heartbeated."""

from __future__ import annotations

import argparse
import os
import signal
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


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin reports EPERM for a group whose same-UID members have all
        # exited but have not yet disappeared from the process table.
        return False
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
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + grace_seconds
    while _process_group_exists(process_group_id) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)

    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
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
                return process.wait(timeout=heartbeat_interval_seconds)
            except subprocess.TimeoutExpired:
                pass
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
    parser.add_argument("--apply-attempt-id", required=True)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=30.0)
    parser.add_argument("--heartbeat-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--termination-grace-seconds", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    terraform_bin = args.terraform_bin.resolve()
    gate_runner = args.gate_runner.resolve()
    plan = args.plan.resolve()
    if not terraform_bin.is_file() or not os.access(terraform_bin, os.X_OK):
        print("FATAL: Terraform executable is unavailable", file=sys.stderr)
        return 2
    if not gate_runner.is_file() or not plan.is_file():
        print("FATAL: release gate runner or saved plan is unavailable", file=sys.stderr)
        return 2

    try:
        return run_supervised(
            [
                str(terraform_bin),
                "apply",
                "-input=false",
                "-lock=true",
                "-lock-timeout=5m",
                str(plan),
            ],
            [
                "bash",
                str(gate_runner),
                "heartbeat-deployment-lock",
                "--plan",
                str(plan),
                "--apply-attempt-id",
                args.apply_attempt_id,
            ],
            heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            heartbeat_timeout_seconds=args.heartbeat_timeout_seconds,
            termination_grace_seconds=args.termination_grace_seconds,
        )
    except SupervisorError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
