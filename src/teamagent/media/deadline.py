"""One absolute deadline shared by every media-job stage."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class MediaDeadlineExceededError(TimeoutError):
    """The immutable media-job deadline has no remaining budget."""


@dataclass(frozen=True, slots=True)
class DeadlineBudget:
    """Expose the remaining wall-clock budget without resetting it per call."""

    deadline_epoch_s: float
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    def remaining(self, *, cap_s: float | None = None) -> float:
        remaining = self.deadline_epoch_s - self.clock()
        if remaining <= 0:
            raise MediaDeadlineExceededError("media job deadline exceeded")
        if cap_s is not None:
            if cap_s <= 0:
                raise ValueError("deadline cap must be positive")
            remaining = min(remaining, cap_s)
        return max(0.001, remaining)

    def checkpoint(self) -> None:
        self.remaining()


def botocore_config(budget: DeadlineBudget) -> Any:
    """Bound one no-retry AWS call's connect+read phases by remaining time."""

    from botocore.config import Config

    remaining = budget.remaining()
    phase_timeout = max(0.001, min(30.0, remaining / 2.0))
    return Config(
        connect_timeout=phase_timeout,
        read_timeout=phase_timeout,
        retries={"mode": "standard", "total_max_attempts": 1},
    )


__all__ = ["DeadlineBudget", "MediaDeadlineExceededError", "botocore_config"]
