"""テンプレ差し替え層（**一から作らない**）。

テンプレのパスを受け取り、台帳（``inventory.py``）で 10 セル＋訴求軸＋界隈ブロックの
差し替え対象を解決して埋める。PPTX を新規生成することはしない。

なぜ ``_replace_placeholders`` を使わないか（計画 §2-2 renderer 新関数）:
既存 ``media/operations.py`` の ``_replace_placeholders`` は placeholder が 1 つ当たると
``paragraphs[0].text = replaced`` で **全段落を段落 0 へ潰す**。訴求軸（17 段落）・
切り抜きメモ（5 段落）・界隈詳細（3 段落）はそれで必ず壊れる。ここでは段落ごとに
**先頭 run の text だけ** を差し替え、余剰 run を消し、段落数の増減は
``<a:p>`` の deepcopy / 末尾削除で行う（run 書式 sz / フォント / ``bodyPr`` が残る）。

python-pptx は media extra にしか無いので **遅延 import** する（mcp イメージを汚さない）。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from teamagent.skills.clip_proposal.analysis import (
    CELL_COUNT,
    ClipProposalAnalysis,
)
from teamagent.skills.clip_proposal.inventory import (
    CLIP_TEMPLATE_INVENTORY_V1,
    OUTPUT_DOC_AUTHOR,
    OUTPUT_DOC_DESCRIPTION,
    PROPOSAL_SLIDE_INDEX,
    ImageSlotSpec,
    TemplateInventory,
    TextFrameSpec,
)

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC = "http://purl.org/dc/elements/1.1/"
_THUMBNAIL_REL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"

#: 台帳が要求する shape が欠けている / 段落数や枠寸法が違う（＝テンプレ改竄）。
#: 出力側（V6）の消毒漏れ検知も同じコードで返す（利用者へ出すのはこのマーカーだけ）。
TEMPLATE_INVALID = "MEDIA_CLIP_TEMPLATE_INVALID"


class ClipTemplateInvalidError(RuntimeError):
    """テンプレが台帳と一致しない。**出力ファイルを 1 バイトも作らずに**上げる。"""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{TEMPLATE_INVALID}: {detail}")
        self.code = TEMPLATE_INVALID
        self.detail = detail


@dataclass(frozen=True)
class TextOp:
    """1 枠への差し替え指示。``paragraphs`` の長さはテンプレ段落数と独立でよい。"""

    shape_id: int
    role: str
    paragraphs: tuple[str, ...]


@dataclass(frozen=True)
class ImageOp:
    """1 枠への画像差し込み指示（v1 は代表コマの静止画）。"""

    shape_id: int
    role: str
    kind: str
    fit: str = "cover"


@dataclass(frozen=True)
class FillPlan:
    """テンプレへ流し込む全量。PPTX に触れずに組めるので単体テストで固定できる。"""

    template_profile: str
    text_ops: tuple[TextOp, ...]
    image_slots: tuple[ImageOp, ...]
    filled_cells: int
    empty_cells: tuple[int, ...]


def _qn(tag: str) -> str:
    return f"{{{_A}}}{tag.split(':', 1)[1]}"


def truncate_for_frame(text: str, spec: TextFrameSpec) -> str:
    """枠の実寸から決めた字数上限で、**自然な位置**（句読点・助詞境界）で切り詰める。

    autofit は全枠 none（自動縮小が効かない）ため、溢れたら枠外へ流れて読めなくなる。
    切り詰めたことは呼び出し側が利用者へ明示する（黙って削らない）。
    """

    limit = spec.max_chars
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    for boundary in ("。", "、", "！", "？", "・", "／", " "):
        position = head.rfind(boundary)
        if position >= limit // 2:
            return head[: position + 1].rstrip()
    return head


def _axis_paragraphs(analysis: ClipProposalAnalysis, spec: TextFrameSpec) -> tuple[str, ...]:
    """訴求軸枠の段落（見出し 2 行 ＋ 空行 ＋ 「N、本文」× 実数）。

    テンプレは 17 段落だが、抽出できた軸が 5 本に満たない回もある。段落数を保つことを
    不変条件にすると作りたい成果物が落ちるので、**実数ぶんだけ** 並べる。
    """

    lines: list[str] = ["本編での訴求軸＝強み", "（切り抜く文脈）", ""]
    for axis in analysis.axes:
        lines.append(f"{axis.index}、{truncate_for_frame(axis.text, spec)}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) <= 3:
        lines.append("（本編から確認できた訴求軸がありませんでした）")
    return tuple(lines)


def build_fill_plan(
    analysis: ClipProposalAnalysis,
    *,
    inventory: TemplateInventory = CLIP_TEMPLATE_INVENTORY_V1,
) -> FillPlan:
    """解析結果 → テンプレ差し替え指示（PPTX に触らない純関数）。

    採用できなかったセルは **枠ごと空文字** にする。テンプレの指示文
    （『ここは切り抜きに使用する箇所のみを…』等）を残すと、そのまま得意先へ出る。
    """

    text_ops: list[TextOp] = [
        TextOp(
            shape_id=inventory.axes.shape_id,
            role=inventory.axes.role,
            paragraphs=_axis_paragraphs(analysis, inventory.axes),
        )
    ]
    image_slots: list[ImageOp] = [
        ImageOp(
            shape_id=inventory.main_video_slot.shape_id,
            role=inventory.main_video_slot.role,
            kind=inventory.main_video_slot.kind,
            fit="contain",
        )
    ]

    by_cell = {clip.cell_index: clip for clip in analysis.clips}
    empty: list[int] = []
    for index in range(CELL_COUNT):
        cell = inventory.cell(index)
        clip = by_cell.get(index)
        if clip is None:
            empty.append(index)
            for spec in cell.text_frames():
                text_ops.append(TextOp(shape_id=spec.shape_id, role=spec.role, paragraphs=("",)))
            continue
        text_ops.extend(
            (
                TextOp(
                    shape_id=cell.band_top.shape_id,
                    role=cell.band_top.role,
                    paragraphs=(truncate_for_frame(clip.band_top, cell.band_top),),
                ),
                TextOp(
                    shape_id=cell.band_bottom.shape_id,
                    role=cell.band_bottom.role,
                    paragraphs=(truncate_for_frame(clip.band_bottom, cell.band_bottom),),
                ),
                TextOp(
                    shape_id=cell.search_word.shape_id,
                    role=cell.search_word.role,
                    paragraphs=(truncate_for_frame(clip.search_word, cell.search_word),),
                ),
                TextOp(
                    shape_id=cell.label.shape_id,
                    role=cell.label.role,
                    paragraphs=(truncate_for_frame(clip.label, cell.label),),
                ),
                TextOp(
                    shape_id=cell.detail.shape_id,
                    role=cell.detail.role,
                    paragraphs=tuple(
                        truncate_for_frame(line, cell.detail) for line in clip.detail_lines
                    ),
                ),
                TextOp(
                    shape_id=cell.note.shape_id,
                    role=cell.note.role,
                    paragraphs=tuple(
                        truncate_for_frame(line, cell.note) for line in clip.render_note_lines()
                    ),
                ),
                TextOp(
                    shape_id=cell.hook_copy.shape_id,
                    role=cell.hook_copy.role,
                    paragraphs=(truncate_for_frame(clip.hook_copy, cell.hook_copy),),
                ),
            )
        )
        image_slots.extend(
            (
                ImageOp(
                    shape_id=cell.hook_slot.shape_id,
                    role=cell.hook_slot.role,
                    kind=cell.hook_slot.kind,
                ),
                ImageOp(
                    shape_id=cell.clip_slot.shape_id,
                    role=cell.clip_slot.role,
                    kind=cell.clip_slot.kind,
                ),
            )
        )
    return FillPlan(
        template_profile=inventory.version,
        text_ops=tuple(text_ops),
        image_slots=tuple(image_slots),
        filled_cells=CELL_COUNT - len(empty),
        empty_cells=tuple(empty),
    )


# ---------------------------------------------------------------------------
# PPTX 側（遅延 import・media extra 前提）
# ---------------------------------------------------------------------------


def _shapes_by_id(slide: Any) -> dict[int, Any]:
    return {int(shape.shape_id): shape for shape in slide.shapes}


def _paragraph_elements(shape: Any) -> list[Any]:
    return list(shape.text_frame._txBody.findall(_qn("a:p")))


def _body_pr_vert(shape: Any) -> str:
    body_pr = shape.text_frame._txBody.find(_qn("a:bodyPr"))
    if body_pr is None:
        return ""
    return str(body_pr.get("vert") or "")


def validate_template(
    template_path: str,
    *,
    inventory: TemplateInventory = CLIP_TEMPLATE_INVENTORY_V1,
) -> None:
    """差し替え **前** に台帳と機械照合する（fail-closed）。

    照合するのは (1) shape の実在 (2) 枠 EMU の一致 (3) テンプレ側の段落数
    (4) ``bodyPr`` の ``vert``。1 つでも外れたら ``ClipTemplateInvalidError``。
    """

    from pptx import Presentation

    presentation = Presentation(template_path)
    try:
        slide = presentation.slides[PROPOSAL_SLIDE_INDEX]
    except IndexError as exc:
        raise ClipTemplateInvalidError("proposal slide missing") from exc
    shapes = _shapes_by_id(slide)

    for spec in inventory.text_frames():
        shape = shapes.get(spec.shape_id)
        if shape is None:
            raise ClipTemplateInvalidError(f"missing shape {spec.shape_id} ({spec.role})")
        if not shape.has_text_frame:
            raise ClipTemplateInvalidError(f"shape {spec.shape_id} has no text frame ({spec.role})")
        actual_paragraphs = len(_paragraph_elements(shape))
        if actual_paragraphs != spec.template_paragraphs:
            raise ClipTemplateInvalidError(
                f"shape {spec.shape_id} paragraphs {actual_paragraphs}"
                f" != {spec.template_paragraphs} ({spec.role})"
            )
        if (int(shape.width), int(shape.height)) != (spec.width_emu, spec.height_emu):
            raise ClipTemplateInvalidError(
                f"shape {spec.shape_id} frame {shape.width}x{shape.height}"
                f" != {spec.width_emu}x{spec.height_emu} ({spec.role})"
            )
        if _body_pr_vert(shape) != spec.vert:
            raise ClipTemplateInvalidError(
                f"shape {spec.shape_id} vert {_body_pr_vert(shape)!r}"
                f" != {spec.vert!r} ({spec.role})"
            )

    for slot in inventory.image_slots():
        shape = shapes.get(slot.shape_id)
        if shape is None:
            raise ClipTemplateInvalidError(f"missing image slot {slot.shape_id} ({slot.role})")
        if (int(shape.width), int(shape.height)) != (slot.width_emu, slot.height_emu):
            raise ClipTemplateInvalidError(
                f"image slot {slot.shape_id} frame {shape.width}x{shape.height}"
                f" != {slot.width_emu}x{slot.height_emu} ({slot.role})"
            )


def _blank_run() -> Any:
    """空の ``<a:r><a:t/></a:r>``。lxml を直接触らず python-pptx の oxml で組む。"""

    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls

    return parse_xml(f"<a:r {nsdecls('a')}><a:t></a:t></a:r>")


def _new_run_like(paragraph: Any) -> Any:
    """段落に run が 1 つも無いとき、書式を保ったまま run を 1 つ作る。

    空段落は ``a:endParaRPr`` に書式を持っている。それを ``a:rPr`` として写すことで、
    sz / フォントを保ったままテキストを入れられる（素で run を足すと既定書式に落ちる）。
    """

    run = _blank_run()
    end_props = paragraph.find(_qn("a:endParaRPr"))
    if end_props is None:
        paragraph.append(run)
        return run
    run_props = copy.deepcopy(end_props)
    run_props.tag = _qn("a:rPr")
    run.insert(0, run_props)
    # a:endParaRPr は段落の末尾に来る決まりなので、run はその前へ挿す。
    paragraph.insert(list(paragraph).index(end_props), run)
    return run


def _set_paragraph_text(paragraph: Any, text: str) -> None:
    """段落の **先頭 run の text だけ** を差し替え、余剰 run と改行を削る。"""

    runs = paragraph.findall(_qn("a:r"))
    for line_break in paragraph.findall(_qn("a:br")):
        paragraph.remove(line_break)
    if not runs:
        runs = [_new_run_like(paragraph)]
    first, *extra = runs
    text_element = first.find(_qn("a:t"))
    if text_element is None:
        text_element = _blank_run().find(_qn("a:t"))
        first.append(text_element)
    text_element.text = text
    for run in extra:
        paragraph.remove(run)


def apply_shape_text(shape: Any, paragraphs: Sequence[str]) -> None:
    """枠 1 つへ段落列を流し込む（段落書式・``bodyPr`` を壊さない）。

    段落が足りなければ **直前の ``<a:p>`` を deepcopy** して挿入し、余剰なら末尾から
    削る。``_replace_placeholders`` のように全段落を段落 0 へ潰さない。
    """

    body = shape.text_frame._txBody
    existing = list(body.findall(_qn("a:p")))
    if not existing:
        raise ClipTemplateInvalidError(f"shape {shape.shape_id} has no paragraph")
    wanted = list(paragraphs) or [""]

    while len(existing) < len(wanted):
        clone = copy.deepcopy(existing[-1])
        body.insert(list(body).index(existing[-1]) + 1, clone)
        existing.append(clone)
    while len(existing) > len(wanted):
        body.remove(existing.pop())

    for paragraph, text in zip(existing, wanted, strict=True):
        _set_paragraph_text(paragraph, text)


def _referenced_media_sha256(path: str, *, inventory: TemplateInventory) -> set[str]:
    """提案スライドで **台帳の画像スロットが実際に参照している** 画像の SHA256。

    「slide に居る picture」ではなく「台帳に載っている shape_id の picture」に限る。
    台帳に無い shape がぶら下げた画像は、消毒漏れとして落としたい対象そのもの。
    """

    import hashlib

    from pptx import Presentation

    allowed_ids = {slot.shape_id for slot in inventory.image_slots()}
    presentation = Presentation(path)
    referenced: set[str] = set()
    for shape in presentation.slides[PROPOSAL_SLIDE_INDEX].shapes:
        if int(shape.shape_id) not in allowed_ids:
            continue
        image = getattr(shape, "image", None)
        if image is None:
            continue
        referenced.add(hashlib.sha256(image.blob).hexdigest())
    return referenced


def _clean_core_properties(raw: bytes) -> bytes:
    """``docProps/core.xml`` の人名欄を固定値へ潰す（実在社員名を出力へ残さない）。

    lxml ではなく stdlib の ElementTree を使う（``lxml`` は型スタブが無く strict mypy を
    通らない上、この 3 要素の書き換えに追加依存を増やす理由が無い）。OOXML の標準
    prefix を登録してから直列化するので、PowerPoint が読む形は変わらない。
    """

    import xml.etree.ElementTree as ET

    for prefix, uri in (
        ("cp", _CP),
        ("dc", _DC),
        ("dcterms", "http://purl.org/dc/terms/"),
        ("dcmitype", "http://purl.org/dc/dcmitype/"),
        ("xsi", "http://www.w3.org/2001/XMLSchema-instance"),
    ):
        ET.register_namespace(prefix, uri)

    root = ET.fromstring(raw)
    fixed = {
        f"{{{_DC}}}creator": OUTPUT_DOC_AUTHOR,
        f"{{{_CP}}}lastModifiedBy": OUTPUT_DOC_AUTHOR,
        f"{{{_DC}}}description": OUTPUT_DOC_DESCRIPTION,
    }
    for tag, value in fixed.items():
        node = root.find(tag)
        if node is None:
            node = ET.SubElement(root, tag)
        node.text = value
    cleaned: bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return cleaned


def _drop_thumbnail_refs(raw: bytes) -> bytes:
    """``docProps/thumbnail.*`` を指す 1 要素を丸ごと落とす。

    ``[Content_Types].xml`` の ``<Override>`` と ``_rels/.rels`` の ``<Relationship>``
    はどちらも属性だけを持つ自己完結要素なので、要素単位で削る。XML を組み直さない
    ので、**他の part の参照はバイト単位で無傷**のまま残る。
    """

    pattern = re.compile(rb"<(?:Override|Relationship)\b[^>]*?/>")
    needles = (b"docProps/thumbnail", _THUMBNAIL_REL.encode())
    return pattern.sub(
        lambda match: (
            b"" if any(needle in match.group(0) for needle in needles) else match.group(0)
        ),
        raw,
    )


def sanitize_output(
    output_path: str,
    *,
    inventory: TemplateInventory = CLIP_TEMPLATE_INVENTORY_V1,
    inserted_media_sha256: frozenset[str] = frozenset(),
) -> str:
    """V6: **配達前に出力 PPTX を開き直して**消毒漏れを機械照合する。

    ``validate_template`` は入力テンプレの shape / EMU / 段落数 / vert しか見ない。
    docProps に残った実在社員名も、shape を消しただけで part が残った第三者画像
    （孤児 media part）も、そこは通り抜ける。消毒漏れのテンプレを
    ``CLIP_TEMPLATE_PATH`` に置いた瞬間、実在社員名と第三者の画像を含む PPTX が
    得意先へ出てしまうので、**出力側でもう一度** 見る。

    (1) ``docProps/core.xml`` の ``dc:creator`` / ``cp:lastModifiedBy`` /
        ``dc:description`` を固定値へ上書きし、``docProps/thumbnail.*`` を
        ``[Content_Types].xml`` と ``_rels/.rels`` の参照ごと物理削除する。
    (2) ``ppt/media/*`` を列挙し、台帳の allowlist ＋ 今回差し込んだ分 ＋
        台帳スロットが実際に参照している画像のどれでもない媒体が 1 つでもあれば、
        **出力を消してから** ``MEDIA_CLIP_TEMPLATE_INVALID`` を上げる。
    """

    import hashlib
    import os
    import zipfile

    staging = f"{output_path}.sanitized"
    orphan = ""
    try:
        # 出力を開き直す段でこけても、配達可能な場所にファイルを残さない
        # （この計算を try の外へ出すと、開けない PPTX が残って次段へ流れる）。
        allowed = set(inventory.allowed_media_sha256) | set(inserted_media_sha256)
        if not inventory.allowed_media_sha256:
            # v1（テンプレ資産が repo に無く allowlist が空）の暫定則。
            # 「台帳の画像スロットが実際に参照している画像」だけを許す。
            # ⚠ これは *正規スロットに第三者の画像が入っていても通る* ことを意味する
            # （寸法さえ合えば validate_template も通る）。台帳へ SHA256 を列挙した
            # 時点でこの暫定則は自動的に外れ、allowlist だけが唯一の根拠になる。
            allowed |= _referenced_media_sha256(output_path, inventory=inventory)

        with zipfile.ZipFile(output_path) as source:
            names = source.namelist()
            with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as target:
                for name in names:
                    if name.startswith("docProps/thumbnail"):
                        continue
                    raw = source.read(name)
                    if name == "docProps/core.xml":
                        raw = _clean_core_properties(raw)
                    elif name in ("[Content_Types].xml", "_rels/.rels"):
                        raw = _drop_thumbnail_refs(raw)
                    elif name.startswith("ppt/media/"):
                        digest = hashlib.sha256(raw).hexdigest()
                        if digest not in allowed:
                            orphan = orphan or f"{name} sha256={digest[:12]}"
                    target.writestr(source.getinfo(name), raw)
        if orphan:
            raise ClipTemplateInvalidError(f"unexpected media part {orphan}")
        os.replace(staging, output_path)
    except BaseException:
        # 破棄しきる。消毒に失敗した PPTX を配達可能な場所へ残さない。
        for path in (staging, output_path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    return output_path


def apply_fill_plan(
    template_path: str,
    plan: FillPlan,
    output_path: str,
    *,
    inventory: TemplateInventory = CLIP_TEMPLATE_INVENTORY_V1,
    inserted_media_sha256: frozenset[str] = frozenset(),
) -> str:
    """テンプレを開き、``plan`` の差し替えを適用して ``output_path`` へ保存する。

    ``validate_template`` を先に通す。台帳と合わなければ **1 バイトも書かない**。
    保存後は ``sanitize_output``（V6）で開き直し、docProps の人名と孤児 media part を
    落とす／検出する。画像スロットの差し込み（静止画）は本 PR の範囲外で、
    ``plan.image_slots`` の解決結果だけを返す（実差し込みは消毒済みテンプレ到着後の便）。
    """

    validate_template(template_path, inventory=inventory)

    from pptx import Presentation

    presentation = Presentation(template_path)
    slide = presentation.slides[PROPOSAL_SLIDE_INDEX]
    shapes = _shapes_by_id(slide)
    for op in plan.text_ops:
        shape = shapes.get(op.shape_id)
        if shape is None:  # validate_template を通っていれば起きない
            raise ClipTemplateInvalidError(f"missing shape {op.shape_id} ({op.role})")
        apply_shape_text(shape, op.paragraphs)
    presentation.save(output_path)
    return sanitize_output(
        output_path, inventory=inventory, inserted_media_sha256=inserted_media_sha256
    )


def resolve_image_slots(
    plan: FillPlan,
) -> dict[str, ImageSlotSpec]:
    """``image_slots`` を計画どおりのキー（``"<shape_id>:<rank>"``）へ解決する。

    render_child の composer JSON が要求する形（計画 §2-2「変更ファイル」）。
    """

    by_id = {slot.shape_id: slot for slot in CLIP_TEMPLATE_INVENTORY_V1.image_slots()}
    resolved: dict[str, ImageSlotSpec] = {}
    for op in plan.image_slots:
        spec = by_id.get(op.shape_id)
        if spec is None:
            raise ClipTemplateInvalidError(f"unknown image slot {op.shape_id} ({op.role})")
        resolved[f"{op.shape_id}:1"] = spec
    return resolved


__all__ = [
    "TEMPLATE_INVALID",
    "ClipTemplateInvalidError",
    "FillPlan",
    "ImageOp",
    "TextOp",
    "apply_fill_plan",
    "apply_shape_text",
    "build_fill_plan",
    "resolve_image_slots",
    "sanitize_output",
    "truncate_for_frame",
    "validate_template",
]
