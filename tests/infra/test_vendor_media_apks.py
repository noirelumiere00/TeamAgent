"""All I/O at the Alpine/Docker/S3 boundaries is local or mocked."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "vendor_media_apks", ROOT / "infra/docker/vendor_media_apks.py"
)
vendor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vendor)


def apk(path: Path, *, name: str = "alpha", version: str = "1.0-r0", arch: str = "aarch64") -> None:
    with tarfile.open(path, "w:gz") as archive:
        body = f"pkgname = {name}\npkgver = {version}\narch = {arch}\n".encode()
        member = tarfile.TarInfo(".PKGINFO")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    lock = tmp_path / "inventory.lock"
    lock.write_text("alpha-1.0-r0\nbeta-2.0-r1\n")
    source = json.loads((ROOT / "infra/docker/media-apk-source.json").read_text())
    source["inventory_sha256"] = vendor.digest(lock)
    source["base_inventory"] = ["base-1.0-r0"]
    source["bundle"] = None
    source_path = tmp_path / "source.json"
    vendor.write_json(source_path, source)
    cache = tmp_path / "cache"
    cache.mkdir()
    apk(cache / "alpha-1.0-r0.apk")
    apk(cache / "beta-2.0-r1.apk", name="beta", version="2.0-r1", arch="noarch")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == "docker", "unexpected real AWS/network command"
        base = vendor.read_json(source_path)["base_inventory"]
        return subprocess.CompletedProcess(command, 0, "\n".join(base) + "\n", "")

    monkeypatch.setattr(vendor.subprocess, "run", run)
    monkeypatch.setattr(
        vendor.urllib.request, "urlopen", lambda *a, **k: pytest.fail("unexpected download")
    )
    return lock, source_path, cache, calls


def imported(inputs, tmp_path):
    lock, source, cache, _ = inputs
    output = tmp_path / "imported"
    vendor.import_apks(source, lock, output, cache)
    return output


def published_source(output: Path) -> dict:
    source = vendor.read_json(output / "media-apk-source.json")
    source["bundle"]["version_id"] = "immutable-version"
    vendor.write_json(output / "media-apk-source.json", source)
    return source


def mock_get(monkeypatch, archive: Path, *, version="immutable-version"):
    def run(command, **kwargs):
        assert command[:3] == ["aws", "s3api", "get-object"]
        assert command[command.index("--version-id") + 1] == "immutable-version"
        destination = Path(command[command.index("--version-id") + 2])
        destination.write_bytes(archive.read_bytes())
        return subprocess.CompletedProcess(command, 0, json.dumps({"VersionId": version}), "")

    monkeypatch.setattr(vendor.subprocess, "run", run)


def test_import_covers_every_package_and_is_deterministic(inputs, tmp_path):
    lock, source, cache, calls = inputs
    output = imported(inputs, tmp_path)
    other = tmp_path / "second"
    vendor.import_apks(source, lock, other, cache)
    assert vendor.digest(output / "bundle.tar") == vendor.digest(other / "bundle.tar")
    record = vendor.read_json(output / "media-apk-source.json")
    assert record["bundle"]["version_id"] is None
    assert record["bundle"]["bucket"] == vendor.BUCKET
    assert record["bundle"]["key"].endswith(vendor.digest(output / "bundle.tar") + ".tar")
    assert list(vendor.checksums(output / "media-apk-sha256.lock", vendor.inventory(lock))) == [
        "apks/alpha-1.0-r0.apk",
        "apks/beta-2.0-r1.apk",
    ]
    assert len(calls) == 2
    assert "--network" in calls[0] and calls[0][calls[0].index("--network") + 1] == "none"
    assert "apk --no-network verify /vendor/apks/*.apk >&2" in calls[0][-1]
    assert record["verification_image"] in calls[0]
    assert "--allow-untrusted" not in str(calls)
    assert not list(tmp_path.glob(".media-apk-import-*"))


def test_only_exact_base_versions_are_excluded(inputs, tmp_path):
    lock, source_path, cache, _ = inputs
    source = vendor.read_json(source_path)
    source["base_inventory"] = ["alpha-1.0-r0", "base-1.0-r0", "beta-1.0-r0"]
    vendor.write_json(source_path, source)
    (cache / "alpha-1.0-r0.apk").unlink()
    output = imported(inputs, tmp_path)
    assert list((output / "context/apks").iterdir()) == [output / "context/apks/beta-2.0-r1.apk"]
    assert (output / "context/inventory.lock").read_bytes() == lock.read_bytes()
    assert list(
        vendor.checksums(output / "media-apk-sha256.lock", vendor.additions(lock, source))
    ) == ["apks/beta-2.0-r1.apk"]
    vendor.verify_context(output / "context", lock, output / "media-apk-sha256.lock", source)
    source["base_inventory"] = ["base-1.0-r0"]
    with pytest.raises(ValueError, match="additional packages"):
        vendor.verify_context(output / "context", lock, output / "media-apk-sha256.lock", source)


def test_import_rejects_unproven_base_inventory(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(
        vendor.subprocess,
        "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, "other-1-r0\n", ""),
    )
    with pytest.raises(ValueError, match="base inventory mismatch"):
        imported(inputs, tmp_path)
    assert not (tmp_path / "imported").exists()


@pytest.mark.parametrize("base", [[], ["base-1-r0", "base-1-r0"], ["../base-1-r0"], [None]])
def test_source_rejects_invalid_base_inventory(inputs, base):
    lock, source_path, _, _ = inputs
    source = vendor.read_json(source_path)
    source["base_inventory"] = base
    vendor.write_json(source_path, source)
    with pytest.raises(ValueError):
        vendor.load_source(source_path, lock, published=False)


def test_manifest_cannot_change_the_base_inventory(inputs, tmp_path):
    output = imported(inputs, tmp_path)
    manifest_path = output / "context/manifest.json"
    manifest = vendor.read_json(manifest_path)
    manifest["base_inventory"] = ["other-1-r0"]
    vendor.write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="manifest identity"):
        vendor.verify_context(
            output / "context",
            inputs[0],
            output / "media-apk-sha256.lock",
            vendor.read_json(output / "media-apk-source.json"),
        )


@pytest.mark.parametrize(
    "rows",
    [
        "alpha-1.0-r0\nalpha-1.0-r0\n",
        "../alpha-1.0-r0\n",
        "alpha-1.0-r0",
        "beta-2.0-r1\nalpha-1.0-r0\n",
        "alpha-1.0-r0\nalpha-2.0-r0\n",
    ],
)
def test_inventory_rejects_unsafe_duplicate_or_noncanonical_rows(tmp_path, rows):
    path = tmp_path / "lock"
    path.write_text(rows)
    with pytest.raises(ValueError):
        vendor.inventory(path)


@pytest.mark.parametrize(
    "field,value", [("name", "renamed"), ("version", "1.0-r1"), ("arch", "x86_64")]
)
def test_import_rejects_wrong_signed_identity(inputs, tmp_path, field, value):
    lock, source, cache, _ = inputs
    apk(cache / "alpha-1.0-r0.apk", **{field: value})
    with pytest.raises(ValueError, match="package/version/architecture"):
        vendor.import_apks(source, lock, tmp_path / "output", cache)
    assert not (tmp_path / "output").exists()


def test_failed_signature_does_not_expose_import_or_publish(inputs, tmp_path, monkeypatch):
    lock, source, cache, _ = inputs

    def reject(command, **kwargs):
        assert command[0] == "docker"
        raise subprocess.CalledProcessError(1, command, stderr="UNTRUSTED signature")

    monkeypatch.setattr(vendor.subprocess, "run", reject)
    with pytest.raises(subprocess.CalledProcessError):
        vendor.import_apks(source, lock, tmp_path / "output", cache)
    assert not (tmp_path / "output").exists()


def test_download_tries_direct_urls_without_an_index(tmp_path, monkeypatch):
    urls = []

    def urlopen(url, **kwargs):
        urls.append(url)
        if len(urls) < 3:
            raise vendor.urllib.error.HTTPError(url, 404, "gone", {}, None)
        return io.BytesIO(b"signed apk bytes")

    monkeypatch.setattr(vendor.urllib.request, "urlopen", urlopen)
    path = tmp_path / "alpha-1.0-r0.apk"
    assert vendor.download("alpha-1.0-r0", path, None) == urls[-1]
    assert path.read_bytes() == b"signed apk bytes"
    assert all(url.endswith("/alpha-1.0-r0.apk") and "APKINDEX" not in url for url in urls)


def test_download_unavailable_fails_instead_of_selecting_a_new_version(tmp_path, monkeypatch):
    def gone(url, **kwargs):
        raise vendor.urllib.error.HTTPError(url, 404, "gone", {}, None)

    monkeypatch.setattr(vendor.urllib.request, "urlopen", gone)
    with pytest.raises(ValueError, match="--apk-dir"):
        vendor.download("alpha-1.0-r0", tmp_path / "alpha-1.0-r0.apk", None)


def test_fetch_checks_exact_version_tar_and_all_apk_bytes(inputs, tmp_path, monkeypatch):
    output = imported(inputs, tmp_path)
    published_source(output)
    mock_get(monkeypatch, output / "bundle.tar")
    destination = tmp_path / "fetched"
    vendor.fetch(
        output / "media-apk-source.json", inputs[0], output / "media-apk-sha256.lock", destination
    )
    source = vendor.read_json(output / "media-apk-source.json")
    vendor.verify_context(destination, inputs[0], output / "media-apk-sha256.lock", source)
    (destination / "apks/alpha-1.0-r0.apk").write_bytes(b"same name, changed bytes")
    with pytest.raises(ValueError, match="APK SHA-256 mismatch"):
        vendor.verify_context(destination, inputs[0], output / "media-apk-sha256.lock", source)


@pytest.mark.parametrize("wrong", ["version", "archive", "apk"])
def test_fetch_failure_is_atomic(inputs, tmp_path, monkeypatch, wrong):
    output = imported(inputs, tmp_path)
    source = published_source(output)
    archive = output / "bundle.tar"
    if wrong == "archive":
        archive.write_bytes(archive.read_bytes() + b"corrupt")
    if wrong == "apk":
        (output / "context/apks/alpha-1.0-r0.apk").write_bytes(b"rebuilt same version")
        vendor.create_bundle(output / "context", archive)
        source["bundle"]["sha256"] = vendor.digest(archive)
        source["bundle"]["size_bytes"] = archive.stat().st_size
        source["bundle"]["key"] = (
            f"media-apks/aarch64/{source['inventory_sha256']}/{vendor.digest(archive)}.tar"
        )
        vendor.write_json(output / "media-apk-source.json", source)
    mock_get(monkeypatch, archive, version="wrong" if wrong == "version" else "immutable-version")
    with pytest.raises(ValueError, match=r"(VersionId|SHA-256)"):
        vendor.fetch(
            output / "media-apk-source.json",
            inputs[0],
            output / "media-apk-sha256.lock",
            tmp_path / "fetched",
        )
    assert not (tmp_path / "fetched").exists()
    assert not list(tmp_path.glob(".media-apk-fetch-*"))


@pytest.mark.parametrize("kind", ["extra", "missing", "duplicate", "symlink", "traversal"])
def test_archive_rejects_unsafe_members(inputs, tmp_path, monkeypatch, kind):
    output = imported(inputs, tmp_path)
    source = published_source(output)
    archive = output / "bundle.tar"
    with tarfile.open(archive) as original:
        entries = [(member, original.extractfile(member).read()) for member in original]
    if kind == "missing":
        entries.pop()
    elif kind in {"extra", "duplicate", "traversal"}:
        member = copy.copy(entries[0][0])
        member.name = {
            "extra": "unexpected.apk",
            "duplicate": entries[0][0].name,
            "traversal": "../escaped",
        }[kind]
        entries.append((member, entries[0][1]))
    else:
        entries[0][0].type = tarfile.SYMTYPE
        entries[0][0].linkname = "../../escaped"
        entries[0][0].size = 0
    with tarfile.open(archive, "w:") as rebuilt:
        for member, body in entries:
            rebuilt.addfile(member, io.BytesIO(body))
    source["bundle"].update(sha256=vendor.digest(archive), size_bytes=archive.stat().st_size)
    source["bundle"]["key"] = (
        f"media-apks/aarch64/{source['inventory_sha256']}/{vendor.digest(archive)}.tar"
    )
    vendor.write_json(output / "media-apk-source.json", source)
    mock_get(monkeypatch, archive)
    with pytest.raises(ValueError, match="APK archive"):
        vendor.fetch(
            output / "media-apk-source.json",
            inputs[0],
            output / "media-apk-sha256.lock",
            tmp_path / "fetched",
        )
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "missing", "symlink", "checksum"])
def test_context_and_checksum_lock_are_exact(inputs, tmp_path, mutation):
    output = imported(inputs, tmp_path)
    context = output / "context"
    checksum = output / "media-apk-sha256.lock"
    if mutation == "extra":
        (context / "apks/extra-1-r0.apk").write_bytes(b"extra")
    elif mutation == "missing":
        (context / "apks/alpha-1.0-r0.apk").unlink()
    elif mutation == "symlink":
        (context / "apks/link.apk").symlink_to("alpha-1.0-r0.apk")
    else:
        checksum.write_bytes(
            checksum.read_bytes()
            + (
                checksum.read_bytes().splitlines()[0] + b"\n"
                if mutation == "duplicate"
                else b"bad\n"
            )
        )
    with pytest.raises(ValueError):
        vendor.verify_context(
            context, inputs[0], checksum, vendor.read_json(output / "media-apk-source.json")
        )


@pytest.mark.parametrize("recover", [False, True])
def test_publish_readback_and_retry_after_successful_put(inputs, tmp_path, monkeypatch, recover):
    output = imported(inputs, tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "docker":
            return subprocess.CompletedProcess(command, 0, "base-1.0-r0\n", "")
        if command[2] == "put-object":
            assert command[command.index("--if-none-match") + 1] == "*"
            assert command[command.index("--ssekms-key-id") + 1] == vendor.KMS_KEY
            if recover:
                raise subprocess.CalledProcessError(1, command, stderr="PreconditionFailed")
        if command[2] == "get-object":
            Path(command[command.index("--version-id") + 2]).write_bytes(
                (output / "bundle.tar").read_bytes()
            )
        return subprocess.CompletedProcess(command, 0, '{"VersionId":"immutable-version"}', "")

    monkeypatch.setattr(vendor.subprocess, "run", run)
    vendor.publish(output)
    assert (
        vendor.read_json(output / "media-apk-source.json")["bundle"]["version_id"]
        == "immutable-version"
    )
    assert [c[2] for c in calls if c[0] == "aws"] == (
        ["put-object", "head-object", "get-object"] if recover else ["put-object", "get-object"]
    )


def test_failed_publish_readback_keeps_descriptor_unpublished(inputs, tmp_path, monkeypatch):
    output = imported(inputs, tmp_path)

    def run(command, **kwargs):
        if command[0] == "docker":
            return subprocess.CompletedProcess(command, 0, "base-1.0-r0\n", "")
        if command[:3] == ["aws", "s3api", "get-object"]:
            Path(command[command.index("--version-id") + 2]).write_bytes(b"corruption")
        return subprocess.CompletedProcess(command, 0, '{"VersionId":"immutable-version"}', "")

    monkeypatch.setattr(vendor.subprocess, "run", run)
    with pytest.raises(ValueError, match="SHA-256/size"):
        vendor.publish(output)
    assert vendor.read_json(output / "media-apk-source.json")["bundle"]["version_id"] is None


def test_failed_json_update_preserves_previous_upload_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "source.json"
    vendor.write_json(path, {"version_id": None})

    def fail_replace(*args):
        raise OSError("disk full/interrupted update")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted update"):
        vendor.write_json(path, {"version_id": "uploaded"})
    assert vendor.read_json(path) == {"version_id": None}
    assert not list(tmp_path.glob(".source.json-*"))


def test_pending_source_stops_before_any_external_io(inputs, tmp_path):
    lock, source, _, calls = inputs
    assert (
        vendor.main(
            [
                "fetch",
                "--source",
                str(source),
                "--lock",
                str(lock),
                "--checksums",
                str(tmp_path / "absent"),
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == 1
    )
    assert calls == [] and not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("architecture", "x86_64"),
        ("schema_version", True),
        ("verification_image", "alpine:latest"),
        ("inventory_sha256", "0" * 64),
    ],
)
def test_source_rejects_wrong_trust_roots(inputs, field, value):
    lock, source_path, _, _ = inputs
    source = vendor.read_json(source_path)
    source[field] = value
    vendor.write_json(source_path, source)
    with pytest.raises(ValueError):
        vendor.load_source(source_path, lock, published=False)
