"""Offline sales25 contract, pinned CLI and adversarial checks against the real 25-page FMT."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from teamagent.skills.first_visit import sales25
from teamagent.skills.first_visit import sales25_contract as contract
from tests.skills.first_visit.sales25_fixture import OWNER, inputs, prepare

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict[str, Any]]:
    base = tmp_path_factory.mktemp("sales25")
    root, output = base / "inputs", base / "output"
    prepare(root)
    contract.seal_inputs(root, owner_id=OWNER)
    result = sales25.generate(root, output, owner_id=OWNER)
    assert result["status"] == "passed", result
    return root, output, result


@pytest.fixture
def authored(tmp_path: Path) -> Path:
    root = tmp_path / "inputs"
    prepare(root)
    return root


def change(root: Path, relative: str, mutate: Any) -> None:
    path = root / relative
    data = contract.read_json(path)
    mutate(data)
    path.write_bytes(contract.json_bytes(data))


def test_plan_fingerprints_and_minimal_bundle() -> None:
    lock = contract.verify_bundle()
    assert lock["git_commit"] == "f8c98b4a5555479e28fee71a4195eaf38feeec09"
    assert len(lock["files"]) == 9
    assert (
        lock["distribution_zip_sha256"]
        == "b3ac594df90530f50cb61026b03e5282a1cba0470e8a402486dc3adfc505fb10"
    )
    # Kept independent of the mutable lock so an accidental pin update stays visible in review.
    assert (
        lock["files"]["assets/template.pptx"]
        == "376571ebba5777a26f0703cbe7ce489a646a80d4d1cd5c67469a868baf9524a7"
    )
    assert (
        lock["files"]["scripts/fill_template.py"]
        == "8d85602234cfa542282e866b8d6d715cd61b157869391c49fcbadde7958c5feb"
    )
    assert lock["local_patches"]["scripts/check_fmt.py"]["upstream_sha256"] == (
        "4cd0df65a60564718ce9d13f7058ed78569b3fa3be9aa4b6ea44d0a1c1ec28e5"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/fill_template.py",
        "scripts/check_fmt.py",
        "assets/template.pptx",
        "../sales25.lock.json",
    ],
)
def test_bundle_sha_tamper(tmp_path: Path, relative: str) -> None:
    bundle = tmp_path / "sales25"
    shutil.copytree(contract.BUNDLE, bundle)
    shutil.copyfile(contract.BUNDLE.parent / "sales25.lock.json", tmp_path / "sales25.lock.json")
    path = bundle / relative
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        contract.verify_bundle(bundle)


def test_missing_and_measured_zero_remain_distinct() -> None:
    case, raw, decisions = inputs()
    post = raw["q0"]["videos"][0]
    post["stats"]["collectCount"] = 0
    post["missingFields"].append("stats.collectCount")
    raw["q0"]["videos"][1]["stats"]["collectCount"] = 0
    original = copy.deepcopy(raw)
    result = contract.convert_inputs(case, raw, decisions, owner_id=OWNER)
    posts = result["selection/source_q0.json"]["posts"]
    assert posts[0]["stats"]["collectCount"] is None
    assert posts[1]["stats"]["collectCount"] == 0
    assert result["raw/q0.json"] == original["q0"]
    assert raw == original
    del post["missingFields"]
    with pytest.raises(ValueError, match="legacy zero"):
        contract.convert_inputs(case, raw, decisions, owner_id=OWNER)


def test_duplicates_keep_first_rank_and_cross_query_membership() -> None:
    case, raw, decisions = inputs()
    duplicate = copy.deepcopy(raw["q0"]["videos"][0])
    duplicate["rank_display"] = 101
    raw["q0"]["videos"].append(duplicate)
    other = raw["q1"]["videos"][4]
    old_id = other["id"]
    other["id"], other["url"] = duplicate["id"], duplicate["url"]
    next(d for d in decisions["q1"] if d["id"] == old_id)["id"] = duplicate["id"]
    result = contract.convert_inputs(case, raw, decisions, owner_id=OWNER)
    assert len(result["raw/q0.json"]["videos"]) == 101
    assert len(result["selection/source_q0.json"]["posts"]) == 100
    assert result["selection/source_q0.json"]["posts"][0]["rank"] == 1
    assert result["selection/source_q1.json"]["posts"][4]["rank"] == 5
    assert result["selection/source_q1.json"]["posts"][4]["id"] == duplicate["id"]


def test_rank_ties_zero_and_missing_eng() -> None:
    _, raw, _ = inputs()
    posts = [{**p, "rank": p["rank_display"], "eligible": True} for p in raw["q0"]["videos"]]
    posts[0]["stats"]["playCount"] = 0
    posts[1]["stats"]["shareCount"] = None
    rankings = contract._rankings(list(reversed(posts)))
    assert rankings["top_views"][0] == posts[1]["id"]  # Equal views use original display rank.
    assert posts[0]["id"] not in rankings["top_eng"]
    assert posts[1]["id"] not in rankings["top_eng"]
    assert rankings["top_saves"][0] == posts[0]["id"]


@pytest.mark.parametrize("status", ["failed", "unknown", "ok"])
def test_failed_empty_acquisition_is_not_zero(status: str) -> None:
    case, raw, decisions = inputs()
    raw["q0"].update(videos=[], status=status)
    decisions["q0"] = []
    with pytest.raises(ValueError, match="measured zero"):
        contract.convert_inputs(case, raw, decisions, owner_id=OWNER)
    raw["q0"].update(status="observed_zero", error_code="TIKTOK_TRULY_EMPTY")
    assert (
        contract.convert_inputs(case, raw, decisions, owner_id=OWNER)["selection/source_q0.json"][
            "posts"
        ]
        == []
    )


@pytest.mark.parametrize(
    "relative,mutation",
    [
        ("selection/source_q0.json", lambda d: d["posts"][0]["stats"].update(playCount=9999)),
        (
            "selection/source_q0.json",
            lambda d: d["posts"][0].update(url="https://example.invalid/other"),
        ),
        ("selection/source_q0.json", lambda d: d.update(query="別query")),
        ("raw/q0.json", lambda d: d.update(owner_id="owner-other")),
        ("raw/q0.json", lambda d: d["videos"][0].update(rank_display=2)),
        ("raw/q0.json", lambda d: d.update(sessions=2)),
        ("raw/q0.json", lambda d: d.update(missingness_preserved=False)),
    ],
)
def test_raw_source_and_acquisition_mismatch_rejected(
    authored: Path, relative: str, mutation: Any
) -> None:
    change(authored, relative, mutation)
    with pytest.raises(ValueError):
        contract.seal_inputs(authored, owner_id=OWNER)


def test_owner_mismatch(authored: Path) -> None:
    with pytest.raises(ValueError, match="owner mismatch"):
        contract.seal_inputs(authored, owner_id="owner-other")
    contract.seal_inputs(authored, owner_id=OWNER)
    with pytest.raises(ValueError, match="owner"):
        sales25.generate(authored, authored.parent / "output", owner_id="owner-other")
    assert not (authored.parent / "output").exists()


def test_manifest_sha_and_membership(authored: Path) -> None:
    contract.seal_inputs(authored, owner_id=OWNER)
    contract.verify_manifest(authored, owner_id=OWNER)
    path = authored / "case.json"
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="manifest"):
        contract.verify_manifest(authored, owner_id=OWNER)
    path.write_bytes(original)
    extra = authored / "sales25-manifest-extra.json"
    extra.write_text("{}")
    with pytest.raises(ValueError, match="membership"):
        contract.verify_manifest(authored, owner_id=OWNER)
    extra.unlink()
    with pytest.raises(FileExistsError):
        contract.seal_inputs(authored, owner_id=OWNER)


def test_manifest_detects_same_size_manuscript_tamper(authored: Path) -> None:
    contract.seal_inputs(authored, owner_id=OWNER)
    path = authored / "deck-input.json"
    original = path.read_bytes()
    changed = original.replace("合成入力のみ".encode(), "合成実験のみ".encode())
    assert changed != original and len(changed) == len(original)
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="manifest"):
        contract.verify_manifest(authored, owner_id=OWNER)


@pytest.mark.parametrize(
    "relative,mutation",
    [
        ("deck-plan.json", lambda d: d.pop("analysis_selection")),
        ("deck-plan.json", lambda d: d["analysis_selection"].update(top_n=5)),
        ("deck-plan.json", lambda d: d["analysis_selection"].update(target_count_per_query=30)),
        (
            "deck-plan.json",
            lambda d: d["analysis_selection"]["queries"][0]["rankings"]["top_views"].reverse(),
        ),
        ("deck-plan.json", lambda d: d["slides"][3]["post_ids"].reverse()),
        ("top5-analysis.json", lambda d: d["posts"].pop("9000000010")),
        (
            "deck-input.json",
            lambda d: d["images"]["p21_image_01"].update(url="https://example.invalid/other"),
        ),
        ("deck-plan.json", lambda d: d["pr_examples"][0].update(pr_basis="")),
        ("deck-input.json", lambda d: d["texts"].update(t029="別原稿")),
        (
            "deck-input.json",
            lambda d: d["images"]["p04_image_01"].update(url=d["images"]["p04_image_02"]["url"]),
        ),
        (
            "deck-input.json",
            lambda d: d["images"]["p21_image_02"].update(path=d["images"]["p21_image_01"]["path"]),
        ),
    ],
)
def test_top10_rankings_and_pr_gate(authored: Path, relative: str, mutation: Any) -> None:
    change(authored, relative, mutation)
    with pytest.raises(ValueError):
        contract.seal_inputs(authored, owner_id=OWNER)


def test_paths_and_symlinks(authored: Path) -> None:
    change(
        authored,
        "deck-input.json",
        lambda d: d["images"]["p21_image_01"].update(path="../outside.png"),
    )
    with pytest.raises(ValueError, match="relative path"):
        contract.seal_inputs(authored, owner_id=OWNER)
    with pytest.raises(ValueError, match="relative path"):
        contract.write_inputs(authored.parent / "new", {"../escape.json": {}})
    linked = authored / "linked"
    linked.symlink_to(authored / "case.json")
    with pytest.raises(ValueError, match="symlink"):
        contract.seal_inputs(authored, owner_id=OWNER)


@pytest.mark.parametrize("qid", ["q0", "q3"])
def test_search_order_keeps_ineligible_posts(authored: Path, qid: str) -> None:
    decisions = contract.read_json(authored / f"selection/decisions_{qid}.json")
    for index, decision in enumerate(decisions, 1):
        decision["eligible"] = index != 8 and index <= 10
    query = next(
        q for q in contract.read_json(authored / "case.json")["queries"] if q["query_id"] == qid
    )
    source = contract._source(
        query, contract.read_json(authored / f"raw/{qid}.json"), decisions, OWNER
    )
    (authored / f"selection/decisions_{qid}.json").write_bytes(contract.json_bytes(decisions))
    (authored / f"selection/source_{qid}.json").write_bytes(contract.json_bytes(source))
    if qid == "q0":
        change(
            authored,
            "deck-plan.json",
            lambda d: d["analysis_selection"]["queries"][0].update(
                rankings=contract._rankings(source["posts"])
            ),
        )
    contract.validate_inputs(authored, owner_id=OWNER)
    page = 2 if qid == "q0" else 14
    change(authored, "deck-plan.json", lambda d: d["slides"][page]["post_ids"].pop(7))
    with pytest.raises(ValueError, match="search-order post IDs"):
        contract.validate_inputs(authored, owner_id=OWNER)


@pytest.mark.parametrize("tag", ["PR", "pr", "Pr", "product", 1])
def test_pr_hashtag_case(authored: Path, tag: str | int) -> None:
    for relative, key in (("raw/q0.json", "videos"), ("selection/source_q0.json", "posts")):
        change(authored, relative, lambda d, key=key: d[key][0].update(isAd=False, hashtags=[tag]))
    if tag in ("product", 1):
        with pytest.raises(ValueError, match="PR post/evidence mismatch"):
            contract.validate_inputs(authored, owner_id=OWNER)
    else:
        contract.validate_inputs(authored, owner_id=OWNER)


@pytest.mark.parametrize("image_key", ["p04_image_01", "p21_image_01"])
def test_reviewed_display_derivative_preserves_photo_count(authored: Path, image_key: str) -> None:
    image = "images/display.png"
    shutil.copyfile(authored / "images/portrait.png", authored / image)
    change(authored, "deck-input.json", lambda d: d["images"][image_key].update(path=image))
    with pytest.raises(ValueError, match="image"):
        contract.validate_inputs(authored, owner_id=OWNER)
    change(
        authored,
        "top5-analysis.json",
        lambda d: d["posts"]["9000000001"]["visual"].update(opened_display_files=[image]),
    )
    review = contract.read_json(authored / "top5-analysis.json")["posts"]["9000000001"]
    assert review["photo_count"] == len(review["visual"]["opened_files"]) == 3
    contract.seal_inputs(authored, owner_id=OWNER)
    result = sales25.generate(authored, authored.parent / "out", owner_id=OWNER)
    assert result["status"] == "passed"
    (authored / image).unlink()
    with pytest.raises(ValueError, match="missing review evidence"):
        contract.validate_inputs(authored, owner_id=OWNER)


@pytest.mark.parametrize("paths", [None, "images/portrait.png", [1]])
def test_display_files_require_path_list(authored: Path, paths: Any) -> None:
    change(
        authored,
        "top5-analysis.json",
        lambda d: d["posts"]["9000000001"]["visual"].update(opened_display_files=paths),
    )
    with pytest.raises(ValueError, match="opened_display_files"):
        contract.validate_inputs(authored, owner_id=OWNER)


def test_25_pages_checker_results_links_notes_and_native_table(
    built: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, output, result = built
    assert result["delivery_status"] == "incomplete"
    assert result["visual_review"] == "not_verified"
    assert (
        result["output_sha256"] == hashlib.sha256((output / "deck.pptx").read_bytes()).hexdigest()
    )
    analysis = contract.read_json(output / "qa/check_top5_analysis.json")
    assert analysis["returncode"] == 0
    assert analysis["result"]["selection_mode"] == "query_top10"
    assert analysis["result"]["analysis_top_n"] == 10
    assert analysis["result"]["required_n"] == analysis["result"]["reviewed_n"] == 30
    assert contract.read_json(output / "qa/check_fmt.json")["returncode"] == 0
    with zipfile.ZipFile(output / "deck.pptx") as z:
        p = ET.fromstring(z.read("ppt/presentation.xml"))
        assert len(p.findall("p:sldIdLst/p:sldId", NS)) == 25
        assert b"https://example.invalid/synthetic" in z.read("ppt/slides/_rels/slide1.xml.rels")
        for page in range(1, 26):
            assert "合成入力のみ" in z.read(f"ppt/notesSlides/notesSlide{page}.xml").decode()
        assert ET.fromstring(z.read("ppt/slides/slide24.xml")).find(".//a:tbl", NS) is not None
    contract.verify_manifest(root, owner_id=OWNER)


@pytest.mark.parametrize("attribute", ["x", "cx"])
def test_one_shape_position_or_size_tamper_rejected(
    built: tuple[Path, Path, dict[str, Any]], tmp_path: Path, attribute: str
) -> None:
    root, output, _ = built
    with zipfile.ZipFile(output / "deck.pptx") as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    slide = ET.fromstring(parts["ppt/slides/slide1.xml"])
    node = slide.find(".//a:xfrm/a:off" if attribute == "x" else ".//a:xfrm/a:ext", NS)
    assert node is not None
    node.set(attribute, str(int(node.attrib[attribute]) + 1))
    parts["ppt/slides/slide1.xml"] = ET.tostring(slide)
    pptx = tmp_path / "tampered.pptx"
    with zipfile.ZipFile(pptx, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    result = sales25.check_deck(root, pptx, tmp_path / "qa", owner_id=OWNER)
    assert result["status"] == "invalid"
    assert contract.read_json(tmp_path / "qa/check_fmt.json")["returncode"] == 1


def test_missing_input_filler_fails_and_saves_result(authored: Path) -> None:
    change(authored, "deck-input.json", lambda d: d["texts"].pop("date"))
    contract.seal_inputs(authored, owner_id=OWNER)
    out = authored.parent / "out"
    with pytest.raises(ValueError, match="filler failed"):
        sales25.generate(authored, out, owner_id=OWNER)
    record = contract.read_json(out / "filler-qa/fill_template.json")
    assert record["returncode"] == 2
    assert "Missing text input" in record["result"]["error"]
    assert not (out / "deck.pptx").exists()


def test_placeholder_rejected_by_checker(authored: Path) -> None:
    change(authored, "deck-input.json", lambda d: d["texts"].update(date="未入力"))
    contract.seal_inputs(authored, owner_id=OWNER)
    result = sales25.generate(authored, authored.parent / "out", owner_id=OWNER)
    assert result["status"] == "invalid"


def test_incomplete_analysis_exit2_is_saved(authored: Path) -> None:
    change(
        authored,
        "top5-analysis.json",
        lambda d: d["posts"]["9000000010"].update(
            audio={"status": "unmeasured", "summary": "未計測", "reason": "合成試験"}
        ),
    )
    contract.seal_inputs(authored, owner_id=OWNER)
    out = authored.parent / "out"
    result = sales25.generate(authored, out, owner_id=OWNER)
    assert result["status"] == "incomplete"
    assert contract.read_json(out / "qa/check_top5_analysis.json")["returncode"] == 2


def test_no_clobber_or_output_inside_inputs(built: tuple[Path, Path, dict[str, Any]]) -> None:
    root, output, _ = built
    with pytest.raises(FileExistsError):
        sales25.generate(root, output, owner_id=OWNER)
    with pytest.raises(ValueError, match="outside sealed"):
        sales25.generate(root, root / "output", owner_id=OWNER)


def test_neutral_query_shortfall_stays_incomplete(authored: Path) -> None:
    change(
        authored,
        "raw/q3.json",
        lambda d: d.update(videos=d["videos"][:50], stop_reason="exhausted"),
    )
    change(authored, "selection/source_q3.json", lambda d: d.update(posts=d["posts"][:50]))
    path = authored / "selection/decisions_q3.json"
    path.write_bytes(contract.json_bytes(contract.read_json(path)[:50]))
    contract.seal_inputs(authored, owner_id=OWNER)
    out = authored.parent / "out"
    result = sales25.generate(authored, out, owner_id=OWNER)
    assert contract.read_json(out / "qa/check_top5_analysis.json")["result"]["status"] == "passed"
    assert result["status"] == "incomplete"
    assert result["acquisition"][3] == {
        "query_id": "q3",
        "target": 100,
        "actual": 50,
        "shortfall": 50,
    }


def test_pillow13_real_filler_formats_aspect_padding_and_alpha(authored: Path) -> None:
    env = dict(os.environ)
    # CI already has 13; local validation can supply a read-only isolated installation.
    if overlay := env.get("SALES25_PILLOW13_PATH"):
        env["PYTHONPATH"] = overlay + os.pathsep + env.get("PYTHONPATH", "")
    version = subprocess.run(
        [sys.executable, "-B", "-c", "import PIL; print(PIL.__version__)"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert version.stdout.strip() == "13.0.0", "Pillow 13.0.0 required for this compatibility test"
    contract.seal_inputs(authored, owner_id=OWNER)
    out = authored.parent / "pillow13"
    program = (
        "import json,sys; from pathlib import Path; "
        "from teamagent.skills.first_visit.sales25 import generate; "
        "print(json.dumps(generate(Path(sys.argv[1]), Path(sys.argv[2]), owner_id=sys.argv[3])))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, str(authored), str(out), OWNER],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "passed"
    fields = contract.read_json(contract.BUNDLE / "assets/fields.json")["fields"]
    data = contract.read_json(authored / "deck-input.json")
    status = contract.read_json(out / "build-status.json")
    checked: set[str] = set()
    with zipfile.ZipFile(out / "deck.pptx") as archive:
        for record in status["fields"]:
            if record["kind"] != "image":
                continue
            filename = data["images"][record["key"]]["path"]
            if filename in checked:
                continue
            checked.add(filename)
            field = next(f for f in fields if f["key"] == record["key"])
            with Image.open(BytesIO(archive.read(record["media_part"]))) as image:
                if filename.endswith("portrait.png"):
                    assert image.size == (72, 128)  # Native ratio keeps original bytes/dimensions.
                else:
                    box = field["bbox"]
                    assert image.size == (round(box["width"] * 4), round(box["height"] * 4))
                    corner = image.getpixel((0, 0))
                    assert isinstance(corner, tuple)
                    assert image.mode == "RGBA" and corner[3] == 0
                    bounds = image.getchannel("A").getbbox()
                    assert bounds is not None
                    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
                    expected_ratio = 2 if filename.endswith("jpeg") else 1
                    assert abs(width / height - expected_ratio) < 0.02
                    assert abs(bounds[0] - (image.width - bounds[2])) <= 1
                    assert abs(bounds[1] - (image.height - bounds[3])) <= 1
                    if filename.endswith("transparent.png"):
                        center = image.getpixel((image.width // 2, image.height // 2))
                        assert isinstance(center, tuple)
                        assert 120 <= center[3] <= 136
    assert len(checked) == 3
