"""テンプレ台帳 v1（(slide, shape_id) → 役割 / 期待段落数 / 枠EMU / vert）。

**この台帳は消毒済みテンプレ資産の受入契約**である（計画 §2-2「テンプレ資産の消毒」5）。
消毒済みテンプレ本体はこの PR に同梱しない（他社名・第三者の顔写真・社員実名を含む
生テンプレを repo へ入れない・置き場は §4 論点 5 の裁定待ち）。台帳だけを先に確定し、
テンプレが届いたら ``validate_template`` が機械照合する。

数値の出どころ（実見・2026-09-11）:
- **shape_id と段落数**は配布された黒版テンプレ実物から読んだ値。
- **テキスト枠の EMU と ``vert``** は記入例から読んだ値。黒版テンプレは
  縦書きフック枠に ``vert`` 属性が無く（横書き・指示文のみ）、帯とフック枠も
  記入例側で手動リサイズされている。計画 §2-2 消毒 4 の裁定どおり **v1 は記入例の
  寸法を焼き込む**（autofit は全枠 none ＝自動縮小が効かないため、字数上限は
  この実寸から計算する）。
- **画像スロットの EMU（``_HOOK_SLOT_EMU`` / ``_CLIP_SLOT_EMU`` / ``_MAIN_SLOT_EMU``）は
  黒版テンプレ由来**。記入例では該当枠が実 MP4 に置き換わっており shape 自体が存在しない
  （実見で MISSING を確認）ため、記入例からは読めない。**帯は記入例サイズ・画像枠は黒版
  サイズという混成**になっているので、消毒済みテンプレが届いたら「帯を広げた状態で画像枠が
  レイアウト上成立するか」を目視で確認すること（計画 §4 論点 6 の確認リスト）。

配達前の出力側照合（``template_fill.sanitize_output`` / V6）が使う媒体 allowlist:
- ``ALLOWED_MEDIA_SHA256`` は **消毒済みテンプレが正規に持ってよい媒体の SHA256**。
  テンプレ資産がまだ repo に無いので v1 は空＝「提案スライドの台帳スロットが実際に
  参照している画像」以外の媒体を 1 つでも見つけたら出力を破棄する（fail-closed）。
  消毒済みテンプレが届いたら、その媒体の SHA256 をここへ列挙する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PROPOSAL_SLIDE_INDEX = 0  # 提案 1 枚目（10 セル＋訴求軸＋界隈ブロック）

#: 消毒済みテンプレが正規に持ってよい媒体の SHA256（hex・小文字）。
#: v1 は空。テンプレ資産の受入時にここへ列挙する（docstring の V6 節を参照）。
ALLOWED_MEDIA_SHA256: frozenset[str] = frozenset()

#: 出力 PPTX の docProps へ焼き込む固定値（実在社員名を残さない）。
OUTPUT_DOC_AUTHOR = "Aico"
OUTPUT_DOC_DESCRIPTION = ""

CellKind = Literal["community", "insight"]


@dataclass(frozen=True)
class TextFrameSpec:
    """テキストを持つ枠 1 つの受入契約。"""

    shape_id: int
    role: str
    #: テンプレ側の段落数（＝テンプレ改竄検知）。composer が渡す段落数はこれと独立。
    template_paragraphs: int
    width_emu: int
    height_emu: int
    #: ``bodyPr`` の ``vert``（縦書きは ``eaVert``・横書きは空文字）。
    vert: str = ""
    #: この枠に入る最大文字数（v1 実寸から算出・超過分は自然な位置で切り詰める）。
    max_chars: int = 0


@dataclass(frozen=True)
class ImageSlotSpec:
    """画像を差し込む枠 1 つ。``picture`` は既に PICTURE、``shape`` は AUTO_SHAPE。"""

    shape_id: int
    role: str
    kind: Literal["picture", "shape"]
    width_emu: int
    height_emu: int

    @property
    def aspect_ratio(self) -> float:
        return round(self.width_emu / self.height_emu, 4)


@dataclass(frozen=True)
class CellSpec:
    """提案スライドの 1 セル（モック＋帯＋検索ワード＋見出し＋詳細＋メモ）。"""

    index: int
    kind: CellKind
    band_top: TextFrameSpec
    band_bottom: TextFrameSpec
    search_word: TextFrameSpec
    label: TextFrameSpec
    detail: TextFrameSpec
    note: TextFrameSpec
    hook_copy: TextFrameSpec
    hook_slot: ImageSlotSpec
    clip_slot: ImageSlotSpec

    def text_frames(self) -> tuple[TextFrameSpec, ...]:
        return (
            self.band_top,
            self.band_bottom,
            self.search_word,
            self.label,
            self.detail,
            self.note,
            self.hook_copy,
        )


@dataclass(frozen=True)
class TemplateInventory:
    """提案スライド 1 枚ぶんの台帳。"""

    version: str
    axes: TextFrameSpec
    main_video_slot: ImageSlotSpec
    cells: tuple[CellSpec, ...]
    #: 消毒済みテンプレが正規に持ってよい媒体の SHA256（v1 は空 ＝ 台帳スロット参照分のみ許す）。
    allowed_media_sha256: frozenset[str] = ALLOWED_MEDIA_SHA256

    def text_frames(self) -> tuple[TextFrameSpec, ...]:
        frames: list[TextFrameSpec] = [self.axes]
        for cell in self.cells:
            frames.extend(cell.text_frames())
        return tuple(frames)

    def image_slots(self) -> tuple[ImageSlotSpec, ...]:
        slots: list[ImageSlotSpec] = [self.main_video_slot]
        for cell in self.cells:
            slots.extend((cell.hook_slot, cell.clip_slot))
        return tuple(slots)

    def cell(self, index: int) -> CellSpec:
        return self.cells[index]


# --- 実測値（黒版テンプレ: shape_id / 段落数、記入例: EMU / vert） ------------------

_COMMUNITY_IDS = {
    "band_top": (102, 108, 114, 120, 126),
    "band_bottom": (104, 110, 116, 122, 128),
    "search_word": (106, 112, 118, 124, 130),
    "label": (146, 152, 156, 160, 164),
    "detail": (148, 154, 158, 162, 166),
    "note": (5, 9, 13, 17, 21),
    "hook_copy": (132, 134, 136, 138, 140),
    "hook_slot": (4, 43, 48, 53, 56),
    "clip_slot": (10, 12, 14, 16, 18),
}

_INSIGHT_IDS = {
    "band_top": (182, 185, 188, 191, 194),
    "band_bottom": (183, 186, 189, 192, 195),
    "search_word": (184, 187, 190, 193, 196),
    "label": (202, 213, 217, 221, 227),
    "detail": (203, 215, 219, 223, 229),
    "note": (25, 30, 34, 38, 42),
    "hook_copy": (197, 198, 199, 200, 201),
    "hook_slot": (58, 60, 62, 64, 66),
    "clip_slot": (20, 22, 24, 26, 28),
}

# 記入例から読んだ v1 実寸（EMU）。上段（界隈）と下段（インサイト）で帯の高さが違う。
_BAND_TOP_EMU = {"community": (1096804, 427936), "insight": (1100406, 452832)}
_BAND_BOTTOM_EMU = {"community": (1022010, 348942), "insight": (1022010, 395682)}
_SEARCH_EMU = (1022010, 331128)
_LABEL_EMU = (1744258, 164179)
_DETAIL_EMU = (1744258, 609162)
_NOTE_EMU = (596983, 870708)
_HOOK_COPY_EMU = (376164, 933497)
_HOOK_SLOT_EMU = (560618, 909282)
_CLIP_SLOT_EMU = (1008454, 569443)
_AXES_EMU = (2071282, 4960820)
_MAIN_SLOT_EMU = (2071283, 1169590)

# 黒版テンプレ側の段落数（テンプレ改竄検知の期待値）。
_TEMPLATE_PARAGRAPHS = {
    "band_top": 1,
    "band_bottom": 1,
    "search_word": 1,
    "label": 1,
    "detail_community": 3,
    "detail_insight": 1,
    "note": 5,
    "hook_copy": 1,
    "axes": 17,
}

# v1 実寸から計算した字数上限（ざっくり: 枠幅 ÷ 1 文字の実幅・改行 2 行想定）。
_MAX_CHARS = {
    "band_top": 22,
    "band_bottom": 24,
    "search_word": 16,
    "label": 16,
    "detail_line": 28,
    "note_line": 24,
    "hook_copy": 12,
    "axes_line": 30,
}


def _cells(kind: CellKind) -> tuple[CellSpec, ...]:
    ids = _COMMUNITY_IDS if kind == "community" else _INSIGHT_IDS
    detail_paragraphs = _TEMPLATE_PARAGRAPHS[
        "detail_community" if kind == "community" else "detail_insight"
    ]
    cells: list[CellSpec] = []
    for index in range(5):
        cells.append(
            CellSpec(
                index=index if kind == "community" else index + 5,
                kind=kind,
                band_top=TextFrameSpec(
                    shape_id=ids["band_top"][index],
                    role=f"{kind}.band_top",
                    template_paragraphs=_TEMPLATE_PARAGRAPHS["band_top"],
                    width_emu=_BAND_TOP_EMU[kind][0],
                    height_emu=_BAND_TOP_EMU[kind][1],
                    max_chars=_MAX_CHARS["band_top"],
                ),
                band_bottom=TextFrameSpec(
                    shape_id=ids["band_bottom"][index],
                    role=f"{kind}.band_bottom",
                    template_paragraphs=_TEMPLATE_PARAGRAPHS["band_bottom"],
                    width_emu=_BAND_BOTTOM_EMU[kind][0],
                    height_emu=_BAND_BOTTOM_EMU[kind][1],
                    max_chars=_MAX_CHARS["band_bottom"],
                ),
                search_word=TextFrameSpec(
                    shape_id=ids["search_word"][index],
                    role=f"{kind}.search_word",
                    template_paragraphs=_TEMPLATE_PARAGRAPHS["search_word"],
                    width_emu=_SEARCH_EMU[0],
                    height_emu=_SEARCH_EMU[1],
                    max_chars=_MAX_CHARS["search_word"],
                ),
                label=TextFrameSpec(
                    shape_id=ids["label"][index],
                    role=f"{kind}.label",
                    template_paragraphs=_TEMPLATE_PARAGRAPHS["label"],
                    width_emu=_LABEL_EMU[0],
                    height_emu=_LABEL_EMU[1],
                    max_chars=_MAX_CHARS["label"],
                ),
                detail=TextFrameSpec(
                    shape_id=ids["detail"][index],
                    role=f"{kind}.detail",
                    template_paragraphs=detail_paragraphs,
                    width_emu=_DETAIL_EMU[0],
                    height_emu=_DETAIL_EMU[1],
                    max_chars=_MAX_CHARS["detail_line"],
                ),
                note=TextFrameSpec(
                    shape_id=ids["note"][index],
                    role=f"{kind}.note",
                    template_paragraphs=_TEMPLATE_PARAGRAPHS["note"],
                    width_emu=_NOTE_EMU[0],
                    height_emu=_NOTE_EMU[1],
                    max_chars=_MAX_CHARS["note_line"],
                ),
                hook_copy=TextFrameSpec(
                    shape_id=ids["hook_copy"][index],
                    role=f"{kind}.hook_copy",
                    template_paragraphs=_TEMPLATE_PARAGRAPHS["hook_copy"],
                    width_emu=_HOOK_COPY_EMU[0],
                    height_emu=_HOOK_COPY_EMU[1],
                    vert="eaVert",
                    max_chars=_MAX_CHARS["hook_copy"],
                ),
                hook_slot=ImageSlotSpec(
                    shape_id=ids["hook_slot"][index],
                    role=f"{kind}.hook_slot",
                    kind="shape",
                    width_emu=_HOOK_SLOT_EMU[0],
                    height_emu=_HOOK_SLOT_EMU[1],
                ),
                clip_slot=ImageSlotSpec(
                    shape_id=ids["clip_slot"][index],
                    role=f"{kind}.clip_slot",
                    kind="picture",
                    width_emu=_CLIP_SLOT_EMU[0],
                    height_emu=_CLIP_SLOT_EMU[1],
                ),
            )
        )
    return tuple(cells)


CLIP_TEMPLATE_INVENTORY_V1 = TemplateInventory(
    version="clip-proposal-v1",
    axes=TextFrameSpec(
        shape_id=142,
        role="axes",
        template_paragraphs=_TEMPLATE_PARAGRAPHS["axes"],
        width_emu=_AXES_EMU[0],
        height_emu=_AXES_EMU[1],
        max_chars=_MAX_CHARS["axes_line"],
    ),
    main_video_slot=ImageSlotSpec(
        shape_id=6,
        role="main_video",
        kind="picture",
        width_emu=_MAIN_SLOT_EMU[0],
        height_emu=_MAIN_SLOT_EMU[1],
    ),
    cells=_cells("community") + _cells("insight"),
)


__all__ = [
    "ALLOWED_MEDIA_SHA256",
    "CLIP_TEMPLATE_INVENTORY_V1",
    "OUTPUT_DOC_AUTHOR",
    "OUTPUT_DOC_DESCRIPTION",
    "PROPOSAL_SLIDE_INDEX",
    "CellKind",
    "CellSpec",
    "ImageSlotSpec",
    "TemplateInventory",
    "TextFrameSpec",
]
