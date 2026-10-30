#!/usr/bin/env python3
"""Apply pinned Wolfi CA/pip-wheel APKs to the Python runtime, fail closed."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import tarfile
import zlib
from pathlib import Path

DB = "usr/lib/apk/db/installed"
NAME = "ca-certificates-bundle"
CERT = "etc/ssl/certs/ca-certificates.crt"


def q1(data: bytes) -> str:
    return "Q1" + base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")


def load_package(apk: Path, lock_path: Path, apk_sha256: str, version: str) -> dict:
    lock = json.loads(lock_path.read_bytes())
    name = lock["apkindex"]["P"]
    if name not in (NAME, "py3-pip-wheel"):
        raise ValueError("unexpected replacement package")
    raw = apk.read_bytes()
    if hashlib.sha256(raw).hexdigest() != apk_sha256 or apk_sha256 != lock["archive_sha256"]:
        raise ValueError("APK SHA-256 differs from the build argument/lock")
    index = lock["apkindex"]
    if (index["P"], index["V"], index["A"], int(index["S"])) != (
        name,
        version,
        "aarch64",
        len(raw),
    ):
        raise ValueError("APK identity/size differs from the lock")

    # APK v2 C is the SHA-1 of the compressed control member, not of the whole APK.
    control = zlib.decompressobj(wbits=31)
    control.decompress(raw)
    if not control.eof or not control.unused_data:
        raise ValueError("expected APK v2 control and data gzip members")
    if q1(raw[: len(raw) - len(control.unused_data)]) != index["C"]:
        raise ValueError("APK control checksum differs from APKINDEX")

    sbom = f"var/lib/db/sbom/{name}-{version}.spdx.json"
    payload = CERT if name == NAME else wheel_path(version)
    files, links, inventory = {}, {}, []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        info = archive.extractfile(".PKGINFO").read().decode()
        metadata: dict[str, list[str]] = {}
        for line in info.splitlines():
            if " = " in line:
                key, value = line.split(" = ", 1)
                metadata.setdefault(key, []).append(value)
        for field, key in {
            "P": "pkgname",
            "V": "pkgver",
            "A": "arch",
            "I": "size",
            "T": "pkgdesc",
            "U": "url",
            "L": "license",
            "o": "origin",
            "m": "maintainer",
            "t": "builddate",
            "c": "commit",
            "D": "depend",
            "k": "provider_priority",
        }.items():
            if " ".join(metadata.get(key, [])) != index.get(field, ""):
                raise ValueError(f"APK .PKGINFO differs from APKINDEX: {field}")
        if hashlib.sha256(control.unused_data).hexdigest() != metadata["datahash"][0]:
            raise ValueError("APK datahash mismatch")

        directory = None
        for member in archive:
            if member.name in (".PKGINFO", ".melange.yaml"):
                continue
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe APK path")
            if member.isdir():
                directory = member.name
                inventory.append(f"F:{directory}")
                if (member.uid, member.gid, member.mode) != (0, 0, 0o755):
                    raise ValueError("unexpected APK directory ownership/mode")
                continue
            if str(path.parent) != directory:
                raise ValueError("APK file without its parent directory")
            if member.isfile() and member.name in (payload, sbom):
                data = archive.extractfile(member).read()
                files[member.name] = data
                expected_mode = 0o644
            elif member.issym() and name == NAME:
                data = member.linkname.encode()
                links[member.name] = member.linkname
                expected_mode = 0o777
            else:
                raise ValueError(f"unexpected APK payload: {member.name}")
            if (member.uid, member.gid, member.mode) != (0, 0, expected_mode):
                raise ValueError("unexpected APK file ownership/mode")
            if hashlib.sha1(data).hexdigest() != member.pax_headers["APK-TOOLS.checksum.SHA1"]:
                raise ValueError("APK file checksum mismatch")
            inventory.append(f"R:{path.name}")
            if member.issym():
                inventory.append("a:0:0:777")
            inventory.append(f"Z:{q1(data)}")
    if set(files) != {payload, sbom} or len(links) != (3 if name == NAME else 0):
        raise ValueError("unexpected package file set")
    record = "\n".join([*(f"{key}:{value}" for key, value in index.items()), *inventory])
    return {"lock": lock, "record": record.encode(), "files": files, "links": links}


def replaced_db(before: bytes, package: dict) -> bytes:
    name = package["lock"]["apkindex"]["P"]
    blocks = before.split(b"\n\n")
    positions = [i for i, block in enumerate(blocks) if f"P:{name}".encode() in block.splitlines()]
    if len(positions) != 1:
        raise ValueError("expected exactly one base package record")
    position = positions[0]
    if f"V:{package['lock']['base_version']}".encode() not in blocks[position].splitlines():
        raise ValueError("unexpected base version")
    blocks[position] = package["record"]
    return b"\n\n".join(blocks)


def old_sbom(package: dict) -> str:
    lock = package["lock"]
    return f"var/lib/db/sbom/{lock['apkindex']['P']}-{lock['base_version']}.spdx.json"


def wheel_path(version: str) -> str:
    return f"usr/share/python-wheels/pip-{version.rsplit('-r', 1)[0]}-py3-none-any.whl"


def stale_paths(package: dict) -> list[str]:
    paths = [old_sbom(package)]
    if package["lock"]["apkindex"]["P"] == "py3-pip-wheel":
        paths.append(wheel_path(package["lock"]["base_version"]))
    return paths


def verify(root: Path, before: bytes, packages: list[dict]) -> None:
    # Exact bytes bind both scanners' installed inventory and every other package block.
    after = before
    for package in packages:
        after = replaced_db(after, package)
    if (root / DB).read_bytes() != after:
        raise ValueError("installed DB changed outside the exact replacement block")
    for package in packages:
        verify_payload(root, package)


def verify_payload(root: Path, package: dict) -> None:
    for path, data in package["files"].items():
        if (root / path).is_symlink() or (root / path).read_bytes() != data:
            raise ValueError(f"payload differs from the pinned APK: {path}")
    for path, target in package["links"].items():
        if not (root / path).is_symlink() or os.readlink(root / path) != target:
            raise ValueError(f"CA symlink differs from the pinned APK: {path}")
    for path in stale_paths(package):
        if (root / path).exists() or (root / path).is_symlink():
            raise ValueError(f"stale package payload/SBOM remains: {path}")


def prepare(root: Path, packages: list[dict]) -> None:
    before = (root / DB).read_bytes()
    after = before
    for package in packages:
        after = replaced_db(after, package)
    for package in packages:
        prepare_payload(root, package)
    (root / DB).write_bytes(after)
    verify(root, before, packages)


def prepare_payload(root: Path, package: dict) -> None:
    for path, target in package["links"].items():
        if not (root / path).is_symlink() or os.readlink(root / path) != target:
            raise ValueError(f"base CA symlink differs from the pinned APK: {path}")
    for path, data in package["files"].items():
        destination = root / path
        if destination.is_symlink():
            raise ValueError(f"unexpected payload symlink: {path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(0o644)
    for path in stale_paths(package):
        (root / path).unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "finalize"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True, action="append")
    parser.add_argument("--lock", type=Path, required=True, action="append")
    parser.add_argument("--base-db", type=Path, required=True)
    parser.add_argument("--apk-sha256", action="append")
    parser.add_argument("--version", action="append")
    args = parser.parse_args()
    hashes = args.apk_sha256 or [os.environ.get("CA_CERTIFICATES_BUNDLE_APK_SHA256")]
    versions = args.version or [os.environ.get("CA_CERTIFICATES_BUNDLE_VERSION")]
    packages = [
        load_package(apk, lock, digest, version)
        for apk, lock, digest, version in zip(args.apk, args.lock, hashes, versions, strict=True)
    ]
    if args.mode == "prepare":
        args.base_db.write_bytes((args.root / DB).read_bytes())
        prepare(args.root, packages)
    else:
        for package in packages:
            for path in stale_paths(package):
                (args.root / path).unlink()
        verify(args.root, args.base_db.read_bytes(), packages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
