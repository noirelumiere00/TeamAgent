from __future__ import annotations

import subprocess
import sys
import time

import pytest

from teamagent.media.deadline import DeadlineBudget, MediaDeadlineExceededError
from teamagent.media.operations import MediaOperationError, _run
from teamagent.media.worker import _worker_signal_scope


def test_remaining_budget_never_resets_and_honors_local_cap() -> None:
    now = [100.0]
    budget = DeadlineBudget(115.0, clock=lambda: now[0])

    assert budget.remaining() == 15.0
    assert budget.remaining(cap_s=4.0) == 4.0
    now[0] = 114.5
    assert budget.remaining(cap_s=4.0) == 0.5
    now[0] = 115.0
    with pytest.raises(MediaDeadlineExceededError):
        budget.remaining()


def test_subprocess_receives_only_remaining_absolute_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    seen_timeout: list[float] = []

    class FakeProcess:
        pid = 12345
        returncode = 0

        def communicate(self, *, timeout: float | None = None) -> tuple[bytes, bytes]:
            assert timeout is not None
            seen_timeout.append(float(timeout))
            now[0] = 116.0
            return b"", b""

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    budget = DeadlineBudget(115.0, clock=lambda: now[0])

    with pytest.raises(MediaOperationError) as caught:
        _run(["ffmpeg"], budget=budget, timeout_s=180)

    assert caught.value.code == "MEDIA_JOB_DEADLINE_EXCEEDED"
    assert seen_timeout == [15.0]


def test_hung_subprocess_group_is_killed_within_absolute_wall_clock_bound() -> None:
    started = time.monotonic()
    budget = DeadlineBudget(time.time() + 0.2)

    with pytest.raises(MediaOperationError) as caught:
        _run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            budget=budget,
            timeout_s=60,
        )

    assert caught.value.code == "MEDIA_JOB_DEADLINE_EXCEEDED"
    assert time.monotonic() - started < 3.0


def test_worker_watchdog_interrupts_blocking_read_before_terminal_reserve() -> None:
    started = time.monotonic()

    with pytest.raises(MediaDeadlineExceededError):
        with _worker_signal_scope(time.time() + 0.2):
            time.sleep(60)

    assert time.monotonic() - started < 2.0
