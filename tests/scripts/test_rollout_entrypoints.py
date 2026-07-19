from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "entrypoint",
    (
        ROOT / "scripts" / "hmac_rollout_gate.py",
        ROOT / "scripts" / "terraform_hmac_gate.py",
        ROOT / "scripts" / "terraform_hmac_payload.py",
        ROOT / "scripts" / "terraform_hmac_promotion_gate.py",
        ROOT / "scripts" / "measure_worker_release.py",
        ROOT / "scripts" / "check_hmac_runtime_state.py",
        ROOT / "infra" / "terraform" / "eventbridge_apply_saga.py",
    ),
)
def test_rollout_entrypoint_resolves_repository_imports_without_editable_install(
    entrypoint: Path,
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import runpy,sys; runpy.run_path(sys.argv[1], run_name='entrypoint_import_test')",
            str(entrypoint),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
