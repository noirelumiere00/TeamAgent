"""Schedule date cleanup and checks shared by local and isolated proposal renderers."""

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
_PLAN_LABEL = (
    r"投稿|配信|制作|撮影|納品|入稿|校了|公開|実施|工程|フェーズ|PHASE|スケジュール|ガント"
)
# shortcut: 計画ラベル・見出しで範囲を限定。曖昧な固定欄は新FMTでPB-DATEへ移行する。
_KEEP_LABEL = r"実績|事例|過去|出典|発行|公表|提案日|(?:利用者|依頼者|依頼文)指定|指定(?:の)?期間"
_SCHEDULE_HEADING = re.compile(
    r"[｜|\s]*(?:(?:投稿|配信|制作|実施)\s*)?(?:工程|スケジュール|ガント(?:チャート)?)[\s:：]*$"
)
_PLAN_DATE = (
    r"(?<![\d/／.．])(?:\d{4}[-－/／.．]\d{1,2}(?:[-－/／.．]\d{1,2})?"
    r"|(?:\d{4}年)?\d{1,2}月(?:\d{1,2}日)?|\d{1,2}[/／]\d{1,2})(?!\d)"
)
_SCHEDULE_PART = re.compile(
    rf"(?P<date>{_PLAN_DATE})|(?P<keep>{_KEEP_LABEL})|(?P<plan>{_PLAN_LABEL})"
    r"|(?P<break>[。；;、，\n\v]|／(?!\d))",
    re.IGNORECASE,
)


def is_schedule_heading(text: str) -> bool:
    """Only an explicit schedule heading gives unlabelled axis cells plan context."""
    return _SCHEDULE_HEADING.fullmatch(text) is not None


def undated_schedule_text(
    text: str,
    *,
    field_label: str = "",
    specified_periods: tuple[str, ...] = (),
) -> str:
    """Replace plan dates, keeping evidence clauses, URLs and request-bound periods."""
    # shortcut: 「（1/2）」形のページ番号は日付と区別できないため保護。1桁同士の日付が来たら見直す。
    protected = [
        match.span() for match in re.finditer(r"https?://[^\s）)]+|[（(]\d[/／]\d[）)]", text)
    ]
    protected.extend(
        match.span()
        for period in specified_periods
        if period
        for match in re.finditer(re.escape(period), text)
    )
    default_context = ""
    for label in _SCHEDULE_PART.finditer(field_label):
        if label.lastgroup == "keep":
            default_context = "keep"
        elif label.lastgroup == "plan" and default_context != "keep":
            default_context = "plan"
    context = default_context
    dates = []
    for part in _SCHEDULE_PART.finditer(text):
        if part.lastgroup == "break":
            if part.group() not in ("\n", "\v", "、", "，") or context != "plan":
                context = default_context
        elif part.lastgroup == "keep":
            context = "keep"
        elif part.lastgroup == "plan" and context != "keep":
            context = "plan"
        elif part.lastgroup == "date" and context == "plan":
            if any(start <= part.start() and part.end() <= end for start, end in protected):
                continue
            # 日付の直後に付く「発行」「投稿実績」も過去の根拠として保持する。
            if re.match(
                r"(?:の|に)?(?:投稿|配信|公開|制作)?(?:実績|発行|公表)", text[part.end() :]
            ):
                continue
            dates.append(part.span())
    for start, end in reversed(dates):
        text = text[:start] + "未定" + text[end:]
    return text


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
