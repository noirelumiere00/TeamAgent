#!/usr/bin/env python3
"""Create one race-checked, host-independent Docker build-context tar."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import stat
import sys
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ContextError(ValueError):
    """The source directory cannot be represented as a canonical context."""


@dataclass(frozen=True)
class Snapshot:
    relative: str
    kind: str
    device: int
    inode: int
    size: int
    mode: int
    mtime_ns: int
    ctime_ns: int


def _snapshot(relative: str, metadata: os.stat_result) -> Snapshot:
    file_type = stat.S_IFMT(metadata.st_mode)
    if stat.S_ISDIR(file_type):
        kind = "directory"
    elif stat.S_ISREG(file_type):
        kind = "file"
    elif stat.S_ISLNK(file_type):
        kind = "symlink"
    else:
        raise ContextError(f"unsupported build-context entry: {relative}")
    return Snapshot(
        relative=relative,
        kind=kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mode=metadata.st_mode,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _walk(root: Path) -> Iterator[tuple[Path, Snapshot]]:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ContextError(f"cannot enumerate build context: {directory}") from exc
        entries.sort(key=lambda entry: os.fsencode(entry.name), reverse=True)
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if relative.startswith("/") or any(
                part in {"", ".", ".."} for part in PurePosixPath(relative).parts
            ):
                raise ContextError(f"unsafe build-context path: {relative}")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ContextError(f"cannot stat build-context entry: {relative}") from exc
            snapshot = _snapshot(relative, metadata)
            yield path, snapshot
            if snapshot.kind == "directory":
                pending.append(path)


def _entries(root: Path) -> list[tuple[Path, Snapshot]]:
    entries = list(_walk(root))
    entries.sort(key=lambda item: item[1].relative.encode("utf-8"))
    if not any(snapshot.kind != "directory" for _path, snapshot in entries):
        raise ContextError("build context has no files")
    return entries


def _same_snapshot(path: Path, expected: Snapshot) -> bool:
    try:
        actual = _snapshot(expected.relative, path.lstat())
    except (OSError, ContextError):
        return False
    return actual == expected


def _regular_body(path: Path, expected: Snapshot) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContextError(f"cannot open build-context file: {expected.relative}") from exc
    try:
        before = _snapshot(expected.relative, os.fstat(descriptor))
        if before != expected:
            raise ContextError(f"build-context race detected: {expected.relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            body = handle.read()
        after = _snapshot(expected.relative, os.fstat(descriptor))
        if after != expected or len(body) != expected.size:
            raise ContextError(f"build-context race detected: {expected.relative}")
        return body
    finally:
        os.close(descriptor)


def _symlink_target(path: Path, expected: Snapshot) -> str:
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise ContextError(f"cannot read build-context symlink: {expected.relative}") from exc
    if not _same_snapshot(path, expected):
        raise ContextError(f"build-context race detected: {expected.relative}")
    pure_target = PurePosixPath(target)
    if pure_target.is_absolute():
        raise ContextError(f"absolute build-context symlink: {expected.relative}")
    resolved: list[str] = []
    for part in PurePosixPath(expected.relative).parent.joinpath(pure_target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise ContextError(f"escaping build-context symlink: {expected.relative}")
            resolved.pop()
        else:
            resolved.append(part)
    return target


def _tar_info(snapshot: Snapshot) -> tarfile.TarInfo:
    info = tarfile.TarInfo(snapshot.relative)
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    info.pax_headers = {}
    if snapshot.kind == "directory":
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
    elif snapshot.kind == "symlink":
        info.type = tarfile.SYMTYPE
        info.mode = 0o777
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o755 if snapshot.mode & stat.S_IXUSR else 0o644
        info.size = snapshot.size
    return info


def create_canonical_tar(
    root: Path,
    output: Path,
    *,
    before_final_check: Callable[[], None] | None = None,
) -> str:
    """Write a canonical context and return its lowercase SHA-256."""

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ContextError("build-context root is not a directory")
    output = output.absolute()
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ContextError("canonical tar must be outside the build-context root")
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = _entries(root)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise ContextError(f"temporary output already exists: {temporary}")
    try:
        with tarfile.open(
            temporary,
            mode="w:",
            format=tarfile.GNU_FORMAT,
            dereference=False,
        ) as archive:
            for path, snapshot in entries:
                info = _tar_info(snapshot)
                if snapshot.kind == "file":
                    body = _regular_body(path, snapshot)
                    archive.addfile(info, fileobj=io.BytesIO(body))
                elif snapshot.kind == "symlink":
                    info.linkname = _symlink_target(path, snapshot)
                    archive.addfile(info)
                else:
                    archive.addfile(info)
        if before_final_check is not None:
            before_final_check()
        final_entries = _entries(root)
        if [snapshot for _path, snapshot in final_entries] != [
            snapshot for _path, snapshot in entries
        ]:
            raise ContextError("build-context race detected after archive creation")
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    digest = hashlib.sha256()
    with output.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_canonical_tar(
    archive_path: Path,
    *,
    reject_symlinks: bool = False,
) -> str:
    """Verify canonical metadata/order/path rules and return exact SHA-256."""

    archive_path = archive_path.resolve(strict=True)
    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        raise ContextError("canonical build-context tar is missing or empty")
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
    except tarfile.TarError as exc:
        raise ContextError("canonical build-context tar is invalid") from exc
    names = [member.name for member in members]
    if not names or names != sorted(names, key=lambda value: value.encode("utf-8")):
        raise ContextError("canonical build-context tar order is invalid")
    if len(names) != len(set(names)):
        raise ContextError("canonical build-context tar has duplicate paths")
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or member.name != path.as_posix()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ContextError("canonical build-context tar path is unsafe")
        if (
            member.uid != 0
            or member.gid != 0
            or member.uname != "root"
            or member.gname != "root"
            or member.mtime != 0
            or member.pax_headers
        ):
            raise ContextError("canonical build-context tar metadata is invalid")
        if member.isdir():
            expected_mode = 0o755
        elif member.isfile():
            expected_mode = 0o755 if member.mode & 0o111 else 0o644
        elif member.issym():
            if reject_symlinks:
                raise ContextError("canonical build-context tar contains a symlink")
            expected_mode = 0o777
            target = PurePosixPath(member.linkname)
            if target.is_absolute():
                raise ContextError("canonical build-context tar symlink is absolute")
            resolved: list[str] = []
            for part in path.parent.joinpath(target).parts:
                if part in {"", "."}:
                    continue
                if part == "..":
                    if not resolved:
                        raise ContextError("canonical build-context tar symlink escapes context")
                    resolved.pop()
                else:
                    resolved.append(part)
        else:
            raise ContextError("canonical build-context tar entry type is invalid")
        if member.mode != expected_mode:
            raise ContextError("canonical build-context tar mode is invalid")
    digest = hashlib.sha256()
    with archive_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "verify":
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=("verify",))
        parser.add_argument("archive", type=Path)
        parser.add_argument("--no-symlinks", action="store_true")
        args = parser.parse_args()
        try:
            print(
                verify_canonical_tar(
                    args.archive,
                    reject_symlinks=args.no_symlinks,
                )
            )
        except (ContextError, OSError, UnicodeError) as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 2
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        print(create_canonical_tar(args.root, args.output))
    except (ContextError, OSError, UnicodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
