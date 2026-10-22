"""Nonblocking date checks shared by local and isolated proposal renderers."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any

_PHASE = re.compile(r"(?:フェーズ|PHASE)\s*([0-9一二三四五六七八九十]+)", re.IGNORECASE)
_MONTH = re.compile(r"(?<!\d)(?:(20\d{2})年)?(1[0-2]|0?[1-9])月")
_RANGE = re.compile(r"^[\s〜～~\-−–—から]+$")
_DAY = re.compile(r"(?<!\d)(?:(20\d{2})[/年])?(1[0-2]|0?[1-9])[/月](3[01]|[12]\d|0?[1-9])日?(?!\d)")
_PRODUCTION = re.compile(
    r"制作(?:工程|開始|進行)|(?:スケジュール|ガント)[\s\S]*制作|制作[\s\S]*(?:スケジュール|ガント)"
)


def proposal_today() -> date:
    """Use the proposal's Japanese business date even in UTC worker containers."""
    return datetime.now(timezone(timedelta(hours=9))).date()


def _body_text(slide: Any) -> str:
    return unicodedata.normalize(
        "NFKC",
        "\n".join(
            "".join(node.text or "" for node in paragraph.xpath(".//a:t"))
            for paragraph in slide._element.xpath(".//a:p")
        ),
    )


def _phase_months(text: str, default_year: int) -> list[tuple[str, set[int]]]:
    phases = list(_PHASE.finditer(text))
    result = []
    year = default_year
    previous_month = 0
    for index, phase in enumerate(phases):
        end = phases[index + 1].start() if index + 1 < len(phases) else len(text)
        period = text[phase.end() : min(end, phase.end() + 80)]
        months = list(_MONTH.finditer(period))
        if not months:
            continue
        first = months[0]
        month = int(first.group(2))
        if not first.group(1) and month < previous_month:
            year += 1
        year = int(first.group(1) or year)
        previous_month = month
        start = year * 12 + month - 1
        finish = start
        if len(months) > 1 and _RANGE.fullmatch(period[first.end() : months[1].start()]):
            last = months[1]
            last_year = int(last.group(1) or year)
            finish = last_year * 12 + int(last.group(2)) - 1
            if not last.group(1) and finish < start:
                finish += 12
        if start <= finish <= start + 36:
            result.append((f"フェーズ{phase.group(1)}", set(range(start, finish + 1))))
    return result


def _production_dates(text: str, anchor: date) -> list[date]:
    result = []
    for match in _DAY.finditer(text):
        year, month, day = match.groups()
        years = [int(year)] if year else [anchor.year - 1, anchor.year, anchor.year + 1]
        candidates = []
        for candidate_year in years:
            try:
                candidates.append(date(candidate_year, int(month), int(day)))
            except ValueError:
                continue
        if candidates:
            # shortcut: 年なし工程表は投稿日の前後半年と解釈、長期工程表は年付き日付にする。
            result.append(min(candidates, key=lambda value: abs((value - anchor).days)))
    return result


def schedule_warnings(
    presentation: Any,
    proposal_date: date,
    *,
    posting_start_date: date | None = None,
) -> list[tuple[int, str]]:
    """Return one-based page/reason pairs; leave body and notes unchanged."""
    warnings = []
    phases: list[tuple[int, str, set[int]]] = []
    for page, slide in enumerate(presentation.slides, 1):
        text = _body_text(slide)
        phases.extend(
            (page, label, months)
            for label, months in _phase_months(text, (posting_start_date or proposal_date).year)
        )
        if _PRODUCTION.search(text):
            days = _production_dates(text, posting_start_date or proposal_date)
            if days and min(days) < proposal_date:
                warnings.append(
                    (
                        page,
                        f"制作工程の開始 {min(days).isoformat()} が"
                        f"提案日 {proposal_date.isoformat()} より前",
                    )
                )
    for index, (page, label, months) in enumerate(phases):
        for other_page, other_label, other_months in phases[index + 1 :]:
            overlap = months & other_months
            if label == other_label or not overlap:
                continue
            period = "・".join(f"{value // 12}年{value % 12 + 1}月" for value in sorted(overlap))
            reason = f"{label}と{other_label}の期間が同じ月に重なっています（{period}）"
            warnings.extend((affected, reason) for affected in sorted({page, other_page}))
    return list(dict.fromkeys(warnings))
