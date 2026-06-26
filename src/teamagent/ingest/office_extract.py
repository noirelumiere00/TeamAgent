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
import zipfile
from io import BytesIO

import structlog

logger = structlog.get_logger(__name__)


def _diagnose_non_zip(data: bytes, *, fmt: str) -> None:
    """OOXML(pptx/docx/xlsx=zip) を開く前の健全性診断。

    zip でないバイト列（DL途中切れで中央ディレクトリ欠落 / 確認 HTML 混入 / 真の破損）が
    後段で無音の ``BadZipFile`` になる前に、先頭バイトと実サイズを WARN に出して原因を切り分け
    可能にする。修復はせず、判定が False のときだけログる（呼び出し側の例外処理は不変）。
    """
    if zipfile.is_zipfile(BytesIO(data)):
        return
    logger.warning(
        "office_not_zip",
        fmt=fmt,
        bytes=len(data),
        head=data[:16].decode("latin-1", "replace"),
    )


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GDOC_NATIVE_MIME = "application/vnd.google-apps.document"

# pipeline 側の mime_type 判定用にひとまとめで export。
OFFICE_BINARY_MIMES = frozenset({DOCX_MIME, PPTX_MIME, XLSX_MIME})

# xlsx 抽出時に拾うセル数の上限ガード（極端に大きい sheet で OOM 防止）。
_XLSX_MAX_CELLS_PER_SHEET = 10000

# python-pptx の GROUP shape type 値（MSO_SHAPE_TYPE.GROUP == 6）。
# 列挙体を import せずに数値で比較する（renderer.py:27-28 と同じ流儀）。
_GROUP_SHAPE_TYPE = 6


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


def _collect_shape_text(
    shapes: object,
    parts: list[str],
    *,
    include_tables: bool,
    recurse_groups: bool,
) -> None:
    """shape 群を走査して本文テキストを ``parts`` へ追記.

    ``include_tables=True`` のとき ``shape.has_table`` のセル文字列も拾う。
    ``recurse_groups=True`` のとき group shape（``shape_type ==
    _GROUP_SHAPE_TYPE``）の子 shape を再帰する。両 flag False のときは
    従来の非再帰・本文 run のみの挙動と完全一致する。
    renderer.py:56-73 の walk 実装を参考にした。
    """
    for shape in shapes:  # type: ignore[attr-defined]
        # group shape は（要求時のみ）子 shape を再帰
        if recurse_groups and getattr(shape, "shape_type", None) == _GROUP_SHAPE_TYPE:
            _collect_shape_text(
                shape.shapes,
                parts,
                include_tables=include_tables,
                recurse_groups=recurse_groups,
            )
            continue
        # 表セル（include_tables のときだけ）
        if include_tables and getattr(shape, "has_table", False):
            for row in shape.table.rows:
                for cell in row.cells:
                    if cell.text:
                        parts.append(cell.text)
            continue
        if getattr(shape, "has_text_frame", False):
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    if run.text:
                        parts.append(run.text)
        elif hasattr(shape, "text") and getattr(shape, "text", None):
            parts.append(shape.text)


def extract_pptx_pages(
    data: bytes,
    *,
    include_notes: bool = False,
    include_tables: bool = False,
) -> list[tuple[int, str]]:
    """pptx → slide ごとのテキスト。空 slide は除外.

    既定（両 flag False）では従来挙動（本文 run のみ・非再帰）と完全一致。
    ``include_notes=True`` で各 slide のノート（``slide.notes_slide``）本文を、
    ``include_tables=True`` で表セル文字列を追加で拾う。
    いずれかの flag が True のときだけ group shape を再帰し、子 shape の
    テキスト/表も拾う（従来は group 配下を取りこぼしていた）。
    """
    from pptx import Presentation  # python-pptx（lazy import）

    # 拡張要求があるときだけ group を再帰（既定は従来の非再帰挙動を維持）
    recurse_groups = include_notes or include_tables

    _diagnose_non_zip(data, fmt="pptx")  # 非zipなら原因(HTML/途中切れ/破損)を WARN（修復はしない）
    prs = Presentation(BytesIO(data))
    out: list[tuple[int, str]] = []
    for i, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        _collect_shape_text(
            slide.shapes,
            parts,
            include_tables=include_tables,
            recurse_groups=recurse_groups,
        )
        if include_notes and slide.has_notes_slide:
            notes = slide.notes_slide
            notes_tf = getattr(notes, "notes_text_frame", None)
            if notes_tf is not None and notes_tf.text:
                parts.append(notes_tf.text)
        text = _normalize_text("\n".join(parts))
        if text:
            out.append((i, text))
    return out


def _collect_sheet_cells(ws: object, sheet_index: int) -> list[str]:
    """1 worksheet を走査してセル文字列を返す（OOM ガード付き）.

    1 sheet あたり最大 ``_XLSX_MAX_CELLS_PER_SHEET`` セルまで。
    上限に達した場合は WARN ログを出して以降のセルは捨てる。
    """
    parts: list[str] = []
    cell_count = 0
    truncated = False
    for row in ws.iter_rows(values_only=True):  # type: ignore[attr-defined]
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
            sheet_index=sheet_index,
            sheet_title=ws.title,  # type: ignore[attr-defined]
            max_cells=_XLSX_MAX_CELLS_PER_SHEET,
        )
    return parts


def extract_xlsx_pages(
    data: bytes,
    *,
    formula_fallback: bool = False,
) -> list[tuple[int, str]]:
    """xlsx → sheet ごとのテキスト。空 sheet・空セルは除外.

    1 sheet あたり最大 ``_XLSX_MAX_CELLS_PER_SHEET`` セルまで（OOM ガード）。
    ``data_only=True`` で式の評価結果を取得（最後に Excel が保存した値）。
    ``read_only=True`` でストリーミング読込（メモリ削減）。

    ``formula_fallback=True`` のとき、``data_only=True`` で全セル None だった
    （＝キャッシュ値を持たない＝openpyxl 等で書かれた未評価の式 sheet）について
    のみ、``data_only=False`` で book を再ロードし式文字列（``=SUM(...)`` 等）を
    拾う。既定 False では従来挙動（キャッシュ値のみ）と完全一致。
    """
    from openpyxl import load_workbook  # lazy import

    wb = load_workbook(BytesIO(data), data_only=True, read_only=True)
    # data_only で空だった sheet を index→title で記録（fallback 用）
    empty_titles: dict[int, str] = {}
    out: list[tuple[int, str]] = []
    for i, ws in enumerate(wb.worksheets, start=1):
        parts = _collect_sheet_cells(ws, i)
        text = _normalize_text(" ".join(parts))
        if text:
            out.append((i, text))
        elif formula_fallback:
            empty_titles[i] = ws.title

    if formula_fallback and empty_titles:
        # data_only=False で式文字列を再取得（None だった sheet のみ）
        wb_raw = load_workbook(BytesIO(data), data_only=False, read_only=True)
        raw_sheets = wb_raw.worksheets
        for i, _title in empty_titles.items():
            ws_raw = raw_sheets[i - 1]  # worksheets 並びは安定
            parts = _collect_sheet_cells(ws_raw, i)
            text = _normalize_text(" ".join(parts))
            if text:
                out.append((i, text))
        # sheet index 昇順に整列（fallback 分を本来の位置へ）
        out.sort(key=lambda pair: pair[0])

    return out


def extract_office_pages(
    data: bytes,
    mime_type: str,
    *,
    include_notes: bool = False,
    include_tables: bool = False,
    formula_fallback: bool = False,
    min_chars: int = 0,
) -> list[tuple[int, str]]:
    """mime_type に応じて office 抽出器を dispatch.

    docx → [(1, text)]（page 概念なし→ 1 ページ扱い）
    pptx → [(slide_num, text), ...]
    xlsx → [(sheet_idx, text), ...]

    pdf_extract.extract_pdf_pages と同じ I/F なので chunk_pages を再利用できる。

    すべての追加 kwarg は後方互換（既定値は現行挙動と完全一致）:

    * ``include_notes`` / ``include_tables`` — pptx のノート・表セルも拾う
      （mime が pptx 以外なら無視）。
    * ``formula_fallback`` — xlsx でキャッシュ値が無い式 sheet の式文字列を拾う
      （mime が xlsx 以外なら無視）。
    * ``min_chars`` — ``>0`` のとき、結合テキスト長が ``min_chars`` 未満の
      ページを空扱いで落とす（極小テキストの偽「中身あり」防止）。全 mime 共通。

    例外は現行同様にそのまま上位へ伝播（pipeline 側で fail-open）。
    """
    if mime_type == DOCX_MIME:
        text = extract_docx_text(data)
        pages = [(1, text)] if text else []
    elif mime_type == PPTX_MIME:
        pages = extract_pptx_pages(
            data,
            include_notes=include_notes,
            include_tables=include_tables,
        )
    elif mime_type == XLSX_MIME:
        pages = extract_xlsx_pages(data, formula_fallback=formula_fallback)
    else:
        raise ValueError(f"Unsupported office mime type: {mime_type}")

    if min_chars > 0:
        pages = [(num, text) for num, text in pages if len(text) >= min_chars]
    return pages


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
