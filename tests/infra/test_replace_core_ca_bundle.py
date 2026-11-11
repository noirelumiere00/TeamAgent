"""Pinned Wolfi APKs change only their runtime package records and payloads."""

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
    module.prepare(base, [package])
    module.verify(base, BEFORE, [package])
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
    inputs = (
        APK,
        LOCK,
        PIP_APK,
        PIP_LOCK,
        EXPAT_APK,
        EXPAT_LOCK,
        SCRIPT,
        *(path for name in GLIBC_NAMES for path in _glibc_inputs(name)),
    )
    for path in inputs:
        destination = source / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    output = tmp_path / "context.tar"
    original_sha = context.create_canonical_tar(source, output)
    with tarfile.open(output) as archive:
        for path in inputs:
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
        (base / "var/lib/db/sbom/ca-certificates-bundle-20260909-r3.spdx.json").write_bytes(b"{}")
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
        (APK, LOCK, SHA256, VERSION),
        (PIP_APK, PIP_LOCK, PIP_SHA256, PIP_VERSION),
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


def _python_packages(module, dev=False):
    import json

    packages = [module.load_package(PIP_APK, PIP_LOCK, PIP_SHA256, PIP_VERSION)]
    for part in ("", "-base", "-runtime", *(("-base-dev", "-dev") if dev else ())):
        lock = ROOT / f"infra/docker/core-python-3.14{part}.lock.json"
        data = json.loads(lock.read_bytes())
        packages.append(
            module.load_package(
                ROOT / "infra/docker/vendor" / data["archive"],
                lock,
                data["archive_sha256"],
                data["apkindex"]["V"],
            )
        )
    return packages


def _python_base(tmp_path):
    base = _pip_base(tmp_path)
    providers = (
        "ld-linux-aarch64.so.1 libbz2.so.1 libc.so.6 libcrypto.so.4 libexpat.so.1 "
        "libffi.so.8 libgdbm.so.6 libgdbm_compat.so.4 liblzma.so.5 libm.so.6 "
        "libmpdec.so.4 libncursesw.so.6 libpanelw.so.6 libreadline.so.8 libsqlite3.so.0 "
        "libssl.so.4 libtinfo.so.6 libuuid.so.1 libz.so.1 libzstd.so.1"
    )
    before = (
        PIP_BEFORE
        + (
            "P:base-libraries\nV:1\np:"
            + " ".join("so:" + name for name in providers.split())
            + "\n\nP:python-3.14-base\nV:3.14.8_git20261008-r0\n"
            "F:usr/bin\nR:python3.14\nF:usr/lib/python3.14\nR:obsolete.py\n"
            "F:var/lib/db/sbom\nR:python-3.14-base-3.14.8_git20261008-r0.spdx.json\n\n"
        ).encode()
    )
    (base / DB).write_bytes(before)
    for name in (
        "usr/bin/python3.14",
        "usr/lib/python3.14/obsolete.py",
        "var/lib/db/sbom/python-3.14-base-3.14.8_git20261008-r0.spdx.json",
    ):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old payload")
    return base, before


def test_python_updates_binary_libpython_and_inventory_with_runtime_split(tmp_path):
    module = _module()
    base, before = _python_base(tmp_path)
    packages = _python_packages(module)
    module.prepare(base, packages)
    module.verify(base, before, packages)
    assert hashlib.sha256((base / "usr/bin/python3.14").read_bytes()).hexdigest() == (
        "81060a4e37f0e6746c7f53468dcd28c200053b4a2bb377a3fe2169edf3f770ac"
    )
    assert (base / "usr/bin/python3.14").stat().st_mode & 0o777 == 0o755
    assert (base / "usr/lib/libpython3.14.so.1.0").read_bytes()[:4] == b"\x7fELF"
    assert os.readlink(base / "usr/bin/python3") == "python3.14"
    assert not (base / "usr/lib/python3.14/obsolete.py").exists()
    assert (base / DB).read_bytes().count(b"V:3.14.10_git20261028-r0\n") == 3
    # Every unrelated record remains byte-identical, even across the package split.
    blocks = (base / DB).read_bytes().split(b"\n\n")
    assert blocks[0:2] == before.split(b"\n\n")[0:2]
    assert blocks[4] == before.split(b"\n\n")[4]
    spec = importlib.util.spec_from_file_location("smoke_core", ROOT / "infra/docker/smoke_core.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    smoke._assert_python_packages(base)


@pytest.mark.parametrize(
    "change", ["base-version", "missing-library", "existing-runtime", "old-dev"]
)
def test_python_rejects_base_or_dependency_drift_before_writing(tmp_path, change):
    module = _module()
    base, before = _python_base(tmp_path)
    if change == "base-version":
        before = before.replace(b"V:3.14.8_git20261008-r0", b"V:3.14.7-r0")
    elif change == "missing-library":
        before = before.replace(b"so:libssl.so.4", b"so:libssl.so.3")
    elif change == "existing-runtime":
        before += b"P:python-3.14-runtime\nV:3.14.8_git20261021-r3\n\n"
    else:
        before += (
            b"P:python-3.14-dev\nV:3.14.8_git20261008-r0\nD:python-3.14=3.14.8_git20261008-r0\n\n"
        )
    (base / DB).write_bytes(before)
    with pytest.raises(ValueError):
        module.prepare(base, _python_packages(module))
    assert (base / DB).read_bytes() == before
    assert (base / "usr/bin/python3.14").read_bytes() == b"old payload"
    assert (base / OLD_WHEEL).read_bytes() == b"old payload"


@pytest.mark.parametrize("change", ["libpython", "executable-mode", "stale-file", "symlink"])
def test_python_verification_rejects_payload_drift(tmp_path, change):
    module = _module()
    base, before = _python_base(tmp_path)
    packages = _python_packages(module)
    module.prepare(base, packages)
    if change == "libpython":
        (base / "usr/lib/libpython3.14.so.1.0").write_bytes(b"corrupt")
    elif change == "executable-mode":
        (base / "usr/bin/python3.14").chmod(0o644)
    elif change == "stale-file":
        (base / "usr/lib/python3.14/obsolete.py").write_bytes(b"old")
    else:
        (base / "usr/bin/python3").unlink()
        (base / "usr/bin/python3").symlink_to("python3.15")
    with pytest.raises(ValueError):
        module.verify(base, before, packages)


def test_python_development_packages_follow_the_same_version(tmp_path):
    module = _module()
    base, before = _python_base(tmp_path)
    before += (
        b"P:python-3.14-base-dev\nV:3.14.8_git20261008-r0\n"
        b"D:python-3.14-base=3.14.8_git20261008-r0\n\n"
        b"P:python-3.14-dev\nV:3.14.8_git20261008-r0\n"
        b"D:python-3.14-base-dev=3.14.8_git20261008-r0 python-3.14=3.14.8_git20261008-r0\n\n"
    )
    (base / DB).write_bytes(before)
    packages = _python_packages(module, dev=True)
    module.prepare(base, packages)
    module.verify(base, before, packages)
    assert (base / DB).read_bytes().count(b"V:3.14.10_git20261028-r0\n") == 5
    assert os.readlink(base / "usr/bin/python3-config") == "python3.14-config"
    assert os.readlink(base / "usr/lib/libpython3.14.so") == "libpython3.14.so.1.0"


EXPAT_APK = ROOT / "infra/docker/vendor/libexpat1-2.10.0-r0.apk"
EXPAT_LOCK = ROOT / "infra/docker/core-libexpat1.lock.json"
EXPAT_SHA256 = "68d2212a36051d122200630086b3f9f3edae4785b4700599ac47edb92364e8c5"
EXPAT_VERSION = "2.10.0-r0"
OLD_EXPAT_LIBRARY = "usr/lib/libexpat.so.1.13.0"
OLD_EXPAT_SBOM = "var/lib/db/sbom/libexpat1-2.9.0-r0.spdx.json"


def _expat_package(module):
    return module.load_package(EXPAT_APK, EXPAT_LOCK, EXPAT_SHA256, EXPAT_VERSION)


def _expat_base(tmp_path):
    base, before = _python_base(tmp_path)
    before += (
        b"P:libexpat1\nV:2.9.0-r0\no:expat\n"
        b"D:so:ld-linux-aarch64.so.1 so:libc.so.6\n"
        b"p:so-ver:libexpat.so.1=2.9.0-r0 so:libexpat.so.1=1\n"
        b"F:usr/lib\nR:libexpat.so.1\nR:libexpat.so.1.13.0\n"
        b"F:var/lib/db/sbom\nR:libexpat1-2.9.0-r0.spdx.json\n\n"
    )
    (base / DB).write_bytes(before)
    for name in (OLD_EXPAT_LIBRARY, OLD_EXPAT_SBOM):
        (base / name).write_bytes(b"old expat payload")
    (base / "usr/lib/libexpat.so.1").symlink_to("libexpat.so.1.13.0")
    return base, before


def test_expat_replaces_only_its_record_library_symlink_and_sbom(tmp_path):
    module = _module()
    package = _expat_package(module)
    base, before = _expat_base(tmp_path)
    module.prepare(base, [package])
    module.verify(base, before, [package])
    assert (base / DB).read_bytes().split(b"\n\n")[:-2] == before.split(b"\n\n")[:-2]
    assert not (base / OLD_EXPAT_LIBRARY).exists()
    assert not (base / OLD_EXPAT_SBOM).exists()
    spec = importlib.util.spec_from_file_location("smoke_core", ROOT / "infra/docker/smoke_core.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    smoke._assert_expat_package(base)


@pytest.mark.parametrize("field", ["C", "P", "V", "A", "S", "I", "T", "c", "D", "p", "o"])
def test_expat_rejects_apkindex_mismatch(tmp_path, field):
    import json

    module = _module()
    lock = json.loads(EXPAT_LOCK.read_text())
    lock["apkindex"][field] = "0" if field in ("S", "I") else "wrong"
    bad = tmp_path / "lock.json"
    bad.write_text(json.dumps(lock))
    with pytest.raises(ValueError):
        module.load_package(EXPAT_APK, bad, EXPAT_SHA256, EXPAT_VERSION)


def test_expat_rejects_wrong_apk_sha_before_writing(tmp_path):
    module = _module()
    with pytest.raises(ValueError, match="APK SHA-256"):
        module.load_package(EXPAT_APK, EXPAT_LOCK, "0" * 64, EXPAT_VERSION)
    corrupt = tmp_path / "corrupt.apk"
    corrupt.write_bytes(EXPAT_APK.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="APK SHA-256"):
        module.load_package(corrupt, EXPAT_LOCK, EXPAT_SHA256, EXPAT_VERSION)


@pytest.mark.parametrize("change", ["base-version", "missing-library", "duplicate", "extra-origin"])
def test_expat_rejects_base_or_dependency_drift_before_writing(tmp_path, change):
    module = _module()
    base, before = _expat_base(tmp_path)
    if change == "base-version":
        before = before.replace(b"V:2.9.0-r0", b"V:2.8.0-r0")
    elif change == "missing-library":
        before = before.replace(b"so:libc.so.6", b"so:libc.so.5", 1)
    elif change == "duplicate":
        before += b"P:libexpat1\nV:2.9.0-r0\no:expat\n\n"
    else:
        before += b"P:expat-dev\nV:2.9.0-r0\no:expat\n\n"
    (base / DB).write_bytes(before)
    with pytest.raises(ValueError):
        module.prepare(base, [_expat_package(module)])
    assert (base / DB).read_bytes() == before
    assert (base / OLD_EXPAT_LIBRARY).read_bytes() == b"old expat payload"
    assert os.readlink(base / "usr/lib/libexpat.so.1") == "libexpat.so.1.13.0"


@pytest.mark.parametrize(
    "change", ["library", "mode", "symlink", "sbom", "old-library", "old-sbom", "other-package"]
)
def test_expat_final_verification_fails_closed(tmp_path, change):
    module = _module()
    package = _expat_package(module)
    base, before = _expat_base(tmp_path)
    module.prepare(base, [package])
    if change == "mode":
        (base / "usr/lib/libexpat.so.1.13.1").chmod(0o644)
    elif change == "symlink":
        (base / "usr/lib/libexpat.so.1").unlink()
        (base / "usr/lib/libexpat.so.1").symlink_to("libexpat.so.1.13.0")
    elif change == "other-package":
        db = base / DB
        db.write_bytes(db.read_bytes().replace(b"P:python-3.14\n", b"P:python-3.15\n"))
    else:
        paths = {
            "library": "usr/lib/libexpat.so.1.13.1",
            "sbom": "var/lib/db/sbom/libexpat1-2.10.0-r0.spdx.json",
            "old-library": OLD_EXPAT_LIBRARY,
            "old-sbom": OLD_EXPAT_SBOM,
        }
        (base / paths[change]).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        module.verify(base, before, [package])


def test_expat_combines_with_existing_pinned_replacements_and_finalizes_copy(tmp_path):
    module = _module()
    base, before = _expat_base(tmp_path)
    packages = [
        module.load_package(APK, LOCK, SHA256, VERSION),
        *_python_packages(module),
        _expat_package(module),
    ]
    module.prepare(base, packages)
    module.verify(base, before, packages)
    baseline = tmp_path / "installed-before"
    baseline.write_bytes(before)
    retained = {
        path for package in packages for kind in ("files", "links") for path in package[kind]
    }
    for package in packages:
        for name in module.stale_paths(package, before, retained):
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"base layer COPY overlay")
    command = [
        sys.executable,
        str(SCRIPT),
        "finalize",
        "--root",
        str(base),
        "--base-db",
        str(baseline),
    ]
    for package in packages:
        lock = package["lock"]
        name = lock["apkindex"]["P"]
        path = {
            "ca-certificates-bundle": LOCK,
            "py3-pip-wheel": PIP_LOCK,
        }.get(name, ROOT / f"infra/docker/core-{name}.lock.json")
        command += [
            "--apk",
            str(ROOT / "infra/docker/vendor" / lock["archive"]),
            "--lock",
            str(path),
            "--apk-sha256",
            lock["archive_sha256"],
            "--version",
            lock["apkindex"]["V"],
        ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    module.verify(base, before, packages)


GLIBC_NAMES = ("glibc-2.44", "glibc-2.44-locale-posix", "ld-linux-2.44")
GLIBC_VERSION = "2.44-r16"


def _glibc_inputs(name):
    return (
        ROOT / f"infra/docker/vendor/{name}-{GLIBC_VERSION}.apk",
        ROOT / f"infra/docker/core-{name}.lock.json",
    )


def _glibc_packages(module):
    import json

    packages = []
    for name in GLIBC_NAMES:
        apk, lock = _glibc_inputs(name)
        data = json.loads(lock.read_bytes())
        packages.append(module.load_package(apk, lock, data["archive_sha256"], GLIBC_VERSION))
    return packages


def _glibc_base(tmp_path, packages):
    base, before = _expat_base(tmp_path)
    (base / "lib").symlink_to("usr/lib")
    for name in ("libgcc", "merged-lib", "merged-sbin", "merged-usrsbin"):
        before += f"P:{name}\nV:1\n\n".encode()
    for package in packages:
        name = package["lock"]["apkindex"]["P"]
        stale = (f"usr/lib/obsolete-{name}", f"var/lib/db/sbom/{name}-2.44-r8.spdx.json")
        record = f"P:{name}\nV:2.44-r8\no:glibc-2.44\n"
        for path in stale:
            record += f"F:{Path(path).parent}\nR:{Path(path).name}\n"
            (base / path).write_bytes(b"old glibc payload")
        before += (record + "\n").encode()
    (base / DB).write_bytes(before)
    (base / "usr/lib/libc.so.6").write_bytes(b"old libc")
    (base / "usr/lib/ld-linux-aarch64.so.1").write_bytes(b"old loader")
    (base / "usr/bin").mkdir(parents=True, exist_ok=True)
    (base / "usr/bin/ld.so").symlink_to("../../lib/ld-linux-aarch64.so.1")
    return base, before


def test_glibc_replaces_every_origin_record_and_preserves_loader_symlink(tmp_path):
    module = _module()
    packages = _glibc_packages(module)
    base, before = _glibc_base(tmp_path, packages)
    module.prepare(base, packages)
    module.verify(base, before, packages)
    assert (base / DB).read_bytes().split(b"\n\n")[:-4] == before.split(b"\n\n")[:-4]
    assert (base / DB).read_bytes().count(b"V:2.44-r16\n") == 3
    assert os.readlink(base / "usr/bin/ld.so") == "../../lib/ld-linux-aarch64.so.1"
    assert (base / "usr/bin/ld.so").resolve() == base / "usr/lib/ld-linux-aarch64.so.1"
    for package in packages:
        assert not (base / module.old_sbom(package)).exists()
    spec = importlib.util.spec_from_file_location("smoke_core", ROOT / "infra/docker/smoke_core.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    smoke._assert_glibc_packages(base)


@pytest.mark.parametrize("name", GLIBC_NAMES)
@pytest.mark.parametrize("field", ["C", "P", "V", "A", "S", "I", "T", "c", "D", "p", "o"])
def test_glibc_rejects_apkindex_or_archive_mismatch(tmp_path, name, field):
    import json

    module = _module()
    apk, lock = _glibc_inputs(name)
    data = json.loads(lock.read_bytes())
    digest = data["archive_sha256"]
    data["apkindex"][field] = "0" if field in ("S", "I") else "wrong"
    bad = tmp_path / "lock.json"
    bad.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        module.load_package(apk, bad, digest, GLIBC_VERSION)
    with pytest.raises(ValueError, match="APK SHA-256"):
        module.load_package(apk, lock, "0" * 64, GLIBC_VERSION)


@pytest.mark.parametrize(
    "change",
    ["base-version", "missing-provider", "duplicate", "extra-origin", "origin", "partial", "musl"],
)
def test_glibc_rejects_inventory_and_dependency_drift_before_writing(tmp_path, change):
    module = _module()
    packages = _glibc_packages(module)
    base, before = _glibc_base(tmp_path, packages)
    if change == "base-version":
        before = before.replace(b"V:2.44-r8", b"V:2.44-r7", 1)
    elif change == "missing-provider":
        before = before.replace(b"P:libgcc\nV:1\n\n", b"")
    elif change == "duplicate":
        before += b"P:glibc-2.44\nV:2.44-r8\no:glibc-2.44\n\n"
    elif change == "extra-origin":
        before += b"P:glibc-2.44-dev\nV:2.44-r8\no:glibc-2.44\n\n"
    elif change == "origin":
        before = before.replace(b"o:glibc-2.44", b"o:glibc-2.43", 1)
    elif change == "partial":
        packages = packages[:-1]
    else:
        before += b"P:musl\nV:1\n\n"
    (base / DB).write_bytes(before)
    with pytest.raises(ValueError):
        module.prepare(base, packages)
    assert (base / DB).read_bytes() == before
    assert (base / "usr/lib/libc.so.6").read_bytes() == b"old libc"
    assert (base / "usr/lib/ld-linux-aarch64.so.1").read_bytes() == b"old loader"


@pytest.mark.parametrize(
    "change",
    [
        "libc",
        "loader",
        "locale",
        "mode",
        "special-mode",
        "symlink",
        "sbom",
        "stale",
        "other-package",
    ],
)
def test_glibc_final_verification_fails_closed(tmp_path, change):
    module = _module()
    packages = _glibc_packages(module)
    base, before = _glibc_base(tmp_path, packages)
    module.prepare(base, packages)
    if change in ("mode", "special-mode"):
        # The macOS sandbox strips setuid/setgid; sticky still exercises the special-bit mask.
        (base / "usr/lib/libc.so.6").chmod(0o644 if change == "mode" else 0o1755)
    elif change == "symlink":
        (base / "usr/bin/ld.so").unlink()
        (base / "usr/bin/ld.so").symlink_to("../../lib/obsolete-loader.so")
    elif change == "other-package":
        db = base / DB
        db.write_bytes(db.read_bytes().replace(b"P:python-3.14\n", b"P:python-3.15\n"))
    else:
        path = {
            "libc": "usr/lib/libc.so.6",
            "loader": "usr/lib/ld-linux-aarch64.so.1",
            "locale": "usr/lib/locale/C.utf8/LC_CTYPE",
            "sbom": "var/lib/db/sbom/ld-linux-2.44-2.44-r16.spdx.json",
            "stale": "var/lib/db/sbom/glibc-2.44-2.44-r8.spdx.json",
        }[change]
        (base / path).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        module.verify(base, before, packages)


def test_glibc_combines_with_existing_replacements_and_finalizes_copy(tmp_path):
    module = _module()
    glibc = _glibc_packages(module)
    base, before = _glibc_base(tmp_path, glibc)
    packages = [
        module.load_package(APK, LOCK, SHA256, VERSION),
        *_python_packages(module),
        _expat_package(module),
        *glibc,
    ]
    module.prepare(base, packages)
    baseline = tmp_path / "installed-before"
    baseline.write_bytes(before)
    retained = {
        path for package in packages for kind in ("files", "links") for path in package[kind]
    }
    for package in packages:
        for name in module.stale_paths(package, before, retained):
            (base / name).write_bytes(b"base layer COPY overlay")
    command = [
        sys.executable,
        str(SCRIPT),
        "finalize",
        "--root",
        str(base),
        "--base-db",
        str(baseline),
    ]
    for package in packages:
        lock = package["lock"]
        name = lock["apkindex"]["P"]
        lock_path = {"ca-certificates-bundle": LOCK, "py3-pip-wheel": PIP_LOCK}.get(
            name, ROOT / f"infra/docker/core-{name}.lock.json"
        )
        command += [
            "--apk",
            str(ROOT / "infra/docker/vendor" / lock["archive"]),
            "--lock",
            str(lock_path),
            "--apk-sha256",
            lock["archive_sha256"],
            "--version",
            lock["apkindex"]["V"],
        ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    module.verify(base, before, packages)
