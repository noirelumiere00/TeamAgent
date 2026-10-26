"""Prove that transfer safety tests kill mutations without editing production files."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MUTATE_AND_TEST = """
import importlib
from pathlib import Path
import sys
import pytest

module = importlib.import_module('teamagent.slack2drive.transfer')
source = Path(module.__file__).read_text()
old, new, node = sys.argv[1:]
assert source.count(old) == 1, 'mutation anchor changed'
exec(compile(source.replace(old, new), module.__file__, 'exec'), module.__dict__)
raise SystemExit(pytest.main(['-q', node]))
"""


@pytest.mark.parametrize(
    ("mutation", "old", "new", "test_name", "failure"),
    [
        (
            "dry_run_guard",
            "if not commit:",
            "if False:",
            "test_dry_run_hashes_stream_but_never_writes_or_reserves_id",
            "assert 'saved' == 'would_upload'",
        ),
        (
            "source_hash_guard",
            'require(digest.hexdigest() == state["sha256"], "source_changed_between_downloads")',
            'require(True, "source_changed_between_downloads")',
            "test_source_change_between_downloads_prevents_final_upload_commit",
            "DID NOT RAISE",
        ),
    ],
    ids=["dry-run-write-guard", "source-checksum-guard"],
)
def test_transfer_safety_mutation_is_killed(
    mutation: str, old: str, new: str, test_name: str, failure: str
) -> None:
    node = f"tests/slack2drive/test_transfer.py::{test_name}"
    result = subprocess.run(
        [
            "/usr/bin/perl",
            "-e",
            "alarm 300; exec @ARGV",
            sys.executable,
            "-c",
            MUTATE_AND_TEST,
            old,
            new,
            node,
        ],
        cwd=ROOT,
        env={"LC_ALL": "C", "LANG": "C", "PYTHONPATH": "src", "PATH": os.defpath},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=310,
        check=False,
    )
    log_dir = ROOT / "artifacts/slack2drive/validation"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"mutation-{mutation}.log").write_text(result.stdout, encoding="utf-8")
    assert result.returncode == 1, result.stdout
    assert f"FAILED {node}" in result.stdout, result.stdout
    assert failure in result.stdout, result.stdout
    assert "ERROR collecting" not in result.stdout, result.stdout
