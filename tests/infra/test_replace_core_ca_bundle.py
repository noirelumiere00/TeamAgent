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
APK = ROOT / "infra/docker/vendor/ca-certificates-bundle-20261020-r0.apk"
LOCK = ROOT / "infra/docker/core-ca-bundle.lock.json"
SHA256 = "d2a458ae71b3f6f65302a8dd61c35f75f3eef87aeb5583a3f97523e99698cf49"
VERSION = "20261020-r0"
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
    # Keep legacy-base upgrade fixtures alongside the same-version runtime checks.
    load = module.load_package

    def load_legacy(apk, lock, digest, version):
        package = load(apk, lock, digest, version)
        package["lock"]["base_version"] = (
            "20260909-r2" if package["lock"]["apkindex"]["P"] == module.NAME else "26.2.1-r2"
        )
        return package

    module.load_package = load_legacy
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
    module.prepare(base, [package])
    module.verify(base, BEFORE, [package])
    result = (base / DB).read_bytes().split(b"\n\n")
    assert result[0] == BEFORE.split(b"\n\n")[0]
    assert result[2:] == BEFORE.split(b"\n\n")[2:]
    for field in (
        "V:20261020-r0",
        "C:Q1Uxft6BYfAMq8F4Gi9M7Q2XromUs=",
        "S:118573",
        "I:229626",
        "t:1793012842",
        "c:8061afafa65af5a9026707a8880e14120dd6f4a0",
        "R:ca-certificates-bundle-20261020-r0.spdx.json",
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
    expected = "86723ddeaf4263db18ae1ce6ad2865d5680eded14522b0c8c3dbad2e1ff6084c"
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
    for path in (APK, LOCK, PIP_APK, PIP_LOCK, SCRIPT):
        destination = source / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    output = tmp_path / "context.tar"
    original_sha = context.create_canonical_tar(source, output)
    with tarfile.open(output) as archive:
        for path in (APK, LOCK, PIP_APK, PIP_LOCK, SCRIPT):
            assert (
                archive.extractfile(path.relative_to(ROOT).as_posix()).read() == path.read_bytes()
            )
    (source / LOCK.relative_to(ROOT)).write_bytes(LOCK.read_bytes() + b"\n")
    assert context.create_canonical_tar(source, output) != original_sha


def test_final_exec_form_rechecks_copied_payload_and_removes_stale_sbom(tmp_path):
    module = _module()
    package = module.load_package(APK, LOCK, SHA256, VERSION)
    final = _base(tmp_path)
    module.prepare(final, [package])
    (final / OLD_SBOM).write_bytes(b"base layer SBOM")
    before = tmp_path / "base-installed"
    before.write_bytes(BEFORE)
    import json

    data = json.loads(LOCK.read_text())
    data["base_version"] = "20260909-r2"
    fixture_lock = tmp_path / "fixture.lock.json"
    fixture_lock.write_text(json.dumps(data))
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
            str(fixture_lock),
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
    module.verify(final, BEFORE, [package])


@pytest.mark.parametrize("change", ["other-package", "certificate", "sbom", "file-checksum"])
def test_verification_rejects_changes(tmp_path, change):
    module = _module()
    package = module.load_package(APK, LOCK, SHA256, VERSION)
    base = _base(tmp_path)
    module.prepare(base, [package])
    if change == "other-package":
        db = base / DB
        db.write_bytes(db.read_bytes().replace(b"V:3.14.8_git20261008-r0", b"V:3.15.0-r0"))
    elif change == "file-checksum":
        db = base / DB
        db.write_bytes(db.read_bytes().replace(b"Z:Q1", b"Z:Q2", 1))
    elif change == "certificate":
        (base / CERT).write_bytes(b"different certificate")
    else:
        (base / "var/lib/db/sbom/ca-certificates-bundle-20261020-r0.spdx.json").write_bytes(b"{}")
    with pytest.raises(ValueError):
        module.verify(base, BEFORE, [package])


def test_refuses_unexpected_base_version_without_changing_db(tmp_path):
    module = _module()
    package = module.load_package(APK, LOCK, SHA256, VERSION)
    base = _base(tmp_path)
    db = base / DB
    old = BEFORE.replace(b"V:20260909-r2", b"V:20260909-r1")
    db.write_bytes(old)
    with pytest.raises(ValueError, match="base version"):
        module.prepare(base, [package])
    assert db.read_bytes() == old


PIP_APK = ROOT / "infra/docker/vendor/py3-pip-wheel-26.2.2-r2.apk"
PIP_LOCK = ROOT / "infra/docker/core-pip-wheel.lock.json"
PIP_SHA256 = "6dcca154f23de54db689ec2006bb65c67de74d556f55f8836bf5d438da8b8ee1"
PIP_VERSION = "26.2.2-r2"
OLD_WHEEL = "usr/share/python-wheels/pip-26.2.1-py3-none-any.whl"
OLD_PIP_SBOM = "var/lib/db/sbom/py3-pip-wheel-26.2.1-r2.spdx.json"
PIP_BEFORE = BEFORE + b"P:py3-pip-wheel\nV:26.2.1-r2\n\n"


def _pip_base(tmp_path):
    base = _base(tmp_path)
    (base / DB).write_bytes(PIP_BEFORE)
    for name in (OLD_WHEEL, OLD_PIP_SBOM):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old payload")
    return base


def _packages(module):
    return [
        module.load_package(APK, LOCK, SHA256, VERSION),
        module.load_package(PIP_APK, PIP_LOCK, PIP_SHA256, PIP_VERSION),
    ]


def test_ca_and_pip_replace_only_their_records_and_payloads(tmp_path):
    module = _module()
    packages = _packages(module)
    base = _pip_base(tmp_path)
    module.prepare(base, packages)
    module.verify(base, PIP_BEFORE, packages)
    blocks = (base / DB).read_bytes().split(b"\n\n")
    assert blocks[0] == PIP_BEFORE.split(b"\n\n")[0]
    assert blocks[2] == PIP_BEFORE.split(b"\n\n")[2]
    assert blocks[1] == packages[0]["record"]
    assert blocks[3] == packages[1]["record"]
    assert b"k:10" in blocks[3].splitlines()
    assert blocks[3].count(b"Z:Q1") == 2
    assert not (base / OLD_WHEEL).exists()
    assert not (base / OLD_PIP_SBOM).exists()
    spec = importlib.util.spec_from_file_location("smoke_core", ROOT / "infra/docker/smoke_core.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    smoke._assert_pip_wheel(base)


@pytest.mark.parametrize("field", ["C", "P", "V", "A", "S", "I", "T", "c", "k"])
def test_pip_rejects_apkindex_mismatch(tmp_path, field):
    import json

    module = _module()
    lock = json.loads(PIP_LOCK.read_text())
    lock["apkindex"][field] = "0" if field in ("S", "I", "k") else "wrong"
    bad = tmp_path / "lock.json"
    bad.write_text(json.dumps(lock))
    with pytest.raises(ValueError):
        module.load_package(PIP_APK, bad, PIP_SHA256, PIP_VERSION)


@pytest.mark.parametrize("change", ["wheel", "sbom", "old-wheel", "old-sbom", "other-package"])
def test_pip_final_verification_fails_closed(tmp_path, change):
    module = _module()
    packages = _packages(module)
    base = _pip_base(tmp_path)
    module.prepare(base, packages)
    paths = {
        "wheel": "usr/share/python-wheels/pip-26.2.2-py3-none-any.whl",
        "sbom": "var/lib/db/sbom/py3-pip-wheel-26.2.2-r2.spdx.json",
        "old-wheel": OLD_WHEEL,
        "old-sbom": OLD_PIP_SBOM,
    }
    if change == "other-package":
        db = base / DB
        db.write_bytes(db.read_bytes().replace(b"P:python-3.14", b"P:python-3.15"))
    else:
        (base / paths[change]).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        module.verify(base, PIP_BEFORE, packages)


def test_combined_cli_finalizes_the_docker_copy_without_stale_files(tmp_path):
    module = _module()
    packages = _packages(module)
    base = _pip_base(tmp_path)
    before = tmp_path / "base-installed"
    fixture_locks = []
    for source, version in ((LOCK, "20260909-r2"), (PIP_LOCK, "26.2.1-r2")):
        import json

        data = json.loads(source.read_text())
        data["base_version"] = version
        target = tmp_path / source.name
        target.write_text(json.dumps(data))
        fixture_locks.append(target)
    command = [
        sys.executable,
        str(SCRIPT),
        "prepare",
        "--root",
        str(base),
        "--base-db",
        str(before),
    ]
    for apk, lock, digest, version in (
        (APK, fixture_locks[0], SHA256, VERSION),
        (PIP_APK, fixture_locks[1], PIP_SHA256, PIP_VERSION),
    ):
        command += [
            "--apk",
            str(apk),
            "--lock",
            str(lock),
            "--apk-sha256",
            digest,
            "--version",
            version,
        ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    assert before.read_bytes() == PIP_BEFORE
    # COPY overlays the runtime base, leaving the old wheel and SBOMs until finalize.
    for name in (OLD_SBOM, OLD_WHEEL, OLD_PIP_SBOM):
        (base / name).write_bytes(b"old base layer")
    command[2] = "finalize"
    subprocess.run(command, check=True, capture_output=True, text=True)
    module.verify(base, PIP_BEFORE, packages)


def test_pip_rejects_unexpected_base_before_changing_either_package(tmp_path):
    module = _module()
    base = _pip_base(tmp_path)
    db = base / DB
    before = PIP_BEFORE.replace(b"V:26.2.1-r2", b"V:26.2.1-r1")
    db.write_bytes(before)
    with pytest.raises(ValueError, match="base version"):
        module.prepare(base, _packages(module))
    assert db.read_bytes() == before
    assert (base / CERT).read_bytes() == b"old certificates"


@pytest.mark.parametrize("base_version", ["26.2.2-r1", "26.2.2-r2"])
def test_same_wheel_filename_is_retained_on_revision_or_identical_base(tmp_path, base_version):
    module = _module()
    package = module.load_package(PIP_APK, PIP_LOCK, PIP_SHA256, PIP_VERSION)
    package["lock"]["base_version"] = base_version
    base = _pip_base(tmp_path)
    (base / OLD_WHEEL).unlink()
    (base / OLD_PIP_SBOM).unlink()
    before = PIP_BEFORE.replace(b"V:26.2.1-r2", f"V:{base_version}".encode())
    (base / DB).write_bytes(before)
    sbom = base / module.old_sbom(package)
    sbom.write_bytes(b"base sbom")
    wheel = base / module.wheel_path(base_version)
    wheel.write_bytes(b"base wheel")
    module.prepare(base, [package])
    module.verify(base, before, [package])
    assert wheel.read_bytes() == package["files"][str(wheel.relative_to(base))]
