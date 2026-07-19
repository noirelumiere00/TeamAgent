#!/usr/bin/env python3
"""Recompute a Git SHA-1 tree from an extracted ``git archive`` source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path


class SourceTreeError(ValueError):
    """The extracted source cannot represent the expected Git tree."""


def _object_id(kind: str, body: bytes) -> bytes:
    framed = f"{kind} {len(body)}\0".encode() + body
    return hashlib.sha1(framed, usedforsecurity=False).digest()


def _tree_id(directory: Path) -> bytes:
    entries: list[tuple[bytes, bool, bytes, bytes]] = []
    with os.scandir(directory) as children:
        for child in children:
            name = os.fsencode(child.name)
            metadata = child.stat(follow_symlinks=False)
            path = Path(child.path)
            if stat.S_ISDIR(metadata.st_mode):
                mode = b"40000"
                object_id = _tree_id(path)
                is_directory = True
            elif stat.S_ISLNK(metadata.st_mode):
                mode = b"120000"
                object_id = _object_id("blob", os.fsencode(os.readlink(path)))
                is_directory = False
            elif stat.S_ISREG(metadata.st_mode):
                mode = b"100755" if metadata.st_mode & stat.S_IXUSR else b"100644"
                object_id = _object_id("blob", path.read_bytes())
                is_directory = False
            else:
                raise SourceTreeError(f"unsupported source entry: {path}")
            entries.append((name, is_directory, mode, object_id))
    entries.sort(key=lambda item: item[0] + (b"/" if item[1] else b"\0"))
    body = b"".join(
        mode + b" " + name + b"\0" + object_id for name, _is_directory, mode, object_id in entries
    )
    return _object_id("tree", body)


def verify_source_tree(root: Path, expected_tree: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", expected_tree) is None:
        raise SourceTreeError("expected tree must be a full SHA-1 object ID")
    root = root.resolve()
    if not root.is_dir() or (root / ".git").exists():
        raise SourceTreeError("source root must be an extracted archive without .git")
    actual = _tree_id(root).hex()
    if actual != expected_tree:
        raise SourceTreeError(f"source tree mismatch: expected {expected_tree}, got {actual}")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-tree", required=True)
    args = parser.parse_args()
    try:
        actual = verify_source_tree(args.root, args.expected_tree)
    except (OSError, SourceTreeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "tree": actual}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
