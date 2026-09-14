"""テンプレ差し替え層。**一から作らず**、台帳で解決した枠だけを埋める。

期待値の出どころは記入例から起こした台帳 ``CLIP_TEMPLATE_INVENTORY_V1``。
テンプレ実物は repo に置かないので、台帳から同じ構造の合成テンプレを組んで検査する。

本番の失敗モードを再現する:
- 訴求軸 17 段落 / 切り抜きメモ 5 段落 / 界隈詳細 3 段落 の枠へ、**段落数の違う**本文を流す
- テンプレの shape が 1 つ欠けている（消毒スクリプトの事故・別版の混入）
- テンプレの段落数が改竄されている
- 採用できなかったセルが残る（テンプレの指示文がそのまま得意先へ出る事故）
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from teamagent.skills.clip_proposal.analysis import CELL_COUNT
from teamagent.skills.clip_proposal.inventory import CLIP_TEMPLATE_INVENTORY_V1 as INVENTORY
from teamagent.skills.clip_proposal.inventory import PROPOSAL_SLIDE_INDEX
from teamagent.skills.clip_proposal.template_fill import (
    CORE_PROPERTY_EXPECTATIONS,
    ClipTemplateInvalidError,
    apply_fill_plan,
    apply_shape_text,
    build_fill_plan,
    resolve_image_slots,
    truncate_for_frame,
    validate_template,
)

from .fixtures import (
    PARAGRAPH_SIZES,
    build_synthetic_template,
    build_unsanitized_template,
    load_golden_dump,
    make_png_bytes,
    sample_analysis,
)

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


# ---------------------------------------------------------------------------
# 受入ダンプと台帳の突合（自己言及を断つ）
# ---------------------------------------------------------------------------


def test_validate_template_accepts_the_golden_dump() -> None:
    """合成テンプレは **台帳ではなく受入ダンプ** から組む。

    どちらか片方だけを書き換えると赤くなる。台帳から合成して台帳で検証すると、
    台帳が実在資産と食い違っていても受入テストは永久に緑になる。
    """

    dump = load_golden_dump()
    assert dump["profile"] == INVENTORY.version
    assert dump["slide_index"] == PROPOSAL_SLIDE_INDEX

    dumped = {
        (
            int(e["shape_id"]),
            e["role"],
            int(e["paragraphs"]),
            int(e["width_emu"]),
            int(e["height_emu"]),
            str(e.get("vert") or ""),
        )
        for e in dump["text_frames"]
    }
    expected = {
        (s.shape_id, s.role, s.template_paragraphs, s.width_emu, s.height_emu, s.vert)
        for s in INVENTORY.text_frames()
    }
    assert dumped == expected

    dumped_slots = {
        (int(e["shape_id"]), e["role"], e["kind"], int(e["width_emu"]), int(e["height_emu"]))
        for e in dump["image_slots"]
    }
    assert dumped_slots == {
        (s.shape_id, s.role, s.kind, s.width_emu, s.height_emu) for s in INVENTORY.image_slots()
    }


def test_image_slot_emu_comes_from_the_black_template_not_the_filled_example() -> None:
    """台帳 docstring の但し書きを固定する（記入例には画像枠の shape が無い）。

    帯は記入例サイズ・画像枠は黒版サイズという混成なので、両者が一致しないことを
    明示的に検査して「いつの間にか片方に寄せた」が起きたら気づけるようにする。
    """

    hook_slot = INVENTORY.cell(0).hook_slot
    band_top = INVENTORY.cell(0).band_top
    assert (hook_slot.width_emu, hook_slot.height_emu) == (560618, 909282)
    assert (band_top.width_emu, band_top.height_emu) == (1096804, 427936)
    assert hook_slot.aspect_ratio < 1.0  # 9:16 寄り（フック）
    assert INVENTORY.cell(0).clip_slot.aspect_ratio > 1.5  # 16:9（切り抜き）


# ---------------------------------------------------------------------------
# V6: 配達前に出力を開き直す（消毒漏れテンプレの検知）
# ---------------------------------------------------------------------------


def _docprops(path: Path) -> tuple[str, str, str]:
    presentation = _open(path)
    props = presentation.core_properties
    return (props.author or "", props.last_modified_by or "", props.comments or "")


def _media_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return [name for name in archive.namelist() if name.startswith("ppt/media/")]


def test_output_docprops_never_carry_a_real_employee_name(tmp_path: Path) -> None:
    """消毒漏れテンプレ（docProps に実在社員名）を食っても、出力に名前を残さない。"""

    template = tmp_path / "unsanitized.pptx"
    build_synthetic_template(str(template), author="高林 拓也")
    assert _docprops(template)[0] == "高林 拓也"

    output = tmp_path / "out.pptx"
    apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))

    author, last_modified_by, comments = _docprops(output)
    assert author == "Aico"
    assert last_modified_by == "Aico"
    assert comments == ""
    with zipfile.ZipFile(output) as archive:
        blob = b"".join(archive.read(name) for name in archive.namelist())
    assert "高林".encode() not in blob


def test_orphan_media_part_discards_the_output(tmp_path: Path) -> None:
    """shape を消しても part が残った第三者画像を検知して、出力ごと捨てる。

    validate_template は shape / EMU / 段落数 / vert しか見ないのでここを通り抜ける。
    """

    template = tmp_path / "orphan.pptx"
    build_unsanitized_template(str(template))
    # 消毒漏れの実体: 台帳スロットの数より media part が 1 つ多い。
    assert len(_media_names(template)) == len(_media_names_of_inventory_slots()) + 1

    output = tmp_path / "out.pptx"
    with pytest.raises(ClipTemplateInvalidError) as excinfo:
        apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))
    assert excinfo.value.code == "MEDIA_CLIP_TEMPLATE_INVALID"
    assert "unexpected media part" in excinfo.value.detail
    assert not output.exists()  # 配達可能な場所に残さない


def _media_names_of_inventory_slots() -> list[str]:
    """台帳の picture スロットが持つ画像の本数（合成テンプレでは 1 種類に重複排除される）。"""

    return ["ppt/media/image1.png"]


def test_sanitize_output_accepts_media_the_ledger_slots_reference(tmp_path: Path) -> None:
    """正規の画像（台帳スロットが参照している分）は落とさない。"""

    template = tmp_path / "clean.pptx"
    build_synthetic_template(str(template))
    output = tmp_path / "out.pptx"
    apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))
    assert output.exists()
    assert _media_names(output)  # 画像は残っている（全消しではない）


def test_sanitize_output_allows_media_inserted_this_run(tmp_path: Path) -> None:
    """今回差し込んだ分は SHA256 を渡せば通る（画像差し込みを足す便の受け口）。"""

    import hashlib

    orphan = make_png_bytes(8, 8)
    template = tmp_path / "with-extra.pptx"
    build_synthetic_template(str(template), orphan_media=orphan)
    output = tmp_path / "out.pptx"
    apply_fill_plan(
        str(template),
        build_fill_plan(sample_analysis()),
        str(output),
        inserted_media_sha256=frozenset({hashlib.sha256(orphan).hexdigest()}),
    )
    assert output.exists()


def test_thumbnail_part_is_removed_with_its_references(tmp_path: Path) -> None:
    template = tmp_path / "thumb.pptx"
    build_synthetic_template(str(template))
    _inject_thumbnail(template)
    with zipfile.ZipFile(template) as archive:
        assert "docProps/thumbnail.jpeg" in archive.namelist()
        assert b"thumbnail" in archive.read("_rels/.rels")
        assert b"thumbnail" in archive.read("[Content_Types].xml")

    output = tmp_path / "out.pptx"
    apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        rels = archive.read("_rels/.rels").decode("utf-8")
        content_types = archive.read("[Content_Types].xml").decode("utf-8")
    assert not any(name.startswith("docProps/thumbnail") for name in names)
    assert "thumbnail" not in rels
    assert "thumbnail" not in content_types


def _inject_thumbnail(path: Path) -> None:
    """``[Content_Types].xml`` 側の Override も足す（PowerPoint が実際に付ける形）。

    python-pptx の既定パッケージは ``docProps/thumbnail.jpeg`` と ``_rels/.rels`` の
    参照を最初から持っているが Override は持たない。3 経路すべてを消すことを
    固定したいので、足りない 1 本をここで補う。
    """

    import shutil

    staging = path.with_suffix(".staging.pptx")
    override = '<Override PartName="/docProps/thumbnail.jpeg" ContentType="image/jpeg"/>'
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for name in source.namelist():
            raw = source.read(name)
            if name == "[Content_Types].xml":
                raw = raw.replace(b"</Types>", override.encode() + b"</Types>")
            target.writestr(source.getinfo(name), raw)
    shutil.move(str(staging), str(path))


def test_a_broken_output_is_not_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消毒の途中でこけても、配達可能な場所に PPTX を残さない。"""

    import teamagent.skills.clip_proposal.template_fill as module

    def boom(path: str, *, inventory: object) -> set[str]:
        raise RuntimeError("cannot reopen")

    monkeypatch.setattr(module, "_referenced_media_sha256", boom)
    template = tmp_path / "clean.pptx"
    build_synthetic_template(str(template))
    output = tmp_path / "out.pptx"
    with pytest.raises(RuntimeError):
        apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))
    assert not output.exists()
    assert not (tmp_path / "out.pptx.sanitized").exists()


def test_a_populated_allowlist_stops_trusting_the_slot_reference(tmp_path: Path) -> None:
    """台帳へ SHA256 を列挙したら、暫定則（スロット参照は無条件で許す）が外れる。

    v1 の暫定則は「正規スロットに第三者の画像が入っていても通る」。消毒済みテンプレが
    届いて allowlist が埋まったら、allowlist だけが唯一の根拠にならないといけない。
    """

    import dataclasses

    strict = dataclasses.replace(INVENTORY, allowed_media_sha256=frozenset({"0" * 64}))
    template = tmp_path / "clean.pptx"
    build_synthetic_template(str(template))
    output = tmp_path / "out.pptx"
    with pytest.raises(ClipTemplateInvalidError) as excinfo:
        apply_fill_plan(
            str(template), build_fill_plan(sample_analysis()), str(output), inventory=strict
        )
    assert "unexpected media part" in excinfo.value.detail
    assert not output.exists()


def test_a_populated_allowlist_accepts_exactly_what_it_lists(tmp_path: Path) -> None:
    import dataclasses
    import hashlib
    import zipfile as zf

    template = tmp_path / "clean.pptx"
    build_synthetic_template(str(template))
    with zf.ZipFile(template) as archive:
        digests = {
            hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith("ppt/media/")
        }
    strict = dataclasses.replace(INVENTORY, allowed_media_sha256=frozenset(digests))
    output = tmp_path / "out.pptx"
    apply_fill_plan(
        str(template), build_fill_plan(sample_analysis()), str(output), inventory=strict
    )
    assert output.exists()


def test_docprops_are_verified_by_bytes_not_only_rewritten(tmp_path: Path) -> None:
    """消毒が効かないテンプレを黙って通さない（書いたあと必ずバイトで確かめる）。

    書き込みは python-pptx に任せる（信用できないテンプレ由来の XML を自前パーサへ
    食わせない＝bandit B314 / XXE を避ける）。効いたかどうかは別に確かめる。
    """

    import teamagent.skills.clip_proposal.template_fill as module

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(module, "scrub_core_properties", lambda presentation: None)
        template = tmp_path / "unsanitized.pptx"
        build_synthetic_template(str(template), author="高林 拓也")
        output = tmp_path / "out.pptx"
        with pytest.raises(ClipTemplateInvalidError) as excinfo:
            apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))
    finally:
        monkey.undo()
    assert "not scrubbed" in excinfo.value.detail
    assert not output.exists()


@pytest.mark.parametrize(
    ("local_name", "expected"),
    [(name, value) for name, value in CORE_PROPERTY_EXPECTATIONS],
)
def test_every_core_property_on_the_list_is_scrubbed(
    tmp_path: Path, local_name: str, expected: str
) -> None:
    """creator / lastModifiedBy / description / subject / keywords / category を全部見る。"""

    import zipfile as zf

    from teamagent.skills.clip_proposal.template_fill import _core_property_text

    template = tmp_path / "named.pptx"
    build_synthetic_template(str(template), author="高林 拓也")
    output = tmp_path / "out.pptx"
    apply_fill_plan(str(template), build_fill_plan(sample_analysis()), str(output))
    with zf.ZipFile(output) as archive:
        raw = archive.read("docProps/core.xml")
    assert _core_property_text(raw, local_name) == expected
