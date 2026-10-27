"""Pinned, source-only kit in media; PPTX generation remains offline and undelivered."""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from teamagent.skills.first_visit import contract

ROOT = Path(__file__).resolve().parents[3]
BUNDLE = ROOT / "tools/first_visit/tiktok-skills-kit-25eccd8.tar.gz"
LOCK = ROOT / "tools/first_visit/kit.lock.json"
COMMIT = "25eccd8219f01cad18dbb4f5a863b5f41d8f52fb"
TREE = "e12d92cc1fe03a3c3e0b7a75acdaa66b8d128ad6"
ARCHIVE_SHA = "97baaaee69446c6d8672a6c1ad95856b41603d8cbeb968c5b2a7bebbcff6663d"
ARCHIVE_TREE = "49b9631700181827d1aa263247da56d9d9c61278"
NPM_SHA = "3bb73fbaf5a467786e5ff8c99fe0c8fa7788068ff7e0a1e37621708b8de98798"


def test_aico_pin_identifies_the_first_visit_only_entry() -> None:
    assert contract.KIT_COMMIT == COMMIT
    assert contract.KIT_TREE == TREE


def test_bundled_npm_lock_rejects_known_vulnerable_image_size() -> None:
    with tarfile.open(BUNDLE) as archive:
        stream = archive.extractfile("tiktok-deck/package-lock.json")
        assert stream is not None
        packages = json.load(stream)["packages"]
    versions = {
        path: package["version"]
        for path, package in packages.items()
        if path.endswith("/image-size") or package.get("name") == "image-size"
    }
    assert versions, "image-size must remain covered by the offline security check"
    # GHSA-5p2g-fcmc-qvqq / GHSA-w3rx-r6r6-pgpr: check nested copies too.
    for path, version in versions.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (path, version)
        assert tuple(map(int, version.split("."))) >= (2, 0, 3), (path, version)


def test_archive_excludes_operational_documents() -> None:
    with tarfile.open(BUNDLE) as archive:
        files = [Path(member.name) for member in archive.getmembers() if member.isfile()]
    forbidden = [
        str(path)
        for path in files
        if (len(path.parts) == 1 and path.suffix.lower() == ".md")
        or path.name.upper().startswith(("DM_AUDIT", "CHANGES_", "STATUS", "VALIDATION"))
    ]
    assert forbidden == [], f"operational documents must not be shipped: {forbidden}"


def test_archive_is_the_exact_pinned_path_selection(tmp_path: Path) -> None:
    assert hashlib.sha256(BUNDLE.read_bytes()).hexdigest() == ARCHIVE_SHA
    record = json.loads(LOCK.read_text())
    assert record["git_commit"] == COMMIT and record["git_tree"] == TREE
    assert record["archive_git_tree"] == ARCHIVE_TREE
    assert record["archive_sha256"] == ARCHIVE_SHA
    assert record["deck_package_lock_sha256"] == NPM_SHA

    with tarfile.open(BUNDLE) as archive:
        assert archive.pax_headers["comment"] == COMMIT
        files = sorted(member.name for member in archive.getmembers() if member.isfile())
        assert files == record["paths"] and len(files) == 33
        assert all(name == "VERSION" or name.startswith("tiktok-deck/") for name in files)
        assert not any(Path(name).suffix.lower() == ".md" for name in files)
        for member in archive.getmembers():
            assert member.isfile() or member.isdir()
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            assert not Path(member.name).is_absolute() and ".." not in parts
            assert ".git" not in parts and "node_modules" not in parts
            stream = archive.extractfile(member)
            assert stream is not None
            body = stream.read()
            if member.name == "tiktok-deck/package-lock.json":
                assert hashlib.sha256(body).hexdigest() == NPM_SHA
        archive.extractall(tmp_path, filter="data")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "infra/deploy/verify_source_tree.py"),
            "--root",
            str(tmp_path),
            "--expected-tree",
            ARCHIVE_TREE,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_media_adds_no_apk_and_keeps_fixed_inputs_in_the_build_context() -> None:
    docker = (ROOT / "infra/docker/Dockerfile.teamagent-media-worker").read_text()
    ignore = (ROOT / "infra/docker/Dockerfile.teamagent-media-worker.dockerignore").read_text()
    apk = (ROOT / "infra/docker/media-apk.lock").read_bytes()
    assert len(apk.splitlines()) == 241
    assert hashlib.sha256(apk).hexdigest() == (
        "b589be112ee5f3aa0ec8874cfcad691c62dc61ab42007c5dd53a8b80b012dbbd"
    )
    packages = docker[
        docker.index("FROM 718959508629") : docker.index("FROM runtime-packages AS python-deps")
    ]
    assert hashlib.sha256(packages.encode()).hexdigest() == (
        "8e27624480a1a8d6494275b836a5662fc72f89224b94178e3ee761c7808ad28b"
    )
    assert "COPY tools/first_visit/kit.lock.json" in docker
    assert "COPY tools/first_visit/tiktok-skills-kit-25eccd8.tar.gz" in docker
    assert ARCHIVE_SHA in docker and NPM_SHA in docker
    assert hashlib.sha256(LOCK.read_bytes()).hexdigest() in docker
    assert "npm ci --omit=dev --ignore-scripts --no-audit --no-fund" in docker
    assert "/deps/first-visit-kit/ /app/tools/tiktok-skills-kit/" in docker
    assert "!tools/first_visit/kit.lock.json" in ignore
    assert "!tools/first_visit/tiktok-skills-kit-25eccd8.tar.gz" in ignore
    assert 'ENTRYPOINT ["/app/.venv/bin/python", "-m", "teamagent.media.tool_worker"]' in docker


def test_canonical_build_context_binds_the_kit_and_its_lock(tmp_path: Path) -> None:
    context = tmp_path / "context"
    inputs = context / "tools/first_visit"
    inputs.mkdir(parents=True)
    for source in (BUNDLE, LOCK):
        shutil.copyfile(source, inputs / source.name)
    output = tmp_path / "context.tar"

    def snapshot() -> str:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "infra/docker/canonical_build_context.py"),
                str(context),
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return hashlib.sha256(output.read_bytes()).hexdigest()

    original = snapshot()
    with tarfile.open(output) as archive:
        for source in (BUNDLE, LOCK):
            stream = archive.extractfile(f"tools/first_visit/{source.name}")
            assert stream is not None and stream.read() == source.read_bytes()
    (inputs / LOCK.name).write_text("{}\n")
    assert snapshot() != original


@pytest.fixture(scope="module")
def installed_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    modules = os.environ.get("FIRST_VISIT_NODE_MODULES")
    if not modules:
        pytest.skip("FIRST_VISIT_NODE_MODULES must point to lock-installed local dependencies")
    kit = tmp_path_factory.mktemp("first-visit-kit")
    with tarfile.open(BUNDLE) as archive:
        archive.extractall(kit, filter="data")
    shutil.copytree(modules, kit / "tiktok-deck/node_modules")
    lock = json.loads((kit / "tiktok-deck/package-lock.json").read_text())
    for relative, package in lock["packages"].items():
        if relative:
            installed = json.loads((kit / "tiktok-deck" / relative / "package.json").read_text())
            assert installed["version"] == package["version"]
    return kit


@pytest.mark.parametrize("missing_anatomy", [False, True])
def test_pptx_only_synthetic_smoke(
    tmp_path: Path, installed_bundle: Path, missing_anatomy: bool
) -> None:
    kit_path = os.environ.get("FIRST_VISIT_KIT_PATH")
    assert kit_path, "the unchanged Git-based Aico gate must run on a clean host checkout"
    assert contract._kit_problems(Path(kit_path)) == []
    # Fixture authoring stays on the clean host kit, outside the runtime artifact.
    fixture = runpy.run_path(str(Path(kit_path) / "tools/smoke_fixture.py"))["make_fixture"]
    inputs = tmp_path / "inputs"
    evidence = fixture(inputs, kit=installed_bundle)
    synthesis_path = inputs / "case/authored/synthesis.json"
    synthesis = json.loads(synthesis_path.read_text())
    synthesis.pop("scripts")
    synthesis_path.write_text(json.dumps(synthesis, ensure_ascii=False))
    original = {p.relative_to(inputs): p.read_bytes() for p in inputs.rglob("*") if p.is_file()}
    work = tmp_path / "work"
    shutil.copytree(inputs, work)
    case, run = work / "case", work / "run"
    if missing_anatomy:
        (run / "anatomy" / f"{evidence['selected_ids'][0]}.json").unlink()
    # Exercise the image's Git/LibreOffice-free mode using only Python and Node on PATH.
    binaries = tmp_path / "bin"
    binaries.mkdir()
    node = shutil.which("node")
    assert node
    (binaries / "node").symlink_to(node)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(installed_bundle / "tiktok-deck/tools/build_parts.py"),
            str(case),
            str(run),
            "--only",
            "first_visit",
            "--skin",
            "@deepdive",
        ],
        env={
            **os.environ,
            "PATH": str(binaries),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SOFFICE_BINARY": "",
            "FONTCONFIG_FILE": "",
            "TIKTOK_FONT_DIRECTORIES": "",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == (1 if missing_anatomy else 0), result.stdout + result.stderr
    status = json.loads((case / "output/作業/delivery_status.json").read_text())
    assert status["delivery_ready"] is False
    assert status["checks"]["visual_qa"] == "unverified"
    assert status["runtime"]["soffice"] is None
    assert status["kit_version"]["git_commit"] is None
    assert not list((case / "output").rglob("*.pdf"))
    assert not list((case / "output").rglob("*.png"))
    if missing_anatomy:
        assert status["status"] == "incomplete" and status["artifacts"] == []
        assert not list((case / "output").glob("*.pptx"))
    else:
        assert status["status"] == "build_verified"
        assert status["checks"]["machine_qa"] == "passed"
        assert status["checks"]["render"] == "not_requested"
        (artifact,) = status["artifacts"]
        assert artifact["variant"] == "初訪" and artifact["pdf"] is None
        assert len(list((case / "output").glob("*.pptx"))) == 1
        report = json.loads(Path(artifact["preflight_report"]).read_text())
        assert report["pptx_sha256"] == artifact["pptx"]["sha256"]
        assert report["counts"]["致命的"] == report["counts"]["重大"] == 0
    assert {
        p.relative_to(inputs): p.read_bytes() for p in inputs.rglob("*") if p.is_file()
    } == original
