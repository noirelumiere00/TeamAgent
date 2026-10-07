"""DeckSpec → 編集できる PowerPoint（media worker 側の python-pptx 描画）。

各スライドの部品の種類（入力欄・表・グラフ・図）・ノート・書体・字の大きさを、保存した .pptx を
python-pptx で開き直して確かめる（描いた側のオブジェクトではなく、ファイルの中身を見る）。
"""

from __future__ import annotations

import io
from typing import Any

import pytest

pptx = pytest.importorskip("pptx")

from pptx import Presentation  # noqa: E402
from pptx.enum.chart import XL_CHART_TYPE  # noqa: E402
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER  # noqa: E402

from teamagent.media.deck_contracts import DeckSpec  # noqa: E402
from teamagent.media.deck_render import (  # noqa: E402
    DeckRenderError,
    render_deck_csv,
    render_deck_files,
    render_deck_pptx,
)
from teamagent.skills.search_surface_check.deck import build_surface_deck  # noqa: E402
from tests.skills.search_surface_check.deck_fixtures import (  # noqa: E402
    CLIENT,
    NOW,
    REPORT_ID,
    ai_conclusion,
    covers,
    surface,
)

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


@pytest.fixture(scope="module")
def built() -> tuple[DeckSpec, Any]:
    s = surface(15, conclusion=ai_conclusion())
    deck = build_surface_deck(
        [s],
        client_name=CLIENT,
        client_accounts=["kurashiru.com"],
        measured_epoch=NOW,
        report_id=REPORT_ID,
        addressee="テスト株式会社 御中",
        covers=covers(s.posts),
    )
    body = render_deck_pptx(deck.spec, deck.images)
    return deck.spec, Presentation(io.BytesIO(body))


def _slide(spec: DeckSpec, prs: Any, slide_id: str) -> Any:
    index = [s.slide_id for s in spec.slides].index(slide_id)
    return prs.slides[index]


def _kinds(slide: Any) -> list[str]:
    """部品の種類: 入力欄 / 表 / グラフ / 図 / 図の枠（画像なし）/ それ以外。"""
    out = []
    for shape in slide.shapes:
        name = shape.__class__.__name__
        if getattr(shape, "has_chart", False):
            out.append("chart")
        elif getattr(shape, "has_table", False):
            out.append("table")
        elif name == "PlaceholderPicture" or shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            out.append("picture")
        elif name == "PicturePlaceholder":
            out.append("picture_frame")
        elif shape.is_placeholder:
            out.append("placeholder")
        else:
            out.append("other")
    return out


def _layout_size_pt(slide: Any, shape: Any) -> float:
    """入力欄の字の大きさ（run に無ければレイアウトの lstStyle から）。"""
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if run.font.size is not None:
                return float(run.font.size.pt)
    idx = shape.placeholder_format.idx
    layout_ph = next(p for p in slide.slide_layout.placeholders if p.placeholder_format.idx == idx)
    rpr = layout_ph._element.find(f".//{_A}lstStyle/{_A}lvl1pPr/{_A}defRPr")
    return int(rpr.get("sz")) / 100


def test_layouts_are_named_and_slides_pick_them_by_name(built: tuple[DeckSpec, Any]) -> None:
    spec, prs = built
    names = [layout.name for layout in prs.slide_layouts]
    assert names == ["R_表紙", "R_結論", "R_数字", "R_グラフ", "R_表", "R_動画カード", "R_付録の表"]
    for slide_spec, slide in zip(spec.slides, prs.slides, strict=True):
        assert slide.slide_layout.name == slide_spec.layout
    assert prs.slide_width == 12_192_000 and prs.slide_height == 6_858_000


def test_part_kinds_per_slide(built: tuple[DeckSpec, Any]) -> None:
    spec, prs = built
    cover = _kinds(_slide(spec, prs, "SS-01"))
    assert cover.count("picture") == 1 and "placeholder" in cover
    assert "table" in _kinds(_slide(spec, prs, "SS-05"))
    assert "chart" in _kinds(_slide(spec, prs, "SS-07"))
    cards = _kinds(_slide(spec, prs, "SS-08"))
    assert cards.count("picture") == 5
    assert "table" in _kinds(_slide(spec, prs, "SS-09"))
    for slide in prs.slides:
        # 置いた部品はすべて入力欄・表・グラフ・図（手で置いた図形やテキストボックスは無い）
        assert "other" not in _kinds(slide)


def test_every_slide_has_title_condition_footer_page_number_and_notes(
    built: tuple[DeckSpec, Any],
) -> None:
    _, prs = built
    for number, slide in enumerate(prs.slides, start=1):
        types = [s.placeholder_format.type for s in slide.placeholders]
        assert PP_PLACEHOLDER.TITLE in types
        assert PP_PLACEHOLDER.FOOTER in types and PP_PLACEHOLDER.SLIDE_NUMBER in types
        texts = [s.text_frame.text for s in slide.placeholders if s.has_text_frame]
        assert any(t.startswith("「スパイスカレー 作り方」で検索した上位 15 本") for t in texts)
        number_ph = next(
            s
            for s in slide.placeholders
            if s.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER
        )
        assert number_ph._element.find(f".//{_A}fld").get("type") == "slidenum"
        assert number_ph.text_frame.text == str(number)
        notes = slide.notes_slide.notes_text_frame.text
        assert notes.startswith("【このページは何を見て何を出しているか】")


def test_no_empty_prompt_placeholders_are_left(built: tuple[DeckSpec, Any]) -> None:
    _, prs = built
    for slide in prs.slides:
        for shape in slide.placeholders:
            if shape.has_text_frame:
                assert shape.text_frame.text.strip(), shape.name


def test_theme_fonts_colors_and_japanese_language(built: tuple[DeckSpec, Any]) -> None:
    _, prs = built
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    theme = prs.slide_master.part.part_related_by(RT.THEME).blob.decode("utf-8")
    assert theme.count('typeface="游ゴシック"') == 4  # 見出し・本文 × latin・ea
    assert '<a:accent1><a:srgbClr val="2F4B7C"/>' in theme
    assert '<a:dk2><a:srgbClr val="2A3351"/>' in theme
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    assert run.font._rPr.get("lang") == "ja-JP"
                    assert run.font.name is None  # 書体はテーマを参照（run に書かない）


def test_font_sizes_follow_the_template(built: tuple[DeckSpec, Any]) -> None:
    spec, prs = built
    for slide in prs.slides:
        title = slide.shapes.title
        assert _layout_size_pt(slide, title) == 28
    conclusion = _slide(spec, prs, "SS-04")
    body = next(s for s in conclusion.placeholders if s.placeholder_format.idx == 1)
    assert _layout_size_pt(conclusion, body) == 14

    def table_pt(slide: Any) -> float:
        table = next(s for s in slide.shapes if getattr(s, "has_table", False)).table
        return float(table.cell(1, 0).text_frame.paragraphs[0].runs[0].font.size.pt)

    assert table_pt(_slide(spec, prs, "SS-05")) == 14  # 6 行以下
    assert table_pt(_slide(spec, prs, "SS-09")) == 12  # 上位 10 本
    assert table_pt(_slide(spec, prs, "SS-12-1")) == 11  # 付録


def test_table_is_native_with_header_links_and_right_aligned_numbers(
    built: tuple[DeckSpec, Any],
) -> None:
    spec, prs = built
    slide = _slide(spec, prs, "SS-09")
    frame = next(s for s in slide.shapes if getattr(s, "has_table", False))
    assert frame.name == "SS-09｜上位 10 本"
    table = frame.table
    header = [table.cell(0, c).text for c in range(len(table.columns))]
    assert header[:2] == ["順位", "アカウント（元投稿）"] and "保存率" in header
    link = table.cell(1, 1).text_frame.paragraphs[0].runs[0].hyperlink.address
    assert link and link.startswith("https://www.tiktok.com/@gonosara/")
    plays_col = header.index("再生")
    from pptx.enum.text import PP_ALIGN

    assert table.cell(1, plays_col).text_frame.paragraphs[0].alignment == PP_ALIGN.RIGHT
    tc_pr = table.cell(0, 0)._tc.tcPr
    assert tc_pr.find(f"{_A}solidFill/{_A}schemeClr").get("val") == "accent1"
    # 罫線は横線だけ（accent4）・縦線なし
    body_pr = table.cell(1, 0)._tc.tcPr
    assert body_pr.find(f"{_A}lnB/{_A}solidFill/{_A}schemeClr").get("val") == "accent4"
    assert body_pr.find(f"{_A}lnL/{_A}noFill") is not None


def test_chart_is_native_with_embedded_data_top_first(built: tuple[DeckSpec, Any]) -> None:
    spec, prs = built
    frame = next(s for s in _slide(spec, prs, "SS-07").shapes if getattr(s, "has_chart", False))
    assert frame.name == "SS-07｜投稿者の構成"
    chart = frame.chart
    assert chart.chart_type == XL_CHART_TYPE.BAR_STACKED_100
    assert chart.part.chart_workbook.xlsx_part is not None  # 「データの編集」で開ける
    assert chart.category_axis.reverse_order is True
    assert list(chart.plots[0].categories) == ["本数", "再生"]
    assert chart.plots[0].data_labels.number_format == "0%"
    assert chart.has_legend


def test_pictures_are_not_cropped_and_have_alt_text_and_links(
    built: tuple[DeckSpec, Any],
) -> None:
    spec, prs = built
    slide = _slide(spec, prs, "SS-08")
    pictures = [s for s in slide.shapes if s.__class__.__name__ == "PlaceholderPicture"]
    assert len(pictures) == 5
    for pic in pictures:
        assert (pic.crop_left, pic.crop_right, pic.crop_top, pic.crop_bottom) == (0, 0, 0, 0)
        descr = pic._element.nvPicPr.cNvPr.get("descr")
        assert descr.endswith("表紙") and " 位 @" in descr
        assert pic.click_action.hyperlink.address.startswith("https://www.tiktok.com/")
    second = next(p for p in pictures if p._element.nvPicPr.cNvPr.get("descr").startswith("2 位"))
    assert second.width / second.height == pytest.approx(3 / 4, rel=0.01)  # 3:4 のまま収める


def test_sections_and_document_properties(built: tuple[DeckSpec, Any]) -> None:
    _, prs = built
    xml = prs.part._element.xml
    for name in ("表紙", "本編", "動画別", "付録"):
        assert f'name="{name}"' in xml
    props = prs.core_properties
    assert props.identifier == REPORT_ID
    assert REPORT_ID in props.comments and "取得時点" in props.comments
    assert props.title == "「スパイスカレー 作り方」TikTok 検索 上位 15 本の顔ぶれ"


def test_csv_has_bom_and_all_rows(built: tuple[DeckSpec, Any]) -> None:
    spec, _ = built
    body = render_deck_csv(spec)
    assert body is not None and body.startswith("﻿".encode())
    lines = body.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("検索語,媒体,順位")
    assert len(lines) == 16


def test_files_entry_and_missing_image_is_rejected(built: tuple[DeckSpec, Any]) -> None:
    spec, _ = built
    with pytest.raises(DeckRenderError, match="missing"):
        render_deck_files(spec.model_dump_json(), {})
    s = surface(3)
    deck = build_surface_deck([s], client_name=None, measured_epoch=NOW, report_id=REPORT_ID)
    files = render_deck_files(deck.spec.model_dump_json(), deck.images)
    assert set(files) == {"deck.pptx", "deck.csv"}
    prs = Presentation(io.BytesIO(files["deck.pptx"]))
    cover = prs.slides[0]
    frames = [s for s in cover.placeholders if s.placeholder_format.type == PP_PLACEHOLDER.PICTURE]
    assert len(frames) == 1  # 画像が無くても「枠だけ」は残す
