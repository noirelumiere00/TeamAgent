"""テンプレ差し替え層。**一から作らず**、台帳で解決した枠だけを埋める。

期待値の出どころは記入例（初田製作所）から起こした台帳 ``CLIP_TEMPLATE_INVENTORY_V1``。
テンプレ実物は repo に置かないので、台帳から同じ構造の合成テンプレを組んで検査する。

本番の失敗モードを再現する:
- 訴求軸 17 段落 / 切り抜きメモ 5 段落 / 界隈詳細 3 段落 の枠へ、**段落数の違う**本文を流す
- テンプレの shape が 1 つ欠けている（消毒スクリプトの事故・別版の混入）
- テンプレの段落数が改竄されている
- 採用できなかったセルが残る（テンプレの指示文がそのまま得意先へ出る事故）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from teamagent.skills.clip_proposal.analysis import CELL_COUNT
from teamagent.skills.clip_proposal.inventory import CLIP_TEMPLATE_INVENTORY_V1 as INVENTORY
from teamagent.skills.clip_proposal.template_fill import (
    ClipTemplateInvalidError,
    apply_fill_plan,
    apply_shape_text,
    build_fill_plan,
    resolve_image_slots,
    truncate_for_frame,
    validate_template,
)

from .fixtures import PARAGRAPH_SIZES, build_synthetic_template, sample_analysis

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{_A}}}{tag}"


def _open(path: Path):  # type: ignore[no-untyped-def]
    from pptx import Presentation

    return Presentation(str(path))


def _shape(path: Path, shape_id: int):  # type: ignore[no-untyped-def]
    slide = _open(path).slides[0]
    for shape in slide.shapes:
        if int(shape.shape_id) == shape_id:
            return shape
    raise AssertionError(f"shape {shape_id} not found")


def _paragraph_texts(shape) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        "".join(node.text or "" for node in paragraph.findall(f"{{{_A}}}r/{{{_A}}}t"))
        for paragraph in shape.text_frame._txBody.findall(_qn("p"))
    ]


def _paragraph_sizes(shape) -> list[str]:  # type: ignore[no-untyped-def]
    sizes: list[str] = []
    for paragraph in shape.text_frame._txBody.findall(_qn("p")):
        run = paragraph.find(_qn("r"))
        props = None if run is None else run.find(_qn("rPr"))
        sizes.append("" if props is None else str(props.get("sz") or ""))
    return sizes


@pytest.fixture()
def template(tmp_path: Path) -> Path:
    path = tmp_path / "clip_template_v1.pptx"
    build_synthetic_template(str(path))
    return path


# ---------------------------------------------------------------------------
# 台帳の解決（PPTX に触らない純関数）
# ---------------------------------------------------------------------------


def test_plan_resolves_every_cell_plus_axes() -> None:
    plan = build_fill_plan(sample_analysis())
    assert plan.template_profile == "clip-proposal-v1"
    assert plan.filled_cells == CELL_COUNT
    assert plan.empty_cells == ()
    # 訴求軸 1 枠 ＋ 10 セル × 7 枠。
    assert len(plan.text_ops) == 1 + CELL_COUNT * 7
    covered = {op.shape_id for op in plan.text_ops}
    assert covered == {spec.shape_id for spec in INVENTORY.text_frames()}


def test_plan_blanks_cells_that_lost_their_clip() -> None:
    """採用できなかったセルはテンプレの指示文ごと空にする（残すと得意先へ出る）。"""

    plan = build_fill_plan(sample_analysis(clip_count=7))
    assert plan.filled_cells == 7
    assert plan.empty_cells == (7, 8, 9)
    cell = INVENTORY.cell(9)
    blanked = {op.shape_id: op.paragraphs for op in plan.text_ops}
    for spec in cell.text_frames():
        assert blanked[spec.shape_id] == ("",)


def test_axes_block_keeps_the_heading_and_numbers_the_real_axes() -> None:
    plan = build_fill_plan(sample_analysis())
    axes_op = next(op for op in plan.text_ops if op.role == "axes")
    assert axes_op.paragraphs[0] == "本編での訴求軸＝強み"
    assert axes_op.paragraphs[1] == "（切り抜く文脈）"
    assert "１、" not in "".join(axes_op.paragraphs)  # 全角番号はテンプレ側の飾り
    assert axes_op.paragraphs[3].startswith("1、訴求軸1")


def test_axes_block_says_so_when_nothing_was_confirmed() -> None:
    analysis = sample_analysis()
    empty = type(analysis)(client_name=analysis.client_name, clips=analysis.clips)
    plan = build_fill_plan(empty)
    axes_op = next(op for op in plan.text_ops if op.role == "axes")
    assert axes_op.paragraphs[-1] == "（本編から確認できた訴求軸がありませんでした）"


def test_image_slots_resolve_to_the_ledger_ids() -> None:
    plan = build_fill_plan(sample_analysis())
    resolved = resolve_image_slots(plan)
    assert len(resolved) == 1 + CELL_COUNT * 2
    assert resolved["6:1"].role == "main_video"
    # フック枠は 9:16 寄り、切り抜き枠は 16:9。取り違えると資料が崩れる。
    assert resolved["4:1"].aspect_ratio < 1.0
    assert resolved["10:1"].aspect_ratio > 1.5


def test_truncation_prefers_a_natural_boundary() -> None:
    spec = INVENTORY.cell(0).band_bottom
    text = "1か月の導入研修と工場実習で基礎から学べます、未経験でも安心です"
    trimmed = truncate_for_frame(text, spec)
    assert len(trimmed) <= spec.max_chars
    assert trimmed.endswith("、")


# ---------------------------------------------------------------------------
# テンプレ検証（fail-closed）
# ---------------------------------------------------------------------------


def test_valid_template_passes(template: Path) -> None:
    validate_template(str(template))


def test_missing_shape_fails_closed_without_writing_a_byte(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pptx"
    build_synthetic_template(str(broken), omit_shape_id=INVENTORY.cell(3).note.shape_id)
    output = tmp_path / "out.pptx"
    with pytest.raises(ClipTemplateInvalidError) as excinfo:
        apply_fill_plan(str(broken), build_fill_plan(sample_analysis()), str(output))
    assert excinfo.value.code == "MEDIA_CLIP_TEMPLATE_INVALID"
    assert not output.exists()


def test_validate_template_alone_rejects_a_missing_shape(tmp_path: Path) -> None:
    """検証だけを単体で呼んでも欠落を検出する（差し替え側の保険に依存しない）。"""

    broken = tmp_path / "broken.pptx"
    build_synthetic_template(str(broken), omit_shape_id=INVENTORY.cell(3).note.shape_id)
    with pytest.raises(ClipTemplateInvalidError) as excinfo:
        validate_template(str(broken))
    assert "missing shape" in excinfo.value.detail


def test_tampered_paragraph_count_fails_closed(tmp_path: Path) -> None:
    tampered = tmp_path / "tampered.pptx"
    build_synthetic_template(str(tampered), paragraph_delta={INVENTORY.axes.shape_id: -3})
    with pytest.raises(ClipTemplateInvalidError) as excinfo:
        validate_template(str(tampered))
    assert "paragraphs" in excinfo.value.detail


def test_vertical_hook_frame_is_part_of_the_contract(template: Path) -> None:
    hook = INVENTORY.cell(0).hook_copy
    assert hook.vert == "eaVert"
    body_pr = _shape(template, hook.shape_id).text_frame._txBody.find(_qn("bodyPr"))
    assert body_pr.get("vert") == "eaVert"


# ---------------------------------------------------------------------------
# 差し替え（段落書式を壊さない）
# ---------------------------------------------------------------------------


def test_fill_writes_every_cell(template: Path, tmp_path: Path) -> None:
    output = tmp_path / "filled.pptx"
    apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))
    assert output.exists()
    cell = INVENTORY.cell(0)
    assert _paragraph_texts(_shape(output, cell.band_top.shape_id)) == ["上帯0"]
    assert _paragraph_texts(_shape(output, cell.hook_copy.shape_id)) == ["フック0"]
    assert _paragraph_texts(_shape(output, cell.search_word.shape_id)) == ["🔍テスト商材 採用"]


def test_multi_paragraph_frames_keep_one_run_per_paragraph(template: Path, tmp_path: Path) -> None:
    """訴求軸・切り抜きメモ・界隈詳細が段落 0 へ潰れないこと（旗艦）。

    ``_replace_placeholders`` 相当（``paragraphs[0].text = ...``）へ差し替えると、
    段落数が 1 になって必ず赤くなる。
    """

    output = tmp_path / "filled.pptx"
    apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))

    axes = _paragraph_texts(_shape(output, INVENTORY.axes.shape_id))
    assert len(axes) > 1
    assert axes[0] == "本編での訴求軸＝強み"

    cell = INVENTORY.cell(0)
    detail = _paragraph_texts(_shape(output, cell.detail.shape_id))
    assert len(detail) == 3
    assert detail[0].startswith("・界隈言語：")

    note = _paragraph_texts(_shape(output, cell.note.shape_id))
    assert len(note) == 6  # 記入例実測の段落構成（テンプレは 5 段落）
    assert note[0] == "切り抜き箇所"
    assert note[3] == ""


def test_paragraph_run_formatting_survives_the_swap(template: Path, tmp_path: Path) -> None:
    """段落ごとの ``sz`` が差し替え後も段落ごとに残る（全段落が同じ値に潰れない）。"""

    cell = INVENTORY.cell(0)
    before = _paragraph_sizes(_shape(template, cell.note.shape_id))
    assert before == [str(size) for size in PARAGRAPH_SIZES[:5]]

    output = tmp_path / "filled.pptx"
    apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))
    after = _paragraph_sizes(_shape(output, cell.note.shape_id))

    # 6 段落へ増えても、先頭 5 段落は元の書式のまま。
    assert after[:5] == before
    assert len(set(after[:5])) == 5  # 段落 0 の書式で塗り潰されていない


def test_shrinking_a_frame_drops_trailing_paragraphs_only(template: Path) -> None:
    shape = _shape(template, INVENTORY.axes.shape_id)
    apply_shape_text(shape, ["A", "B"])
    assert _paragraph_texts(shape) == ["A", "B"]
    assert _paragraph_sizes(shape) == [str(PARAGRAPH_SIZES[0]), str(PARAGRAPH_SIZES[1])]


def test_growing_a_frame_clones_the_last_paragraph_format(template: Path) -> None:
    cell = INVENTORY.cell(0)
    shape = _shape(template, cell.detail.shape_id)  # テンプレ 3 段落
    apply_shape_text(shape, ["1", "2", "3", "4", "5"])
    sizes = _paragraph_sizes(shape)
    assert _paragraph_texts(shape) == ["1", "2", "3", "4", "5"]
    assert sizes[3] == sizes[2] == str(PARAGRAPH_SIZES[2])


def test_extra_runs_in_a_paragraph_are_removed(template: Path) -> None:
    from lxml import etree

    shape = _shape(template, INVENTORY.cell(0).label.shape_id)
    paragraph = shape.text_frame._txBody.find(_qn("p"))
    extra = etree.SubElement(paragraph, _qn("r"))
    etree.SubElement(extra, _qn("t")).text = "残骸"
    apply_shape_text(shape, ["新しい見出し"])
    assert _paragraph_texts(shape) == ["新しい見出し"]
    assert len(paragraph.findall(_qn("r"))) == 1


def test_empty_paragraph_gets_a_run_that_keeps_the_format(template: Path) -> None:
    from lxml import etree

    shape = _shape(template, INVENTORY.cell(0).note.shape_id)
    body = shape.text_frame._txBody
    paragraph = body.findall(_qn("p"))[1]
    for run in paragraph.findall(_qn("r")):
        paragraph.remove(run)
    end_props = etree.SubElement(paragraph, _qn("endParaRPr"))
    end_props.set("sz", "3300")
    apply_shape_text(shape, ["a", "b", "c", "d", "e"])
    assert _paragraph_texts(shape)[1] == "b"
    assert _paragraph_sizes(shape)[1] == "3300"
