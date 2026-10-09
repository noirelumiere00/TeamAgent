"""編集できる PowerPoint（ネイティブの表・グラフ・ノート・マスター）の入力契約 DeckSpec。

役割の分け方:
    - mcp（skills）は DeckSpec を組むだけ。python-pptx は import しない（mcp イメージに無い）。
    - media worker 側（``teamagent.media.deck_render``）が python-pptx で描く。

DeckSpec は JSON にできる純データで、描く側は「何をどこに置くか」を作文しない。
寸法・色・書体（テンプレート）も DeckSpec の ``template`` で渡す。正本は
``teamagent.skills._deck.layouts`` の 1 か所で、描く側に寸法の数字を持たせない。

この型を media に置くのは、media worker のイメージが ``src/teamagent/media/`` の決めた
ファイルだけを持ち、skills を持たないため（skills → media.contracts は既存の向き）。
skills 側は ``teamagent.skills._deck.spec`` から同じ型を使う。
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DECK_SPEC_VERSION = "deck-v1"
MAX_DECK_SLIDES = 60
MAX_DECK_IMAGES = 40

ThemeColor = Literal[
    "dk1",
    "lt1",
    "dk2",
    "lt2",
    "accent1",
    "accent2",
    "accent3",
    "accent4",
    "accent5",
    "accent6",
]
BoxKind = Literal["title", "body", "picture", "table", "chart", "footer", "slide_number"]
Align = Literal["l", "ctr", "r"]
Anchor = Literal["t", "ctr", "b"]
SourceLabel = Literal["取得値", "集計", "AI の読み（数字は照合済み）", "AI の推定（未照合）"]

_HEX = r"^[0-9A-F]{6}$"
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_HTTPS = re.compile(r"^https://[^\s]+$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── テンプレート（マスター・レイアウト・テーマ） ─────────────────────────────


class ThemeSpec(_Model):
    name: str = Field(min_length=1, max_length=40)
    colors: dict[str, Annotated[str, Field(pattern=_HEX)]]
    major_font: str = Field(min_length=1, max_length=40)
    minor_font: str = Field(min_length=1, max_length=40)
    lang: str = Field(default="ja-JP", pattern=r"^[a-z]{2}-[A-Z]{2}$")

    @model_validator(mode="after")
    def _all_slots(self) -> ThemeSpec:
        need = {"dk1", "lt1", "dk2", "lt2", "hlink", "folHlink"} | {
            f"accent{i}" for i in range(1, 7)
        }
        missing = need - set(self.colors)
        if missing:
            raise ValueError(f"theme colors missing: {sorted(missing)}")
        return self


class BoxSpec(_Model):
    """レイアウト上の入力欄 1 つ（placeholder）。座標は EMU。"""

    key: str
    kind: BoxKind
    idx: int = Field(ge=0, le=99)
    name: str = Field(min_length=1, max_length=40, description="選択ウィンドウに出る名前")
    prompt: str = Field(default="", max_length=40, description="空のときに見える案内")
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)
    font_pt: float | None = Field(default=None, ge=6, le=96)
    bold: bool = False
    color: ThemeColor | None = None
    align: Align = "l"
    anchor: Anchor = "t"
    shrink_on_overflow: bool = False
    bullets: bool = False
    space_before_pt: float = Field(default=0, ge=0, le=48, description="段落の前の空き")
    line_spacing_pct: int = Field(default=100, ge=80, le=200)

    @model_validator(mode="after")
    def _key_shape(self) -> BoxSpec:
        if not _KEY.fullmatch(self.key):
            raise ValueError(f"box key must be a lower snake slug: {self.key!r}")
        return self


class TileSpec(_Model):
    """レイアウトに置く地の四角（数字のタイル）。飾りではなく区切り。"""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)
    color: ThemeColor = "lt2"


class LayoutSpec(_Model):
    name: str = Field(min_length=1, max_length=40)
    boxes: tuple[BoxSpec, ...] = Field(min_length=1)
    tiles: tuple[TileSpec, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> LayoutSpec:
        keys = [b.key for b in self.boxes]
        idxs = [b.idx for b in self.boxes]
        if len(set(keys)) != len(keys) or len(set(idxs)) != len(idxs):
            raise ValueError(f"layout {self.name!r} has duplicate box key/idx")
        if sum(1 for b in self.boxes if b.kind == "title") > 1:
            raise ValueError(f"layout {self.name!r} has more than one title")
        return self

    def box(self, key: str) -> BoxSpec:
        for b in self.boxes:
            if b.key == key:
                return b
        raise KeyError(key)


class TypographySpec(_Model):
    """入力欄の外で使う細かい寸法（表・グラフ・箇条書き）。正本は _deck/layouts.py。"""

    chart_pt: float = Field(ge=6, le=40)
    chart_gap_width: int = Field(ge=0, le=500)
    chart_min_label_share: float = Field(ge=0, le=0.5, description="これ未満の区間は値を出さない")
    table_row_in_per_pt: float = Field(gt=0, le=0.2, description="行の高さ（in）÷ 字の大きさ（pt）")
    table_cell_margin_emu: int = Field(ge=0)
    bullet_indent_emu: int = Field(ge=0)
    text_inset_emu: int = Field(ge=0, description="入力欄の上下の内側の空き（左右は 0）")


class TemplateSpec(_Model):
    name: str = Field(min_length=1, max_length=40)
    slide_w: int = Field(gt=0)
    slide_h: int = Field(gt=0)
    theme: ThemeSpec
    typography: TypographySpec
    master_boxes: tuple[BoxSpec, ...] = Field(min_length=1)
    layouts: tuple[LayoutSpec, ...] = Field(min_length=1, max_length=11)

    @model_validator(mode="after")
    def _unique_layouts(self) -> TemplateSpec:
        names = [layout.name for layout in self.layouts]
        if len(set(names)) != len(names):
            raise ValueError("layout names must be unique")
        return self

    def layout(self, name: str) -> LayoutSpec:
        for layout in self.layouts:
            if layout.name == name:
                return layout
        raise KeyError(name)


# ── スライドの中身 ────────────────────────────────────────────────────────────


class TextRun(_Model):
    text: str = Field(max_length=2000)
    bold: bool = False
    link: str | None = Field(default=None, pattern=_HTTPS.pattern)
    color: ThemeColor | None = None


class TextPara(_Model):
    runs: tuple[TextRun, ...] = Field(min_length=1)
    bullet: bool = True
    align: Align | None = None


class TextFill(_Model):
    kind: Literal["text"] = "text"
    box: str
    paragraphs: tuple[TextPara, ...] = Field(min_length=1)
    shape_name: str = Field(min_length=1, max_length=80)


class TableCell(_Model):
    text: str = Field(max_length=200)
    link: str | None = Field(default=None, pattern=_HTTPS.pattern)
    bold: bool = False
    align: Align = "l"


class TableFill(_Model):
    kind: Literal["table"] = "table"
    box: str
    columns: tuple[str, ...] = Field(min_length=1, max_length=10)
    rows: tuple[tuple[TableCell, ...], ...] = Field(min_length=1, max_length=40)
    font_pt: float = Field(ge=8, le=20)
    col_weights: tuple[float, ...] = ()
    numeric_cols: tuple[int, ...] = ()
    emphasis_rows: tuple[int, ...] = Field(default=(), description="太字にする行（0 始まり）")
    shape_name: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def _shape(self) -> TableFill:
        width = len(self.columns)
        if any(len(row) != width for row in self.rows):
            raise ValueError("table row width must match columns")
        if self.col_weights and len(self.col_weights) != width:
            raise ValueError("col_weights must match columns")
        if any(not 0 <= c < width for c in self.numeric_cols):
            raise ValueError("numeric_cols out of range")
        return self


class ChartSeries(_Model):
    name: str = Field(min_length=1, max_length=40)
    values: tuple[float, ...] = Field(min_length=1)
    color: ThemeColor = "accent4"


class ChartFill(_Model):
    kind: Literal["chart"] = "chart"
    box: str
    chart_type: Literal["bar", "bar_stacked_100"]
    categories: tuple[str, ...] = Field(min_length=1, max_length=20)
    series: tuple[ChartSeries, ...] = Field(min_length=1, max_length=8)
    number_format: str = Field(default="#,##0", max_length=20)
    point_colors: tuple[ThemeColor, ...] = Field(
        default=(), description="系列 1 本の横棒で、棒ごとの色（注目する棒だけ accent1）"
    )
    legend: bool = False
    shape_name: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def _shape(self) -> ChartFill:
        n = len(self.categories)
        if any(len(s.values) != n for s in self.series):
            raise ValueError("series length must match categories")
        if self.point_colors and (len(self.series) != 1 or len(self.point_colors) != n):
            raise ValueError("point_colors needs exactly one series and one color per bar")
        if self.chart_type == "bar_stacked_100" and any(
            v < 0 for s in self.series for v in s.values
        ):
            raise ValueError("100% stacked bars need non-negative values")
        return self


class PictureFill(_Model):
    kind: Literal["picture"] = "picture"
    box: str
    image: str | None = Field(default=None, description="images[].name。None なら枠だけ残す")
    alt_text: str = Field(min_length=1, max_length=200)
    link: str | None = Field(default=None, pattern=_HTTPS.pattern)
    shape_name: str = Field(min_length=1, max_length=80)


Fill = Annotated[TextFill | TableFill | ChartFill | PictureFill, Field(discriminator="kind")]


class SlideSpec(_Model):
    slide_id: str = Field(pattern=r"^SS-[0-9]{2}(?:-[0-9]+){0,2}$")
    layout: str
    section: str = Field(min_length=1, max_length=40)
    fills: tuple[Fill, ...] = Field(min_length=1)
    notes: str = Field(default="", max_length=20000)


class DeckImage(_Model):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    media_type: Literal["image/jpeg", "image/png"]


class DeckProperties(_Model):
    title: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=200)
    keywords: str = Field(default="", max_length=200)
    report_id: str = Field(min_length=1, max_length=80)
    measured_at: str = Field(min_length=1, max_length=40, description="取得時点（JST の文字列）")
    author: str = Field(default="Aico", max_length=40)


class CsvSpec(_Model):
    columns: tuple[str, ...] = Field(min_length=1, max_length=40)
    rows: tuple[tuple[str, ...], ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> CsvSpec:
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("csv row width must match columns")
        return self


class DeckSpec(_Model):
    spec_version: Literal["deck-v1"] = "deck-v1"
    template: TemplateSpec
    properties: DeckProperties
    footer: str = Field(default="", max_length=80)
    slides: tuple[SlideSpec, ...] = Field(min_length=1, max_length=MAX_DECK_SLIDES)
    images: tuple[DeckImage, ...] = Field(default=(), max_length=MAX_DECK_IMAGES)
    csv: CsvSpec | None = None

    @model_validator(mode="after")
    def _references(self) -> DeckSpec:
        image_names = {img.name for img in self.images}
        if len(image_names) != len(self.images):
            raise ValueError("image names must be unique")
        seen_sections: list[str] = []
        for slide in self.slides:
            try:
                layout = self.template.layout(slide.layout)
            except KeyError as exc:
                raise ValueError(f"{slide.slide_id}: unknown layout {slide.layout!r}") from exc
            used: set[str] = set()
            for fill in slide.fills:
                try:
                    box = layout.box(fill.box)
                except KeyError as exc:
                    raise ValueError(
                        f"{slide.slide_id}: layout {layout.name!r} has no box {fill.box!r}"
                    ) from exc
                expected = {
                    "text": ("title", "body"),
                    "table": ("table",),
                    "chart": ("chart",),
                    "picture": ("picture",),
                }[fill.kind]
                if box.kind not in expected:
                    raise ValueError(
                        f"{slide.slide_id}: {fill.kind} cannot go into {box.kind} box {box.key!r}"
                    )
                if fill.box in used:
                    raise ValueError(f"{slide.slide_id}: box {fill.box!r} filled twice")
                used.add(fill.box)
                if isinstance(fill, PictureFill) and fill.image and fill.image not in image_names:
                    raise ValueError(f"{slide.slide_id}: unknown image {fill.image!r}")
            if not seen_sections or seen_sections[-1] != slide.section:
                if slide.section in seen_sections:
                    raise ValueError(f"section {slide.section!r} must be contiguous")
                seen_sections.append(slide.section)
        return self

    def sections(self) -> list[tuple[str, list[int]]]:
        """セクション名と、その中のスライド番号（0 始まり）。"""
        out: list[tuple[str, list[int]]] = []
        for index, slide in enumerate(self.slides):
            if not out or out[-1][0] != slide.section:
                out.append((slide.section, []))
            out[-1][1].append(index)
        return out


__all__ = [
    "DECK_SPEC_VERSION",
    "BoxSpec",
    "ChartFill",
    "ChartSeries",
    "CsvSpec",
    "DeckImage",
    "DeckProperties",
    "DeckSpec",
    "Fill",
    "LayoutSpec",
    "PictureFill",
    "SlideSpec",
    "SourceLabel",
    "TableCell",
    "TableFill",
    "TemplateSpec",
    "TextFill",
    "TextPara",
    "TextRun",
    "ThemeColor",
    "ThemeSpec",
    "TileSpec",
    "TypographySpec",
]
