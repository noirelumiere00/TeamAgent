"""Offline archive builder: frozen ZIP evidence and optional Go stdlib self-tests."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/terraform_archive_2_8_0/x_dispatch.zip"


def test_regression_fixture_matches_tracked_handler_and_provider_zip_metadata() -> None:
    assert base64.b64encode(hashlib.sha256(FIXTURE.read_bytes()).digest()).decode() == (
        "eONRY7AJKh4sc2llELhKLXJ4ZcZAe8+t6ahb7EJTlmw="
    )
    with zipfile.ZipFile(FIXTURE) as archive:
        assert archive.namelist() == ["handler.py"]
        entry = archive.getinfo("handler.py")
        assert (
            archive.read(entry)
            == (ROOT / "infra/terraform/lambda/x_dispatch/handler.py").read_bytes()
        )
        assert entry.compress_type == zipfile.ZIP_DEFLATED
        assert entry.external_attr >> 16 == 0o100644
        assert entry.date_time == (2049, 1, 1, 0, 0, 0)
        assert entry.flag_bits == 8
        assert entry.extra == b""


def test_go_archive_builder_self_tests(tmp_path: Path) -> None:
    go = shutil.which("go")
    if go is None:
        pytest.skip("Go 1.25.8 is not installed; archive builder execution requires it")
    version = subprocess.run([go, "env", "GOVERSION"], capture_output=True, text=True, check=False)
    if version.stdout.strip() != "go1.25.8":
        # 圧縮の byte は Go の版で変わる。本物の ZIP は 1.25.8 でしか作らないので、他の版では検査しない。
        pytest.skip(
            f"Go 1.25.8 is required for byte-identical archives (found {version.stdout.strip()})"
        )
    result = subprocess.run(
        [go, "test", "build_dispatcher_archive.go", "build_dispatcher_archive_test.go"],
        cwd=ROOT / "infra/deploy",
        env={**os.environ, "GOTOOLCHAIN": "local", "GOCACHE": str(tmp_path / "go-cache")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
