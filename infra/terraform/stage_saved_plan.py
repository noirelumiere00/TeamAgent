#!/usr/bin/env python3
"""Stage one opaque Terraform plan from one source descriptor into a private inode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

_MAX_PLAN_BYTES = 4 * 1024 * 1024 * 1024


class PlanStagingError(RuntimeError):
    """The saved plan could not be copied without a mutable-path race."""


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def stage(source: Path, destination: Path) -> str:
    """Copy from one O_NOFOLLOW source open and return the exact staged SHA-256."""

    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        destination_flags |= os.O_NOFOLLOW
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = os.open(source, source_flags)
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAX_PLAN_BYTES
        ):
            raise PlanStagingError("saved plan source is not a bounded regular file")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        destination_fd = os.open(destination, destination_flags, 0o600)
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise PlanStagingError("saved plan staging write made no progress")
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        staged = os.fstat(destination_fd)
        if (
            _identity(before) != _identity(after)
            or copied != before.st_size
            or staged.st_size != copied
            or not stat.S_ISREG(staged.st_mode)
            or staged.st_nlink != 1
            or stat.S_IMODE(staged.st_mode) != 0o600
        ):
            raise PlanStagingError("saved plan changed while it was staged")
        return digest.hexdigest()
    except OSError as exc:
        raise PlanStagingError("saved plan could not be staged") from exc
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        digest = stage(args.source, args.destination)
    except PlanStagingError:
        try:
            args.destination.unlink()
        except FileNotFoundError:
            pass
        print('{"code":"saved_plan_staging_failed","ok":false}')
        return 2
    print(json.dumps({"ok": True, "sha256": digest}, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
