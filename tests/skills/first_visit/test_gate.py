"""Pinned kit integration and fail-closed mutations, entirely offline."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.first_visit import contract

from .fixture import VID, make_fixture, write_json
from .test_conversion import synthetic_inputs


@pytest.fixture
def kit_config() -> dict[str, Path]:
    path = os.environ.get("FIRST_VISIT_KIT_PATH")
    if not path:
        pytest.skip("FIRST_VISIT_KIT_PATH is required for the external pinned kit gate")
    config = {"kit_path": Path(path)}
    if python := os.environ.get("FIRST_VISIT_KIT_PYTHON"):
        config["kit_python"] = Path(python)
    return config


@pytest.fixture
def complete(tmp_path: Path, kit_config: dict[str, Path]) -> Path:
    return make_fixture(tmp_path / "case", synthetic_inputs(), kit_config["kit_path"])


def read(root: Path, file: str) -> Any:
    return json.loads((root / file).read_text(encoding="utf-8"))


def test_complete_synthetic_case_passes_actual_first_gate_and_seals_all_bytes(
    complete: Path,
    kit_config: dict[str, Path],
) -> None:
    assert contract.missing_inputs(complete, **kit_config) == []
    assert "scripts" not in read(complete, "authored/synthesis.json")
    manifest = contract.seal_inputs(complete, **kit_config)
    assert manifest["schema_version"] == contract.CONTRACT_VERSION
    assert manifest["kit"] == {
        "version": contract.KIT_VERSION,
        "git_commit": contract.KIT_COMMIT,
        "git_tree": contract.KIT_TREE,
        "research_input_gate_sha256": contract.KIT_GATE_SHA256,
    }
    paths = {
        p.relative_to(complete).as_posix()
        for p in complete.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert manifest["files"].keys() == paths
    for file, record in manifest["files"].items():
        data = (complete / file).read_bytes()
        assert record == {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "contract_version": contract.CONTRACT_VERSION,
        }
    assert contract.verify_manifest(complete, **kit_config) == []
    with pytest.raises(FileExistsError):
        contract.seal_inputs(complete, **kit_config)


@pytest.mark.parametrize(
    "file",
    [
        "labels.json",
        "first_visit.json",
        "selection.json",
        "authored/synthesis.json",
        f"anatomy/{VID}.json",
        f"anatomy_page/{VID}.json",
        f"transcripts/{VID}.json",
        f"media/{VID}.mp4",
        f"frames/{VID}/frames.json",
        f"frames/{VID}/contact.jpg",
        f"frames/{VID}/001.jpg",
        f"assets/covers/{VID}.jpg",
    ],
)
def test_each_missing_evidence_blocks_seal(
    complete: Path,
    kit_config: dict[str, Path],
    file: str,
) -> None:
    (complete / file).unlink()
    assert contract.missing_inputs(complete, **kit_config)
    with pytest.raises(contract.IncompleteInputsError):
        contract.seal_inputs(complete, **kit_config)
    assert not (complete / "manifest.json").exists()


def test_missing_list_includes_analysis_transcripts_synthesis_and_review_decision(
    tmp_path: Path,
    kit_config: dict[str, Path],
) -> None:
    root = tmp_path / "new"
    inputs = synthetic_inputs()
    inputs["case.json"]["research"].pop("review_collect")
    contract.write_inputs(root, inputs)
    problems = "\n".join(contract.missing_inputs(root, **kit_config))
    for fragment in ["labels.json", "anatomy/", "transcripts/", "synthesis.json", "未回答"]:
        assert fragment in problems


@pytest.mark.parametrize(
    "field,value",
    [
        ("order_basis", None),
        ("order_basis", "frequency"),
        ("sessions", 2),
        ("sessions", True),
        ("unfiltered", False),
        ("missingness_preserved", False),
        ("acquired_on", None),
        ("acquired_on", "2026-99-42"),
        ("stop_reason", None),
        ("diag", {"captchaDetected": True}),
    ],
)
def test_incomplete_source_metadata_cannot_be_labelled_as_complete(
    complete: Path,
    kit_config: dict[str, Path],
    field: str,
    value: Any,
) -> None:
    raw = read(complete, "raw/axis_01.json")
    raw["aico"]["source"][field] = value
    write_json(complete / "raw/axis_01.json", raw)
    assert contract.missing_inputs(complete, **kit_config)


@pytest.mark.parametrize(
    "status,code,allowed",
    [
        ("observed_zero", "TIKTOK_TRULY_EMPTY", True),
        ("observed_zero", None, False),
        ("failed", "TIKTOK_BOT_WALL", False),
        ("unknown", None, False),
        ("ok", None, False),
    ],
)
def test_observed_empty_own_axis_differs_from_acquisition_failure(
    tmp_path: Path,
    kit_config: dict[str, Path],
    status: str,
    code: str | None,
    allowed: bool,
) -> None:
    inputs = synthetic_inputs()
    raw = inputs["raw/axis_02.json"]
    raw["videos"] = []
    raw["aico"]["status"] = status
    raw["aico"]["source"].update(status=status, error_code=code)
    raw.update(ok=status == "ok", errorCode=code)
    root = make_fixture(tmp_path / "case", inputs, kit_config["kit_path"])
    assert (contract.missing_inputs(root, **kit_config) == []) is allowed


@pytest.mark.parametrize("change", ["count", "id_url", "rank", "ad", "media", "missingFields"])
def test_reopened_raw_is_validated_even_with_updated_cache(
    complete: Path,
    kit_config: dict[str, Path],
    change: str,
) -> None:
    raw = read(complete, "raw/axis_01.json")
    video = raw["videos"][0]
    if change == "count":
        video["stats"]["playCount"] = -1
    elif change == "id_url":
        video["url"] = "https://www.tiktok.com/@synthetic/video/123456789"
    elif change == "rank":
        video["rank_display"] = True
    elif change == "ad":
        video["isAd"] = None
    elif change == "media":
        video["mediaType"] = None
    else:
        video["stats"]["playCount"] = None
        video["missingFields"] = []
    write_json(complete / "raw/axis_01.json", raw)
    cache = read(complete, "first_visit.json")
    cache["inputs_sha256"]["raw/axis_01.json"] = hashlib.sha256(
        (complete / "raw/axis_01.json").read_bytes()
    ).hexdigest()
    write_json(complete / "first_visit.json", cache)
    assert contract.missing_inputs(complete, **kit_config)


@pytest.mark.parametrize(
    "change",
    [
        "labels",
        "cache",
        "empty_selection",
        "frame_time",
        "unopened",
        "transcript",
        "synthesis",
    ],
)
def test_actual_kit_gate_rejects_evidence_mutations(
    complete: Path,
    kit_config: dict[str, Path],
    change: str,
) -> None:
    if change == "labels":
        file = "labels.json"
        data = read(complete, file)
        data["posts"][0]["axes"][0]["confirmed"] = False
    elif change == "cache":
        file = "first_visit.json"
        data = read(complete, file)
        data["inputs_sha256"]["raw/axis_01.json"] = "0" * 64
    elif change == "empty_selection":
        file, data = "selection.json", []
    elif change in {"frame_time", "unopened"}:
        file = f"anatomy/{VID}.json"
        data = read(complete, file)
        if change == "frame_time":
            data["frames"][0]["sec"] = 12.0
        else:
            data["opened"] = []
    elif change == "transcript":
        file = f"transcripts/{VID}.json"
        data = read(complete, file)
        data["video_id"] = "123456789"
    else:
        file = "authored/synthesis.json"
        data = read(complete, file)
        data["proposals"]["items"] = []
    write_json(complete / file, data)
    assert contract.missing_inputs(complete, **kit_config)


@pytest.mark.parametrize("change", ["replace", "add", "remove", "version", "pin"])
def test_manifest_detects_mutation_and_file_membership_changes(
    complete: Path,
    kit_config: dict[str, Path],
    change: str,
) -> None:
    contract.seal_inputs(complete, **kit_config)
    if change == "replace":
        (complete / f"frames/{VID}/001.jpg").write_bytes(b"changed")
    elif change == "add":
        (complete / "unexpected.json").write_text("{}")
    elif change == "remove":
        (complete / f"transcripts/{VID}.json").unlink()
    else:
        manifest = read(complete, "manifest.json")
        if change == "version":
            manifest["schema_version"] += 1
        else:
            manifest["kit"]["git_commit"] = "0" * 40
        write_json(complete / "manifest.json", manifest)
    assert contract.verify_manifest(complete, **kit_config)


@pytest.mark.parametrize("change", ["vocab", "cover", "opened", "spec", "symlink"])
def test_evidence_outside_case_cannot_escape_manifest(
    complete: Path,
    kit_config: dict[str, Path],
    tmp_path: Path,
    change: str,
) -> None:
    if change == "cover":
        file = "labels.json"
        data = read(complete, file)
        data["posts"][0]["cover"] = "../outside.jpg"
    elif change == "opened":
        file = f"anatomy/{VID}.json"
        data = read(complete, file)
        data["opened"].append("/tmp/outside.jpg")
    elif change == "symlink":
        (complete / "escaped").symlink_to(tmp_path)
        assert contract.missing_inputs(complete, **kit_config)
        return
    else:
        file = "case.json"
        data = read(complete, file)
        if change == "vocab":
            data["vocab"] = "../outside.json"
        else:
            data["research"]["review_spec"] = "@../../outside"
    write_json(complete / file, data)
    assert contract.missing_inputs(complete, **kit_config)


def test_fixed_kit_version_mismatch_blocks_inputs(
    complete: Path,
    kit_config: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "KIT_VERSION", "different")
    assert any("kit:" in p for p in contract.missing_inputs(complete, **kit_config))


@pytest.mark.parametrize(
    "status,error,allowed",
    [
        ("0件", None, True),
        ("アクセス拒否", "HTTP 403", False),
        ("0件", "HTTP 403", False),
    ],
)
def test_review_opt_in_requires_observation_of_zero_instead_of_empty_failed_files(
    complete: Path,
    kit_config: dict[str, Path],
    status: str,
    error: str | None,
    allowed: bool,
) -> None:
    cfg = read(complete, "case.json")
    cfg["research"]["review_collect"] = True
    write_json(complete / "case.json", cfg)
    reviews = complete / "reviews"
    reviews.mkdir()
    product = {
        "url": "https://example.invalid/synthetic",
        "status": status,
        "site_total": 0,
        "fetched": 0,
        "error": error,
    }
    write_json(reviews / "acquire_log.json", {"products": [product]})
    (reviews / "reviews.jsonl").write_text("")
    assert (contract.missing_inputs(complete, **kit_config) == []) is allowed


@pytest.mark.parametrize(
    "status,official", [("unknown", []), ("none", ["synthetic"]), ("confirmed", [])]
)
def test_official_unknown_and_none_are_not_interchangeable(
    complete: Path,
    kit_config: dict[str, Path],
    status: str,
    official: list[str],
) -> None:
    cfg = read(complete, "case.json")
    cfg["brands"][0].update(official_status=status, official=official)
    write_json(complete / "case.json", cfg)
    assert contract.missing_inputs(complete, **kit_config)


def test_gate_is_read_only_for_case_and_external_kit(
    complete: Path,
    kit_config: dict[str, Path],
) -> None:
    before = contract._manifest(complete)
    assert contract.missing_inputs(complete, **kit_config) == []
    assert contract._manifest(complete) == before
    assert contract._kit_problems(kit_config["kit_path"]) == []


def test_seal_rejects_changes_made_during_validation(
    complete: Path,
    kit_config: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = contract.missing_inputs

    def change_after_validation(root: Path, **kwargs: Any) -> list[str]:
        problems = original(root, **kwargs)
        (root / f"frames/{VID}/001.jpg").write_bytes(b"changed after gate")
        return problems

    monkeypatch.setattr(contract, "missing_inputs", change_after_validation)
    with pytest.raises(contract.IncompleteInputsError, match="検査中"):
        contract.seal_inputs(complete, **kit_config)
    assert not (complete / "manifest.json").exists()
