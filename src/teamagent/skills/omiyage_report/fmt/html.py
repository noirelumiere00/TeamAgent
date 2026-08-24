"""DeckContent + FMT spec → 1920x1080 セクション列 HTML（決定論・無作文）。

- レイアウトの正典 = dc.html の section テンプレート（spec ``layout.layout_canon``）。
  寸法トークンが食い違う箇所は tokens / image_rules（=DESIGN.md）で上書き済み
  （known_drift: 見出し40px・ランキングカード150x267・実例パネル132x235・表紙角丸14px）。
- 文言・数値は入力データと spec 固定文言のみ。レンダラはここで作文しない。
- 全表示テキストは HTML エスケープ + 絵文字除去（``text_rules.display_text_preprocess``）。
- フォントは同梱書体のサブセット woff2 を data:URI 埋め込み（fonts.py・glyph ゲート）。
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from teamagent.skills.omiyage_report.fmt.contract import (
    AData,
    BData,
    Card,
    CData,
    CGroup,
    DData,
    DeckContent,
    EData,
    HData,
    RankingCard,
    Slide,
)
from teamagent.skills.omiyage_report.fmt.fonts import FontRole, build_embedded_fonts
from teamagent.skills.omiyage_report.fmt.spec import FmtDeckSpec

# 絵文字・装飾記号の除去（矢印・約物は残す）。元データは改変しない=表示時のみ。
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # 絵文字ブロック全般
    "\U00002600-\U000027bf"  # Misc Symbols / Dingbats
    "\U0001f1e6-\U0001f1ff"  # Regional indicators
    "⬀-⯿"  # ⭐ 等
    "︎️‍⃣"  # VS15/16・ZWJ・囲み keycap
    "]+"
)

_NUMERIC_CELL = re.compile(r"^[0-9,.%+\-/KMkm万億件本位回人]+$")


def strip_display_symbols(text: str) -> str:
    return _EMOJI.sub("", text)


def shorten_url(url: str, *, max_len: int = 42) -> str:
    display = re.sub(r"^https://(www\.)?", "", url).rstrip("/")
    if len(display) <= max_len:
        return display
    return display[: max_len - 1] + "…"


def bar_pct(value: float, maximum: float) -> int:
    if maximum <= 0:
        return 0
    return max(0, min(99, round(value / maximum * 99)))


def fmt_number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


@dataclass
class _Sink:
    """描画テキストを役割スタック別に収集（フォントサブセットの母集合）。"""

    chars: dict[FontRole, set[str]] = field(
        default_factory=lambda: {"mincho": set(), "gothic": set(), "latin": set()}
    )

    def take(self, role: FontRole, text: str) -> str:
        cleaned = strip_display_symbols(text)
        self.chars[role].update(cleaned)
        return _html.escape(cleaned, quote=True)


class _DeckHtmlBuilder:
    def __init__(self, content: DeckContent, spec: FmtDeckSpec) -> None:
        self._content = content
        self._spec = spec
        self._sink = _Sink()
        self._total = len(content.slides)
        meta = content.deck_meta
        accents = spec.tokens.colors.brand_accents
        self._accent_a = meta.brand_a.accent_color or accents.brand_a.value
        self._accent_b = meta.brand_b.accent_color or accents.brand_b.value

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _c(self, name: str) -> str:
        return self._spec.color(name)

    def _part_label(self, slide: Slide) -> str:
        if slide.part is not None:
            title = self._content.deck_meta.part_titles[slide.part - 1]
            return f"PART {slide.part} — {title}"
        if slide.type == "H":
            return "SUMMARY"
        return ""

    def _header(self, slide: Slide) -> str:
        sink = self._sink
        parts = ['<div class="head"><div class="head-l">']
        if slide.q_number:
            parts.append(f'<span class="qnum">{sink.take("latin", slide.q_number)}</span>')
        parts.append(f'<h1 class="heading">{sink.take("mincho", slide.heading)}</h1></div>')
        label = self._part_label(slide)
        if label:
            parts.append(f'<span class="plabel">{sink.take("latin", label)}</span>')
        parts.append('</div><div class="hrule"></div>')
        if slide.lead:
            parts.append(f'<p class="lead">{sink.take("gothic", slide.lead)}</p>')
        return "".join(parts)

    def _tag(self, slide: Slide) -> str:
        """スライド下部ブロック（U2脚注 24px muted + タグ帯）。どちらも無ければ空。"""
        sink = self._sink
        parts: list[str] = []
        if slide.footnote:
            parts.append(f'<p class="fnote">{sink.take("gothic", slide.footnote)}</p>')
        if slide.tag is not None:
            parts.append(
                '<div class="tagband">'
                f'<span class="tagchip">{sink.take("gothic", slide.tag.variant)}</span>'
                f'<p class="tagtext">{sink.take("gothic", slide.tag.text)}</p>'
                "</div>"
            )
        if not parts:
            return ""
        return f'<div class="botblock">{"".join(parts)}</div>'

    def _footer(self, page_no: int) -> str:
        sink = self._sink
        running = sink.take("latin", self._content.deck_meta.running_head)
        numbering = sink.take("latin", f"{page_no:02d} / {self._total:02d}")
        return f'<div class="foot"><span>{running}</span><span>{numbering}</span></div>'

    def _thumb(
        self,
        card: Card,
        *,
        width: int,
        height: int,
        radius: int,
        shadow: str,
        with_url: bool,
    ) -> str:
        sink = self._sink
        parts = [
            '<div class="thumbwrap">',
            f'<div class="thumbbox" style="width:{width}px;height:{height}px;'
            f'border-radius:{radius}px;box-shadow:{shadow}">'
            f'<img src="{_html.escape(card.image.data_uri, quote=True)}" alt=""></div>',
        ]
        if card.image.image_kind == "provided_thumbnail":
            parts.append(f'<p class="imglabel">{sink.take("gothic", "提供サムネ")}</p>')
        if with_url:
            parts.append(
                f'<p class="imgurl">{sink.take("latin", shorten_url(card.source_url))}</p>'
            )
        parts.append("</div>")
        return "".join(parts)

    def _example_column(self, card: Card | None) -> str:
        if card is None:
            return ""
        sink = self._sink
        sizes = self._spec.image_rules.sizes_px.example_panel_m
        parts = [
            '<div class="excol">',
            self._thumb(
                card,
                width=sizes.w,
                height=sizes.h,
                radius=12,
                shadow="0 18px 40px rgba(26,27,31,.18)",
                with_url=True,
            ),
        ]
        if card.caption:
            parts.append(f'<p class="excap">{sink.take("gothic", card.caption)}</p>')
        parts.append("</div>")
        return "".join(parts)

    # ------------------------------------------------------------------
    # slide types
    # ------------------------------------------------------------------

    def _slide_a(self, slide: Slide, data: AData) -> str:
        sink = self._sink
        meta = self._content.deck_meta
        cover = self._spec.image_rules.sizes_px.cover_pair
        parts = [
            '<div class="cv-top">'
            f'<span class="cv-addr">{sink.take("gothic", meta.addressee)}</span>'
            f'<span class="cv-conf">{sink.take("latin", "CONFIDENTIAL")}</span></div>',
            '<div class="cv-mid"><div class="cv-left">',
            f'<p class="cv-cat">{sink.take("latin", meta.category_en)}</p>',
            '<p class="cv-brands">'
            + sink.take("mincho", f"{meta.brand_a.name} × {meta.brand_b.name}")
            + "</p>",
            '<h1 class="cv-title">'
            + "<br>".join(
                sink.take("mincho", line) for line in meta.cover_title.splitlines() or [""]
            )
            + "</h1>",
            f'<p class="cv-abs">{sink.take("gothic", meta.abstract)}</p>',
            "</div>",
        ]
        if data.thumbnail_pair is not None:
            first, second = data.thumbnail_pair
            shadow = "0 30px 60px rgba(26,27,31,.22)"
            parts.append(
                '<div class="cv-thumbs">'
                '<div style="margin-top:-70px">'
                + self._thumb(
                    first, width=cover.w, height=cover.h, radius=14, shadow=shadow, with_url=False
                )
                + "</div>"
                '<div style="margin-top:90px">'
                + self._thumb(
                    second, width=cover.w, height=cover.h, radius=14, shadow=shadow, with_url=False
                )
                + "</div></div>"
            )
        parts.append("</div>")
        labels = ("手法", "対象", "制約")
        rows = []
        for index, value in enumerate(meta.method_target_constraints):
            rows.append(
                '<div class="cv-def">'
                f'<span class="cv-def-k">{sink.take("gothic", labels[index])}</span>'
                f'<span class="cv-def-v">{sink.take("gothic", value)}</span></div>'
            )
        parts.append(
            '<div class="cv-btm"><div class="cv-defs">'
            + "".join(rows)
            + f'</div><span class="cv-issuer">{sink.take("gothic", meta.issuer)}</span></div>'
        )
        return "".join(parts)

    def _slide_b(self, slide: Slide, data: BData) -> str:
        sink = self._sink
        q_rows = []
        for index, item in enumerate(data.q_list):
            extra = (
                "border-bottom:1px solid var(--ch-rule);" if index == len(data.q_list) - 1 else ""
            )
            q_rows.append(
                f'<div class="ch-q" style="{extra}">'
                f'<span class="ch-qn">{sink.take("latin", item.q_number)}</span>'
                f'<span class="ch-qq">{sink.take("gothic", item.question)}</span></div>'
            )
        return (
            '<div class="ch-left">'
            f'<span class="ch-part">{sink.take("latin", f"PART {data.part}")}</span>'
            f'<div class="ch-ghost">{sink.take("latin", str(data.part))}</div>'
            f'<h1 class="ch-title">{sink.take("mincho", data.title)}</h1>'
            f'<p class="ch-abs">{sink.take("gothic", data.abstract)}</p>'
            "</div>"
            f'<div class="ch-right">{"".join(q_rows)}</div>'
        )

    def _c_card(
        self,
        brand_name: str,
        accent: str,
        note: str | None,
        groups: tuple[CGroup, ...],
        side: str,
        max_by_unit: Mapping[str, float],
        units_with_pairs: frozenset[str],
    ) -> str:
        sink = self._sink
        values = [group.value_a if side == "a" else group.value_b for group in groups]
        counts = [group.count_a if side == "a" else group.count_b for group in groups]
        # 数値の色付けは「その群（同一単位・カード内）の最大値」のみ。
        # 単位あたり1行しか無いカードでは色付けしない（全部に色を付けない規範）。
        card_max_by_unit: dict[str, float] = {}
        for group, value in zip(groups, values, strict=True):
            if value > card_max_by_unit.get(group.unit, 0.0):
                card_max_by_unit[group.unit] = value
        rows = [
            '<div class="cc-head">'
            f'<span class="cc-legend"><span class="cc-sw" style="background:{accent}"></span>'
            f"<span>{sink.take('gothic', brand_name)}</span></span>"
        ]
        if note:
            rows.append(f'<div class="cc-note">{sink.take("gothic", note)}</div>')
        rows.append("</div>")
        for group, value, count in zip(groups, values, counts, strict=True):
            is_max = (
                group.unit in units_with_pairs
                and value > 0
                and value == card_max_by_unit.get(group.unit)
            )
            label_weight = 700 if is_max else 500
            value_color = accent if is_max else self._c("ink")
            count_html = (
                f'<span class="cc-count">{sink.take("gothic", f"{count}件")}　</span>'
                if count is not None
                else ""
            )
            value_text = sink.take("latin", f"{fmt_number(value)}{group.unit}")
            maximum = max_by_unit.get(group.unit, 0.0)
            rows.append(
                '<div class="cc-row">'
                '<div class="cc-line">'
                f'<span class="cc-label" style="font-weight:{label_weight}">'
                f"{sink.take('gothic', group.label)}</span>"
                f'<span>{count_html}<span class="cc-val" style="color:{value_color}">'
                f"{value_text}</span></span></div>"
                f'<div class="cc-track"><div style="width:{bar_pct(value, maximum)}%;'
                f'height:8px;background:{accent}"></div></div>'
                "</div>"
            )
        return f'<div class="cc-card">{"".join(rows)}</div>'

    def _slide_c(self, slide: Slide, data: CData) -> str:
        meta = self._content.deck_meta
        # バー正規化は「同一単位の全値（両ブランド横断）の最大=100%相当」（canon Q2/Q3 実測）
        max_by_unit: dict[str, float] = {}
        unit_rows: dict[str, int] = {}
        for group in data.groups:
            max_by_unit[group.unit] = max(
                max_by_unit.get(group.unit, 0.0), group.value_a, group.value_b
            )
            unit_rows[group.unit] = unit_rows.get(group.unit, 0) + 1
        units_with_pairs = frozenset(unit for unit, count in unit_rows.items() if count >= 2)
        cards = (
            '<div class="cc-pair">'
            + self._c_card(
                meta.brand_a.name,
                self._accent_a,
                data.note_a,
                data.groups,
                "a",
                max_by_unit,
                units_with_pairs,
            )
            + self._c_card(
                meta.brand_b.name,
                self._accent_b,
                data.note_b,
                data.groups,
                "b",
                max_by_unit,
                units_with_pairs,
            )
            + "</div>"
        )
        return (
            self._header(slide)
            + f'<div class="content-row">{cards}{self._example_column(data.example)}</div>'
            + self._tag(slide)
        )

    def _slide_d(self, slide: Slide, data: DData) -> str:
        sink = self._sink
        column_count = len(data.columns)
        template = "1.3fr " + " ".join(["1fr"] * (column_count - 1))
        head_cells = "".join(
            f'<span class="tb-h">{sink.take("gothic", column)}</span>' for column in data.columns
        )
        # 列ごとの最大値（数値セルのみ・列/群の最大値のみ accent）
        max_by_column: dict[int, float] = {}
        for column_index in range(1, column_count):
            best: float | None = None
            seen = 0
            for row in data.rows:
                parsed = _parse_cell_number(row[column_index])
                if parsed is None:
                    continue
                seen += 1
                if best is None or parsed > best:
                    best = parsed
            if best is not None and seen >= 2:
                max_by_column[column_index] = best
        body_rows = []
        for row in data.rows:
            cells = [f'<span class="tb-c0">{sink.take("gothic", row[0])}</span>']
            for column_index, cell in enumerate(row[1:], start=1):
                parsed = _parse_cell_number(cell)
                is_max = (
                    parsed is not None
                    and column_index in max_by_column
                    and parsed == max_by_column[column_index]
                )
                style = f"color:{self._accent_a};font-weight:700;" if is_max else ""
                css = "tb-num" if _NUMERIC_CELL.match(cell.replace(" ", "")) else "tb-txt"
                role: FontRole = "latin" if css == "tb-num" else "gothic"
                cells.append(f'<span class="{css}" style="{style}">{sink.take(role, cell)}</span>')
            body_rows.append(
                f'<div class="tb-row" style="grid-template-columns:{template}">'
                + "".join(cells)
                + "</div>"
            )
        table = (
            '<div class="tb">'
            f'<div class="tb-head" style="grid-template-columns:{template}">{head_cells}</div>'
            + "".join(body_rows)
            + "</div>"
        )
        return (
            self._header(slide)
            + f'<div class="content-row">{table}{self._example_column(data.example)}</div>'
            + self._tag(slide)
        )

    def _e_card(self, ranking_card: RankingCard, max_eg: float) -> str:
        sink = self._sink
        sizes = self._spec.image_rules.sizes_px.ranking_card
        accent = self._accent_b if ranking_card.brand == "b" else self._accent_a
        is_max = max_eg > 0 and ranking_card.eg_rate_pct == max_eg
        eg_style = f"color:{accent};font-weight:700;font-size:32px" if is_max else ""
        metrics = (
            '<div class="ec-metrics">'
            '<div class="ec-m"><p class="ec-k">'
            + sink.take("gothic", "再生数")
            + '</p><p class="ec-v">'
            + sink.take("latin", fmt_number(ranking_card.views))
            + "</p></div>"
            '<div class="ec-m"><p class="ec-k">'
            + sink.take("gothic", "EG率")
            + f'</p><p class="ec-v" style="{eg_style}">'
            + sink.take("latin", f"{ranking_card.eg_rate_pct:.2f}%")
            + "</p></div></div>"
            '<div class="ec-m ec-followers"><p class="ec-k">'
            + sink.take("gothic", "フォロワー")
            + '</p><p class="ec-f">'
            + sink.take("latin", fmt_number(ranking_card.followers))
            + "</p></div>"
        )
        return (
            '<div class="ec-card">'
            + self._thumb(
                ranking_card,
                width=sizes.w,
                height=sizes.h,
                radius=12,
                shadow="0 16px 34px rgba(26,27,31,.16)",
                with_url=True,
            )
            + f'<p class="ec-name">{sink.take("gothic", ranking_card.account_name)}</p>'
            + metrics
            + f'<p class="ec-sum">{sink.take("gothic", ranking_card.content_summary)}</p>'
            + "</div>"
        )

    def _slide_e(self, slide: Slide, data: EData) -> str:
        max_eg = max(card.eg_rate_pct for card in data.cards)
        cards = "".join(self._e_card(card, max_eg) for card in data.cards)
        return self._header(slide) + f'<div class="ec-row">{cards}</div>' + self._tag(slide)

    def _slide_h(self, slide: Slide, data: HData) -> str:
        sink = self._sink
        rows = []
        for h_row in data.summary_rows:
            rows.append(
                '<div class="sm-row">'
                f'<span class="sm-no">{sink.take("latin", str(h_row.number))}</span>'
                f'<h2 class="sm-pat">{sink.take("mincho", h_row.pattern)}</h2>'
                f'<p class="sm-desc">{sink.take("gothic", h_row.description)}</p>'
                "</div>"
            )
        band = ""
        if data.cta or data.conclusion is not None:
            band_parts = ['<div class="sm-band">']
            if data.conclusion is not None:
                band_parts.append(f'<p class="sm-concl">{sink.take("mincho", data.conclusion)}</p>')
            if data.cta:
                band_parts.append(
                    f'<p class="sm-cta">{sink.take("gothic", self._spec.cta_text)}</p>'
                )
            band_parts.append("</div>")
            band = "".join(band_parts)
        return (
            self._header(slide)
            + f'<div class="sm-rows">{"".join(rows)}</div>'
            + band
            + self._tag(slide)
        )

    # ------------------------------------------------------------------
    # assembly
    # ------------------------------------------------------------------

    def _render_slide(self, slide: Slide, page_no: int) -> str:
        data = slide.data
        if isinstance(data, AData):
            body, css, with_footer = self._slide_a(slide, data), "slide-a", False
        elif isinstance(data, BData):
            body, css, with_footer = self._slide_b(slide, data), "slide-b", False
        elif isinstance(data, CData):
            body, css, with_footer = self._slide_c(slide, data), "slide-c", True
        elif isinstance(data, DData):
            body, css, with_footer = self._slide_d(slide, data), "slide-d", True
        elif isinstance(data, EData):
            body, css, with_footer = self._slide_e(slide, data), "slide-e", True
        elif isinstance(data, HData):
            body, css, with_footer = self._slide_h(slide, data), "slide-h", True
        else:  # pragma: no cover - contract 検証済み
            raise AssertionError(f"unsupported slide payload: {type(data)!r}")
        footer = self._footer(page_no) if with_footer else ""
        label = _html.escape(f"{page_no:02d} {slide.type}", quote=True)
        return (
            f'<section class="slide {css}" data-label="{label}" data-type="{slide.type}">'
            f"{body}{footer}</section>"
        )

    def build(self, *, font_dir: Path | None = None) -> str:
        slides_html = "".join(
            self._render_slide(slide, page_no)
            for page_no, slide in enumerate(self._content.slides, start=1)
        )
        embedded = build_embedded_fonts(dict(self._sink.chars), base_dir=font_dir)
        style = _build_stylesheet(self._spec, embedded.families, self._accent_a)
        title = _html.escape(strip_display_symbols(self._content.deck_meta.cover_title))
        return (
            "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<style>{embedded.css}{style}</style></head>"
            f"<body>{slides_html}</body></html>"
        )


def _parse_cell_number(cell: str) -> float | None:
    compact = cell.strip().replace(",", "")
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)(?:[%本件位回人KM]|万|億)?$", compact)
    if match is None:
        return None
    return float(match.group(1))


def _build_stylesheet(
    spec: FmtDeckSpec,
    families: Mapping[FontRole, str],
    accent_a: str,
) -> str:
    colors = spec.tokens.colors
    mincho = families["mincho"]
    gothic = families["gothic"]
    latin = families["latin"]
    ch = colors.chapter_dark_whites
    return f"""
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#111114;font-family:{gothic};color:{colors.ink.value}}}
.slide{{width:1920px;height:1080px;position:relative;overflow:hidden;background:{colors.paper.value};
  display:flex;flex-direction:column;padding:56px 110px 104px}}
.slide-a{{padding:72px 110px 64px}}
.slide-b{{background:{colors.dark.value};color:{colors.paper.value};flex-direction:row;gap:90px;padding:90px 110px}}
.slide-e{{padding-bottom:88px}}
.head{{flex:none;display:flex;align-items:baseline;justify-content:space-between;gap:40px}}
.head-l{{display:flex;align-items:baseline;gap:20px;min-width:0}}
.qnum{{font-family:{latin};font-weight:650;font-size:46px;letter-spacing:.06em;color:{accent_a};flex:none}}
.heading{{font-family:{mincho};font-weight:700;font-size:40px;line-height:1.2;letter-spacing:.01em}}
.plabel{{flex:none;font-family:{latin};font-weight:500;font-size:24px;letter-spacing:.12em;color:{colors.muted.value}}}
.hrule{{flex:none;border-bottom:1px solid {colors.rule_strong.value};margin-top:18px}}
.lead{{flex:none;margin-top:16px;font-size:25px;line-height:1.85;color:{colors.lead.value}}}
.content-row{{display:flex;gap:56px;margin-top:20px;flex:1;min-height:0}}
.botblock{{flex:none;margin-top:auto}}
.fnote{{font-size:24px;line-height:1.5;color:{colors.muted.value};margin-bottom:10px}}
.tagband{{flex:none;border-top:1px solid {colors.rule_strong.value};padding-top:18px;
  display:flex;gap:24px;align-items:flex-start}}
.tagchip{{flex:none;background:{colors.ink.value};color:{colors.paper.value};font-size:24px;font-weight:900;
  letter-spacing:.18em;padding:7px 16px}}
.tagtext{{font-size:26px;line-height:1.6;color:{colors.body.value}}}
.foot{{position:absolute;left:110px;right:110px;bottom:34px;display:flex;justify-content:space-between;
  font-family:{latin};font-weight:500;font-size:24px;color:{colors.muted.value};letter-spacing:.08em}}
.thumbwrap{{display:flex;flex-direction:column;align-items:center;flex:none}}
.thumbbox{{background:#ffffff;overflow:hidden;flex:none}}
.thumbbox img{{width:100%;height:100%;object-fit:contain;display:block}}
.imglabel{{margin-top:8px;font-size:24px;max-width:250px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:{colors.muted.value}}}
.imgurl{{margin-top:4px;max-width:250px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:{latin};font-size:24px;color:{colors.muted.value}}}
.excol{{flex:none;width:300px;display:flex;flex-direction:column;align-items:center;
  background:{colors.panel.value};padding:20px;gap:0}}
.excap{{margin-top:14px;font-size:24px;line-height:1.55;color:{colors.body.value}}}
.cv-top{{flex:none;display:flex;justify-content:space-between;align-items:baseline;
  border-bottom:1px solid {colors.rule_strong.value};padding-bottom:24px}}
.cv-addr{{font-size:27px;font-weight:700}}
.cv-conf{{font-family:{latin};font-weight:500;font-size:24px;letter-spacing:.14em;color:{colors.muted.value}}}
.cv-mid{{flex:1;display:flex;gap:80px;align-items:center;min-height:0}}
.cv-left{{flex:1}}
.cv-cat{{font-family:{latin};font-weight:600;font-size:24px;letter-spacing:.22em;color:{accent_a}}}
.cv-brands{{margin-top:26px;font-family:{mincho};font-weight:700;font-size:34px;color:{colors.body_sub.value}}}
.cv-title{{margin-top:10px;font-family:{mincho};font-weight:800;font-size:104px;line-height:1.18;letter-spacing:.01em}}
.cv-abs{{margin-top:36px;max-width:900px;font-size:26px;line-height:1.85;color:{colors.body.value}}}
.cv-thumbs{{flex:none;display:flex;gap:26px;align-items:center}}
.cv-btm{{flex:none;display:flex;justify-content:space-between;align-items:flex-end;gap:60px}}
.cv-defs{{flex:1;max-width:1150px}}
.cv-def{{display:flex;gap:30px;border-top:1px solid {colors.hairline.value};padding:14px 0}}
.cv-def-k{{flex:none;width:80px;font-weight:900;font-size:24px;letter-spacing:.1em}}
.cv-def-v{{font-size:24px;line-height:1.6;color:{colors.body_sub.value}}}
.cv-issuer{{flex:none;font-size:26px;font-weight:700}}
.ch-left{{flex:1;display:flex;flex-direction:column}}
.ch-part{{font-family:{latin};font-weight:600;font-size:26px;letter-spacing:.3em;color:{ch.text_dim}}}
.ch-ghost{{font-family:{latin};font-weight:700;font-size:400px;line-height:.85;margin-top:40px;
  color:transparent;-webkit-text-stroke:2px rgba(246,244,239,.4)}}
.ch-title{{margin:auto 0 0;font-family:{mincho};font-weight:700;font-size:80px;line-height:1.2;color:{ch.text_strong}}}
.ch-abs{{margin-top:30px;max-width:920px;font-size:26px;line-height:1.85;color:{ch.text_mid}}}
.ch-right{{flex:none;width:520px;display:flex;flex-direction:column;justify-content:flex-end}}
.ch-q{{display:flex;gap:22px;border-top:1px solid {ch.rule};padding:20px 0}}
.ch-qn{{flex:none;width:64px;font-family:{latin};font-weight:600;font-size:24px;color:{ch.text_dim}}}
.ch-qq{{font-size:24px;color:{ch.text_strong}}}
.cc-pair{{flex:1;display:flex;gap:44px;align-items:stretch;min-width:0}}
.cc-card{{flex:1;min-height:0;border:1px solid {colors.rule_card.value};padding:30px 38px 34px;
  display:flex;flex-direction:column;justify-content:space-between;gap:16px}}
.cc-head{{display:flex;flex-direction:column;align-items:flex-start;gap:10px}}
.cc-legend{{display:inline-flex;align-items:center;gap:12px;font-weight:700;font-size:28px}}
.cc-sw{{width:14px;height:14px;display:inline-block;flex:none}}
.cc-note{{font-size:24px;color:{colors.muted.value}}}
.cc-row{{flex:none}}
.cc-line{{display:flex;justify-content:space-between;align-items:baseline;gap:16px}}
.cc-label{{font-size:25px;color:{colors.body_sub.value}}}
.cc-count{{font-size:24px;color:{colors.muted.value}}}
.cc-val{{font-family:{latin};font-weight:700;font-size:28px;font-variant-numeric:tabular-nums}}
.cc-track{{height:8px;background:{colors.track.value};margin-top:5px}}
.tb{{flex:1;display:flex;flex-direction:column;justify-content:space-between;min-width:0}}
.tb-head{{display:grid;gap:0 18px;border-bottom:2px solid {colors.rule_strong.value};padding-bottom:12px}}
.tb-h{{font-size:24px;font-weight:500;color:{colors.muted.value}}}
.tb-row{{flex:1;display:grid;gap:0 18px;align-items:center;border-bottom:1px solid {colors.rule_row.value}}}
.tb-c0{{font-size:27px;font-weight:700}}
.tb-num{{font-family:{latin};font-variant-numeric:tabular-nums;font-size:27px}}
.tb-txt{{font-size:25px;color:{colors.body_sub.value}}}
.ec-row{{margin-top:20px;flex:1;min-height:0;display:flex;gap:34px}}
.ec-card{{flex:1;display:flex;flex-direction:column;align-items:stretch;min-width:0}}
.ec-card .thumbwrap{{align-self:center}}
.ec-name{{margin-top:16px;font-size:26px;font-weight:700}}
.ec-metrics{{margin-top:12px;border-top:1px solid {colors.rule_strong.value};padding-top:12px;
  display:flex;flex-direction:column;gap:9px}}
.ec-m{{display:flex;justify-content:space-between;align-items:baseline;gap:10px}}
.ec-followers{{margin-top:11px}}
.ec-k{{font-size:24px;color:{colors.muted.value}}}
.ec-v{{font-family:{latin};font-variant-numeric:tabular-nums;font-weight:600;font-size:30px;line-height:1.15}}
.ec-f{{font-family:{latin};font-variant-numeric:tabular-nums;font-size:26px;color:{colors.body_sub.value}}}
.ec-sum{{margin-top:14px;font-size:24px;line-height:1.5;color:{colors.body.value}}}
.sm-rows{{flex:1;display:flex;flex-direction:column;justify-content:center;margin-top:8px;min-height:0}}
.sm-row{{display:grid;grid-template-columns:96px 420px 1fr;gap:0 50px;padding:26px 0;
  border-bottom:1px solid {colors.rule_card.value}}}
.sm-no{{font-family:{latin};font-weight:600;font-size:64px;line-height:1;color:{colors.hairline.value}}}
.sm-pat{{font-family:{mincho};font-weight:700;font-size:31px;line-height:1.35}}
.sm-desc{{font-size:24px;line-height:1.62;color:{colors.body_sub.value}}}
.sm-band{{flex:none;margin-top:24px;background:{colors.dark.value};color:{colors.paper.value};padding:34px 48px}}
.sm-concl{{font-family:{mincho};font-weight:700;font-size:27px;line-height:1.6}}
.sm-cta{{margin-top:12px;font-size:24px;line-height:1.6;color:{ch.text_mid}}}
"""


def render_deck_html(
    content: DeckContent,
    spec: FmtDeckSpec,
    *,
    font_dir: Path | None = None,
) -> str:
    """検証済み DeckContent → 自己完結 HTML（外部参照なし・フォント/画像 data:URI）。"""
    return _DeckHtmlBuilder(content, spec).build(font_dir=font_dir)
