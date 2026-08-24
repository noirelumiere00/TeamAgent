"""編集可能ネイティブPPTX（併走・近似版）の組み立て。

U3裁定: 正 = 画像モードPPTX。本モジュールは同じ計測JSONから shape 描画で
「編集用（見た目は画像版が正）」を生成する。フォントは閲覧側依存のため近似
（書体名だけ指定・埋め込みなし）。表紙とファイル名にその旨を明記する。

レイアウトは html.py のテンプレートを粗く写した決定論配置。ここでも
無作文原則（文言は入力データと spec 固定文言のみ）と contain 規律を守る。
"""

from __future__ import annotations

import base64
import re
from typing import Literal, cast

from teamagent.skills.omiyage_report.fmt.contract import (
    AData,
    BData,
    Card,
    CData,
    DData,
    DeckContent,
    EData,
    HData,
    Slide,
)
from teamagent.skills.omiyage_report.fmt.html import (
    bar_pct,
    fmt_number,
    shorten_url,
    strip_display_symbols,
)
from teamagent.skills.omiyage_report.fmt.ooxml import (
    Align,
    Paragraph,
    Picture,
    Rect,
    Run,
    SlidePage,
    TextBox,
    build_pptx,
    contain_box,
    image_size,
)
from teamagent.skills.omiyage_report.fmt.spec import FmtDeckSpec

EDIT_MARKER = "編集用（見た目は画像版が正）"

_MARGIN = 110
_CONTENT_W = 1920 - 2 * _MARGIN

_DATA_URI_HEAD = re.compile(r"^data:image/(jpeg|png);base64,")

_MINCHO = "Shippori Mincho B1"
_GOTHIC = "Zen Kaku Gothic New"
_LATIN = "Instrument Sans"


def _decode_data_uri(data_uri: str) -> tuple[bytes, Literal["jpeg", "png"]]:
    match = _DATA_URI_HEAD.match(data_uri)
    if match is None:  # pragma: no cover - contract 検証済み
        raise ValueError("unsupported image data URI")
    body = re.sub(r"\s+", "", data_uri[match.end() :])
    return base64.b64decode(body), cast('Literal["jpeg", "png"]', match.group(1))


def _composite_over(rgba: str, background_hex: str) -> str:
    """spec の rgba(...) 白系を dark 地色へ合成した近似 sRGB HEX（OOXMLはα非対応）。"""
    match = re.match(r"rgba\(([0-9.]+),\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)\)", rgba)
    if match is None:
        raise ValueError(f"invalid rgba: {rgba!r}")
    red, green, blue, alpha = (float(part) for part in match.groups())
    base = tuple(int(background_hex[i : i + 2], 16) for i in (1, 3, 5))
    mixed = tuple(
        round(channel * alpha + b * (1 - alpha))
        for channel, b in zip((red, green, blue), base, strict=True)
    )
    return "#{:02X}{:02X}{:02X}".format(*mixed)


class _EditableBuilder:
    def __init__(self, content: DeckContent, spec: FmtDeckSpec) -> None:
        self._content = content
        self._spec = spec
        meta = content.deck_meta
        accents = spec.tokens.colors.brand_accents
        self._accent_a = meta.brand_a.accent_color or accents.brand_a.value
        self._accent_b = meta.brand_b.accent_color or accents.brand_b.value
        self._total = len(content.slides)

    def _c(self, name: str) -> str:
        return self._spec.color(name)

    # ------------------------------------------------------------------
    # shape helpers
    # ------------------------------------------------------------------

    def _text(
        self,
        page: SlidePage,
        text: str,
        *,
        x: float,
        y: float,
        w: float,
        h: float,
        font: str,
        size: float,
        color: str,
        bold: bool = False,
        align: Align = "l",
    ) -> None:
        cleaned = strip_display_symbols(text)
        page.shapes.append(
            TextBox(
                x=x,
                y=y,
                w=w,
                h=h,
                paragraphs=(
                    Paragraph(
                        runs=(Run(cleaned, font, size, color, bold),),
                        align=align,
                    ),
                ),
            )
        )

    def _rule(
        self,
        page: SlidePage,
        y: float,
        *,
        x: float = _MARGIN,
        w: float = _CONTENT_W,
        h: float = 1.0,
        color: str | None = None,
    ) -> None:
        page.shapes.append(Rect(x=x, y=y, w=w, h=h, fill=color or self._c("rule_strong")))

    def _image(
        self, page: SlidePage, card: Card, *, x: float, y: float, w: float, h: float, with_url: bool
    ) -> float:
        """白余白つき contain 配置。戻り値=画像ブロック直下の y。"""
        data, ext = _decode_data_uri(card.image.data_uri)
        page.shapes.append(Rect(x=x, y=y, w=w, h=h, fill="#FFFFFF", line=self._c("rule_card")))
        natural_w, natural_h = image_size(data, ext)
        px, py, pw, ph = contain_box(natural_w, natural_h, x, y, w, h)
        page.shapes.append(Picture(x=px, y=py, w=pw, h=ph, data=data, ext=ext))
        cursor = y + h
        if card.image.image_kind == "provided_thumbnail":
            self._text(
                page,
                "提供サムネ",
                x=x - 40,
                y=cursor + 6,
                w=w + 80,
                h=30,
                font=_GOTHIC,
                size=24,
                color=self._c("muted"),
                align="ctr",
            )
            cursor += 36
        if with_url:
            self._text(
                page,
                shorten_url(card.source_url),
                x=x - 70,
                y=cursor + 4,
                w=w + 140,
                h=30,
                font=_LATIN,
                size=24,
                color=self._c("muted"),
                align="ctr",
            )
            cursor += 34
        return cursor

    def _header(self, page: SlidePage, slide: Slide) -> None:
        x = _MARGIN
        if slide.q_number:
            self._text(
                page,
                slide.q_number,
                x=x,
                y=56,
                w=150,
                h=60,
                font=_LATIN,
                size=46,
                color=self._accent_a,
                bold=True,
            )
            x += 170
        self._text(
            page,
            slide.heading,
            x=x,
            y=62,
            w=1500 - x,
            h=56,
            font=_MINCHO,
            size=40,
            color=self._c("ink"),
            bold=True,
        )
        label = self._part_label(slide)
        if label:
            self._text(
                page,
                label,
                x=1400,
                y=74,
                w=410,
                h=34,
                font=_LATIN,
                size=24,
                color=self._c("muted"),
                align="r",
            )
        self._rule(page, 132)
        if slide.lead:
            self._text(
                page,
                slide.lead,
                x=_MARGIN,
                y=150,
                w=_CONTENT_W,
                h=40,
                font=_GOTHIC,
                size=25,
                color=self._c("lead"),
            )

    def _part_label(self, slide: Slide) -> str:
        if slide.part is not None:
            title = self._content.deck_meta.part_titles[slide.part - 1]
            return f"PART {slide.part} — {title}"
        if slide.type == "H":
            return "SUMMARY"
        return ""

    def _tag(self, page: SlidePage, slide: Slide, *, y: float) -> None:
        if slide.footnote:
            # U2裁定: 初出の用語定義注記（24px muted・タグ帯の直上）
            self._text(
                page,
                slide.footnote,
                x=_MARGIN,
                y=y - 40,
                w=_CONTENT_W,
                h=32,
                font=_GOTHIC,
                size=24,
                color=self._c("muted"),
            )
        if slide.tag is None:
            return
        self._rule(page, y)
        page.shapes.append(Rect(x=_MARGIN, y=y + 18, w=110, h=44, fill=self._c("ink")))
        self._text(
            page,
            slide.tag.variant,
            x=_MARGIN,
            y=y + 26,
            w=110,
            h=30,
            font=_GOTHIC,
            size=24,
            color=self._c("paper"),
            bold=True,
            align="ctr",
        )
        self._text(
            page,
            slide.tag.text,
            x=_MARGIN + 134,
            y=y + 18,
            w=_CONTENT_W - 134,
            h=100,
            font=_GOTHIC,
            size=26,
            color=self._c("body"),
        )

    def _footer(self, page: SlidePage, page_no: int) -> None:
        muted = self._c("muted")
        self._text(
            page,
            self._content.deck_meta.running_head,
            x=_MARGIN,
            y=1022,
            w=900,
            h=30,
            font=_LATIN,
            size=24,
            color=muted,
        )
        self._text(
            page,
            f"{page_no:02d} / {self._total:02d}",
            x=1920 - _MARGIN - 300,
            y=1022,
            w=300,
            h=30,
            font=_LATIN,
            size=24,
            color=muted,
            align="r",
        )

    # ------------------------------------------------------------------
    # slide types
    # ------------------------------------------------------------------

    def _slide_a(self, page: SlidePage, data: AData) -> None:
        meta = self._content.deck_meta
        ink = self._c("ink")
        self._text(
            page,
            meta.addressee,
            x=_MARGIN,
            y=72,
            w=900,
            h=40,
            font=_GOTHIC,
            size=27,
            color=ink,
            bold=True,
        )
        self._text(
            page,
            "CONFIDENTIAL",
            x=1400,
            y=78,
            w=410,
            h=32,
            font=_LATIN,
            size=24,
            color=self._c("muted"),
            align="r",
        )
        self._rule(page, 136)
        self._text(
            page,
            EDIT_MARKER,
            x=1200,
            y=146,
            w=610,
            h=32,
            font=_GOTHIC,
            size=24,
            color=self._c("muted"),
            align="r",
        )
        self._text(
            page,
            meta.category_en,
            x=_MARGIN,
            y=216,
            w=1100,
            h=34,
            font=_LATIN,
            size=24,
            color=self._accent_a,
            bold=True,
        )
        self._text(
            page,
            f"{meta.brand_a.name} × {meta.brand_b.name}",
            x=_MARGIN,
            y=262,
            w=1100,
            h=48,
            font=_MINCHO,
            size=34,
            color=self._c("body_sub"),
            bold=True,
        )
        title_lines = meta.cover_title.splitlines() or [meta.cover_title]
        page.shapes.append(
            TextBox(
                x=_MARGIN,
                y=320,
                w=1180,
                h=280,
                paragraphs=tuple(
                    Paragraph(runs=(Run(strip_display_symbols(line), _MINCHO, 104, ink, True),))
                    for line in title_lines
                ),
            )
        )
        self._text(
            page,
            meta.abstract,
            x=_MARGIN,
            y=620,
            w=900,
            h=150,
            font=_GOTHIC,
            size=26,
            color=self._c("body"),
        )
        if data.thumbnail_pair is not None:
            first, second = data.thumbnail_pair
            self._image(page, first, x=1360, y=190, w=236, h=420, with_url=False)
            self._image(page, second, x=1622, y=330, w=236, h=420, with_url=False)
        labels = ("手法", "対象", "制約")
        y = 820
        for index, value in enumerate(meta.method_target_constraints):
            self._rule(page, y, w=1150, color=self._c("hairline"))
            self._text(
                page,
                labels[index],
                x=_MARGIN,
                y=y + 12,
                w=90,
                h=32,
                font=_GOTHIC,
                size=24,
                color=ink,
                bold=True,
            )
            self._text(
                page,
                value,
                x=_MARGIN + 110,
                y=y + 12,
                w=1040,
                h=52,
                font=_GOTHIC,
                size=24,
                color=self._c("body_sub"),
            )
            y += 62
        self._text(
            page,
            meta.issuer,
            x=1400,
            y=990,
            w=410,
            h=40,
            font=_GOTHIC,
            size=26,
            color=ink,
            bold=True,
            align="r",
        )

    def _slide_b(self, page: SlidePage, data: BData) -> None:
        dark = self._c("dark")
        whites = self._spec.tokens.colors.chapter_dark_whites
        dim = _composite_over(whites.text_dim, dark)
        mid = _composite_over(whites.text_mid, dark)
        strong = _composite_over(whites.text_strong, dark)
        rule = _composite_over(whites.rule, dark)
        self._text(
            page,
            f"PART {data.part}",
            x=_MARGIN,
            y=90,
            w=600,
            h=40,
            font=_LATIN,
            size=26,
            color=dim,
            bold=True,
        )
        ghost = _composite_over("rgba(246, 244, 239, 0.22)", dark)
        self._text(
            page,
            str(data.part),
            x=_MARGIN,
            y=140,
            w=520,
            h=420,
            font=_LATIN,
            size=380,
            color=ghost,
            bold=True,
        )
        self._text(
            page,
            data.title,
            x=_MARGIN,
            y=640,
            w=1000,
            h=110,
            font=_MINCHO,
            size=80,
            color=strong,
            bold=True,
        )
        self._text(
            page, data.abstract, x=_MARGIN, y=780, w=920, h=200, font=_GOTHIC, size=26, color=mid
        )
        base_y = 990 - len(data.q_list) * 88
        x = 1290
        for item in data.q_list:
            page.shapes.append(Rect(x=x, y=base_y, w=520, h=1, fill=rule))
            self._text(
                page,
                item.q_number,
                x=x,
                y=base_y + 22,
                w=70,
                h=32,
                font=_LATIN,
                size=24,
                color=dim,
                bold=True,
            )
            self._text(
                page,
                item.question,
                x=x + 86,
                y=base_y + 22,
                w=434,
                h=60,
                font=_GOTHIC,
                size=24,
                color=strong,
            )
            base_y += 88
        page.shapes.append(Rect(x=x, y=base_y, w=520, h=1, fill=rule))

    def _c_card(
        self,
        page: SlidePage,
        data: CData,
        *,
        side: str,
        x: float,
        w: float,
        name: str,
        accent: str,
        max_by_unit: dict[str, float],
        units_with_pairs: frozenset[str],
    ) -> None:
        page.shapes.append(Rect(x=x, y=210, w=w, h=640, fill=None, line=self._c("rule_card")))
        page.shapes.append(Rect(x=x + 38, y=246, w=14, h=14, fill=accent))
        self._text(
            page,
            name,
            x=x + 64,
            y=236,
            w=w - 100,
            h=36,
            font=_GOTHIC,
            size=28,
            color=self._c("ink"),
            bold=True,
        )
        note = data.note_a if side == "a" else data.note_b
        row_y = 292.0
        if note:
            self._text(
                page,
                note,
                x=x + 38,
                y=row_y,
                w=w - 76,
                h=32,
                font=_GOTHIC,
                size=24,
                color=self._c("muted"),
            )
            row_y += 44
        values = [group.value_a if side == "a" else group.value_b for group in data.groups]
        counts = [group.count_a if side == "a" else group.count_b for group in data.groups]
        card_max_by_unit: dict[str, float] = {}
        for group, value in zip(data.groups, values, strict=True):
            if value > card_max_by_unit.get(group.unit, 0.0):
                card_max_by_unit[group.unit] = value
        available = 830 - row_y
        step = available / max(1, len(data.groups))
        for group, value, count in zip(data.groups, values, counts, strict=True):
            is_max = (
                group.unit in units_with_pairs
                and value > 0
                and value == card_max_by_unit.get(group.unit)
            )
            value_color = accent if is_max else self._c("ink")
            self._text(
                page,
                group.label,
                x=x + 38,
                y=row_y,
                w=w - 300,
                h=34,
                font=_GOTHIC,
                size=25,
                color=self._c("body_sub"),
                bold=is_max,
            )
            value_text = f"{fmt_number(value)}{group.unit}"
            if count is not None:
                self._text(
                    page,
                    f"{count}件",
                    x=x + w - 300,
                    y=row_y + 4,
                    w=110,
                    h=30,
                    font=_GOTHIC,
                    size=24,
                    color=self._c("muted"),
                    align="r",
                )
            self._text(
                page,
                value_text,
                x=x + w - 190,
                y=row_y - 4,
                w=152,
                h=38,
                font=_LATIN,
                size=28,
                color=value_color,
                bold=True,
                align="r",
            )
            bar_y = row_y + 42
            page.shapes.append(Rect(x=x + 38, y=bar_y, w=w - 76, h=8, fill=self._c("track")))
            fill_w = (w - 76) * bar_pct(value, max_by_unit.get(group.unit, 0.0)) / 100
            if fill_w > 0:
                page.shapes.append(Rect(x=x + 38, y=bar_y, w=fill_w, h=8, fill=accent))
            row_y += step

    def _slide_c(self, page: SlidePage, slide: Slide, data: CData) -> None:
        meta = self._content.deck_meta
        max_by_unit: dict[str, float] = {}
        unit_rows: dict[str, int] = {}
        for group in data.groups:
            max_by_unit[group.unit] = max(
                max_by_unit.get(group.unit, 0.0), group.value_a, group.value_b
            )
            unit_rows[group.unit] = unit_rows.get(group.unit, 0) + 1
        units_with_pairs = frozenset(unit for unit, count in unit_rows.items() if count >= 2)
        has_example = data.example is not None
        pair_w = 1290 if has_example else _CONTENT_W
        card_w = (pair_w - 44) / 2
        self._c_card(
            page,
            data,
            side="a",
            x=_MARGIN,
            w=card_w,
            name=meta.brand_a.name,
            accent=self._accent_a,
            max_by_unit=max_by_unit,
            units_with_pairs=units_with_pairs,
        )
        self._c_card(
            page,
            data,
            side="b",
            x=_MARGIN + card_w + 44,
            w=card_w,
            name=meta.brand_b.name,
            accent=self._accent_b,
            max_by_unit=max_by_unit,
            units_with_pairs=units_with_pairs,
        )
        if data.example is not None:
            self._example(page, data.example)
        self._tag(page, slide, y=880)

    def _example(self, page: SlidePage, card: Card) -> None:
        panel_x = 1510.0
        page.shapes.append(Rect(x=panel_x, y=210, w=300, h=640, fill=self._c("panel")))
        cursor = self._image(page, card, x=panel_x + 84, y=240, w=132, h=235, with_url=True)
        if card.caption:
            self._text(
                page,
                card.caption,
                x=panel_x + 20,
                y=cursor + 10,
                w=260,
                h=260,
                font=_GOTHIC,
                size=24,
                color=self._c("body"),
            )

    def _slide_d(self, page: SlidePage, slide: Slide, data: DData) -> None:
        has_example = data.example is not None
        table_w = 1290 if has_example else _CONTENT_W
        columns = len(data.columns)
        first_w = table_w * 0.22
        other_w = (table_w - first_w) / (columns - 1)
        header_y = 214
        for index, column in enumerate(data.columns):
            x = _MARGIN + (first_w + (index - 1) * other_w if index else 0)
            self._text(
                page,
                column,
                x=x,
                y=header_y,
                w=(other_w if index else first_w) - 16,
                h=32,
                font=_GOTHIC,
                size=24,
                color=self._c("muted"),
            )
        page.shapes.append(
            Rect(x=_MARGIN, y=header_y + 40, w=table_w, h=2, fill=self._c("rule_strong"))
        )
        top = header_y + 42
        bottom = 860
        step = (bottom - top) / len(data.rows)
        row_y = float(top)
        for row in data.rows:
            text_y = row_y + step / 2 - 18
            self._text(
                page,
                row[0],
                x=_MARGIN,
                y=text_y,
                w=first_w - 16,
                h=36,
                font=_GOTHIC,
                size=27,
                color=self._c("ink"),
                bold=True,
            )
            for index, cell in enumerate(row[1:], start=1):
                x = _MARGIN + first_w + (index - 1) * other_w
                self._text(
                    page,
                    cell,
                    x=x,
                    y=text_y,
                    w=other_w - 16,
                    h=36,
                    font=_LATIN,
                    size=27,
                    color=self._c("ink"),
                )
            row_y += step
            page.shapes.append(Rect(x=_MARGIN, y=row_y, w=table_w, h=1, fill=self._c("rule_row")))
        if data.example is not None:
            self._example(page, data.example)
        self._tag(page, slide, y=880)

    def _slide_e(self, page: SlidePage, slide: Slide, data: EData) -> None:
        card_w = (_CONTENT_W - 4 * 37) / 5
        max_eg = max(card.eg_rate_pct for card in data.cards)
        for index, ranking_card in enumerate(data.cards):
            x = _MARGIN + index * (card_w + 37)
            cursor = self._image(
                page, ranking_card, x=x + (card_w - 150) / 2, y=210, w=150, h=267, with_url=True
            )
            self._text(
                page,
                ranking_card.account_name,
                x=x,
                y=cursor + 8,
                w=card_w,
                h=34,
                font=_GOTHIC,
                size=26,
                color=self._c("ink"),
                bold=True,
            )
            metrics_y = cursor + 50
            page.shapes.append(Rect(x=x, y=metrics_y, w=card_w, h=1, fill=self._c("rule_strong")))
            accent = self._accent_b if ranking_card.brand == "b" else self._accent_a
            rows = (
                ("再生数", fmt_number(ranking_card.views), self._c("ink")),
                (
                    "EG率",
                    f"{ranking_card.eg_rate_pct:.2f}%",
                    accent if max_eg > 0 and ranking_card.eg_rate_pct == max_eg else self._c("ink"),
                ),
                ("フォロワー", fmt_number(ranking_card.followers), self._c("body_sub")),
            )
            item_y = metrics_y + 12
            for label, value, color in rows:
                self._text(
                    page,
                    label,
                    x=x,
                    y=item_y,
                    w=card_w / 2,
                    h=30,
                    font=_GOTHIC,
                    size=24,
                    color=self._c("muted"),
                )
                self._text(
                    page,
                    value,
                    x=x + card_w / 2,
                    y=item_y - 4,
                    w=card_w / 2,
                    h=34,
                    font=_LATIN,
                    size=30,
                    color=color,
                    bold=True,
                    align="r",
                )
                item_y += 44
            self._text(
                page,
                ranking_card.content_summary,
                x=x,
                y=item_y + 6,
                w=card_w,
                h=110,
                font=_GOTHIC,
                size=24,
                color=self._c("body"),
            )
        self._tag(page, slide, y=880)

    def _slide_h(self, page: SlidePage, slide: Slide, data: HData) -> None:
        top = 210
        band_needed = data.cta or data.conclusion is not None
        bottom = 830 if band_needed else 870
        step = (bottom - top) / len(data.summary_rows)
        row_y = float(top)
        for h_row in data.summary_rows:
            self._text(
                page,
                str(h_row.number),
                x=_MARGIN,
                y=row_y + 12,
                w=96,
                h=70,
                font=_LATIN,
                size=64,
                color=self._c("hairline"),
                bold=True,
            )
            self._text(
                page,
                h_row.pattern,
                x=_MARGIN + 146,
                y=row_y + 16,
                w=420,
                h=90,
                font=_MINCHO,
                size=31,
                color=self._c("ink"),
                bold=True,
            )
            self._text(
                page,
                h_row.description,
                x=_MARGIN + 616,
                y=row_y + 16,
                w=_CONTENT_W - 616,
                h=step - 24,
                font=_GOTHIC,
                size=24,
                color=self._c("body_sub"),
            )
            row_y += step
            page.shapes.append(
                Rect(x=_MARGIN, y=row_y, w=_CONTENT_W, h=1, fill=self._c("rule_card"))
            )
        if band_needed:
            page.shapes.append(Rect(x=_MARGIN, y=850, w=_CONTENT_W, h=150, fill=self._c("dark")))
            text_y = 872.0
            if data.conclusion is not None:
                self._text(
                    page,
                    data.conclusion,
                    x=_MARGIN + 48,
                    y=text_y,
                    w=_CONTENT_W - 96,
                    h=56,
                    font=_MINCHO,
                    size=27,
                    color=self._c("paper"),
                    bold=True,
                )
                text_y += 58
            if data.cta:
                self._text(
                    page,
                    self._spec.cta_text,
                    x=_MARGIN + 48,
                    y=text_y,
                    w=_CONTENT_W - 96,
                    h=44,
                    font=_GOTHIC,
                    size=24,
                    color="#CECCC9",
                )

    # ------------------------------------------------------------------
    # assembly
    # ------------------------------------------------------------------

    def build(self) -> bytes:
        pages: list[SlidePage] = []
        paper = self._c("paper")
        for page_no, slide in enumerate(self._content.slides, start=1):
            data = slide.data
            if isinstance(data, BData):
                page = SlidePage(background=self._c("dark"))
                self._slide_b(page, data)
            else:
                page = SlidePage(background=paper)
                if isinstance(data, AData):
                    self._slide_a(page, data)
                else:
                    self._header(page, slide)
                    if isinstance(data, CData):
                        self._slide_c(page, slide, data)
                    elif isinstance(data, DData):
                        self._slide_d(page, slide, data)
                    elif isinstance(data, EData):
                        self._slide_e(page, slide, data)
                    elif isinstance(data, HData):
                        self._slide_h(page, slide, data)
                    self._footer(page, page_no)
            pages.append(page)
        title = strip_display_symbols(self._content.deck_meta.cover_title)
        return build_pptx(pages, title=f"{title}（{EDIT_MARKER}）")


def render_editable_pptx(content: DeckContent, spec: FmtDeckSpec) -> bytes:
    """検証済み DeckContent → 編集用ネイティブPPTX バイト列。"""
    return _EditableBuilder(content, spec).build()
