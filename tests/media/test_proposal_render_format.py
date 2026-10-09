"""提案書の描画で、枠を埋めてもテンプレの書式（字の大きさ・太字・書体・色）を保つ。

10-09 本番の 83 枚で、53 枚目の 6pt・太字の小枠が置き換え後に既定の 14pt へ戻り、
枠の 2〜7 倍の高さにあふれた（``paragraph.text = ...`` が run の rPr を捨てていた）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from teamagent.media.operations import _replace_placeholders
from teamagent.media.render_child import _replace_proposal_special_tokens


def _frame(*runs: tuple[str, dict[str, Any]], paragraphs: int = 1) -> Any:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    frame = slide.shapes.add_textbox(0, 0, Inches(1), Inches(1)).text_frame
    for _ in range(paragraphs - 1):
        frame.add_paragraph()
    targets = list(frame.paragraphs)
    for index, (text, style) in enumerate(runs):
        paragraph = targets[min(style.get("paragraph", 0), len(targets) - 1)]
        run = paragraph.add_run()
        run.text = text
        if "size" in style:
            run.font.size = Pt(style["size"])
        if style.get("bold"):
            run.font.bold = True
        if "font" in style:
            run.font.name = style["font"]
        if "color" in style:
            run.font.color.rgb = RGBColor.from_string(style["color"])
        assert index >= 0
    return frame


def _sizes(frame: Any) -> list[float | None]:
    return [
        run.font.size.pt if run.font.size is not None else None
        for paragraph in frame.paragraphs
        for run in paragraph.runs
    ]


def test_split_token_keeps_template_size_bold_and_font() -> None:
    style = {"size": 6, "bold": True, "font": "Yu Gothic UI"}
    frame = _frame(("｛", style), ("64:59＋60", style), ("を25文字程度で｝", style))
    _replace_placeholders(frame, {64: "冬限定という希少性が、一粒の重みを増す。"})
    runs = frame.paragraphs[0].runs
    assert frame.text == "冬限定という希少性が、一粒の重みを増す。"
    assert runs and all(run.font.size == Pt(6) for run in runs)
    assert all(run.font.bold for run in runs)
    assert all(run.font.name == "Yu Gothic UI" for run in runs)


def test_format_comes_from_the_run_where_the_token_starts() -> None:
    frame = _frame(
        ("左脳", {"size": 8, "color": "FF0000"}),
        ("｛64:", {"size": 6, "bold": True}),
        ("指示｝", {"size": 6, "bold": True}),
    )
    _replace_placeholders(frame, {64: "冬の一粒"})
    assert frame.text == "左脳冬の一粒"
    assert set(_sizes(frame)) == {6.0}


def test_token_in_a_later_paragraph_uses_that_paragraphs_run() -> None:
    frame = _frame(
        ("見出し", {"size": 20, "paragraph": 0}),
        ("｛12:本文｝", {"size": 9, "paragraph": 1}),
        paragraphs=2,
    )
    _replace_placeholders(frame, {12: "本文です"})
    assert frame.paragraphs[0].text == "見出し本文です"
    assert set(_sizes(frame)) == {9.0}


def test_line_breaks_in_the_value_keep_the_format_on_every_run() -> None:
    frame = _frame(("｛7:箇条｝", {"size": 10, "bold": True}))
    _replace_placeholders(frame, {7: "一つ目\v二つ目\v三つ目"})
    runs = frame.paragraphs[0].runs
    assert len(runs) == 3
    assert all(run.font.size == Pt(10) and run.font.bold for run in runs)


def test_runs_without_format_still_replace() -> None:
    frame = _frame(("｛3:課題｝", {}))
    _replace_placeholders(frame, {3: "課題の本文"})
    assert frame.text == "課題の本文"
    assert _sizes(frame) == [None]


@pytest.mark.parametrize(
    "token",
    ["{{PB-DATE:+7:%m/%d}}", "{{PB-ACCOUNTS}}", "{{PB-TEMPLATE:proposal-builder-v1}}"],
)
def test_special_tokens_keep_template_format(token: str) -> None:
    frame = _frame(("投稿: ", {"size": 12}), (token, {"size": 7, "bold": True}))
    _replace_proposal_special_tokens(
        frame,
        {"PB-ACCOUNTS": "アカウント一覧"},
        date(2026, 11, 20),
    )
    assert "{{" not in frame.text
    assert set(_sizes(frame)) == {7.0}


def test_line_breaks_carry_the_same_format() -> None:
    frame = _frame(("｛7:箇条｝", {"size": 10, "bold": True}))
    _replace_placeholders(frame, {7: "一つ目\v二つ目"})
    breaks = frame.paragraphs[0]._p.findall(
        "{http://schemas.openxmlformats.org/drawingml/2006/main}br"
    )
    assert len(breaks) == 1
    props = breaks[0].find("{http://schemas.openxmlformats.org/drawingml/2006/main}rPr")
    assert props is not None and props.get("sz") == "1000"
