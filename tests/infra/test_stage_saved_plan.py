from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "terraform" / "stage_saved_plan.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("stage_saved_plan_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STAGER = _load_module()


def test_stage_creates_one_private_inode_with_exact_digest(tmp_path: Path) -> None:
    source = tmp_path / "source.tfplan"
    destination = tmp_path / "private" / "saved.tfplan"
    payload = b"opaque-terraform-plan"
    source.write_bytes(payload)

    digest = STAGER.stage(source, destination)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert destination.read_bytes() == payload
    metadata = destination.stat()
    assert metadata.st_nlink == 1
    assert metadata.st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700


def test_source_path_replacement_attempt_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.tfplan"
    replacement = tmp_path / "replacement.tfplan"
    destination = tmp_path / "private" / "saved.tfplan"
    original = b"a" * (1024 * 1024 + 17)
    source.write_bytes(original)
    replacement.write_bytes(b"malicious replacement")
    real_read = STAGER.os.read
    replaced = False

    def replace_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(descriptor, size)
        if chunk and not replaced:
            os.replace(replacement, source)
            replaced = True
        return chunk

    monkeypatch.setattr(STAGER.os, "read", replace_after_first_read)

    with pytest.raises(STAGER.PlanStagingError, match="changed while"):
        STAGER.stage(source, destination)

    assert replaced
    assert source.read_bytes() == b"malicious replacement"


def test_in_place_source_mutation_during_copy_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.tfplan"
    destination = tmp_path / "private" / "saved.tfplan"
    source.write_bytes(b"a" * (1024 * 1024 + 17))
    real_read = STAGER.os.read
    mutated = False

    def mutate_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if chunk and not mutated:
            with source.open("r+b") as handle:
                handle.seek(1024 * 1024)
                handle.write(b"b" * 17)
                handle.flush()
                os.fsync(handle.fileno())
            mutated = True
        return chunk

    monkeypatch.setattr(STAGER.os, "read", mutate_after_first_read)

    with pytest.raises(STAGER.PlanStagingError, match="changed while"):
        STAGER.stage(source, destination)


def test_symlink_source_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real.tfplan"
    source = tmp_path / "source.tfplan"
    real.write_bytes(b"opaque")
    source.symlink_to(real)

    with pytest.raises(STAGER.PlanStagingError):
        STAGER.stage(source, tmp_path / "private" / "saved.tfplan")
