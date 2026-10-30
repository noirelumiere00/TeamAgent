"""SHA固定の実FMTで画像欄の検出・挿入を確認。資材はローカル限定・登録しない。"""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from pptx import Presentation

from teamagent.media.operations import _iter_proposal_image_slots
from teamagent.media.proposal_quality import proposal_field_spec, proposal_readiness_issues
from teamagent.media.render_child import _proposal
from teamagent.skills.proposal_deck.contract import ComposerOutput
from teamagent.skills.proposal_deck.renderer import render_deck
from tests.skills.proposal_deck.test_renderer_images import _TINY_JPEG

_ARTIFACTS = Path.home() / "Documents/Claude/Artifacts"
_TEMPLATE = _ARTIFACTS / "handoff-20261030/fmt86_07ea4170.pptx"
_SHA256 = "07ea41700933d380295c5b8e713d803c8276c46c94b18008dbd8bf230539f883"
# ラベルで確認した画像欄。元FMTの表示順とshape_id（座標には依存しない）。
_IMAGE_FIELDS = {38: [2], 40: [7, 8, 10], 41: [12, 15, 17], 72: [5]}


@pytest.fixture
def real_template() -> Path:
    if not _TEMPLATE.is_file():
        pytest.skip("SHA固定の実FMTローカルコピーがない")
    assert hashlib.sha256(_TEMPLATE.read_bytes()).hexdigest() == _SHA256
    return _TEMPLATE


def test_real_template_image_spec_matches_worker_slots(real_template: Path) -> None:
    prs = Presentation(str(real_template))
    assert len(prs.slides) == 86
    expected = {
        (prs.slides[page - 1].slide_id, shape_id)
        for page, ids in _IMAGE_FIELDS.items()
        for shape_id in ids
    }
    spec = proposal_field_spec(prs)
    assert {(field["slide_id"], field["shape_id"]) for field in spec["images"]} == expected
    assert {
        (slide.slide_id, shape.shape_id) for slide, shape in _iter_proposal_image_slots(prs)
    } == expected
    assert all(field["expected"] == 1 for field in spec["images"])


@pytest.mark.parametrize(
    "scenario", ["none", "single", "valid", "broken", "overflow", "no-conditions"]
)
@pytest.mark.parametrize("backend", ["local", "worker"])
@pytest.mark.parametrize("unit,sector,expected_images", [("BU2", "食品", 1), ("BU1", "美容", 8)])
def test_real_template_filled_images_have_no_image_issue(
    real_template: Path,
    tmp_path: Path,
    backend: str,
    unit: str,
    sector: str,
    expected_images: int,
    scenario: str,
) -> None:
    composer_path = _ARTIFACTS / f"20261022_BU2FMT/composer_{unit}_{sector}.json"
    if not composer_path.is_file():
        pytest.skip("合成composerのローカルコピーがない")
    data = json.loads(composer_path.read_text())
    assert data["business_unit"] == unit and data["product_sector"] == sector
    if scenario == "no-conditions":
        data.update(plans=[], simulation=None, hearing_items=[], estimate_terms={})
    image_count = {
        "none": 0,
        "single": 1,
        "valid": 8,
        "broken": 1,
        "overflow": 9,
        "no-conditions": 0,
    }[scenario]
    # 合成画像だけを追加。原本FMT・composerは変更しない。
    image = tmp_path / "synthetic.jpg"
    image.write_bytes(b"broken" if scenario == "broken" else _TINY_JPEG)
    data["evidence_images"] = {
        58: [
            {"placeholder_id": 58, "rank": rank, "keyword": "合成画像", "image_path": str(image)}
            for rank in range(1, image_count + 1)
        ]
    }
    if image_count == 0:
        data["evidence_images"] = {}
    output = tmp_path / "output.pptx"
    metadata: dict[str, Any] = {}
    if backend == "local":
        render_deck(
            ComposerOutput.model_validate(data),
            real_template,
            output,
            enable_images=True,
            metadata=metadata,
        )
    else:
        shutil.copyfile(real_template, tmp_path / "template.pptx")
        (tmp_path / "composer.json").write_text(json.dumps(data))
        metadata = _proposal(
            tmp_path,
            {
                "kind": "proposal_pptx",
                "template": "template.pptx",
                "composer": "composer.json",
                "output": "output.pptx",
                "fail_if_missing": True,
                "evidence": [
                    {"placeholder_id": 58, "rank": rank, "path": image.name}
                    for rank in range(1, image_count + 1)
                ],
            },
        )
    issues = proposal_readiness_issues(metadata)
    if scenario == "broken":
        assert "image_insertion_failed:1" in issues
    else:
        assert issues == []
    assert "required_image_spec_missing" not in metadata["field_issues"]
    assert len(metadata["image_slots"]) == expected_images
    inserted = sum(field["inserted"] for field in metadata["image_slots"])
    assert metadata["evidence_images"] == inserted
    if scenario in {"single", "valid", "overflow"}:
        assert inserted == min(image_count, expected_images)
        if unit == "BU2":
            assert metadata["image_slots"][0]["slide"] == 54
            slide = Presentation(str(output)).slides[53]
            assert any(shape.shape_type == 13 for shape in slide.shapes)
    if image_count > expected_images:
        assert (
            f"要対応: 画像 {image_count - expected_images} 件は貼り先なし"
            in metadata["field_warnings"]
        )
    if scenario in {"none", "no-conditions"}:
        assert metadata["evidence_images"] == 0
        assert all(field["inserted"] == 0 for field in metadata["image_slots"])
        assert any(
            f"要対応: 画像未挿入 {expected_images} 枠（" in warning
            for warning in metadata["field_warnings"]
        )
    if scenario == "no-conditions" and unit == "BU2":
        hearing = next(slide for slide in Presentation(str(output)).slides if slide.slide_id == 341)
        assert 20 in {shape.shape_id for shape in hearing.shapes}
        assert not {21, 22, 23, 24, 25} & {shape.shape_id for shape in hearing.shapes}
        assert (
            sum(
                shape.text.count("条件未指定・要相談")
                for shape in hearing.shapes
                if shape.has_text_frame
            )
            == 1
        )


@pytest.mark.parametrize("backend", ["local", "worker"])
@pytest.mark.parametrize("unit,sector", [("BU1", "美容"), ("BU2", "食品")])
def test_real_template_content_after_length_fallback(
    real_template: Path, tmp_path: Path, backend: str, unit: str, sector: str
) -> None:
    from unittest.mock import MagicMock

    from teamagent.skills.base import SkillContext
    from teamagent.skills.proposal_deck.contract import LENGTH_RULES
    from teamagent.skills.proposal_deck.schema import ProposalDeckInput
    from teamagent.skills.proposal_deck.skill import ProposalDeckSkill

    composer_path = _ARTIFACTS / f"20261022_BU2FMT/composer_{unit}_{sector}.json"
    if not composer_path.is_file():
        pytest.skip("合成composerのローカルコピーがない")
    data = json.loads(composer_path.read_text())
    fields = {
        str(pid): "あ" * hi
        for pid, (_, hi) in LENGTH_RULES.items()
        if pid in {7, 9, 11} or 58 <= pid <= 92
    }
    fields.update(
        {
            "1": "合成商材 PR×ショート動画施策ご提案書",
            "47": "「朝の余白」— 朝の気持ちに寄り添う。\n「夜のご褒美」— 夜の習慣に結びつく。\n「手軽な習慣」— 続ける気持ちを後押しする。",
            "64": "あ" * 31,
        }
    )
    data["placeholders"].update(fields)
    data["skipped_placeholders"] = [
        item for item in data["skipped_placeholders"] if str(item["id"]) not in fields
    ]
    data["posting_start_date"] = None
    bedrock = MagicMock(model_id="unknown-model")
    deck = ProposalDeckSkill(bedrock=bedrock)
    _, warnings = deck._repair_lengths(
        data,
        ProposalDeckInput(product_name="合成商材", goal="認知", target_persona="成人"),
        SkillContext(),
        spent=0,
    )
    bedrock.converse.assert_not_called()
    output = tmp_path / "output.pptx"
    metadata: dict[str, Any] = {}
    if backend == "local":
        render_deck(ComposerOutput.model_validate(data), real_template, output, metadata=metadata)
    else:
        shutil.copyfile(real_template, tmp_path / "template.pptx")
        (tmp_path / "composer.json").write_text(json.dumps(data))
        metadata = _proposal(
            tmp_path,
            {
                "kind": "proposal_pptx",
                "template": "template.pptx",
                "composer": "composer.json",
                "output": "output.pptx",
                "fail_if_missing": True,
                "evidence": [],
            },
        )
    prs = Presentation(str(output))
    pages = {slide.slide_id: slide for slide in prs.slides}
    assert len(prs.slides) == (83 if unit == "BU1" else 68)
    cover = next(shape.text for shape in pages[256].shapes if shape.shape_id == 2)
    assert cover.count("PR×ショート動画施策") == 1 and cover.count("ご提案書") == 1
    current = next(shape.text for shape in pages[278].shapes if shape.shape_id == 18)
    assert current and "UGC" in current and "○○" not in current
    gantt = {shape.shape_id: shape.text for shape in pages[336].shapes if shape.has_text_frame}
    assert "クライアント確認" in gantt[39] and "クライアント確認" in gantt[47]
    assert "未定" in gantt[34]
    context = {shape.shape_id: shape for shape in pages[308].shapes}
    assert "あ" * 30 in context[84].text
    assert warnings[0] in pages[308].notes_slide.notes_text_frame.text
    # 3つの見出し・1行の別案・狭い訴求軸で、上限の全角文字が収まる。
    from teamagent.media.deck_qa import inspect_pptx

    qa = inspect_pptx(output)
    checked = {(273, 11), (273, 12), (273, 13), (302, 2), (308, 8), (308, 31), (308, 39), (308, 43)}
    checked_names = {
        (index, shape.name)
        for index, slide in enumerate(prs.slides, 1)
        for shape in slide.shapes
        if (slide.slide_id, shape.shape_id) in checked
    }
    assert not [
        finding
        for finding in qa.findings
        if finding.kind == "text_overflow" and (finding.slide, finding.shape) in checked_names
    ]
    if unit == "BU1":
        pink = [
            warning
            for warning in metadata["field_warnings"]
            if "画像の仮枠が残っています" in warning
        ]
        assert len(pink) == 1
        assert all(
            str(next(i for i, slide in enumerate(prs.slides, 1) if slide.slide_id == sid))
            in pink[0]
            for sid in (297, 299, 300)
        )


@pytest.mark.parametrize("backend", ["local", "worker"])
@pytest.mark.parametrize("unit,sector", [("BU1", "美容"), ("BU2", "食品")])
def test_real_template_five_candidates_with_reasons_fit_without_background_overlap(
    real_template: Path, tmp_path: Path, backend: str, unit: str, sector: str
) -> None:
    import math

    from teamagent.media.deck_qa import NS, inspect_pptx

    composer_path = _ARTIFACTS / f"20261022_BU2FMT/composer_{unit}_{sector}.json"
    if not composer_path.is_file():
        pytest.skip("合成composerのローカルコピーがない")
    data = json.loads(composer_path.read_text())
    prefixes = [f"「合成案{chr(0xFF21 + index)}」— " for index in range(5)]
    candidates = "\n".join(prefix + "理" * (80 - len(prefix)) for prefix in prefixes)
    assert all(len(line) == 80 for line in candidates.splitlines())
    data["placeholders"].update({"47": candidates, "56": "背" * 450})
    data["skipped_placeholders"] = [
        item for item in data["skipped_placeholders"] if item["id"] not in {47, 56}
    ]
    output = tmp_path / "output.pptx"
    if backend == "local":
        render_deck(ComposerOutput.model_validate(data), real_template, output)
    else:
        shutil.copyfile(real_template, tmp_path / "template.pptx")
        (tmp_path / "composer.json").write_text(json.dumps(data))
        _proposal(
            tmp_path,
            {
                "kind": "proposal_pptx",
                "template": "template.pptx",
                "composer": "composer.json",
                "output": "output.pptx",
                "fail_if_missing": True,
                "evidence": [],
            },
        )
    prs = Presentation(str(output))
    number, slide = next((i, page) for i, page in enumerate(prs.slides, 1) if page.slide_id == 302)
    fields = {shape.shape_id: shape for shape in slide.shapes}
    choices, background = fields[2], fields[4]
    assert len(prs.slides) == (83 if unit == "BU1" else 68)
    assert choices.height.pt == pytest.approx(80)
    assert choices.top.pt == pytest.approx(428.0024, abs=0.001)
    assert background.text.replace("\n", "").replace("\v", "") == "背" * 450
    assert background.top + background.height <= choices.top - 12 * 12700
    lines = choices.text.replace("\v", "\n").splitlines()
    assert len(lines) == 6 and lines[1:] == candidates.splitlines()
    # localにもPowerPointの実改行(a:br)を残す。a:t中の改行だけには頼らない。
    assert len(choices._element.findall(".//a:br", NS)) == 5
    for shape, size in ((choices, 10), (background, 16)):
        frame = shape.text_frame
        # Keynoteで600字が重なった実測を反映。全ての字を1em、16pt本文は行送り1.5倍で見積もる。
        columns = math.floor(
            (shape.width - frame.margin_left - frame.margin_right) / (size * 12700)
        )
        line_count = sum(
            max(1, math.ceil(len(line) / columns))
            for paragraph in frame.paragraphs
            for line in paragraph.text.replace("\v", "\n").split("\n")
        )
        leading = 1.5 if size == 16 else 1.2
        needed = frame.margin_top + frame.margin_bottom + line_count * size * 12700 * leading
        assert needed <= shape.height
        if size == 16:
            assert shape.top + needed < choices.top
        assert all(
            run.font.size.pt == size
            for p in shape.text_frame.paragraphs
            for run in p.runs
            if run.text
        )
    assert not [
        f
        for f in inspect_pptx(output).findings
        if f.slide == number
        and f.shape in {choices.name, background.name}
        and f.kind in {"text_overflow", "text_autofit_collision"}
    ]
