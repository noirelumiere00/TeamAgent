"""PPTX の表示順で、テンプレより増えた「要確認」と出力検査の警告を通知する。"""

from io import BytesIO
from zipfile import ZipFile

from teamagent.ingest.office_extract import (
    _NOTES_NON_BODY_PLACEHOLDERS,
    _NS,
    MAX_OFFICE_EXTRACTED_CHARACTERS,
    _ordered_slide_parts,
    _relationships,
    _shape_text,
    _TextBudget,
    _xml_root,
    extract_pptx_slide_shapes,
)
from teamagent.media.proposal_cleanup import REVIEW_NOTES_HEADER

UNAVAILABLE_WARNING = "要確認の位置は自動で数えられませんでした"
QUALITY_WARNING_PREFIXES = (
    "警告: FMT見本未上書き",
    "警告: 日付",
    "警告: 本文500字超過",
    "警告: 媒体選択",
)


def _review_notes(data: bytes) -> dict[int, str]:
    notes = {}
    budget = _TextBudget(MAX_OFFICE_EXTRACTED_CHARACTERS, None)
    with ZipFile(BytesIO(data)) as package:
        for number, slide_path in enumerate(_ordered_slide_parts(package), start=1):
            notes_path = next(
                (
                    target
                    for target, relation_type in _relationships(package, slide_path).values()
                    if relation_type.endswith("/notesSlide")
                ),
                None,
            )
            if notes_path is None:
                continue
            parts = []
            for shape in _xml_root(package, notes_path).findall("./p:cSld/p:spTree/p:sp", _NS):
                placeholder = shape.find("./p:nvSpPr/p:nvPr/p:ph", _NS)
                if (
                    placeholder is not None
                    and placeholder.get("type") in _NOTES_NON_BODY_PLACEHOLDERS
                ):
                    continue
                parts.append(_shape_text(shape, budget=budget))
            text = "\n".join(parts)
            if REVIEW_NOTES_HEADER in text:
                notes[number] = text.split(REVIEW_NOTES_HEADER, 1)[1]
    return notes


def _slide_ids(data: bytes) -> list[str]:
    with ZipFile(BytesIO(data)) as package:
        root = _xml_root(package, "ppt/presentation.xml")
        return [slide.get("id", "") for slide in root.findall("./p:sldIdLst/p:sldId", _NS)]


def count_review(data: bytes) -> dict[int, int]:
    counts = {
        number: sum(text.count("要確認") for text in shapes)
        for number, shapes in extract_pptx_slide_shapes(data)
    }
    for number, text in _review_notes(data).items():
        counts[number] += text.count("要確認")
    return counts


def review_slides(out_bytes: bytes, tpl_bytes: bytes) -> list[int]:
    baseline = count_review(tpl_bytes)
    output = count_review(out_bytes)
    baseline_by_id = dict(zip(_slide_ids(tpl_bytes), baseline.values(), strict=True))
    return sorted(
        number
        for (number, count), slide_id in zip(output.items(), _slide_ids(out_bytes), strict=True)
        if count > baseline_by_id.get(slide_id, 0)
    )


def quality_warnings(out_bytes: bytes) -> list[str]:
    """自動生成ノート内の検査警告だけを、表示順のページ番号付きで返す。"""
    extract_pptx_slide_shapes(out_bytes)
    warnings = []
    for number, notes in _review_notes(out_bytes).items():
        for line in notes.splitlines():
            for prefix in QUALITY_WARNING_PREFIXES:
                if prefix in line:
                    warnings.append(f"{number}枚目: {line[line.index(prefix) :].strip()}")
                    break
    return list(dict.fromkeys(warnings))


def format_slides(nums: list[int], limit: int = 10) -> str:
    ordered = sorted(set(nums))
    if not ordered:
        return ""
    text = "・".join(str(number) for number in ordered[:limit]) + "枚目"
    if len(ordered) > limit:
        text += f"ほか{len(ordered) - limit}枚"
    return text


def warning_lines(review: list[int] | None) -> list[str]:
    if review is None:
        return [UNAVAILABLE_WARNING]
    if not review:
        return []
    return [
        f"PowerPoint の左の一覧で {format_slides(review)}に『要確認』が残っています"
        "（確かめてから使ってください）"
    ]
