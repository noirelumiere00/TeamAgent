"""DeckSpec を組む道具（mcp 側・python-pptx は使わない）。

型そのものは ``teamagent.media.deck_contracts``（media worker も同じ型で読む）。ここでは
ページを足す・ノートの型をそろえる・画像を登録する・はみ出しを文の切れ目で切る、を受け持つ。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from teamagent.media.deck_contracts import (
    ChartFill,
    ChartSeries,
    CsvSpec,
    DeckImage,
    DeckProperties,
    DeckSpec,
    Fill,
    PictureFill,
    SlideSpec,
    SourceLabel,
    TableCell,
    TableFill,
    TextFill,
    TextPara,
    TextRun,
    ThemeColor,
)
from teamagent.skills._deck.layouts import TITLE_MAX, report_template
from teamagent.skills._deck.text import clean, fit_text, wording_problems
from teamagent.skills.omiyage_report.fmt.ooxml import OoxmlError, image_size

SWITCH_ENV = "USE_REPORT_DECK_PPTX"

SOURCE_FETCHED: SourceLabel = "取得値"
SOURCE_COUNTED: SourceLabel = "集計"
SOURCE_AI_GROUNDED: SourceLabel = "AI の読み（数字は照合済み）"
SOURCE_AI_GUESS: SourceLabel = "AI の推定（未照合）"
UNMEASURED = "未計測"


class DeckBuildError(ValueError):
    """DeckSpec に入れられない中身（題の字数超え・出せない言い方 等）。"""


def report_deck_enabled() -> bool:
    """``USE_REPORT_DECK_PPTX`` が真のときだけ作る（既定 OFF・試作）。"""
    return (os.environ.get(SWITCH_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Notes:
    """ノートの型。先方にも渡る前提で、社内のファイル名・費用・モデル名は書かない。"""

    what: str
    talk: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    full_text: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = [f"【このページは何を見て何を出しているか】\n{self.what}"]
        if self.talk:
            parts.append("【話すこと】\n" + "\n".join(f"・{t}" for t in self.talk))
        if self.evidence:
            parts.append("【根拠】\n" + "\n".join(f"・{t}" for t in self.evidence))
        if self.sources:
            parts.append("【出典】\n" + "\n".join(f"・{t}" for t in self.sources))
        if self.full_text:
            parts.append("【全文】\n" + "\n\n".join(self.full_text))
        return "\n\n".join(parts)


def run(
    text: str, *, bold: bool = False, link: str | None = None, color: ThemeColor | None = None
) -> TextRun:
    return TextRun(text=text, bold=bold, link=link or None, color=color)


def para(*runs: TextRun | str, bullet: bool = True) -> TextPara:
    return TextPara(
        runs=tuple(r if isinstance(r, TextRun) else TextRun(text=r) for r in runs),
        bullet=bullet,
    )


def text_fill(box: str, paragraphs: Sequence[TextPara], shape_name: str) -> TextFill:
    return TextFill(box=box, paragraphs=tuple(paragraphs), shape_name=shape_name)


def line_fill(box: str, text: str, shape_name: str, *, bold: bool = False) -> TextFill:
    return text_fill(box, [para(run(text, bold=bold), bullet=False)], shape_name)


def cell(
    text: str, *, link: str | None = None, align: Literal["l", "ctr", "r"] = "l", bold: bool = False
) -> TableCell:
    return TableCell(text=text, link=link or None, align=align, bold=bold)


def table_fill(
    box: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[TableCell]],
    *,
    font_pt: float,
    shape_name: str,
    col_weights: Sequence[float] = (),
    numeric_cols: Sequence[int] = (),
    emphasis_rows: Sequence[int] = (),
) -> TableFill:
    return TableFill(
        box=box,
        columns=tuple(columns),
        rows=tuple(tuple(r) for r in rows),
        font_pt=font_pt,
        shape_name=shape_name,
        col_weights=tuple(col_weights),
        numeric_cols=tuple(numeric_cols),
        emphasis_rows=tuple(emphasis_rows),
    )


def chart_fill(
    box: str,
    *,
    chart_type: Literal["bar", "bar_stacked_100"],
    categories: Sequence[str],
    series: Sequence[ChartSeries],
    shape_name: str,
    number_format: str = "#,##0",
    point_colors: Sequence[ThemeColor] = (),
    legend: bool = False,
) -> ChartFill:
    return ChartFill(
        box=box,
        chart_type=chart_type,
        categories=tuple(categories),
        series=tuple(series),
        shape_name=shape_name,
        number_format=number_format,
        point_colors=tuple(point_colors),
        legend=legend,
    )


@dataclass
class DeckBuilder:
    """ページを順に足して DeckSpec にする。題の字数・言い方はここで検査する。"""

    properties: DeckProperties
    footer: str = ""
    slides: list[SlideSpec] = field(default_factory=list)
    images: dict[str, DeckImage] = field(default_factory=dict)
    image_bytes: dict[str, bytes] = field(default_factory=dict)
    csv: CsvSpec | None = None
    authored_texts: list[str] = field(default_factory=list)

    def add_image(self, name: str, data: bytes | None) -> str | None:
        """画像を登録して名前を返す。読めない画像は登録しない（枠だけ残す）。"""
        if not data:
            return None
        if name in self.images:
            return name
        if data[:2] == b"\xff\xd8":
            ext: Literal["jpeg", "png"] = "jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        else:
            return None
        try:
            width, height = image_size(data, ext)
        except OoxmlError:
            return None
        self.images[name] = DeckImage(
            name=name,
            width_px=width,
            height_px=height,
            media_type="image/jpeg" if ext == "jpeg" else "image/png",
        )
        self.image_bytes[name] = data
        return name

    def add_slide(
        self,
        *,
        slide_id: str,
        layout: str,
        section: str,
        title: str | None,
        fills: Sequence[Fill],
        notes: Notes,
    ) -> SlideSpec:
        all_fills: list[Fill] = []
        if title is not None:
            title = clean(title)
            if len(title) > TITLE_MAX:
                raise DeckBuildError(f"{slide_id}: title exceeds {TITLE_MAX} chars: {title!r}")
            self.authored_texts.append(title)
            all_fills.append(line_fill("title", title, f"{slide_id}｜題"))
        all_fills.extend(fills)
        slide = SlideSpec(
            slide_id=slide_id,
            layout=layout,
            section=section,
            fills=tuple(all_fills),
            notes=notes.render(),
        )
        self.slides.append(slide)
        return slide

    def author(self, *texts: str) -> None:
        """コードや AI が書いた文（言い方の検査の対象。投稿の本文は入れない）。"""
        self.authored_texts.extend(texts)

    def build(self) -> DeckSpec:
        problems = [p for t in self.authored_texts for p in wording_problems(t)]
        if problems:
            raise DeckBuildError("資料に出せない言い方: " + " / ".join(sorted(set(problems))))
        return DeckSpec(
            template=report_template(),
            properties=self.properties,
            footer=self.footer,
            slides=tuple(self.slides),
            images=tuple(self.images.values()),
            csv=self.csv,
        )


def fit_body(text: str, limit: int, full_text: list[str]) -> str:
    """本文 1 点を limit 字に収める。切ったら原文を【全文】へ足す（原文は変えない）。"""
    shown, cut = fit_text(clean(text), limit)
    if cut:
        full_text.append(text)
    return shown


__all__ = [
    "SOURCE_AI_GROUNDED",
    "SOURCE_AI_GUESS",
    "SOURCE_COUNTED",
    "SOURCE_FETCHED",
    "SWITCH_ENV",
    "UNMEASURED",
    "ChartSeries",
    "CsvSpec",
    "DeckBuildError",
    "DeckBuilder",
    "DeckProperties",
    "DeckSpec",
    "Notes",
    "PictureFill",
    "cell",
    "chart_fill",
    "fit_body",
    "line_fill",
    "para",
    "report_deck_enabled",
    "run",
    "table_fill",
    "text_fill",
]
