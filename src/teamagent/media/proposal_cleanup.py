"""ローカルと隔離 worker 共通の、提案書の差し込み内容の整形。"""

from __future__ import annotations

import re
from typing import Any

REVIEW_NOTES_HEADER = "要確認一覧（自動生成）"
_NUMERIC = re.compile(r"[｛{]\s*(\d+)\s*[:：]?[^｝}]*[｝}]")
_AUXILIARY = re.compile(r"\{\{(PB-[A-Z0-9_-]{1,60})\}\}")
_ROUND_RECT_ADJ = './/p:spPr/a:prstGeom[@prst="roundRect"]/a:avLst/a:gd[@name="adj"]'
_SAFE_SYMBOLS = str.maketrans(
    {
        **{chr(0x2776 + index): str(index + 1) for index in range(10)},
        "☒": "x",
    }
)


def prepare_proposal_content(
    presentation: Any,
    placeholders: dict[int, str],
    auxiliary: dict[str, str],
) -> tuple[dict[int, str], dict[str, str]]:
    """装飾を触らず、不足の理由は各ページのノートに保存して本文を空にする。"""
    values = {key: value.translate(_SAFE_SYMBOLS) for key, value in placeholders.items()}
    aux_values = {key: value.translate(_SAFE_SYMBOLS) for key, value in auxiliary.items()}
    for slide in presentation.slides:
        # 統合FMTの roundRect に範囲外の角丸値があり、表示系によって形が崩れる。
        for guide in slide._element.xpath(_ROUND_RECT_ADJ):
            match = re.fullmatch(r"val (-?\d+)", guide.get("fmla", ""))
            if match and not 0 <= int(match.group(1)) <= 50000:
                guide.set("fmla", f"val {max(0, min(50000, int(match.group(1))))}")
        combined = "\n".join(_shape_texts(slide.shapes))
        numeric_ids = {int(match.group(1)) for match in _NUMERIC.finditer(combined)}
        aux_keys = {match.group(1) for match in _AUXILIARY.finditer(combined)}
        reviews = [
            f"枠 {key}: {values[key]}"
            for key in sorted(numeric_ids & values.keys())
            if "要確認" in values[key]
        ]
        reviews.extend(
            f"{key}: {aux_values[key]}"
            for key in sorted(aux_keys & aux_values.keys())
            if "要確認" in aux_values[key]
        )
        if reviews:
            frame = slide.notes_slide.notes_text_frame
            existing = frame.text.rstrip()
            frame.text = (existing + "\n\n" if existing else "") + (
                REVIEW_NOTES_HEADER + "\n" + "\n".join(reviews)
            )
        if {46, 47, 56} & numeric_ids:
            # shortcut: 統合FMTの洗濯機見本だけを対象、新FMTでは画像slot契約に移す。
            for shape in list(slide.shapes):
                if (
                    shape.shape_type == 13
                    and "洗濯機" in shape._element.nvPicPr.cNvPr.get("descr", "")
                    and shape.left == 0
                    and shape.top == 0
                    and shape.width == presentation.slide_width
                    and shape.height == presentation.slide_height
                ):
                    element = shape._element
                    element.getparent().remove(element)
    return (
        {key: "" if "要確認" in value else value for key, value in values.items()},
        {key: "" if "要確認" in value else value for key, value in aux_values.items()},
    )


def _shape_texts(shapes: Any) -> list[str]:
    texts = []
    for shape in shapes:
        if shape.shape_type == 6:
            texts.extend(_shape_texts(shape.shapes))
        elif getattr(shape, "has_table", False):
            texts.extend(
                "".join(p.text for p in cell.text_frame.paragraphs)
                for row in shape.table.rows
                for cell in row.cells
            )
        elif getattr(shape, "has_text_frame", False):
            texts.append("".join(p.text for p in shape.text_frame.paragraphs))
    return texts
