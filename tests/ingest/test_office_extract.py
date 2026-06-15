"""office_extract.py のテスト — docx/pptx/xlsx をテスト内で生成して抽出ロジックを検証。

バイナリ fixture を repo に置かず、各 lib (python-docx / python-pptx / openpyxl) で
生成 → BytesIO に save → 抽出という流れ。lib 自体の動作と統合できる。
"""

from __future__ import annotations

from io import BytesIO

import pytest

from teamagent.ingest.office_extract import (
    DOCX_MIME,
    GDOC_NATIVE_MIME,
    OFFICE_BINARY_MIMES,
    PPTX_MIME,
    XLSX_MIME,
    _normalize_text,
    extract_docx_text,
    extract_office_pages,
    extract_pptx_pages,
    extract_xlsx_pages,
)


# -----------------------------------------------------------
# _normalize_text
# -----------------------------------------------------------
def test_normalize_text_removes_nul_and_collapses_whitespace() -> None:
    assert _normalize_text("hello\x00world  \n\nfoo") == "helloworld foo"


def test_normalize_text_empty() -> None:
    assert _normalize_text("") == ""
    assert _normalize_text("   \n\t  ") == ""


# -----------------------------------------------------------
# docx
# -----------------------------------------------------------
def _build_sample_docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("見出しテキスト")
    doc.add_paragraph("本文のサンプルです。")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "セルA"
    table.rows[0].cells[1].text = "セルB"
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_extract_docx_text_includes_paragraphs_and_tables() -> None:
    text = extract_docx_text(_build_sample_docx())
    assert "見出しテキスト" in text
    assert "本文のサンプル" in text
    assert "セルA" in text
    assert "セルB" in text


def test_extract_docx_text_empty() -> None:
    from docx import Document

    buf = BytesIO()
    Document().save(buf)
    # 空 docx は空文字（空白除去後）
    assert extract_docx_text(buf.getvalue()) == ""


# -----------------------------------------------------------
# pptx
# -----------------------------------------------------------
def _build_sample_pptx() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank_layout = prs.slide_layouts[5]  # Title Only
    s1 = prs.slides.add_slide(blank_layout)
    s1.shapes.title.text = "スライド1のタイトル"
    # text box を足す
    tb = s1.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    tb.text_frame.text = "ボディテキスト1"
    s2 = prs.slides.add_slide(blank_layout)
    s2.shapes.title.text = "スライド2のタイトル"
    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_extract_pptx_pages_returns_per_slide_text() -> None:
    pages = extract_pptx_pages(_build_sample_pptx())
    assert len(pages) == 2
    nums = [n for n, _ in pages]
    assert nums == [1, 2]
    s1_text = pages[0][1]
    assert "スライド1のタイトル" in s1_text
    assert "ボディテキスト1" in s1_text
    assert "スライド2のタイトル" in pages[1][1]


def test_extract_pptx_pages_skips_empty_slides() -> None:
    from pptx import Presentation

    prs = Presentation()
    # 空 slide を 1 枚だけ追加（空 layout）
    prs.slides.add_slide(prs.slide_layouts[6])  # Blank
    buf = BytesIO()
    prs.save(buf)
    assert extract_pptx_pages(buf.getvalue()) == []


# -----------------------------------------------------------
# xlsx
# -----------------------------------------------------------
def _build_sample_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Sheet1"
    ws1["A1"] = "顧客名"
    ws1["B1"] = "金額"
    ws1["A2"] = "株式会社X"
    ws1["B2"] = 1000
    ws2 = wb.create_sheet("Sheet2")
    ws2["A1"] = "備考"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_extract_xlsx_pages_returns_per_sheet_text() -> None:
    pages = extract_xlsx_pages(_build_sample_xlsx())
    assert len(pages) == 2
    nums = [n for n, _ in pages]
    assert nums == [1, 2]
    assert "顧客名" in pages[0][1]
    assert "株式会社X" in pages[0][1]
    assert "1000" in pages[0][1]
    assert "備考" in pages[1][1]


def test_extract_xlsx_pages_skips_empty_sheet() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Empty"
    buf = BytesIO()
    wb.save(buf)
    assert extract_xlsx_pages(buf.getvalue()) == []


# -----------------------------------------------------------
# extract_office_pages dispatcher
# -----------------------------------------------------------
def test_extract_office_pages_dispatches_docx() -> None:
    pages = extract_office_pages(_build_sample_docx(), mime_type=DOCX_MIME)
    assert len(pages) == 1
    assert pages[0][0] == 1
    assert "見出し" in pages[0][1]


def test_extract_office_pages_dispatches_pptx() -> None:
    pages = extract_office_pages(_build_sample_pptx(), mime_type=PPTX_MIME)
    assert len(pages) == 2


def test_extract_office_pages_dispatches_xlsx() -> None:
    pages = extract_office_pages(_build_sample_xlsx(), mime_type=XLSX_MIME)
    assert len(pages) == 2


def test_extract_office_pages_unsupported_mime_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported office mime type"):
        extract_office_pages(b"", mime_type="application/octet-stream")


def test_office_binary_mimes_constant_matches_individual() -> None:
    assert DOCX_MIME in OFFICE_BINARY_MIMES
    assert PPTX_MIME in OFFICE_BINARY_MIMES
    assert XLSX_MIME in OFFICE_BINARY_MIMES
    assert GDOC_NATIVE_MIME not in OFFICE_BINARY_MIMES
