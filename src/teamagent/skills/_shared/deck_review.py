"""PPTX の表示順で、テンプレより増えた「要確認」を通知する。"""

from teamagent.ingest.office_extract import extract_pptx_slide_shapes

UNAVAILABLE_WARNING = "要確認の位置は自動で数えられませんでした"


def count_review(data: bytes) -> dict[int, int]:
    return {
        number: sum(text.count("要確認") for text in shapes)
        for number, shapes in extract_pptx_slide_shapes(data)
    }


def review_slides(out_bytes: bytes, tpl_bytes: bytes) -> list[int]:
    baseline = count_review(tpl_bytes)
    return sorted(
        number
        for number, count in count_review(out_bytes).items()
        if count > baseline.get(number, 0)
    )


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
