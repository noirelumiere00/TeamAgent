"""Canonical measurement and sealing for an EC2 worker release tree."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

RELEASE_MANIFEST_NAME = ".release-tree.json"
RELEASE_DIGEST_NAME = ".release-tree-sha256"
RUNTIME_ENV_NAME = "runtime.env"
_EXCLUDED = frozenset({RELEASE_MANIFEST_NAME, RELEASE_DIGEST_NAME, RUNTIME_ENV_NAME})


class WorkerReleaseError(ValueError):
    """A release tree is mutable, malformed, or differs from its sealed measurement."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise WorkerReleaseError("release file is unreadable") from exc
    return digest.hexdigest()


def _relative_target(root: Path, path: Path, target: str) -> None:
    if os.path.isabs(target):
        raise WorkerReleaseError("absolute release symlink is forbidden")
    try:
        resolved = (path.parent / target).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkerReleaseError("release symlink escapes the immutable tree") from exc


def release_entries(root: Path) -> list[dict[str, Any]]:
    """Return a canonical, metadata-complete inventory without following symlinks."""

    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise WorkerReleaseError("release root is unavailable") from exc
    if not canonical_root.is_dir():
        raise WorkerReleaseError("release root is not a directory")
    entries: list[dict[str, Any]] = []
    try:
        paths = sorted(
            canonical_root.rglob("*"),
            key=lambda item: item.relative_to(canonical_root).as_posix(),
        )
    except OSError as exc:
        raise WorkerReleaseError("release tree cannot be enumerated") from exc
    for path in paths:
        relative = path.relative_to(canonical_root).as_posix()
        if relative in _EXCLUDED:
            continue
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise WorkerReleaseError("release entry disappeared") from exc
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            entry: dict[str, Any] = {"mode": mode, "path": relative, "type": "directory"}
        elif stat.S_ISREG(metadata.st_mode):
            entry = {
                "mode": mode,
                "path": relative,
                "sha256": file_sha256(path),
                "size": metadata.st_size,
                "type": "file",
            }
        elif stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(path)
            except OSError as exc:
                raise WorkerReleaseError("release symlink is unreadable") from exc
            _relative_target(canonical_root, path, target)
            entry = {
                "mode": mode,
                "path": relative,
                "target": target,
                "type": "symlink",
            }
        else:
            raise WorkerReleaseError("special files are forbidden in a release")
        entries.append(entry)
    if not entries:
        raise WorkerReleaseError("release tree is empty")
    return entries


def _manifest_bytes(entries: list[dict[str, Any]]) -> bytes:
    return (
        json.dumps(
            {"entries": entries, "kind": "teamagent.worker-release-tree", "schema": 1},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def release_tree_sha256(root: Path) -> str:
    return hashlib.sha256(_manifest_bytes(release_entries(root))).hexdigest()


def _write_exclusive(path: Path, payload: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise WorkerReleaseError("release metadata could not be sealed") from exc


def seal_release(
    root: Path,
    *,
    executable: Path,
    final_path: Path | None = None,
    final_root: Path | None = None,
) -> tuple[str, str]:
    """Write immutable measurement metadata and remove all tree write permissions."""

    if (final_path is None) == (final_root is None):
        raise WorkerReleaseError("exactly one final release location is required")
    executable_digest = file_sha256(executable)
    # Measure the final permission state. Metadata files are excluded from the manifest and are
    # created after application entries become immutable; the root remains writable only until
    # those metadata files have been durably created.
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        metadata = path.lstat()
        if not stat.S_ISLNK(metadata.st_mode):
            os.chmod(path, stat.S_IMODE(metadata.st_mode) & ~0o222)
    entries = release_entries(root)
    manifest = _manifest_bytes(entries)
    digest = hashlib.sha256(manifest).hexdigest()
    if final_path is not None:
        installed_path = final_path
    else:
        assert final_root is not None
        installed_path = final_root / digest
    _write_exclusive(root / RELEASE_MANIFEST_NAME, manifest, 0o400)
    _write_exclusive(root / RELEASE_DIGEST_NAME, f"{digest}\n".encode(), 0o400)
    runtime = (
        f"TEAMAGENT_HMAC_RELEASE_ROOT={installed_path}\n"
        f"TEAMAGENT_HMAC_RELEASE_TREE_SHA256={digest}\n"
        f"TEAMAGENT_HMAC_RUNTIME_EXECUTABLE_SHA256={executable_digest}\n"
    ).encode()
    _write_exclusive(root / RUNTIME_ENV_NAME, runtime, 0o400)

    root_metadata = root.lstat()
    os.chmod(root, stat.S_IMODE(root_metadata.st_mode) & ~0o222)
    return digest, executable_digest


def verify_release(root: Path, *, expected_sha256: str) -> bool:
    """Re-measure every entry and require exact sealed manifest/digest/permissions."""

    if len(expected_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in expected_sha256
    ):
        return False
    try:
        manifest_path = root / RELEASE_MANIFEST_NAME
        digest_path = root / RELEASE_DIGEST_NAME
        stored_manifest = manifest_path.read_bytes()
        stored_digest = digest_path.read_text(encoding="ascii").strip()
        current_manifest = _manifest_bytes(release_entries(root))
        if (
            stored_digest != expected_sha256
            or hashlib.sha256(stored_manifest).hexdigest() != expected_sha256
            or stored_manifest != current_manifest
        ):
            return False
        for path in (root, *root.rglob("*")):
            metadata = path.lstat()
            if not stat.S_ISLNK(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) & 0o222:
                return False
    except (OSError, UnicodeError, WorkerReleaseError):
        return False
    return True


__all__ = [
    "RELEASE_DIGEST_NAME",
    "RELEASE_MANIFEST_NAME",
    "RUNTIME_ENV_NAME",
    "WorkerReleaseError",
    "file_sha256",
    "release_entries",
    "release_tree_sha256",
    "seal_release",
    "verify_release",
]
