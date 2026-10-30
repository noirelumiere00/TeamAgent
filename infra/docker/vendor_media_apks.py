#!/usr/bin/env python3
"""Import once, retain in existing S3, then fetch media APKs without Alpine indexes.

import/publish are operator commands, never part of a release build. fetch is
read-only. Trust roots are the reviewed source/checksum locks and Alpine keys
inside the digest-pinned Chromium base, not keys supplied by the bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BUCKET = "teamagent-dev-image-release-evidence"
KMS_KEY = "arn:aws:kms:ap-northeast-1:718959508629:key/04b8ec11-c259-4312-b6c0-61cad47301e5"
REPOSITORIES = tuple(
    f"https://dl-cdn.alpinelinux.org/alpine/{branch}/{section}/aarch64"
    for branch in ("edge", "v3.24")
    for section in ("main", "community")
)
HEX = re.compile(r"[0-9a-f]{64}")
ENTRY = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]*-[0-9][A-Za-z0-9+_.~:-]*-r[0-9]+")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix=f".{path.name}-", dir=path.parent) as temporary:
        staged = Path(temporary) / "source.json"
        staged.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        staged.replace(path)


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes(), object_pairs_hook=unique_keys)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def package_rows(rows: Any) -> list[str]:
    if not isinstance(rows, list) or not rows or any(not isinstance(row, str) for row in rows):
        raise ValueError("inventory must be a nonempty list of package/version strings")
    if any(not ENTRY.fullmatch(row) for row in rows):
        raise ValueError("unsafe package/version in inventory")
    if rows != sorted(set(rows)):
        raise ValueError("inventory must be unique and sorted")
    names = [row.rsplit("-", 2)[0] for row in rows]
    if len(set(names)) != len(names):
        raise ValueError("inventory contains multiple versions of a package")
    return rows


def inventory(lock: Path) -> list[str]:
    raw = lock.read_text(encoding="ascii")
    rows = package_rows(raw.splitlines())
    if raw != "\n".join(rows) + "\n":
        raise ValueError("inventory must be newline-terminated")
    return rows


def additions(lock: Path, source: dict[str, Any]) -> list[str]:
    base = set(package_rows(source["base_inventory"]))
    return [row for row in inventory(lock) if row not in base]


def load_source(source_path: Path, lock: Path, *, published: bool = True) -> dict[str, Any]:
    source = read_json(source_path)
    if set(source) != {
        "schema_version",
        "architecture",
        "inventory_sha256",
        "verification_image",
        "base_inventory",
        "bundle",
    }:
        raise ValueError("unexpected APK source fields")
    if (
        type(source["schema_version"]) is not int
        or source["schema_version"] != 2
        or source["architecture"] != "aarch64"
    ):
        raise ValueError("unsupported APK source schema/architecture")
    if source["inventory_sha256"] != digest(lock):
        raise ValueError("APK source inventory SHA-256 mismatch")
    if not additions(lock, source):
        raise ValueError("APK source must have additional packages to import")
    if not isinstance(source["verification_image"], str) or not re.fullmatch(
        r"718959508629\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/"
        r"teamagent-mirror/chromium-headless:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}",
        source["verification_image"],
    ):
        raise ValueError("verification image must be a digest-pinned existing Chromium mirror")
    bundle = source["bundle"]
    if bundle is None:
        if published:
            raise ValueError("media APK bundle has not been imported/published")
        return source
    if not isinstance(bundle, dict) or set(bundle) != {
        "bucket",
        "key",
        "version_id",
        "sha256",
        "size_bytes",
    }:
        raise ValueError("unexpected APK bundle fields")
    if not isinstance(bundle["sha256"], str) or not HEX.fullmatch(bundle["sha256"]):
        raise ValueError("invalid APK bundle SHA-256")
    key = f"media-apks/aarch64/{source['inventory_sha256']}/{bundle['sha256']}.tar"
    if bundle["bucket"] != BUCKET or bundle["key"] != key:
        raise ValueError("APK bundle must use the fixed content-addressed evidence prefix")
    if type(bundle["size_bytes"]) is not int or bundle["size_bytes"] <= 0:
        raise ValueError("invalid APK bundle size")
    version = bundle["version_id"]
    if version is not None or published:
        if (
            not isinstance(version, str)
            or version == "null"
            or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,1024}", version)
        ):
            raise ValueError("APK bundle requires an exact S3 VersionId")
    return source


def checksums(path: Path, rows: list[str]) -> dict[str, str]:
    lines = path.read_text(encoding="ascii").splitlines()
    expected = [f"apks/{row}.apk" for row in rows]
    result = {}
    for line in lines:
        if not re.fullmatch(r"[0-9a-f]{64}  apks/[^/]+\.apk", line):
            raise ValueError("invalid APK SHA-256 lock row (first import required?)")
        sha, filename = line.split("  ")
        if filename in result:
            raise ValueError("duplicate APK checksum")
        result[filename] = sha
    if list(result) != expected or path.read_bytes() != ("\n".join(lines) + "\n").encode():
        raise ValueError("APK checksum lock must cover the additional packages exactly in order")
    return result


def verify_context(context: Path, lock: Path, checksum_lock: Path, source: dict[str, Any]) -> None:
    rows = additions(lock, source)
    hashes = checksums(checksum_lock, rows)
    expected = set(hashes) | {"inventory.lock", "SHA256SUMS", "manifest.json"}
    found = set()
    for path in context.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"non-regular APK context member: {path}")
        if path.is_dir():
            if path.relative_to(context).as_posix() != "apks":
                raise ValueError(f"unexpected APK context directory: {path}")
        else:
            found.add(path.relative_to(context).as_posix())
    if found != expected:
        raise ValueError("APK context has missing/extra files")
    if (context / "inventory.lock").read_bytes() != lock.read_bytes():
        raise ValueError("APK context inventory mismatch")
    if (context / "SHA256SUMS").read_bytes() != checksum_lock.read_bytes():
        raise ValueError("APK context checksum lock mismatch")
    manifest = read_json(context / "manifest.json")
    if (
        manifest.get("schema_version"),
        manifest.get("architecture"),
        manifest.get("inventory_sha256"),
    ) != (2, "aarch64", digest(lock)) or manifest.get("base_inventory") != source["base_inventory"]:
        raise ValueError("APK manifest identity mismatch")
    packages = manifest.get("packages")
    if not isinstance(packages, list) or len(packages) != len(rows):
        raise ValueError("APK manifest package count mismatch")
    for (filename, sha), package in zip(hashes.items(), packages, strict=True):
        path = context / filename
        if digest(path) != sha:
            raise ValueError(f"APK SHA-256 mismatch: {filename}")
        if not isinstance(package, dict) or (
            package.get("filename"),
            package.get("sha256"),
            package.get("size_bytes"),
        ) != (filename, sha, path.stat().st_size):
            raise ValueError(f"APK manifest checksum/size mismatch: {filename}")


def verify_signatures(context: Path, image: str, base_inventory: list[str]) -> None:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/arm64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,src={context.resolve()},dst=/vendor,readonly",
            "--entrypoint",
            "/bin/sh",
            image,
            "-ec",
            "apk --no-network verify /vendor/apks/*.apk >&2; "
            "apk list --installed | awk '{print $1}' | LC_ALL=C sort",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout != "\n".join(package_rows(base_inventory)) + "\n":
        raise ValueError("verification image base inventory mismatch")
    # shortcut: metadata supports APK v2; use apk-tools metadata queries when Alpine adopts v3.
    for path in sorted((context / "apks").glob("*.apk")):
        with tarfile.open(path, "r:gz") as archive:
            entries = [entry for entry in archive if entry.name == ".PKGINFO"]
            if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 1024 * 1024:
                raise ValueError(f"invalid APK metadata: {path.name}")
            stream = archive.extractfile(entries[0])
            assert stream is not None  # The metadata member is a regular file.
            with stream:
                info = stream.read().decode("utf-8")
        metadata = unique_keys(
            [
                (line.split(" = ", 1)[0], line.split(" = ", 1)[1])
                for line in info.splitlines()
                if line.startswith(("pkgname = ", "pkgver = ", "arch = "))
            ]
        )
        if f"{metadata.get('pkgname')}-{metadata.get('pkgver')}.apk" != path.name or metadata.get(
            "arch"
        ) not in {"aarch64", "noarch"}:
            raise ValueError(f"APK package/version/architecture mismatch: {path.name}")


def download(row: str, destination: Path, apk_dir: Path | None) -> str:
    if apk_dir is not None and (apk_dir / destination.name).is_file():
        shutil.copyfile(apk_dir / destination.name, destination)
        return "local:" + destination.name
    for repository in REPOSITORIES:
        url = f"{repository}/{row}.apk"
        try:
            with (
                urllib.request.urlopen(url, timeout=60) as response,
                destination.open("wb") as output,
            ):
                shutil.copyfileobj(response, output)
            return url
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    raise ValueError(
        f"APK unavailable at all direct URLs: {row}; recover exact signed APK via --apk-dir"
    )


def create_bundle(context: Path, output: Path) -> None:
    with tarfile.open(output, "w:", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(path for path in context.rglob("*") if path.is_file()):
            info = tarfile.TarInfo(path.relative_to(context).as_posix())
            info.size = path.stat().st_size
            info.mode = 0o644
            with path.open("rb") as stream:
                archive.addfile(info, stream)


def import_apks(source_path: Path, lock: Path, output: Path, apk_dir: Path | None) -> None:
    source = load_source(source_path, lock, published=False)
    rows = additions(lock, source)
    if output.exists():
        raise ValueError(f"refusing to replace existing import: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".media-apk-import-", dir=output.parent) as temporary:
        stage = Path(temporary) / "result"
        context = stage / "context"
        (context / "apks").mkdir(parents=True)
        packages = []
        for row in rows:
            filename = f"apks/{row}.apk"
            path = context / filename
            origin = download(row, path, apk_dir)
            packages.append(
                {
                    "filename": filename,
                    "sha256": digest(path),
                    "size_bytes": path.stat().st_size,
                    "source": origin,
                }
            )
        verify_signatures(context, source["verification_image"], source["base_inventory"])
        shutil.copyfile(lock, context / "inventory.lock")
        (context / "SHA256SUMS").write_text(
            "".join(f"{p['sha256']}  {p['filename']}\n" for p in packages), encoding="ascii"
        )
        write_json(
            context / "manifest.json",
            {
                "schema_version": 2,
                "architecture": "aarch64",
                "inventory_sha256": digest(lock),
                "base_inventory": source["base_inventory"],
                "packages": packages,
            },
        )
        verify_context(context, lock, context / "SHA256SUMS", source)
        archive = stage / "bundle.tar"
        create_bundle(context, archive)
        sha = digest(archive)
        source["bundle"] = {
            "bucket": BUCKET,
            "key": f"media-apks/aarch64/{digest(lock)}/{sha}.tar",
            "version_id": None,
            "sha256": sha,
            "size_bytes": archive.stat().st_size,
        }
        write_json(stage / "media-apk-source.json", source)
        shutil.copyfile(context / "SHA256SUMS", stage / "media-apk-sha256.lock")
        stage.rename(output)


def get_bundle(source: dict[str, Any], archive: Path) -> None:
    bundle = source["bundle"]
    result = subprocess.run(
        [
            "aws",
            "s3api",
            "get-object",
            "--bucket",
            bundle["bucket"],
            "--key",
            bundle["key"],
            "--version-id",
            bundle["version_id"],
            str(archive),
            "--output",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if json.loads(result.stdout).get("VersionId") != bundle["version_id"]:
        raise ValueError("S3 returned a different APK bundle VersionId")
    if archive.stat().st_size != bundle["size_bytes"] or digest(archive) != bundle["sha256"]:
        raise ValueError("APK bundle SHA-256/size mismatch")


def publish(import_dir: Path) -> None:
    context = import_dir / "context"
    source_path = import_dir / "media-apk-source.json"
    source = load_source(source_path, context / "inventory.lock", published=False)
    if source["bundle"] is None or source["bundle"]["version_id"] is not None:
        raise ValueError("publish requires an unpublished import")
    verify_context(
        context, context / "inventory.lock", import_dir / "media-apk-sha256.lock", source
    )
    verify_signatures(context, source["verification_image"], source["base_inventory"])
    archive = import_dir / "bundle.tar"
    # Rebuild from verified files so the upload cannot differ from the checked context.
    with tempfile.TemporaryDirectory(prefix="media-apk-publish-") as temporary:
        rebuilt = Path(temporary) / "bundle.tar"
        create_bundle(context, rebuilt)
        bundle = source["bundle"]
        if (
            digest(rebuilt) != bundle["sha256"]
            or digest(archive) != bundle["sha256"]
            or archive.stat().st_size != bundle["size_bytes"]
        ):
            raise ValueError("import bundle SHA-256/size mismatch")
        try:
            result = subprocess.run(
                [
                    "aws",
                    "s3api",
                    "put-object",
                    "--bucket",
                    BUCKET,
                    "--key",
                    bundle["key"],
                    "--body",
                    str(rebuilt),
                    "--if-none-match",
                    "*",
                    "--server-side-encryption",
                    "aws:kms",
                    "--ssekms-key-id",
                    KMS_KEY,
                    "--output",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as error:
            if "PreconditionFailed" not in (error.stderr or ""):
                raise
            # A previous PUT may have succeeded before readback/process failure.
            result = subprocess.run(
                [
                    "aws",
                    "s3api",
                    "head-object",
                    "--bucket",
                    BUCKET,
                    "--key",
                    bundle["key"],
                    "--output",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        bundle["version_id"] = json.loads(result.stdout).get("VersionId")
        if not isinstance(bundle["version_id"], str) or bundle["version_id"] in {"", "null"}:
            raise ValueError("S3 upload did not return an immutable VersionId")
        get_bundle(source, Path(temporary) / "readback.tar")
    write_json(source_path, source)


def fetch(source_path: Path, lock: Path, checksum_lock: Path, output: Path) -> None:
    source = load_source(source_path, lock)
    hashes = checksums(checksum_lock, additions(lock, source))
    if output.exists():
        raise ValueError(f"refusing to replace existing APK context: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".media-apk-fetch-", dir=output.parent) as temporary:
        stage = Path(temporary) / "context"
        stage.mkdir()
        archive_path = Path(temporary) / "bundle.tar"
        get_bundle(source, archive_path)
        expected = set(hashes) | {"inventory.lock", "SHA256SUMS", "manifest.json"}
        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            if len(members) != len(expected) or {member.name for member in members} != expected:
                raise ValueError("APK archive has missing/extra/duplicate members")
            if any(not member.isfile() or member.size < 0 for member in members):
                raise ValueError("APK archive contains a non-regular member")
            for member in members:
                destination = stage / member.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                assert stream is not None  # Every member was checked to be a regular file.
                with stream, destination.open("wb") as target:
                    shutil.copyfileobj(stream, target)
        verify_context(stage, lock, checksum_lock, source)
        stage.rename(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("import", "fetch", "verify"):
        sub = commands.add_parser(command)
        sub.add_argument("--source", type=Path, required=True)
        sub.add_argument("--lock", type=Path, required=True)
        if command == "import":
            sub.add_argument("--apk-dir", type=Path)
        else:
            sub.add_argument("--checksums", type=Path, required=True)
        sub.add_argument(
            "--output" if command != "verify" else "--context", type=Path, required=True
        )
    commands.add_parser("publish").add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            import_apks(args.source, args.lock, args.output, args.apk_dir)
        elif args.command == "publish":
            publish(args.input)
        elif args.command == "fetch":
            fetch(args.source, args.lock, args.checksums, args.output)
        else:
            source = load_source(args.source, args.lock)
            verify_context(args.context, args.lock, args.checksums, source)
            verify_signatures(args.context, source["verification_image"], source["base_inventory"])
    except (ValueError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f"media APK {args.command} failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr, end="" if error.stderr.endswith("\n") else "\n")
        return 1
    print(f"media APK {args.command}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
