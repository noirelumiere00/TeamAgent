#!/usr/bin/env python3
"""Apply pinned Wolfi CA, pip-wheel and Python APKs, fail closed."""

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
PYTHON_PACKAGES = {
    "python-3.14",
    "python-3.14-base",
    "python-3.14-runtime",
    "python-3.14-dev",
    "python-3.14-base-dev",
}


def q1(data: bytes) -> str:
    return "Q1" + base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")


def load_package(apk: Path, lock_path: Path, apk_sha256: str, version: str) -> dict:
    lock = json.loads(lock_path.read_bytes())
    name = lock["apkindex"]["P"]
    python = name in PYTHON_PACKAGES
    if name not in (NAME, "py3-pip-wheel") and not python:
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
    files, links, modes, inventory = {}, {}, {}, []
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
            "p": "provides",
        }.items():
            if " ".join(metadata.get(key, [])) != index.get(field, ""):
                raise ValueError(f"APK .PKGINFO differs from APKINDEX: {field}")
        if hashlib.sha256(control.unused_data).hexdigest() != metadata["datahash"][0]:
            raise ValueError("APK datahash mismatch")

        directory = None
        directories = set()
        for member in archive:
            if member.name in (".PKGINFO", ".melange.yaml"):
                continue
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe APK path")
            if member.isdir():
                directory = member.name
                directories.add(directory)
                inventory.append(f"F:{directory}")
                if (member.uid, member.gid, member.mode) != (0, 0, 0o755):
                    raise ValueError("unexpected APK directory ownership/mode")
                continue
            if str(path.parent) != directory:
                if not python or str(path.parent) not in directories:
                    raise ValueError("APK file without its parent directory")
                directory = str(path.parent)
                inventory.append(f"F:{directory}")
            if member.isfile() and (python or member.name in (payload, sbom)):
                data = archive.extractfile(member).read()
                files[member.name] = data
                expected_mode = member.mode if python and member.mode in (0o644, 0o755) else 0o644
                modes[member.name] = expected_mode
            elif member.issym() and (name == NAME or python):
                if python and (
                    Path(member.linkname).is_absolute() or ".." in Path(member.linkname).parts
                ):
                    raise ValueError("unsafe Python symlink")
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
            if expected_mode != 0o644:
                inventory.append(f"a:0:0:{expected_mode:o}")
            inventory.append(f"Z:{q1(data)}")
    if python:
        if sbom not in files or (name == "python-3.14-base" and "usr/bin/python3.14" not in files):
            raise ValueError("missing Python payload/SBOM")
    elif set(files) != {payload, sbom} or len(links) != (3 if name == NAME else 0):
        raise ValueError("unexpected package file set")
    record = "\n".join([*(f"{key}:{value}" for key, value in index.items()), *inventory])
    return {"lock": lock, "record": record.encode(), "files": files, "links": links, "modes": modes}


def replaced_db(before: bytes, package: dict) -> bytes:
    name = package["lock"]["apkindex"]["P"]
    blocks = before.split(b"\n\n")
    positions = [i for i, block in enumerate(blocks) if f"P:{name}".encode() in block.splitlines()]
    if package["lock"]["base_version"] is None:
        if positions or name != "python-3.14-runtime":
            raise ValueError("expected new Python runtime package to be absent")
        return before.rstrip(b"\n") + b"\n\n" + package["record"] + b"\n\n"
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


def stale_paths(package: dict, before: bytes, retained: set[str]) -> list[str]:
    if package["lock"]["apkindex"]["P"] in PYTHON_PACKAGES:
        paths = []
        for block in before.decode().split("\n\n"):
            if f"P:{package['lock']['apkindex']['P']}" not in block.splitlines():
                continue
            directory = ""
            for line in block.splitlines():
                if line.startswith("F:"):
                    directory = line[2:]
                elif line.startswith("R:"):
                    path = str(Path(directory) / line[2:])
                    if Path(path).is_absolute() or ".." in Path(path).parts:
                        raise ValueError("unsafe installed DB path")
                    if path not in retained:
                        paths.append(path)
        return paths
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
    retained = {
        path for package in packages for kind in ("files", "links") for path in package[kind]
    }
    for package in packages:
        verify_payload(root, package, stale_paths(package, before, retained))


def verify_payload(root: Path, package: dict, stale: list[str]) -> None:
    for path, data in package["files"].items():
        if (root / path).is_symlink() or (root / path).read_bytes() != data:
            raise ValueError(f"payload differs from the pinned APK: {path}")
        if (root / path).stat().st_mode & 0o777 != package["modes"][path]:
            raise ValueError(f"payload mode differs from the pinned APK: {path}")
    for path, target in package["links"].items():
        if not (root / path).is_symlink() or os.readlink(root / path) != target:
            raise ValueError(f"CA symlink differs from the pinned APK: {path}")
    for path in stale:
        if (root / path).exists() or (root / path).is_symlink():
            raise ValueError(f"stale package payload/SBOM remains: {path}")


def prepare(root: Path, packages: list[dict]) -> None:
    before = (root / DB).read_bytes()
    after = before
    for package in packages:
        after = replaced_db(after, package)
    # Python's new metapackage requires the split runtime and matching development packages.
    providers = set()
    python_dependencies = []
    for block in after.decode().split("\n\n"):
        fields = dict(
            line.split(":", 1)
            for line in block.splitlines()
            if line.startswith(("P:", "V:", "p:", "D:"))
        )
        if "P" in fields:
            providers.update((fields["P"], fields["P"] + "=" + fields["V"]))
            for value in fields.get("p", "").split():
                providers.update((value, value.split("=", 1)[0]))
            if fields["P"] in PYTHON_PACKAGES:
                python_dependencies.extend(fields.get("D", "").split())
    if any(package["lock"]["apkindex"]["P"] in PYTHON_PACKAGES for package in packages):
        for dep in python_dependencies:
            if dep not in providers:
                raise ValueError(f"unsatisfied Python package dependency: {dep}")
    retained = {
        path for package in packages for kind in ("files", "links") for path in package[kind]
    }
    stale = [stale_paths(package, before, retained) for package in packages]
    for package, paths in zip(packages, stale, strict=True):
        prepare_payload(root, package, paths)
    (root / DB).write_bytes(after)
    verify(root, before, packages)


def prepare_payload(root: Path, package: dict, stale: list[str]) -> None:
    for path, target in package["links"].items():
        if package["lock"]["apkindex"]["P"] == NAME and (
            not (root / path).is_symlink() or os.readlink(root / path) != target
        ):
            raise ValueError(f"base CA symlink differs from the pinned APK: {path}")
    for path, data in package["files"].items():
        destination = root / path
        if destination.is_symlink():
            raise ValueError(f"unexpected payload symlink: {path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Rename keeps the running Python and mapped libpython on their original inodes.
        temporary = destination.with_name(destination.name + ".replacement")
        temporary.write_bytes(data)
        temporary.chmod(package["modes"][path])
        temporary.replace(destination)
    for path, target in package["links"].items():
        if package["lock"]["apkindex"]["P"] in PYTHON_PACKAGES:
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.unlink(missing_ok=True)
            destination.symlink_to(target)
    for path in stale:
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
        before = args.base_db.read_bytes()
        retained = {
            path for package in packages for kind in ("files", "links") for path in package[kind]
        }
        for package in packages:
            for path in stale_paths(package, before, retained):
                (args.root / path).unlink()
        verify(args.root, before, packages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
