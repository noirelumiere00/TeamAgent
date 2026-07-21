"""Canonical Docker context normalization and race-regression tests."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "infra/docker/canonical_build_context.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("canonical_build_context", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HELPER = _load_helper()
ContextError = HELPER.ContextError


def _fixture(root: Path, *, reverse: bool) -> None:
    entries = [
        ("z-last.txt", b"last\n", 0o600),
        ("nested/run.sh", b"#!/bin/sh\nexit 0\n", 0o751),
        ("a-first.txt", b"first\n", 0o640),
    ]
    if reverse:
        entries.reverse()
    for relative, body, mode in entries:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(mode)
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    os.symlink("../a-first.txt", root / "nested/link")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_canonical_tar_normalizes_order_owner_mode_mtime_and_xattrs(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _fixture(first, reverse=False)
    _fixture(second, reverse=True)
    for path in second.rglob("*"):
        if path.is_file() and not path.is_symlink():
            os.utime(path, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
            if path.name != "run.sh":
                path.chmod(0o666)
            if hasattr(os, "setxattr"):
                try:
                    os.setxattr(path, "user.teamagent-test", b"host-only")
                except OSError:
                    pass

    first_tar = tmp_path / "first.tar"
    second_tar = tmp_path / "second.tar"
    assert HELPER.create_canonical_tar(first, first_tar) == _sha256(first_tar)
    assert HELPER.create_canonical_tar(second, second_tar) == _sha256(second_tar)
    assert first_tar.read_bytes() == second_tar.read_bytes()

    with tarfile.open(first_tar, "r:") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(
        (member.name for member in members), key=lambda value: value.encode("utf-8")
    )
    assert all(member.uid == member.gid == 0 for member in members)
    assert all(member.uname == member.gname == "root" for member in members)
    assert all(member.mtime == 0 and not member.pax_headers for member in members)
    assert {
        member.name: member.mode for member in members if member.isfile() or member.issym()
    } == {
        "a-first.txt": 0o644,
        "nested/link": 0o777,
        "nested/run.sh": 0o755,
        "z-last.txt": 0o644,
    }


def test_canonical_tar_fails_closed_on_in_place_race(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    payload = context / "payload.txt"
    payload.write_text("before\n", encoding="utf-8")

    def mutate() -> None:
        payload.write_text("after\n", encoding="utf-8")

    output = tmp_path / "context.tar"
    with pytest.raises(ContextError, match="race detected"):
        HELPER.create_canonical_tar(context, output, before_final_check=mutate)
    assert not output.exists()


def test_retained_tar_is_unchanged_after_source_restore_mutation(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    payload = context / "payload.txt"
    payload.write_text("exact archive bytes\n", encoding="utf-8")
    output = tmp_path / "context.tar"
    digest = HELPER.create_canonical_tar(context, output)

    payload.write_text("restored directory was mutated\n", encoding="utf-8")
    assert _sha256(output) == digest
    with tarfile.open(output, "r:") as archive:
        extracted = archive.extractfile("payload.txt")
        assert extracted is not None
        assert extracted.read() == b"exact archive bytes\n"


def test_canonical_tar_rejects_escaping_symlink_and_output_inside_context(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    (context / "payload.txt").write_text("payload\n", encoding="utf-8")
    os.symlink("../../outside", context / "escape")
    with pytest.raises(ContextError, match="escaping"):
        HELPER.create_canonical_tar(context, tmp_path / "escape.tar")
    (context / "escape").unlink()
    with pytest.raises(ContextError, match="outside"):
        HELPER.create_canonical_tar(context, context / "context.tar")


def test_canonical_tar_verifier_rejects_metadata_mutation_and_optional_links(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    _fixture(context, reverse=False)
    canonical = tmp_path / "canonical.tar"
    digest = HELPER.create_canonical_tar(context, canonical)
    assert HELPER.verify_canonical_tar(canonical) == digest
    with pytest.raises(ContextError, match="contains a symlink"):
        HELPER.verify_canonical_tar(canonical, reject_symlinks=True)

    hostile = tmp_path / "hostile.tar"
    with tarfile.open(hostile, "w:", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo("payload.txt")
        info.uid = 501
        info.gid = 20
        info.uname = "host"
        info.gname = "staff"
        info.mtime = 0
        info.mode = 0o644
        body = b"payload"
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    with pytest.raises(ContextError, match="metadata"):
        HELPER.verify_canonical_tar(hostile)
