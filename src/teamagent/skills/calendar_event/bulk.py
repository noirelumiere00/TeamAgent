"""共通時刻と日付・件名の各行を、単発登録と同じ入力へ展開する。"""

from __future__ import annotations

import datetime as dt
import re

from teamagent.skills.calendar_event.schema import CalendarEventInput

_TIME = re.compile(r"(?<!\d)(\d{1,2}):([0-5]\d)\s*[〜～~−–-]\s*(\d{1,2}):([0-5]\d)(?!\d)")
_ROW = re.compile(
    r"^\s*(?:[・*•-]\s*)?(?:(\d{4})[年/\-])?(\d{1,2})[月/\-](\d{1,2})日?"
    r"\s*(?:[（(]([月火水木金土日])(?:曜(?:日)?)?[）)])?\s+(.+?)\s*$"
)
_JST = dt.timezone(dt.timedelta(hours=9))


def parse_bulk(text: str, *, today: dt.date) -> tuple[list[CalendarEventInput], list[str]]:
    """年省略は今日以降で最も近い実在日。曜日は日付の解釈に使わない。"""
    times = list(_TIME.finditer(text))
    if len(times) != 1:
        raise ValueError("one common time range required")
    h1, m1, h2, m2 = map(int, times[0].groups())
    start_time, end_time = dt.time(h1, m1), dt.time(h2, m2)
    rows: list[CalendarEventInput] = []
    warnings: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _ROW.fullmatch(line)
        if match is None:
            # 依頼文・共通時刻は先頭のみ。日付行の間や末尾の不正行は黙って捨てない。
            if rows or (re.match(r"^\s*(?:[・*•-]\s*)?\d", line) and not _TIME.search(line)):
                raise ValueError("invalid event row")
            continue
        year, month, day, weekday, title = match.groups()
        if year:
            date = dt.date(int(year), int(month), int(day))
        else:
            date = None
            for candidate in range(today.year, today.year + 5):
                try:
                    possible = dt.date(candidate, int(month), int(day))
                except ValueError:
                    continue
                if possible >= today:
                    date = possible
                    break
        if date is None:
            raise ValueError("invalid date")
        if weekday and weekday != "月火水木金土日"[date.weekday()]:
            warnings.append(
                f"⚠️ {date.isoformat()} の曜日は{weekday}ではなく"
                f"{'月火水木金土日'[date.weekday()]}です。日付を優先します。"
            )
        rows.append(
            CalendarEventInput(
                title=title,
                start=dt.datetime.combine(date, start_time, tzinfo=_JST).isoformat(),
                end=dt.datetime.combine(date, end_time, tzinfo=_JST).isoformat(),
            )
        )
    if not 1 <= len(rows) <= 50:
        raise ValueError("1 to 50 event rows required")
    return rows, warnings
