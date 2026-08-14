"""メール受信時刻の JST 決定論フォーマッタ（2026-08-14 UTC表示バグ根治）。

背景（本番実測）: 4スキルが各自コピーした ``_iso_or_none`` が UTC で ISO 化し、
LLM が生タイムスタンプから時刻・曜日を自力計算して誤表示していた
（実際 8/13(木) 19:00 JST 受信 → 「8/13（火）10:00」）。

方針: スキル境界で JST 変換済みの「表示文字列」まで決定論的に付与し、
LLM に時刻・曜日を再計算させない（各 schema の description でも禁止を明示）。
Slack 未返信系の ``_shared/slack_unreplied.py`` の JST 前例に揃える。
"""

from __future__ import annotations

import datetime as _dt

_JST = _dt.timezone(_dt.timedelta(hours=9))
_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def jst_iso_or_none(internal_date_ms: int | None) -> str | None:
    """Gmail internalDate(epoch ms) を JST の ISO 文字列にする。

    None のみ不明扱い（0 = 1970 epoch は有効値。morning_digest 版の意味論に統一）。
    """
    if internal_date_ms is None:
        return None
    dt = _dt.datetime.fromtimestamp(internal_date_ms / 1000, tz=_JST)
    return dt.isoformat()


def jst_display_or_none(internal_date_ms: int | None) -> str | None:
    """表示用「MM/DD(曜) HH:MM」（JST・和曜日）。例: ``08/13(木) 19:00``。"""
    if internal_date_ms is None:
        return None
    dt = _dt.datetime.fromtimestamp(internal_date_ms / 1000, tz=_JST)
    return f"{dt.month:02d}/{dt.day:02d}({_WEEKDAY_JA[dt.weekday()]}) {dt:%H:%M}"


__all__ = ["jst_display_or_none", "jst_iso_or_none"]
