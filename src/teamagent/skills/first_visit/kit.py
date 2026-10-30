"""Verify the existing source archive and a Git-free installation, offline."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parents[4] / "tools/first_visit"
ARCHIVE = BUNDLE_DIR / "tiktok-skills-kit-25eccd8.tar.gz"
LOCK = BUNDLE_DIR / "kit.lock.json"


def archive_kit_problems(kit_path: Path, archive: Path = ARCHIVE) -> list[str]:
    """Only locked sources and installed Node dependencies may be present."""
    try:
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["archive_sha256"]:
            return ["kit: tar の SHA-256 が kit.lock.json と一致しません"]
        expected = set(lock["paths"])
        with tarfile.open(archive) as bundle:
            members = {m.name: m for m in bundle.getmembers() if not m.isdir()}
            if set(members) != expected or any(not m.isfile() for m in members.values()):
                return ["kit: tar のパスが kit.lock.json と一致しません"]
            for relative, member in members.items():
                path = kit_path / relative
                if (
                    Path(relative).is_absolute()
                    or ".." in Path(relative).parts
                    or not path.resolve().is_relative_to(kit_path.resolve())
                    or path.is_symlink()
                    or not path.is_file()
                ):
                    return [f"kit: 固定ファイルがありません: {relative}"]
                stream = bundle.extractfile(member)
                if stream is None or path.read_bytes() != stream.read():
                    return [f"kit: 固定ファイルが変更されています: {relative}"]
        for path in kit_path.rglob("*"):
            relative = path.relative_to(kit_path).as_posix()
            if relative == "tiktok-deck/node_modules" or relative.startswith(
                "tiktok-deck/node_modules/"
            ):
                continue
            if path.is_symlink() or (path.is_file() and relative not in expected):
                return [f"kit: lock に無いファイルは使用できません: {relative}"]
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        return [f"kit: 展開版を検証できません: {exc}"]
    return []
