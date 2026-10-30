"""Real Python -> Node -> preflight on a Git-free installation, offline."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.first_visit import contract
from teamagent.skills.first_visit.concrete import _NODE_CLI, _PYTHON_CLI, _run, build_concrete
from teamagent.skills.first_visit.kit import ARCHIVE, archive_kit_problems

from .fixture import make_concrete_fixture, write_json
from .test_conversion import synthetic_inputs


@pytest.fixture(scope="module")
def concrete_kit() -> Path:
    path = os.environ.get("FIRST_VISIT_CONCRETE_KIT_PATH")
    if not path:
        if os.environ.get("CI"):
            pytest.fail("CI must set FIRST_VISIT_CONCRETE_KIT_PATH to the installed archive")
        pytest.skip("FIRST_VISIT_CONCRETE_KIT_PATH is required for real concrete builds")
    kit = Path(path)
    assert not (kit / ".git").exists()
    assert archive_kit_problems(kit) == []
    return kit


@pytest.fixture
def concrete_case(tmp_path: Path, concrete_kit: Path) -> Path:
    return make_concrete_fixture(tmp_path / "case", synthetic_inputs(), concrete_kit)


def config(kit: Path) -> dict[str, Any]:
    return {"kit_path": kit, "kit_archive": ARCHIVE, "part": "concrete"}


def read(root: Path, relative: str) -> Any:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def test_real_concrete_build(concrete_case: Path, concrete_kit: Path) -> None:
    assert contract.missing_inputs(concrete_case, **config(concrete_kit)) == []
    manifest = contract.seal_inputs(concrete_case, **config(concrete_kit))
    assert contract.verify_manifest(concrete_case, **config(concrete_kit)) == []
    result = build_concrete(concrete_case, kit_path=concrete_kit, request_id="synthetic")
    assert result.status == "build_verified", result.model_dump_json(indent=2)
    assert [cli.returncode for cli in result.cli] == [0, 0, 0]
    assert result.pptx_path is not None
    assert result.pptx_path.is_relative_to(concrete_case)
    assert result.pptx_sha256 == hashlib.sha256(result.pptx_path.read_bytes()).hexdigest()
    assert result.preflight is not None
    assert result.preflight.major_count == result.preflight.fatal_count == 0
    research = read(result.pptx_path.parents[1], "research_concrete.json")
    # Read ordering from the actual pinned tool, rather than duplicating its constant.
    tree = ast.parse((concrete_kit / "tiktok-deck/tools/build_research_json.py").read_text())
    assignment = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "PART_ORDER" for t in n.targets)
    )
    order = ast.literal_eval(assignment.value)["concrete"]
    page_ids = [p["id"] for p in research["pages"]]
    assert page_ids == sorted(page_ids, key=order.index)
    assert "video" not in page_ids and "part_videos" not in page_ids
    assert page_ids.count("script") == 3
    assert {"effects", "unsat", "purchase", "scene", "reviews"}.issubset(page_ids)
    assert result.slide_count == len(page_ids) + 1  # cover + one slide per non-video page
    assert result.slide_count == 29
    for relative, record in manifest["files"].items():
        assert (
            hashlib.sha256((concrete_case / relative).read_bytes()).hexdigest() == record["sha256"]
        )
    assert not list(concrete_kit.rglob("__pycache__"))


@pytest.mark.parametrize("missing", ["scripts", "reviews"])
def test_kit_gate_blocks_missing_concrete_material(
    concrete_case: Path, concrete_kit: Path, missing: str
) -> None:
    if missing == "scripts":
        data = read(concrete_case, "authored/synthesis.json")
        data.pop("scripts")
        write_json(concrete_case / "authored/synthesis.json", data)
    else:
        (concrete_case / "reviews/labels/fiction_0.jsonl").unlink()
    problems = contract.missing_inputs(concrete_case, **config(concrete_kit))
    assert any(("台本" if missing == "scripts" else "口コミ分類") in p for p in problems)
    with pytest.raises(contract.IncompleteInputsError):
        contract.seal_inputs(concrete_case, **config(concrete_kit))
    # A first seal does not grant permission to build a concrete proposal.
    if missing == "scripts":
        contract.seal_inputs(concrete_case, kit_path=concrete_kit, kit_archive=ARCHIVE)
        result = build_concrete(concrete_case, kit_path=concrete_kit, request_id="missing")
        assert result.failed_stage == "inputs" and result.cli == []


def test_manifest_change_stops_before_cli(concrete_case: Path, concrete_kit: Path) -> None:
    contract.seal_inputs(concrete_case, **config(concrete_kit))
    (concrete_case / "authored/synthesis.json").write_text("{}")
    result = build_concrete(concrete_case, kit_path=concrete_kit, request_id="changed")
    assert result.failed_stage == "inputs" and result.cli == []
    assert "manifest" in result.problems[0]


@pytest.mark.parametrize("change", ["archive", "source", "extra", "modules"])
def test_invalid_installation_stops_before_cli(
    concrete_case: Path, concrete_kit: Path, tmp_path: Path, change: str
) -> None:
    contract.seal_inputs(concrete_case, **config(concrete_kit))
    kit, archive = concrete_kit, ARCHIVE
    if change == "archive":
        archive = tmp_path / "changed.tar.gz"
        archive.write_bytes(ARCHIVE.read_bytes() + b"tampered")
    else:
        kit = tmp_path / "kit"
        with tarfile.open(ARCHIVE) as bundle:
            bundle.extractall(kit, filter="data")
        if change == "source":
            (kit / "tiktok-deck/src/build_research.js").write_text("process.exit(0)")
        elif change == "extra":
            (kit / "tiktok-deck/tools/json.py").write_text("raise RuntimeError('unlocked')")
    result = build_concrete(
        concrete_case, kit_path=kit, kit_archive=archive, request_id="bad-install"
    )
    assert result.failed_stage == ("dependencies" if change == "modules" else "kit")
    assert result.status == "failed" and result.cli == []


@pytest.mark.parametrize("stage", ["research", "pptx"])
def test_actual_cli_failure_stops_pipeline(
    concrete_case: Path, concrete_kit: Path, stage: str
) -> None:
    if stage == "research":
        cfg = read(concrete_case, "case.json")
        cfg["brands"][0].pop("match")  # Gate accepts this; the actual builder requires it.
        write_json(concrete_case / "case.json", cfg)
    else:
        data = read(concrete_case, "authored/synthesis.json")
        data["scripts"][0]["specs"] = "invalid renderer specs"
        write_json(concrete_case / "authored/synthesis.json", data)
    contract.seal_inputs(concrete_case, **config(concrete_kit))
    result = build_concrete(concrete_case, kit_path=concrete_kit, request_id="cli-error")
    assert result.status == "failed" and result.failed_stage == stage
    assert [cli.stage for cli in result.cli] == (
        ["research"] if stage == "research" else ["research", "pptx"]
    )
    assert result.cli[-1].returncode == 1
    assert ("match" if stage == "research" else "TypeError") in result.cli[-1].stderr_summary
    assert result.pptx_path is None


def test_actual_cli_timeout_stops_pipeline(concrete_case: Path, concrete_kit: Path) -> None:
    contract.seal_inputs(concrete_case, **config(concrete_kit))
    result = build_concrete(
        concrete_case, kit_path=concrete_kit, request_id="timeout", timeout=0.000001
    )
    assert result.status == "failed" and result.failed_stage == "research"
    assert len(result.cli) == 1 and result.cli[0].returncode is None
    assert "timed out" in result.cli[0].stderr_summary


def test_preflight_major_is_incomplete(concrete_case: Path, concrete_kit: Path) -> None:
    data = read(concrete_case, "authored/synthesis.json")
    data["summary"]["headline"] = "前回の振り返り"
    write_json(concrete_case / "authored/synthesis.json", data)
    contract.seal_inputs(concrete_case, **config(concrete_kit))
    result = build_concrete(concrete_case, kit_path=concrete_kit, request_id="major")
    assert result.status == "incomplete", result.model_dump_json(indent=2)
    assert result.failed_stage == "preflight" and result.pptx_path is not None
    assert result.preflight is not None and result.preflight.major_count > 0
    assert result.cli[-1].returncode == 1


@pytest.mark.usefixtures("concrete_kit")
@pytest.mark.parametrize("language", ["python", "node"])
@pytest.mark.parametrize("operation", ["write", "network"])
def test_real_child_isolation(tmp_path: Path, language: str, operation: str) -> None:
    """Exercise guards in real interpreters; no kit stage is replaced by a stub."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    if language == "python":
        script = work / "boundary.py"
        script.write_text(
            "from pathlib import Path\nPath('../outside').write_text('escape')"
            if operation == "write"
            else "import socket\nsocket.create_connection(('127.0.0.1', 9))"
        )
        command = [sys.executable, "-I", "-B", "-c", _PYTHON_CLI, str(work), str(script)]
    else:
        node = shutil.which("node")
        assert node, "real Node is required for concrete integration tests"
        script = work / "boundary.js"
        script.write_text(
            "require('fs').writeFileSync('../outside', 'escape')"
            if operation == "write"
            else "require('net').connect(9, '127.0.0.1')"
        )
        command = [
            node,
            "--permission",
            f"--allow-fs-read={work}",
            f"--allow-fs-write={work}",
            "-e",
            _NODE_CLI,
            str(script),
        ]
    result = _run("research", command, work=work, request_id="boundary", timeout=5)
    assert result.returncode == 1
    assert not outside.exists()
    assert ("outside" if operation == "write" else "disabled") in result.stderr_summary
