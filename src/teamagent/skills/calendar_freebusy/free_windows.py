"""空きウィンドウ計算（calendar_freebusy）。freebusy の busy 区間から空きを出す純関数。

skills 層に置く（ビジネスルール: 営業時間・休日注記・決定的整形）。
Slack/Google I/O は一切しない＝決定的・テスト容易（schedule_propose/slot_finder.py と同思想）。
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable

from teamagent.adapters.gcalendar_client import FreeBusyBlock

_JST = _dt.timezone(_dt.timedelta(hours=9))
_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _parse(iso: str) -> _dt.datetime | None:
    try:
        d = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d.astimezone(_JST) if d.tzinfo else d.replace(tzinfo=_JST)


def day_label_ja(day: _dt.date) -> str:
    """「8/15(土)」形式の日付ラベル（決定的・LLM 不使用）。"""
    return f"{day.month}/{day.day}({_WEEKDAY_JA[day.weekday()]})"


def compute_free_windows(
    busy: Iterable[FreeBusyBlock],
    *,
    day: _dt.date,
    work_start_hour: int = 9,
    work_end_hour: int = 18,
    min_window_min: int = 15,
) -> list[tuple[_dt.datetime, _dt.datetime]]:
    """対象日の営業ウィンドウから busy 区間を interval 減算し、空きウィンドウを返す。

    ルール:
      - 営業ウィンドウは work_start_hour:00〜work_end_hour:00 JST（当日も対象にできる）
      - busy は部分重なり・包含・日跨ぎを含めて営業ウィンドウへクリップして減算する
      - min_window_min 分未満の細切れウィンドウは返さない（使えない空きを見せない）
    """
    window_start = _dt.datetime(day.year, day.month, day.day, work_start_hour, tzinfo=_JST)
    window_end = _dt.datetime(day.year, day.month, day.day, work_end_hour, tzinfo=_JST)
    busy_ranges: list[tuple[_dt.datetime, _dt.datetime]] = []
    for b in busy:
        bs, be = _parse(b.start), _parse(b.end)
        if bs is None or be is None or be <= bs:
            continue
        # 営業ウィンドウへクリップ（日跨ぎ busy もここで対象日ぶんだけに畳まれる）。
        clipped_start, clipped_end = max(bs, window_start), min(be, window_end)
        if clipped_end > clipped_start:
            busy_ranges.append((clipped_start, clipped_end))
    busy_ranges.sort()

    free: list[tuple[_dt.datetime, _dt.datetime]] = []
    cursor = window_start
    for bs, be in busy_ranges:
        if bs > cursor:
            free.append((cursor, bs))
        cursor = max(cursor, be)
    if cursor < window_end:
        free.append((cursor, window_end))
    min_span = _dt.timedelta(minutes=min_window_min)
    return [(s, e) for s, e in free if (e - s) >= min_span]


def non_business_note(day: _dt.date) -> str:
    """土日なら休日注記を返す（平日は空文字）。

    ⚠️ 日本の祝日は判定しない（jpholiday 等の新規依存は追加しない裁定・静的リストは
    鮮度リスク）。祝日の可能性は文言で正直に伝える（偽の「営業日」断定をしない）。
    """
    if day.weekday() < 5:
        return ""
    return (
        f"⚠️ {day_label_ja(day)}は休日です"
        "（土日のみ判定・祝日は未判定のため平日でも祝日の可能性があります）。"
    )


def enumerate_candidate_starts(
    windows: list[tuple[_dt.datetime, _dt.datetime]],
    *,
    duration_min: int,
    step_min: int = 30,
    limit: int = 6,
) -> list[tuple[_dt.datetime, _dt.datetime]]:
    """空きウィンドウ内で duration_min 分の打合せが収まる開始候補を列挙する。

    各ウィンドウ先頭から step_min 分刻み・全体で最大 limit 件（決定的・早い順）。
    """
    dur = _dt.timedelta(minutes=duration_min)
    step = _dt.timedelta(minutes=step_min)
    out: list[tuple[_dt.datetime, _dt.datetime]] = []
    for window_start, window_end in windows:
        cursor = window_start
        while cursor + dur <= window_end and len(out) < limit:
            out.append((cursor, cursor + dur))
            cursor += step
        if len(out) >= limit:
            break
    return out


def format_freebusy_ja(
    day: _dt.date,
    windows: list[tuple[_dt.datetime, _dt.datetime]],
    candidates: list[str],
    note: str,
) -> str:
    """1日ぶんの空き情報を決定的な日本語に整形する（LLM 不使用＝ねつ造ゼロ）。

    note があれば必ず先頭に出す（土日注記を LLM の裁量で落とさせない）。
    """
    lines: list[str] = []
    if note:
        lines.append(note)
    label = day_label_ja(day)
    if not windows:
        lines.append(f"📅 {label} は営業時間内（9:00〜18:00 JST）に空きがありません。")
        return "\n".join(lines)
    lines.append(f"📅 {label} の空き時間（営業時間 9:00〜18:00 JST）:")
    lines.extend(f"・{s.strftime('%H:%M')}〜{e.strftime('%H:%M')}" for s, e in windows)
    if candidates:
        lines.append("開始候補: " + " / ".join(candidates))
    else:
        lines.append("指定の所要時間が収まる開始候補はありませんでした。")
    return "\n".join(lines)
