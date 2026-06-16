"""docx / pptx / xlsx と Google native gdoc のテキスト抽出 + ページ化ユーティリティ。

ingest パイプライン用の純粋関数群。pdf_extract.py と同じ I/F を提供する
（pipeline 側が ``chunk_pages`` を再利用できるように ``[(page_num, text), ...]`` で返す）。

依存（python-docx / python-pptx / openpyxl）は遅延 import — CI で未インストールでも
モジュール import は通る。pyproject に依存記載済み（>=1.1.0 / >=1.0.0 / >=3.1.0）。

Usage:
    from teamagent.ingest.office_extract import extract_office_pages, OFFICE_BINARY_MIMES

    pages = extract_office_pages(data, mime_type="...pptx")
    # docx → [(1, "doc text...")]
    # pptx → [(1, "slide1..."), (2, "slide2..."), ...]
    # xlsx → [(1, "sheet1 text..."), (2, "sheet2 text..."), ...]
"""

from __future__ import annotations

import re
from io import BytesIO

import structlog

logger = structlog.get_logger(__name__)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GDOC_NATIVE_MIME = "application/vnd.google-apps.document"

# pipeline 側の mime_type 判定用にひとまとめで export。
OFFICE_BINARY_MIMES = frozenset({DOCX_MIME, PPTX_MIME, XLSX_MIME})

# xlsx 抽出時に拾うセル数の上限ガード（極端に大きい sheet で OOM 防止）。
_XLSX_MAX_CELLS_PER_SHEET = 10000


def _normalize_text(text: str) -> str:
    """NUL バイト除去 + 連続空白を 1 スペースに圧縮（pdf_extract.py と同じ流儀）."""
    if not text:
        return ""
    # NUL は postgres TEXT で禁止
    text = text.replace("\x00", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_docx_text(data: bytes) -> str:
    """docx → 単一テキスト。paragraph + table セルを順に連結."""
    from docx import Document  # python-docx（lazy import）

    document = Document(BytesIO(data))
    parts: list[str] = []
    for para in document.paragraphs:
        if para.text:
            parts.append(para.text)
    # 表セルも本文として拾う（書式は捨てる）
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return _normalize_text("\n".join(parts))


def extract_pptx_pages(data: bytes) -> list[tuple[int, str]]:
    """pptx → slide ごとのテキスト。空 slide は除外."""
    from pptx import Presentation  # python-pptx（lazy import）

    prs = Presentation(BytesIO(data))
    out: list[tuple[int, str]] = []
    for i, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        if run.text:
                            parts.append(run.text)
            elif hasattr(shape, "text") and getattr(shape, "text", None):
                parts.append(shape.text)
        text = _normalize_text("\n".join(parts))
        if text:
            out.append((i, text))
    return out


def extract_xlsx_pages(data: bytes) -> list[tuple[int, str]]:
    """xlsx → sheet ごとのテキスト。空 sheet・空セルは除外.

    1 sheet あたり最大 ``_XLSX_MAX_CELLS_PER_SHEET`` セルまで（OOM ガード）。
    上限に達した場合は WARN ログを出して以降のセルは捨てる。
    ``data_only=True`` で式の評価結果を取得（最後に Excel が保存した値）。
    ``read_only=True`` でストリーミング読込（メモリ削減）。
    """
    from openpyxl import load_workbook  # lazy import

    wb = load_workbook(BytesIO(data), data_only=True, read_only=True)
    out: list[tuple[int, str]] = []
    for i, ws in enumerate(wb.worksheets, start=1):
        parts: list[str] = []
        cell_count = 0
        truncated = False
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if v is None:
                    continue
                s = str(v).strip()
                if not s:
                    continue
                parts.append(s)
                cell_count += 1
                if cell_count >= _XLSX_MAX_CELLS_PER_SHEET:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            logger.warning(
                "xlsx_sheet_truncated",
                sheet_index=i,
                sheet_title=ws.title,
                max_cells=_XLSX_MAX_CELLS_PER_SHEET,
            )
        text = _normalize_text(" ".join(parts))
        if text:
            out.append((i, text))
    return out


def extract_office_pages(data: bytes, mime_type: str) -> list[tuple[int, str]]:
    """mime_type に応じて office 抽出器を dispatch.

    docx → [(1, text)]（page 概念なし→ 1 ページ扱い）
    pptx → [(slide_num, text), ...]
    xlsx → [(sheet_idx, text), ...]

    pdf_extract.extract_pdf_pages と同じ I/F なので chunk_pages を再利用できる。
    """
    if mime_type == DOCX_MIME:
        text = extract_docx_text(data)
        return [(1, text)] if text else []
    if mime_type == PPTX_MIME:
        return extract_pptx_pages(data)
    if mime_type == XLSX_MIME:
        return extract_xlsx_pages(data)
    raise ValueError(f"Unsupported office mime type: {mime_type}")


__all__ = [
    "DOCX_MIME",
    "GDOC_NATIVE_MIME",
    "OFFICE_BINARY_MIMES",
    "PPTX_MIME",
    "XLSX_MIME",
    "extract_docx_text",
    "extract_office_pages",
    "extract_pptx_pages",
    "extract_xlsx_pages",
]
