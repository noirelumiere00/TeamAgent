"""Bounded concrete transport and offline fixed-kit assembly in the render child."""

from __future__ import annotations

import hashlib
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from teamagent.media.contracts import MAX_INPUT_BYTES
from teamagent.media.operations import MediaOperationError
from teamagent.skills.first_visit import contract
from teamagent.skills.first_visit.concrete import build_concrete


def unpack_snapshot(source: Path, root: Path, *, owner_id: str) -> None:
    """Reject unsafe ZIPs and verify both transport and sealed inventories."""
    try:
        if not 0 < source.stat().st_size <= MAX_INPUT_BYTES:
            raise ValueError("input bound")
        root.mkdir(exist_ok=False)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            names = [entry.filename for entry in members]
            if (
                not 1 <= len(members) <= 5000
                or len(set(names)) != len(names)
                or sum(entry.file_size for entry in members) > MAX_INPUT_BYTES
            ):
                raise ValueError("membership or expanded bound")
            for entry in members:
                name = entry.filename
                if (
                    entry.is_dir()
                    or "\\" in name
                    or PurePosixPath(name).as_posix() != name
                    or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}
                    or entry.flag_bits & 1
                ):
                    raise ValueError("plain file required")
                destination = contract._path(root, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, destination.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
        files = {
            name: {
                "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
                "size": (root / name).stat().st_size,
            }
            for name in names
            if name != "wire-manifest.json"
        }
        if contract._read(root / "wire-manifest.json") != {
            "schema_version": 1,
            "owner_id": owner_id,
            "files": files,
        }:
            raise ValueError("owner or transport inventory mismatch")
        (root / "wire-manifest.json").unlink()
        if contract._read(root / "manifest.json") != contract._manifest(root):
            raise ValueError("sealed inventory mismatch")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError):
        # ZIP names and contents must never reach renderer stderr/logs.
        raise MediaOperationError(
            "MEDIA_CONCRETE_INPUT_INVALID", "concrete snapshot is invalid"
        ) from None


def render_concrete(
    workdir: Path,
    source: Path,
    *,
    owner_id: str,
    request_id: str,
    kit_path: Path,
    timeout: float,
) -> dict[str, Any]:
    inputs = workdir / "concrete-input"
    unpack_snapshot(source, inputs, owner_id=owner_id)
    result = build_concrete(inputs, kit_path=kit_path, request_id=request_id, timeout=timeout)
    payload = result.model_dump(mode="json")
    payload.update(
        owner_id=owner_id,
        request_id=request_id,
        input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    if result.pptx_path is not None:
        report = result.pptx_path.parents[1] / "preflight.json"
        if report.is_file():
            shutil.copyfile(report, workdir / "concrete-preflight.json")
    payload["pptx_path"] = None
    if result.status == "build_verified" and result.pptx_path is not None:
        shutil.copyfile(result.pptx_path, workdir / "concrete.pptx")
        payload["pptx_path"] = "concrete.pptx"
    (workdir / "concrete-result.json").write_bytes(contract._json_bytes(payload))
    return {"status": result.status, "slide_count": result.slide_count, "page_qa": "not_available"}
