"""Slack の期間指定を JST の半開区間へ揃える（外部 I/O なし）。"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone

# 本番の実行環境（chainguard python）には時間帯のデータ（tzdata）が無く、IANA 名での指定は
# import の時点で落ちて mcp 全体が起動しない（2026-10-07 r48 で発生）。日本は夏時間が無いので
# 固定の +9 時間で表す（slack_unreplied 等と同じ）。
JST = timezone(timedelta(hours=9))

# 受けられる語（案内文・ツール説明と揃える）。
PERIOD_WORDS = (
    "今日",
    "昨日",
    "一昨日",
    "今週",
    "先週",
    "先々週",
    "今月",
    "先月",
    "直近N日",
    "M月D日〜D日",
    "M/D〜M/D",
    "YYYY-MM-DD",
)

_SEP = r"\s*(?:〜|~|～|から|\.\.)\s*"
_ISO = r"(\d{4})-(\d{1,2})-(\d{1,2})"
_MD = r"(\d{1,2})(?:月|/)(\d{1,2})日?"
_RECENT_MAX_DAYS = 366


def _month_start(day: datetime) -> datetime:
    return day.replace(day=1)


def _date(year: int, month: int, day: int) -> datetime:
    try:
        return datetime(year, month, day, tzinfo=JST)
    except ValueError as exc:  # 2月30日 など存在しない日
        raise ValueError("bad_period") from exc


def _month_day_range(period: str, today: datetime) -> tuple[datetime, datetime] | None:
    """「M月D日〜D日」「M月D日〜M月D日」「M/D〜M/D」「M/D」→ 年は今年、未来なら昨年。"""
    match = re.fullmatch(
        rf"{_MD}(?:{_SEP}(?:(\d{{1,2}})(?:月|/))?(\d{{1,2}})日?(?:まで)?)?", period
    )
    if not match:
        return None
    start = _date(today.year, int(match[1]), int(match[2]))
    if start > today:
        start = _date(today.year - 1, int(match[1]), int(match[2]))
    if match[4] is None:
        return start, start + timedelta(days=1)
    end = _date(start.year, int(match[3] or match[1]), int(match[4]))
    if end < start and end.month < start.month:  # 12月28日〜1月3日 のような年またぎ
        end = _date(start.year + 1, end.month, end.day)
        if end > today:  # 「10/5〜9/1」のように来年へ飛ぶ範囲は受けない
            raise ValueError("bad_period")
    return start, end + timedelta(days=1)


def resolve_period(period: str, *, now: datetime | None = None) -> tuple[str, str]:
    """今日・昨日・一昨日・今週・先週・先々週・今月・先月・直近N日・M月D日〜D日・ISO 日付
    （範囲の終日は含む）→ oldest/latest（JST の半開区間を Slack ts の文字列で返す）。"""
    now = (now or datetime.now(JST)).astimezone(JST)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today - timedelta(days=today.weekday())
    period = unicodedata.normalize("NFKC", period).strip()
    if period == "今日":
        start, end = today, now
    elif period == "昨日":
        start, end = today - timedelta(days=1), today
    elif period in ("一昨日", "おととい"):
        start, end = today - timedelta(days=2), today - timedelta(days=1)
    elif period == "今週":
        start, end = this_monday, now
    elif period == "先週":
        start, end = this_monday - timedelta(days=7), this_monday
    elif period == "先々週":
        start, end = this_monday - timedelta(days=14), this_monday - timedelta(days=7)
    elif period == "今月":
        start, end = _month_start(today), now
    elif period == "先月":
        end = _month_start(today)
        start = _month_start(end - timedelta(days=1))
    elif match := re.fullmatch(r"(?:直近|過去)(\d{1,3})日(?:間)?", period):
        days = int(match[1])
        if not 1 <= days <= _RECENT_MAX_DAYS:
            raise ValueError("bad_period")
        # 「直近7日」= 今日を含めず丸7日ぶん遡った日の 00:00 〜 今（今日の分も入る）。
        start, end = today - timedelta(days=days), now
    elif match := re.fullmatch(rf"{_ISO}(?:{_SEP}{_ISO}(?:まで)?)?", period):
        start = _date(int(match[1]), int(match[2]), int(match[3]))
        end = start if match[4] is None else _date(int(match[4]), int(match[5]), int(match[6]))
        end += timedelta(days=1)
    elif bounds := _month_day_range(period, today):
        start, end = bounds
    else:
        raise ValueError("bad_period")
    if end <= start:
        raise ValueError("bad_period")
    return str(start.timestamp()), str(end.timestamp())
