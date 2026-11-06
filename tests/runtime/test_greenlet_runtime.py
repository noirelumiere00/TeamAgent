"""Native wheels must switch stacks on the running CPython, not merely import."""

import importlib.util
import subprocess
import sys

import pytest


@pytest.mark.skipif(importlib.util.find_spec("greenlet") is None, reason="optional greenlet wheel")
def test_greenlet_can_switch_stacks() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "faulthandler",
            "-c",
            "from greenlet import greenlet; assert greenlet(lambda: 42).switch() == 42",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
