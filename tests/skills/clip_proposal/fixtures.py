"""clip_proposal テストの共通フィクスチャ。

**テンプレ実物は repo に置かない**（他社名・第三者の顔写真・社員実名を含む）。代わりに
台帳 ``CLIP_TEMPLATE_INVENTORY_V1`` から**同じ構造の合成テンプレ**を組み立てる。
台帳の数値は配布テンプレ実物と記入例（初田製作所）から実見して起こしたものなので、
「記入例 pptx の構造を期待値に使う」という要求はこの台帳経由で満たしている。

合成テンプレは段落ごとに異なる ``sz``（文字サイズ）を持たせてある。差し替え後も
段落ごとの書式が残ることを検査するため（``_replace_placeholders`` 相当の実装に
差し替えると全段落が段落 0 へ潰れて必ず赤くなる）。
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

from teamagent.skills.clip_proposal.analysis import (
    AppealAxis,
    ClipPlan,
    ClipProposalAnalysis,
    ClipWindow,
    Community,
    CommunityTerm,
    Insight,
)
from teamagent.skills.clip_proposal.inventory import (
    CLIP_TEMPLATE_INVENTORY_V1,
    ImageSlotSpec,
    TemplateInventory,
    TextFrameSpec,
)

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

#: 段落番号 → ``sz``。差し替え後も段落ごとに保たれることを検査する。
PARAGRAPH_SIZES = (500, 700, 750, 900, 1000, 1100, 1200)


def _qn(tag: str) -> str:
    return f"{{{_A}}}{tag}"


def make_png_bytes(width: int = 4, height: int = 4) -> bytes:
    """依存を足さずに作る最小 PNG（画像スロット用）。"""

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _force_shape_id(shape: Any, shape_id: int) -> None:
    element = shape._element
    for name in ("nvSpPr", "nvPicPr", "nvGrpSpPr"):
        holder = getattr(element, name, None)
        if holder is not None:
            holder.cNvPr.set("id", str(shape_id))
            holder.cNvPr.set("name", f"shape-{shape_id}")
            return
    raise AssertionError(f"cannot set shape id on {shape}")


def _add_text_frame(slide: Any, spec: TextFrameSpec) -> Any:
    from lxml import etree
    from pptx.util import Emu

    box = slide.shapes.add_textbox(Emu(0), Emu(0), Emu(spec.width_emu), Emu(spec.height_emu))
    _force_shape_id(box, spec.shape_id)
    frame = box.text_frame
    body = frame._txBody

    if spec.vert:
        body.find(_qn("bodyPr")).set("vert", spec.vert)

    # 既定で 1 段落あるので、不足分を足す。段落ごとに sz を変えて書式保持を検査可能にする。
    while len(body.findall(_qn("p"))) < spec.template_paragraphs:
        frame.add_paragraph()
    for index, paragraph in enumerate(body.findall(_qn("p"))):
        run = etree.SubElement(paragraph, _qn("r"))
        props = etree.SubElement(run, _qn("rPr"))
        props.set("lang", "ja-JP")
        props.set("sz", str(PARAGRAPH_SIZES[index % len(PARAGRAPH_SIZES)]))
        text = etree.SubElement(run, _qn("t"))
        text.text = f"{spec.role}#{index}"
    return box


def _add_image_slot(slide: Any, spec: ImageSlotSpec) -> Any:
    import io

    from pptx.util import Emu

    if spec.kind == "picture":
        shape = slide.shapes.add_picture(
            io.BytesIO(make_png_bytes()),
            Emu(0),
            Emu(0),
            Emu(spec.width_emu),
            Emu(spec.height_emu),
        )
    else:
        shape = slide.shapes.add_textbox(Emu(0), Emu(0), Emu(spec.width_emu), Emu(spec.height_emu))
    _force_shape_id(shape, spec.shape_id)
    return shape


def build_synthetic_template(
    path: str,
    *,
    inventory: TemplateInventory = CLIP_TEMPLATE_INVENTORY_V1,
    omit_shape_id: int | None = None,
    paragraph_delta: dict[int, int] | None = None,
) -> str:
    """台帳と同じ構造の合成テンプレを作る。

    ``omit_shape_id`` で 1 枠だけ落とす / ``paragraph_delta`` で段落数をずらすことで、
    テンプレ改竄検知（fail-closed）のテストが書ける。
    """

    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    deltas = paragraph_delta or {}
    for spec in inventory.text_frames():
        if spec.shape_id == omit_shape_id:
            continue
        delta = deltas.get(spec.shape_id, 0)
        if delta:
            spec = TextFrameSpec(
                shape_id=spec.shape_id,
                role=spec.role,
                template_paragraphs=max(1, spec.template_paragraphs + delta),
                width_emu=spec.width_emu,
                height_emu=spec.height_emu,
                vert=spec.vert,
                max_chars=spec.max_chars,
            )
        _add_text_frame(slide, spec)
    for slot in inventory.image_slots():
        if slot.shape_id == omit_shape_id:
            continue
        _add_image_slot(slide, slot)
    presentation.save(path)
    return path


def sample_community(index: int) -> Community:
    return Community(
        name=f"界隈{index}",
        terms=(
            CommunityTerm(term=f"観測語{index}", verified=True),
            CommunityTerm(term=f"未確認語{index}", verified=False),
        ),
        scale_note=f"約{index}万人",
        description=f"界隈{index}の説明",
        is_primary=index == 1,
    )


def sample_insight(index: int) -> Insight:
    return Insight(
        target=f"ターゲット{index}",
        insight=f"本音{index}",
        evidence_hint=f"判断材料{index}",
    )


def sample_clip(cell_index: int) -> ClipPlan:
    kind = "community" if cell_index < 5 else "insight"
    if kind == "community":
        source = sample_community(cell_index + 1)
        label, detail = source.render_name(), source.render_detail_lines()
    else:
        insight = sample_insight(cell_index - 4)
        label, detail = insight.target, insight.render_detail_lines()
    return ClipPlan(
        cell_index=cell_index,
        kind=kind,  # type: ignore[arg-type]
        label=label,
        detail_lines=tuple(detail),
        window=ClipWindow(
            start_sec=float(cell_index * 5),
            end_sec=float(cell_index * 5 + 18),
            hook_start_sec=float(cell_index * 5),
            hook_end_sec=float(cell_index * 5 + 2),
        ),
        hook_copy=f"フック{cell_index}",
        band_top=f"上帯{cell_index}",
        band_bottom=f"下帯{cell_index}",
        search_word="🔍テスト商材 採用",
        quote_evidence=f"引用{cell_index}",
        takeaway=f"伝わること{cell_index}",
    )


def sample_analysis(
    *, clip_count: int = 10, client_name: str = "テスト商事"
) -> ClipProposalAnalysis:
    return ClipProposalAnalysis(
        client_name=client_name,
        axes=tuple(
            AppealAxis(index=i + 1, text=f"訴求軸{i + 1}", quote_evidence=f"引用{i + 1}")
            for i in range(5)
        ),
        clips=tuple(sample_clip(i) for i in range(clip_count)),
        cost_usd=0.12,
        gemini_calls=2,
    )


__all__ = [
    "PARAGRAPH_SIZES",
    "build_synthetic_template",
    "make_png_bytes",
    "sample_analysis",
    "sample_clip",
    "sample_community",
    "sample_insight",
]
