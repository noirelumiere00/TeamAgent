"""Owner-bound transport for an already sealed concrete case."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

from teamagent.media.contracts import MAX_INPUT_BYTES
from teamagent.skills.first_visit import contract


def pack_run(root: Path, owner_id: str) -> bytes:
    """Freeze the sealed inventory without authoring or resealing anything."""
    if not owner_id.strip() or len(owner_id) > 200:
        raise ValueError("concrete owner_id is required")
    manifest = contract._read(root / "manifest.json")
    if manifest != contract._manifest(root):
        raise ValueError("concrete sealed manifest mismatch")
    if "wire-manifest.json" in manifest["files"]:
        raise ValueError("reserved transfer manifest")
    paths = ["manifest.json", *manifest["files"]]
    if len(paths) + 1 > 5000 or sum((root / p).stat().st_size for p in paths) > MAX_INPUT_BYTES:
        raise ValueError("concrete snapshot exceeds transfer bound")
    wire: dict[str, Any] = {"schema_version": 1, "owner_id": owner_id, "files": {}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(paths):
            body = contract._path(root, relative).read_bytes()
            record = {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
            if relative != "manifest.json" and manifest["files"][relative] != {
                **record,
                "contract_version": contract.CONTRACT_VERSION,
            }:
                raise ValueError("concrete input changed while packing")
            wire["files"][relative] = record
            archive.writestr(zipfile.ZipInfo(relative), body)
        archive.writestr(zipfile.ZipInfo("wire-manifest.json"), contract._json_bytes(wire))
    if output.tell() > MAX_INPUT_BYTES:
        raise ValueError("concrete snapshot exceeds transfer bound")
    return output.getvalue()
