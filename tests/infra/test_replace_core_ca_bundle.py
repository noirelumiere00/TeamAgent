"""The pinned Wolfi APK changes exactly one runtime package and its payload."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/docker/replace_core_ca_bundle.py"
APK = ROOT / "infra/docker/vendor/ca-certificates-bundle-20260909-r3.apk"
LOCK = ROOT / "infra/docker/core-ca-bundle.lock.json"
SHA256 = "4f57ebd7dc21d78ecce228f397d0a4ee46b5cfc777ac5f292821d41a97bfd896"
VERSION = "20260909-r3"
DB = "usr/lib/apk/db/installed"
CERT = "etc/ssl/certs/ca-certificates.crt"
OLD_SBOM = "var/lib/db/sbom/ca-certificates-bundle-20260909-r2.spdx.json"
BEFORE = (
    b"C:Q1first\nP:wolfi-baselayout\nV:20230201-r30\nF:etc\nR:os-release\n\n"
    b"C:Q1old\nP:ca-certificates-bundle\nV:20260909-r2\no:ca-certificates\n"
    b"F:etc/ssl/certs\nR:ca-certificates.crt\nZ:Q1old\n\n"
    b"P:python-3.14\nV:3.14.8_git20261008-r0\nD:python-3.14-base\n\n"
)


def _module():
    spec = importlib.util.spec_from_file_location("replace_core_ca_bundle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base(tmp_path):
    base = tmp_path / "base"
    db = base / DB
    db.parent.mkdir(parents=True)
    db.write_bytes(BEFORE)
    for name, target in (
        ("etc/pki/tls/certs/ca-bundle.crt", "../../../ssl/certs/ca-certificates.crt"),
        ("etc/ssl/cert.pem", "certs/ca-certificates.crt"),
        ("etc/ssl/certs/ca-bundle.crt", "ca-certificates.crt"),
    ):
        link = base / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    (base / CERT).write_bytes(b"old certificates")
    old = base / OLD_SBOM
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old sbom")
    return base


def test_pinned_apk_payload_and_complete_package_record(tmp_path):
    module = _module()
    package = module.load_package(APK, LOCK, SHA256, VERSION)
    base = _base(tmp_path)
    module.prepare(base, package)
    module.verify(base, BEFORE, package)
    result = (base / DB).read_bytes().split(b"\n\n")
    assert result[0] == BEFORE.split(b"\n\n")[0]
    assert result[2:] == BEFORE.split(b"\n\n")[2:]
    for field in (
        "V:20260909-r3",
        "C:Q12NXUp1YSrgDFsj3YmYoLOz7Pngg=",
        "S:119164",
        "I:229626",
        "t:1792505521",
        "c:34903eac7b8592ab9906e3aba6b3aa1ad96023df",
        "R:ca-certificates-bundle-20260909-r3.spdx.json",
    ):
        assert field.encode() in result[1].splitlines()
    assert result[1].count(b"Z:Q1") == 5
    assert not (base / OLD_SBOM).exists()
    assert hashlib.sha256((base / CERT).read_bytes()).hexdigest() == (
        "5961c0a3c65459931c023e0075c09733f09aea4ca439cb85bc4a08bb4f3f568b"
    )


def test_rejects_wrong_apk_sha_before_writing(tmp_path):
    module = _module()
    with pytest.raises(ValueError, match="APK SHA-256"):
        module.load_package(APK, LOCK, "0" * 64, VERSION)
    corrupt = tmp_path / "corrupt.apk"
    corrupt.write_bytes(APK.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="APK SHA-256"):
        module.load_package(corrupt, LOCK, SHA256, VERSION)


def test_lock_bytes_are_pinned_in_dockerfile():
    expected = "0533366dfd74a1df3c01286ed6bbdff9c6977b03b62a360c755a131168c3d3a1"
    assert hashlib.sha256(LOCK.read_bytes()).hexdigest() == expected
    assert (
        f"{expected} /tmp/core-ca/lock.json | sha256sum -c -"
        in (ROOT / "infra/docker/Dockerfile.teamagent-mcp").read_text()
    )


def test_vendored_inputs_keep_their_exact_bytes_in_canonical_build_context(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "ca_bundle_canonical_context", ROOT / "infra/docker/canonical_build_context.py"
    )
    context = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = context
    spec.loader.exec_module(context)
    source = tmp_path / "source"
    for path in (APK, LOCK, SCRIPT):
        destination = source / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    output = tmp_path / "context.tar"
    original_sha = context.create_canonical_tar(source, output)
    with tarfile.open(output) as archive:
        for path in (APK, LOCK, SCRIPT):
            assert (
                archive.extractfile(path.relative_to(ROOT).as_posix()).read() == path.read_bytes()
            )
    (source / LOCK.relative_to(ROOT)).write_bytes(LOCK.read_bytes() + b"\n")
    assert context.create_canonical_tar(source, output) != original_sha


def test_final_exec_form_rechecks_copied_payload_and_removes_stale_sbom(tmp_path):
    module = _module()
    package = module.load_package(APK, LOCK, SHA256, VERSION)
    final = _base(tmp_path)
    module.prepare(final, package)
    (final / OLD_SBOM).write_bytes(b"base layer SBOM")
    before = tmp_path / "base-installed"
    before.write_bytes(BEFORE)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "finalize",
            "--root",
            str(final),
            "--apk",
            str(APK),
            "--lock",
            str(LOCK),
            "--base-db",
            str(before),
        ],
        env={
            **os.environ,
            "CA_CERTIFICATES_BUNDLE_APK_SHA256": SHA256,
            "CA_CERTIFICATES_BUNDLE_VERSION": VERSION,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (final / OLD_SBOM).exists()
    module.verify(final, BEFORE, package)


@pytest.mark.parametrize("change", ["other-package", "certificate", "sbom", "file-checksum"])
def test_verification_rejects_changes(tmp_path, change):
    module = _module()
    package = module.load_package(APK, LOCK, SHA256, VERSION)
    base = _base(tmp_path)
    module.prepare(base, package)
    if change == "other-package":
        db = base / DB
        db.write_bytes(db.read_bytes().replace(b"V:3.14.8_git20261008-r0", b"V:3.15.0-r0"))
    elif change == "file-checksum":
        db = base / DB
        db.write_bytes(db.read_bytes().replace(b"Z:Q1", b"Z:Q2", 1))
    elif change == "certificate":
        (base / CERT).write_bytes(b"different certificate")
    else:
        (base / "var/lib/db/sbom/ca-certificates-bundle-20260909-r3.spdx.json").write_bytes(b"{}")
    with pytest.raises(ValueError):
        module.verify(base, BEFORE, package)


def test_refuses_unexpected_base_version_without_changing_db(tmp_path):
    module = _module()
    package = module.load_package(APK, LOCK, SHA256, VERSION)
    base = _base(tmp_path)
    db = base / DB
    old = BEFORE.replace(b"V:20260909-r2", b"V:20260909-r1")
    db.write_bytes(old)
    with pytest.raises(ValueError, match="base version"):
        module.prepare(base, package)
    assert db.read_bytes() == old
