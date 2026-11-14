"""本編内の題・数字の重複を検査する（同じ面の別ページ間）。

条件・順位・母数、表・カード・図のデータ、ノート・付録は照合対象外。
数字は単位と指標を合わせて比較し、偶然同じ値の別指標を混同しない。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from teamagent.media.deck_contracts import SlideSpec, TextFill

_VALUE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:万|億)?\s*(?:回|%|％|倍|本)")
_CONTEXT = re.compile(r"上位\s*\d+\s*本(?:中|のうち)?|\d+(?:・\d+)*\s*位|根拠")


def _text(fill: TextFill) -> str:
    return "\n".join("".join(r.text for r in p.runs) for p in fill.paragraphs)


def _canonical(text: str) -> str:
    return re.sub(r"[\s、。・：:（）()]", "", text)


def _metric(text: str) -> str:
    return next(
        (
            m
            for m in ("保存率", "フォロワー", "いいね", "再生", "出現回数", "直近", "長さ")
            if m in text
        ),
        "本数",
    )


def _number_conflicts(slide: SlideSpec, fills: list[TextFill], title: str) -> list[str]:
    title_values = {_canonical(m.group()) for m in _VALUE.finditer(_CONTEXT.sub("", title))}
    if not title_values:
        return []
    for fill in fills:
        if fill.box != "big_number" and not fill.box.startswith("small_number"):
            continue
        label = next((_text(f) for f in fills if f.box == fill.box.replace("number", "label")), "")
        if _metric(label) != _metric(title):
            continue
        actual = {_canonical(m.group()) for m in _VALUE.finditer(_text(fill))}
        if actual and not title_values.intersection(actual):
            return [f"数字の不一致: {slide.slide_id}: 題と {fill.box}"]
    return []


def duplication_problems(slides: Sequence[SlideSpec]) -> list[str]:
    titles: dict[tuple[str, str], str] = {}
    values: dict[tuple[str, str, str], str] = {}
    problems = []
    for slide in slides:
        if not slide.section.startswith("本編"):
            continue
        fills = [f for f in slide.fills if isinstance(f, TextFill)]
        title = next((_text(f) for f in fills if f.box == "title"), "")
        problems.extend(_number_conflicts(slide, fills, title))
        key = (slide.section, _canonical(title))
        if title and key in titles:
            problems.append(f"題の重複: {titles[key]} / {slide.slide_id}: {title}")
        titles[key] = slide.slide_id
        if slide.layout in ("R_表", "R_動画カード"):
            continue
        for fill in fills:
            if fill.box not in (
                "title",
                "body",
                "reading",
                "big_number",
            ) and not fill.box.startswith("small_number"):
                continue
            text = _CONTEXT.sub("", _text(fill))
            label_box = fill.box.replace("number", "label")
            label = next((_text(f) for f in fills if f.box == label_box), "")
            for line in text.splitlines():
                for match in _VALUE.finditer(line):
                    # 指標名も比較する。値だけ同じ「いいね」と「再生」は別の事実。
                    context = label or line
                    metric = _metric(context)
                    value_key = (slide.section, metric, _canonical(match.group()))
                    previous = values.get(value_key)
                    if previous and previous != slide.slide_id:
                        problems.append(
                            f"数字の重複: {previous} / {slide.slide_id}: {metric} {match.group()}"
                        )
                    values[value_key] = slide.slide_id
    return sorted(set(problems))
