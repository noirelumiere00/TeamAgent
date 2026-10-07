"""Slack の期間指定を JST の半開区間へ揃える（外部 I/O なし）。"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone

# 本番の実行環境（chainguard python）には時間帯のデータ（tzdata）が無く、IANA 名での指定は
# import の時点で落ちて mcp 全体が起動しない（2026-10-07 r48 で発生）。日本は夏時間が無いので
# 固定の +9 時間で表す（slack_unreplied 等と同じ）。
JST = timezone(timedelta(hours=9))


def resolve_period(period: str, *, now: datetime | None = None) -> tuple[str, str]:
    """昨日・今週・先月・ISO 日付（範囲の終日は含む）→ oldest/latest。"""
    now = (now or datetime.now(JST)).astimezone(JST)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period = unicodedata.normalize("NFKC", period).strip()
    if period == "昨日":
        start, end = today - timedelta(days=1), today
    elif period == "今週":
        start, end = today - timedelta(days=today.weekday()), now
    elif period == "先月":
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    else:
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2})(?:\s*(?:〜|~|～|から|\.\.)\s*(\d{4}-\d{2}-\d{2})(?:まで)?)?",
            period,
        )
        if not match:
            raise ValueError("bad_period")
        start = datetime.fromisoformat(match[1]).replace(tzinfo=JST)
        end = datetime.fromisoformat(match[2] or match[1]).replace(tzinfo=JST) + timedelta(days=1)
    if end <= start:
        raise ValueError("bad_period")
    return str(start.timestamp()), str(end.timestamp())
