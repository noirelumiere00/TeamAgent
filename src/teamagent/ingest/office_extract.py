"""docx / pptx / xlsx と Google native gdoc のテキスト抽出 + ページ化ユーティリティ。

ingest パイプライン用の純粋関数群。pdf_extract.py と同じ I/F を提供する
（pipeline 側が ``chunk_pages`` を再利用できるように ``[(page_num, text), ...]`` で返す）。

DOCX/XLSXの依存（python-docx / openpyxl）は遅延importする。PPTXはcoreから
python-pptxを除外できるよう、size/relationship/DTD/entity制限付きのdefusedxmlで
必要なOOXML text partだけを読む。描画用python-pptxはmedia extraだけに置く。

Usage:
    from teamagent.ingest.office_extract import extract_office_pages, OFFICE_BINARY_MIMES

    pages = extract_office_pages(data, mime_type="...pptx")
    # docx → [(1, "doc text...")]
    # pptx → [(1, "slide1..."), (2, "slide2..."), ...]
    # xlsx → [(1, "sheet1 text..."), (2, "sheet2 text..."), ...]
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from io import BytesIO
from pathlib import PurePosixPath
from typing import cast
from xml.etree import ElementTree

import structlog
from defusedxml import ElementTree as DefusedElementTree
from defusedxml.common import DefusedXmlException

logger = structlog.get_logger(__name__)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GDOC_NATIVE_MIME = "application/vnd.google-apps.document"

# pipeline 側の mime_type 判定用にひとまとめで export。
OFFICE_BINARY_MIMES = frozenset({DOCX_MIME, PPTX_MIME, XLSX_MIME})

_OOXML_REQUIRED_PART = {
    DOCX_MIME: "word/document.xml",
    PPTX_MIME: "ppt/presentation.xml",
    XLSX_MIME: "xl/workbook.xml",
}
_ZIP_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OOXML_MAX_MEMBERS = 10_000
_OOXML_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_OOXML_MAX_XML_PART_BYTES = 8 * 1024 * 1024


class OfficePayloadError(zipfile.BadZipFile):
    """抽出前に判別できる Office payload 不正。

    ``category`` は運用ログで機械集計する安定値:

    - ``html_response``: Drive 本体ではなく HTML 応答
    - ``truncated_download`` / ``size_mismatch``: Drive metadata の size と不一致
    - ``corrupt_zip``: ZIP シグネチャはあるが中央ディレクトリ等が壊れている
    - ``format_mismatch``: Office MIME と実体の OOXML package が一致しない

    ``BadZipFile`` の subclass にして、既存の fail-open 呼び出し側との互換性を保つ。
    """

    def __init__(
        self,
        category: str,
        *,
        mime_type: str,
        actual_bytes: int,
        expected_bytes: int | None = None,
    ) -> None:
        self.category = category
        self.mime_type = mime_type
        self.actual_bytes = actual_bytes
        self.expected_bytes = expected_bytes
        super().__init__(f"invalid Office payload: {category}")


def _looks_like_html(data: bytes) -> bool:
    """BOM/空白付きも含め、確認画面・エラーページの HTML 応答を判定する。"""
    head = data[:512].lstrip()
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip()
    lowered = head.lower()
    return (
        lowered.startswith(b"<!doctype html")
        or lowered.startswith(b"<html")
        or (lowered.startswith(b"<?xml") and b"<html" in lowered)
    )


def _validate_office_payload(
    data: bytes,
    *,
    mime_type: str,
    expected_size: int | None,
) -> None:
    """OOXML を開く前に、既知の非抽出 payload を安全な分類例外へ変換する。"""
    actual_size = len(data)

    if _looks_like_html(data):
        raise OfficePayloadError(
            "html_response",
            mime_type=mime_type,
            actual_bytes=actual_size,
            expected_bytes=expected_size,
        )

    if expected_size is not None and actual_size != expected_size:
        category = "truncated_download" if actual_size < expected_size else "size_mismatch"
        raise OfficePayloadError(
            category,
            mime_type=mime_type,
            actual_bytes=actual_size,
            expected_bytes=expected_size,
        )

    stream = BytesIO(data)
    if not zipfile.is_zipfile(stream):
        category = "corrupt_zip" if data.startswith(_ZIP_PREFIXES) else "format_mismatch"
        raise OfficePayloadError(
            category,
            mime_type=mime_type,
            actual_bytes=actual_size,
            expected_bytes=expected_size,
        )

    required_part = _OOXML_REQUIRED_PART[mime_type]
    try:
        with zipfile.ZipFile(stream) as package:
            members = package.infolist()
            if (
                len(members) > _OOXML_MAX_MEMBERS
                or sum(member.file_size for member in members) > _OOXML_MAX_UNCOMPRESSED_BYTES
            ):
                raise OfficePayloadError(
                    "format_mismatch",
                    mime_type=mime_type,
                    actual_bytes=actual_size,
                    expected_bytes=expected_size,
                )
            has_required_part = required_part in package.namelist()
    except OfficePayloadError:
        raise
    except zipfile.BadZipFile as exc:
        raise OfficePayloadError(
            "corrupt_zip",
            mime_type=mime_type,
            actual_bytes=actual_size,
            expected_bytes=expected_size,
        ) from exc
    if not has_required_part:
        raise OfficePayloadError(
            "format_mismatch",
            mime_type=mime_type,
            actual_bytes=actual_size,
            expected_bytes=expected_size,
        )


# xlsx 抽出時に拾うセル数の上限ガード（極端に大きい sheet で OOM 防止）。
_XLSX_MAX_CELLS_PER_SHEET = 10000

_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS = {
    "a": _DRAWING_NS,
    "p": _PRESENTATION_NS,
    "r": _REL_NS,
    "pr": _PACKAGE_REL_NS,
}
_NOTES_NON_BODY_PLACEHOLDERS = {"dt", "ftr", "hdr", "sldImg", "sldNum"}


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


def _xml_root(package: zipfile.ZipFile, member: str) -> ElementTree.Element:
    try:
        info = package.getinfo(member)
    except KeyError as exc:
        raise zipfile.BadZipFile(f"missing OOXML part: {member}") from exc
    if info.file_size > _OOXML_MAX_XML_PART_BYTES:
        raise zipfile.BadZipFile(f"OOXML XML part exceeds size limit: {member}")
    with package.open(info) as stream:
        raw = stream.read(_OOXML_MAX_XML_PART_BYTES + 1)
    if len(raw) > _OOXML_MAX_XML_PART_BYTES:
        raise zipfile.BadZipFile(f"OOXML XML part exceeds size limit: {member}")
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise zipfile.BadZipFile(f"DTD/entity is forbidden in OOXML part: {member}")
    try:
        return cast(
            ElementTree.Element,
            DefusedElementTree.fromstring(
                raw,
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            ),
        )
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        raise zipfile.BadZipFile(f"invalid OOXML part: {member}") from exc


def _safe_ooxml_target(source: str, target: str) -> str:
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    if target.startswith("/"):
        normalized = posixpath.normpath(target.lstrip("/"))
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise zipfile.BadZipFile("OOXML relationship escaped package")
    return path.as_posix()


def _relationships(package: zipfile.ZipFile, source: str) -> dict[str, tuple[str, str]]:
    source_path = PurePosixPath(source)
    rels = (source_path.parent / "_rels" / f"{source_path.name}.rels").as_posix()
    if rels not in package.namelist():
        return {}
    root = _xml_root(package, rels)
    output: dict[str, tuple[str, str]] = {}
    for relation in root.findall("pr:Relationship", _NS):
        relation_id = relation.get("Id", "")
        target = relation.get("Target", "")
        relation_type = relation.get("Type", "")
        target_mode = relation.get("TargetMode", "")
        if not relation_id or not target or target_mode.lower() == "external":
            continue
        output[relation_id] = (_safe_ooxml_target(source, target), relation_type)
    return output


def _text_nodes(element: ElementTree.Element) -> list[str]:
    return [node.text for node in element.findall(".//a:t", _NS) if node.text]


def _collect_pptx_shapes(
    parent: ElementTree.Element,
    *,
    include_tables: bool,
    recurse_groups: bool,
) -> list[str]:
    parts: list[str] = []
    shape_tag = f"{{{_PRESENTATION_NS}}}sp"
    group_tag = f"{{{_PRESENTATION_NS}}}grpSp"
    graphic_tag = f"{{{_PRESENTATION_NS}}}graphicFrame"
    for child in list(parent):
        if child.tag == shape_tag:
            parts.extend(_text_nodes(child))
        elif child.tag == group_tag and recurse_groups:
            parts.extend(
                _collect_pptx_shapes(
                    child,
                    include_tables=include_tables,
                    recurse_groups=True,
                )
            )
        elif child.tag == graphic_tag and include_tables:
            table = child.find(".//a:tbl", _NS)
            if table is not None:
                parts.extend(_text_nodes(table))
    return parts


def _ordered_slide_parts(package: zipfile.ZipFile) -> list[str]:
    presentation = _xml_root(package, "ppt/presentation.xml")
    relationships = _relationships(package, "ppt/presentation.xml")
    paths: list[str] = []
    for slide_id in presentation.findall("./p:sldIdLst/p:sldId", _NS):
        relation_id = slide_id.get(f"{{{_REL_NS}}}id", "")
        target = relationships.get(relation_id)
        if target is None or not target[1].endswith("/slide"):
            raise zipfile.BadZipFile("presentation slide relationship is missing")
        paths.append(target[0])
    return paths


def _notes_text(package: zipfile.ZipFile, slide_path: str) -> list[str]:
    relationships = _relationships(package, slide_path)
    notes_path = next(
        (
            target
            for target, relation_type in relationships.values()
            if relation_type.endswith("/notesSlide")
        ),
        None,
    )
    if notes_path is None:
        return []
    root = _xml_root(package, notes_path)
    parts: list[str] = []
    for shape in root.findall("./p:cSld/p:spTree/p:sp", _NS):
        placeholder = shape.find("./p:nvSpPr/p:nvPr/p:ph", _NS)
        placeholder_type = placeholder.get("type", "") if placeholder is not None else ""
        if placeholder_type in _NOTES_NON_BODY_PLACEHOLDERS:
            continue
        parts.extend(_text_nodes(shape))
    return parts


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
    recurse_groups = include_notes or include_tables
    out: list[tuple[int, str]] = []
    with zipfile.ZipFile(BytesIO(data)) as package:
        for index, slide_path in enumerate(_ordered_slide_parts(package), start=1):
            slide = _xml_root(package, slide_path)
            shape_tree = slide.find("./p:cSld/p:spTree", _NS)
            parts = (
                _collect_pptx_shapes(
                    shape_tree,
                    include_tables=include_tables,
                    recurse_groups=recurse_groups,
                )
                if shape_tree is not None
                else []
            )
            if include_notes:
                parts.extend(_notes_text(package, slide_path))
            text = _normalize_text("\n".join(parts))
            if text:
                out.append((index, text))
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
    expected_size: int | None = None,
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

    * ``expected_size`` — Drive metadata の size。実バイト数との差を途中切れとして分類する。
    * ``include_notes`` / ``include_tables`` — pptx のノート・表セルも拾う
      （mime が pptx 以外なら無視）。
    * ``formula_fallback`` — xlsx でキャッシュ値が無い式 sheet の式文字列を拾う
      （mime が xlsx 以外なら無視）。
    * ``min_chars`` — ``>0`` のとき、結合テキスト長が ``min_chars`` 未満の
      ページを空扱いで落とす（極小テキストの偽「中身あり」防止）。全 mime 共通。

    例外は現行同様にそのまま上位へ伝播（pipeline 側で fail-open）。
    """
    if mime_type not in OFFICE_BINARY_MIMES:
        raise ValueError(f"Unsupported office mime type: {mime_type}")

    _validate_office_payload(data, mime_type=mime_type, expected_size=expected_size)

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
    if min_chars > 0:
        pages = [(num, text) for num, text in pages if len(text) >= min_chars]
    return pages


__all__ = [
    "DOCX_MIME",
    "GDOC_NATIVE_MIME",
    "OFFICE_BINARY_MIMES",
    "PPTX_MIME",
    "XLSX_MIME",
    "OfficePayloadError",
    "extract_docx_text",
    "extract_office_pages",
    "extract_pptx_pages",
    "extract_xlsx_pages",
]
