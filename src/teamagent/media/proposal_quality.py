"""統合提案書の欄仕様と、最終PPTXに対する完成検査（外部接続なし）。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from teamagent.media.proposal_cleanup import _BEAUTY_SECTOR, _empty_image_label, _text_frames
from teamagent.media.proposal_template import BU2_AUXILIARY_KEYS

_AUXILIARY = re.compile(r"\{\{(PB-[A-Z0-9_-]{1,60})\}\}")
# SHA 07ea4170…の実FMTで確認した (slide_id, shape_id)。
_INTEGRATED_IMAGE_FIELDS = {
    (293, 2),
    (295, 7),
    (295, 8),
    (295, 10),
    (296, 12),
    (296, 15),
    (296, 17),
    (327, 5),
}


def expected_proposal_slides(business_unit: str, product_sector: str) -> int:
    return (83 if business_unit == "BU1" else 72) - (
        4 if product_sector.strip() and not _BEAUTY_SECTOR.search(product_sector) else 0
    )


def proposal_field_spec(
    presentation: Any,
    *,
    template_profile: str = "base",
) -> dict[str, Any]:
    """置換前の固定FMTから貼り先・期待数を確定する。取得画像数には依存しない。"""
    auxiliary: list[tuple[int, str, Any]] = []
    images = []
    for slide in presentation.slides:
        for frame in _text_frames(slide.shapes):
            combined = "".join(paragraph.text for paragraph in frame.paragraphs)
            auxiliary.extend((slide.slide_id, key, frame) for key in _AUXILIARY.findall(combined))
    for slide, shape in iter_proposal_image_slots(presentation):
        images.append(
            {
                "slide_id": slide.slide_id,
                "shape_id": shape.shape_id,
                "left": int(shape.left),
                "top": int(shape.top),
                "width": int(shape.width),
                "height": int(shape.height),
                "expected": 1,
                "existing": {s.shape_id for s in _shapes(slide.shapes) if s.shape_type == 13},
            }
        )
    return {
        "auxiliary": auxiliary,
        "images": images,
        "image_spec_error": template_profile == "proposal-builder-v1"
        and {(field["slide_id"], field["shape_id"]) for field in images}
        != _INTEGRATED_IMAGE_FIELDS,
    }


def iter_proposal_image_slots(presentation: Any) -> Iterator[tuple[Any, Any]]:
    """画像明示のラベルで実FMTの貼り先を識別し、記事引用の本文枠は含めない。"""
    for slide in presentation.slides:
        candidates = []
        for shape in _shapes(slide.shapes):
            if shape.shape_type == 13:
                continue
            text = re.sub(r"\s", "", getattr(shape, "text", ""))
            labelled = _empty_image_label(text) or text.endswith("SNS投稿イメージなど貼り付け")
            if labelled or "ここに画像" in text:
                candidates.append(shape)
        for shape in sorted(candidates, key=lambda item: int(item.left)):
            yield slide, shape


def _shapes(shapes: Any) -> Iterator[Any]:
    for shape in shapes:
        if shape.shape_type == 6:
            yield from _shapes(shape.shapes)
        else:
            yield shape


def proposal_field_results(
    presentation: Any,
    spec: dict[str, Any],
    auxiliary: dict[str, str],
    source_notes: dict[str, str] | None,
    *,
    requested_images: int = 0,
) -> dict[str, Any]:
    slides = {
        slide.slide_id: (number, slide) for number, slide in enumerate(presentation.slides, 1)
    }
    issues = ["required_image_spec_missing"] if spec.get("image_spec_error") else []
    warnings = []
    condition_pages = set()
    for slide_id, key, frame in spec["auxiliary"]:
        if slide_id not in slides:
            continue
        value = auxiliary.get(key, "")
        if not value and key.startswith("PB-BU2-HEARING-") and key in (source_notes or {}):
            continue
        missing = not value.strip() or any(
            marker in value for marker in ("要確認", "条件未指定", "要相談")
        )
        if missing and key in BU2_AUXILIARY_KEYS:
            condition_pages.add(slides[slide_id][0])
        elif missing or re.sub(r"\s", "", value) not in re.sub(r"\s", "", frame.text):
            issues.append(f"auxiliary_missing:{key}")
    images = []
    for field in spec["images"]:
        if field["slide_id"] not in slides:
            continue
        number, slide = slides[field["slide_id"]]
        inserted = sum(
            shape.shape_type == 13
            and shape.shape_id not in field["existing"]
            and field["left"] <= shape.left
            and field["top"] <= shape.top
            and shape.left + shape.width <= field["left"] + field["width"]
            and shape.top + shape.height <= field["top"] + field["height"]
            for shape in _shapes(slide.shapes)
        )
        images.append(
            {
                "slide": number,
                "shape_id": field["shape_id"],
                "expected": field["expected"],
                "inserted": inserted,
            }
        )
    if condition_pages:
        pages = "・".join(str(page) for page in sorted(condition_pages))
        warnings.append(f"要対応: 条件未指定・要相談（{pages}枚目）")
    result = {
        "field_issues": sorted(set(issues)),
        "field_warnings": warnings,
        "image_slots": images,
    }
    update_proposal_image_results(result, requested_images=requested_images)
    return result


def update_proposal_image_results(metadata: dict[str, Any], *, requested_images: int) -> None:
    """搬送前の要求数と、除外後の最終枠の実数で画像の完成判定を更新する。"""
    images = metadata["image_slots"]
    capacity = sum(field["expected"] for field in images)
    inserted_images = sum(field["inserted"] for field in images)
    issues = [
        issue
        for issue in metadata["field_issues"]
        if not issue.startswith("image_insertion_failed:")
    ]
    warnings = [w for w in metadata["field_warnings"] if not w.startswith("要対応: 画像")]
    missing_images = sum(max(field["expected"] - field["inserted"], 0) for field in images)
    if missing_images:
        pages = "・".join(
            str(page)
            for page in sorted(
                {field["slide"] for field in images if field["inserted"] < field["expected"]}
            )
        )
        warnings.append(f"要対応: 画像未挿入 {missing_images} 枠（{pages}枚目）")
    failed = min(requested_images, capacity) - inserted_images
    if failed > 0:
        issues.append(f"image_insertion_failed:{failed}")
    if requested_images > capacity:
        warnings.append(f"要対応: 画像 {requested_images - capacity} 件は貼り先なし")
    metadata.update(
        {
            "field_issues": sorted(set(issues)),
            "field_warnings": warnings,
            "requested_evidence_images": requested_images,
            "evidence_images": inserted_images,
        }
    )


# 結果の metadata は 32KB・NaN 不可（contracts.MediaJobResult）。検査の結果で依頼全体を
# 失敗させないよう、要約はこの大きさに収め、収まらなければ件数だけを返す。
_DECK_QA_MAX_BYTES = 8 * 1024


def deck_qa(path: Path, *, expected_slides: int) -> dict[str, Any]:
    try:
        from teamagent.media.deck_qa import inspect_pptx

        summary = inspect_pptx(path, expected_slides=expected_slides).summary()
    except Exception as exc:
        # 例外の文は長さも中身も制御できないので型名だけ。
        return {"qa_error": type(exc).__name__[:80]}
    try:
        encoded = json.dumps(summary, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        encoded = None
    if encoded is None or len(encoded.encode("utf-8")) > _DECK_QA_MAX_BYTES:
        summary = {
            "error_count": int(summary.get("error_count", 0)),
            "warn_count": int(summary.get("warn_count", 0)),
            "top_findings_dropped": True,
        }
    return {"deck_qa": summary}


def inspect_proposal(path: Path, *, expected_slides: int) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_slides": expected_slides,
    }
    metadata.update(deck_qa(path, expected_slides=expected_slides))
    return metadata


def proposal_readiness_issues(metadata: dict[str, Any]) -> list[str]:
    issues = []
    qa = metadata.get("deck_qa")
    if (
        metadata.get("qa_error")
        or not isinstance(qa, dict)
        or type(qa.get("error_count")) is not int
    ):
        issues.append("pptx_l1_unavailable")
    elif qa["error_count"]:
        issues.append(f"pptx_l1_errors:{qa['error_count']}")
    fields = metadata.get("field_issues")
    if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
        issues.append("pptx_fields_unavailable")
    else:
        issues.extend(fields)
    return issues
