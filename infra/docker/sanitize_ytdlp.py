#!/usr/bin/env python3
"""Deterministically remove the out-of-scope Shahid extractor from yt-dlp."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected one exact match, got {text.count(old)}")
    return text.replace(old, new)


def source_tree_digest(root: Path) -> str:
    checksum = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix != ".pyc" and "__pycache__" not in item.parts
    ):
        checksum.update(path.relative_to(root).as_posix().encode("utf-8"))
        checksum.update(b"\0")
        checksum.update(path.read_bytes())
        checksum.update(b"\0")
    return checksum.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--expected-shahid-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    extractor = args.package_root / "extractor"
    shahid = extractor / "shahid.py"
    lazy = extractor / "lazy_extractors.py"
    registry = extractor / "_extractors.py"
    expected = args.expected_shahid_sha256
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("expected Shahid hash must be lowercase SHA-256")
    if digest(shahid) != expected:
        raise RuntimeError("yt-dlp Shahid source hash does not match the signed contract")

    registry_text = replace_once(
        registry.read_text(encoding="utf-8"),
        "from .shahid import (\n    ShahidIE,\n    ShahidShowIE,\n)\n",
        "",
        label="_extractors Shahid import",
    )
    registry.write_text(registry_text, encoding="utf-8")

    lazy_text = lazy.read_text(encoding="utf-8")
    lazy_text, count = re.subn(
        r"\nclass ShahidBaseIE\(AWSIE\):.*?(?=\nclass SharePointIE\()",
        "\n",
        lazy_text,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise RuntimeError("lazy extractor Shahid class block did not match exactly once")
    for name in ("ShahidIE", "ShahidShowIE"):
        lazy_text = replace_once(
            lazy_text,
            f"'{name}': {name}, ",
            "",
            label=f"lazy extractor lookup {name}",
        )
    lazy.write_text(lazy_text, encoding="utf-8")

    shahid.unlink()
    for compiled in extractor.rglob("shahid*.pyc"):
        compiled.unlink()
    if any(extractor.rglob("shahid*.pyc")):
        raise RuntimeError("compiled Shahid extractor remains")

    manifest = {
        "schema_version": 1,
        "action": "remove-out-of-scope-extractor",
        "removed": "yt_dlp/extractor/shahid.py",
        "removed_sha256": expected,
        "modified": {
            "yt_dlp/extractor/_extractors.py": digest(registry),
            "yt_dlp/extractor/lazy_extractors.py": digest(lazy),
        },
        "sanitized_source_tree_sha256": source_tree_digest(args.package_root),
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
