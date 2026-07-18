#!/usr/bin/env python3
"""Create a deterministic index binding every local OpenClaw evidence file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--config-digest", required=True)
    parser.add_argument("--rootfs-inventory-sha256", required=True)
    args = parser.parse_args()

    evidence_dir = args.evidence_dir.resolve(strict=True)
    output = args.output.absolute()
    if output.parent.resolve(strict=True) != evidence_dir:
        raise ValueError("evidence index output must be inside the evidence directory")
    if output.is_symlink():
        raise ValueError("evidence index output must not be a symlink")
    for digest in (args.image_id, args.manifest_digest, args.config_digest):
        if not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"invalid subject digest: {digest!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", args.rootfs_inventory_sha256):
        raise ValueError("invalid rootfs inventory digest")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root, directories, filenames in os.walk(evidence_dir, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *filenames]:
            candidate = root_path / name
            if candidate.is_symlink():
                raise ValueError(f"evidence symlink is forbidden: {candidate}")
        for name in filenames:
            candidate = root_path / name
            if candidate.absolute() == output:
                continue
            if not candidate.is_file():
                raise ValueError(f"unsupported evidence object: {candidate}")
            relative = candidate.relative_to(evidence_dir).as_posix()
            if relative in seen:
                raise ValueError(f"duplicate evidence path: {relative}")
            seen.add(relative)
            entries.append(
                {
                    "path": relative,
                    "size": candidate.stat().st_size,
                    "sha256": sha256_file(candidate),
                }
            )
    entries.sort(key=lambda entry: entry["path"])
    if not entries:
        raise ValueError("evidence directory is empty")

    index = {
        "schemaVersion": 1,
        "subject": {
            "platform": "linux/arm64",
            "imageId": args.image_id,
            "manifestDigest": args.manifest_digest,
            "configDigest": args.config_digest,
            "rootfsInventorySha256": args.rootfs_inventory_sha256,
        },
        "entryCount": len(entries),
        "entries": entries,
        "allRegularEvidenceFilesBound": True,
        "symlinksAllowed": False,
    }
    write_json(output, index)

    rendered = json.loads(output.read_text(encoding="utf-8"))
    if rendered != index:
        raise ValueError("evidence index did not round-trip exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
