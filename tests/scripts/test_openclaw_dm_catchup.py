"""Run the DM catchup regressions in the ordinary pytest CI job."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "infra/openclaw/caller-identity-plugin"


@pytest.mark.parametrize(
    "filename", ["dm-catchup.test.mjs", "dm-catchup-runtime.test.mjs", "dm-catchup-plugin.test.mjs"]
)
def test_dm_catchup_node_regressions(filename: str) -> None:
    result = subprocess.run(
        ["node", "--test", str(PLUGIN / "test" / filename)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
