from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from teamagent.media.deadline import DeadlineBudget, MediaDeadlineExceededError
from teamagent.media.operations import MediaOperationError, _run


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

    def fake_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        seen_timeout.append(float(kwargs["timeout"]))
        now[0] = 116.0
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    budget = DeadlineBudget(115.0, clock=lambda: now[0])

    with pytest.raises(MediaOperationError) as caught:
        _run(["ffmpeg"], budget=budget, timeout_s=180)

    assert caught.value.code == "MEDIA_JOB_DEADLINE_EXCEEDED"
    assert seen_timeout == [15.0]
