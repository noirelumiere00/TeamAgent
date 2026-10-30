"""ローカルと隔離 worker 共通の、提案書の差し込み内容の整形。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from typing import Any

from teamagent.media.proposal_template import BU2_AUXILIARY_KEYS

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
# 実FMTの固定指示は artifacts/deck-gates-20261022/fmt-instruction-inventory.json に記録。
_UNFINISHED = re.compile(
    r"[〇○◯]{2,}|[×✕]{2,}|・{3,}|\.{3,}|\b(?:TBD|TODO)\b|要確認|"
    r"[｛{][^｝}]+[｝}]|貼り付け|※該当記事の引用|ここに画像|"
    r"自動入力|はめ込|転記|差し替え|インサイト調査データ記事引用",
    re.IGNORECASE,
)
_BEAUTY_SECTOR = re.compile(r"美容|化粧品|コスメ|ヘルスケア|beauty|cosmetic|health\s*care", re.I)
_MARGIN = re.compile(r"[^。\n]*貴社マージンを乗せて[^。\n]*[。]?")
_EMME_TITLE = re.compile(r"^｜(?:(?:美容|ライフスタイル)メディア[：:]EMME|EMMEコンテンツイメージ)")


def append_review_note(slide: Any, reason: str) -> None:
    """既存の発表者メモを保持し、自動生成部分に追記する。"""
    frame = slide.notes_slide.notes_text_frame
    existing = frame.text.rstrip()
    if REVIEW_NOTES_HEADER not in existing:
        existing += ("\n\n" if existing else "") + REVIEW_NOTES_HEADER
    frame.text = existing + "\n" + reason


def proposal_template_samples(presentation: Any) -> dict[int, set[str]]:
    """固定の実績紹介と区別し、FMTのキャプション見本本文を保存する。"""
    return {
        slide.slide_id: {
            normalized
            for frame in _text_frames(slide.shapes)
            if re.search(r"【キャプション例】|【商品紹介】", frame.text)
            for text in [frame.text, *(p.text for p in frame.paragraphs)]
            if len(normalized := _normalize(text)) >= 20 and not _NUMERIC.search(text)
        }
        for slide in presentation.slides
    }


def proposal_emme_slide_ids(presentation: Any) -> set[int]:
    """元FMTの媒体見出しで識別し、生成本文中の媒体名で頁を消さない。"""
    return {
        slide.slide_id
        for slide in presentation.slides
        if any(_EMME_TITLE.search(text.strip()) for text in _shape_texts(slide.shapes))
    }


def proposal_bu2_slide_ids(presentation: Any) -> set[int]:
    """元FMTの見出し・枠でBU2の除外頁を決め、生成本文や表示順番号に依存しない。"""
    slides = list(presentation.slides)
    texts = [_shape_texts(slide.shapes) for slide in slides]
    headings = [{_normalize(text).replace("５", "5") for text in page} for page in texts]
    excluded = {
        slide.slide_id
        for slide, page in zip(slides, headings, strict=True)
        if page
        & {
            "｜ショート動画を成果につなげる5つのポイント",
            "｜5つのポイントが連動して、売上エンジンになる",
        }
    }
    starts = [i for i, page in enumerate(headings) if "02|PRワードの開発" in page]
    ends = [i for i, page in enumerate(headings) if "03|コミュニケーション戦略" in page]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return excluded
    start, end = starts[0], ends[0]
    ids = {
        i: {int(match.group(1)) for text in texts[i] for match in _NUMERIC.finditer(text)}
        for i in range(start, end)
    }
    summaries = [i for i, page_ids in ids.items() if {43, 44, 45} <= page_ids]
    proposals = [i for i, page_ids in ids.items() if {46, 47, 56} <= page_ids]
    # shortcut: 統合FMT v1の章末2頁を保持する。別構成は削除せず、新FMT時に契約を更新する。
    if summaries == [end - 2] and proposals == [end - 1]:
        excluded.update(slide.slide_id for slide in slides[start : end - 2])
    return excluded


def proposal_bu1_slide_ids(presentation: Any) -> set[int]:
    """Identify the three BU2-only pages from their original auxiliary fields."""
    return {
        slide.slide_id
        for slide in presentation.slides
        if any(
            match.group(1) in BU2_AUXILIARY_KEYS
            for text in _shape_texts(slide.shapes)
            for match in _AUXILIARY.finditer(text)
        )
    }


def proposal_excluded_slide_ids(
    *,
    emme_slide_ids: set[int],
    bu2_slide_ids: set[int],
    bu1_slide_ids: set[int] | None,
    business_unit: str,
    product_sector: str,
) -> set[int]:
    excluded = set(bu2_slide_ids) if business_unit == "BU2" else set(bu1_slide_ids or ())
    if product_sector.strip() and not _BEAUTY_SECTOR.search(product_sector):
        excluded.update(emme_slide_ids)
    return excluded


def finalize_proposal_content(
    presentation: Any,
    samples: dict[int, set[str]],
    *,
    emme_slide_ids: set[int],
    bu2_slide_ids: set[int],
    bu1_slide_ids: set[int] | None = None,
    business_unit: str = "BU2",
    product_sector: str = "",
    proposal_date: date | None = None,
    posting_start_date: date | None = None,
    specified_periods: tuple[str, ...] = (),
    agency_route: bool = True,
) -> None:
    """出荷前に本文を清掃し、業種選択・日付・見本・文字数をノートへ記録する。"""
    from teamagent.media.operations import _set_text_keeping_format
    from teamagent.media.proposal_dates import (
        is_schedule_heading,
        proposal_today,
        schedule_warnings,
        undated_schedule_text,
    )

    excluded = proposal_excluded_slide_ids(
        emme_slide_ids=emme_slide_ids,
        bu2_slide_ids=bu2_slide_ids,
        bu1_slide_ids=bu1_slide_ids,
        business_unit=business_unit,
        product_sector=product_sector,
    )
    emme = [slide for slide in presentation.slides if slide.slide_id in emme_slide_ids]
    if emme and not product_sector.strip():
        append_review_note(emme[0], "警告: 媒体選択 要確認（業種未指定のためEMMEを保持）")

    # 入力FMTの契約検査後に除外する。残るslide_id・装飾・座標は維持する。
    for slide_id in list(presentation.slides._sldIdLst):
        if int(slide_id.id) in excluded:
            presentation.part.drop_rel(slide_id.rId)
            presentation.slides._sldIdLst.remove(slide_id)

    for slide in presentation.slides:
        frames = list(_text_frames(slide.shapes))
        schedule_label = next(
            (
                frame.text
                for frame in frames
                if not hasattr(frame._parent, "_tc") and is_schedule_heading(frame.text)
            ),
            "",
        )
        for frame in frames:
            if posting_start_date is None:
                field_label = schedule_label
                cell = getattr(frame._parent, "_tc", None)
                if cell is not None:
                    row = cell.getparent()
                    column = row.xpath("./a:tc").index(cell)
                    header = row.getparent().xpath("./a:tr")[0].xpath("./a:tc")[column]
                    field_label += " " + " ".join(
                        node.text or ""
                        for part in (header, row[0])
                        for node in part.xpath(".//a:t")
                    )
                # 本文の run には生の改行が入ることがある（事例一覧など）。段落区切りと混ぜない。
                originals = [p.text.replace("\n", "\v") for p in frame.paragraphs]
                replaced = undated_schedule_text(
                    "\n".join(originals),
                    field_label=field_label,
                    specified_periods=specified_periods,
                )
                for paragraph, original, text in zip(
                    frame.paragraphs, originals, replaced.split("\n"), strict=True
                ):
                    if text != original:
                        _set_text_keeping_format([paragraph], text, 0)
            text = "".join(p.text for p in frame.paragraphs)
            if not text:
                continue
            if not agency_route and "貴社マージンを乗せて" in text:
                text = _MARGIN.sub("", text)
                text = re.sub(r"[・、]?ネット(?:です|になります)[。]?", "", text).rstrip("。")
                _set_text_keeping_format(list(frame.paragraphs), text, 0)
            if _empty_image_label(text) and _image_slot_filled(slide, frame):
                frame.clear()
                continue
            if _UNFINISHED.search(text) or _empty_image_label(text):
                append_review_note(slide, f"要確認（FMT指示文・未完成枠）: {text}")
                frame.clear()
                continue
            original = samples.get(slide.slide_id, set())
            if any(_normalize(t) in original for t in [text, *(p.text for p in frame.paragraphs)]):
                append_review_note(slide, f"警告: FMT見本未上書き 要確認: {text[:80]}")

        count = sum(len(re.sub(r"\s", "", text)) for text in _shape_texts(slide.shapes))
        if count > 500:
            append_review_note(slide, f"警告: 本文500字超過 要確認（{count}字）")

    for number, reason in schedule_warnings(
        presentation, proposal_date or proposal_today(), posting_start_date=posting_start_date
    ):
        append_review_note(presentation.slides[number - 1], f"警告: 日付 要確認（{reason}）")


def _normalize(text: str) -> str:
    return re.sub(r"\s", "", text)


def _empty_image_label(text: str) -> bool:
    normalized = _normalize(text)
    return (
        len(normalized) <= 40
        and normalized.endswith("画像")
        and not re.search(r"[。！？!?]", normalized)
    )


def _image_slot_filled(slide: Any, frame: Any) -> bool:
    shape = frame._parent
    if not hasattr(shape, "left"):
        return False
    return any(
        picture.shape_type == 13
        and shape.left <= picture.left
        and shape.top <= picture.top
        and picture.left + picture.width <= shape.left + shape.width
        and picture.top + picture.height <= shape.top + shape.height
        for picture in slide.shapes
    )


def _text_frames(shapes: Any) -> Iterator[Any]:
    for shape in shapes:
        if shape.shape_type == 6:
            yield from _text_frames(shape.shapes)
        elif getattr(shape, "has_table", False):
            for row in shape.table.rows:
                for cell in row.cells:
                    yield cell.text_frame
        elif getattr(shape, "has_text_frame", False):
            yield shape.text_frame


def prepare_proposal_content(
    presentation: Any,
    placeholders: dict[int, str],
    auxiliary: dict[str, str],
    source_notes: dict[str, str] | None = None,
) -> tuple[dict[int, str], dict[str, str]]:
    """条件欄は相談表示、他の不足欄は空欄化し、理由はノートへ残す。未使用枠だけ除去する。"""
    values = {key: value.translate(_SAFE_SYMBOLS) for key, value in placeholders.items()}
    aux_values = {key: value.translate(_SAFE_SYMBOLS) for key, value in auxiliary.items()}
    unused_hearing = {
        key
        for key, value in aux_values.items()
        if key in BU2_AUXILIARY_KEYS
        and key.startswith("PB-BU2-HEARING-")
        and value == ""
        and key in (source_notes or {})
    }
    for slide in presentation.slides:
        # 統合FMTの roundRect に範囲外の角丸値があり、表示系によって形が崩れる。
        for guide in slide._element.xpath(_ROUND_RECT_ADJ):
            match = re.fullmatch(r"val (-?\d+)", guide.get("fmla", ""))
            if match and not 0 <= int(match.group(1)) <= 50000:
                guide.set("fmla", f"val {max(0, min(50000, int(match.group(1))))}")
        combined = "\n".join(_shape_texts(slide.shapes))
        numeric_ids = {int(match.group(1)) for match in _NUMERIC.finditer(combined)}
        aux_keys = {match.group(1) for match in _AUXILIARY.finditer(combined)}
        sources = [
            reason for key, reason in sorted((source_notes or {}).items()) if key in aux_keys
        ]
        if sources:
            frame = slide.notes_slide.notes_text_frame
            existing = frame.text.rstrip()
            frame.text = (existing + "\n\n" if existing else "") + (
                "入力条件・補足（自動生成）\n" + "\n".join(sources)
            )
        for key in aux_keys:
            if key not in unused_hearing and not aux_values.get(key, "").strip():
                aux_values[key] = "要確認（データ未検出）"
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
        reviews.extend(reason for reason in sources if "要確認" in reason)
        if reviews:
            append_review_note(slide, "\n".join(reviews))
        for shape in list(slide.shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            match = _AUXILIARY.fullmatch(shape.text.strip())
            if match:
                key = match.group(1)
                if (
                    key in BU2_AUXILIARY_KEYS
                    and key.startswith("PB-BU2-HEARING-")
                    and key in unused_hearing
                ):
                    element = shape._element
                    element.getparent().remove(element)
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
        {
            key: (
                "\n".join(
                    "条件未指定・要相談" if "要確認" in line else line
                    for line in value.splitlines()
                )
                if key in BU2_AUXILIARY_KEYS
                else ""
                if "要確認" in value
                else value
            )
            for key, value in aux_values.items()
        },
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
