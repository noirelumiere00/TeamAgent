"""空き枠計算（v0.3 Task4）。freebusy の busy 区間から候補スロットを出す純関数。

skills 層に置く（ビジネスルール: 営業時間・翌営業日以降・候補は別日優先）。
Slack/Google I/O は一切しない＝決定的・テスト容易。
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable

from teamagent.adapters.gcalendar_client import FreeBusyBlock

_JST = _dt.timezone(_dt.timedelta(hours=9))


def _parse(iso: str) -> _dt.datetime | None:
    try:
        d = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d.astimezone(_JST) if d.tzinfo else d.replace(tzinfo=_JST)


def find_slots(
    busy: Iterable[FreeBusyBlock],
    *,
    now: _dt.datetime,
    business_days: int = 5,
    work_start_hour: int = 10,
    work_end_hour: int = 18,
    duration_min: int = 60,
    max_slots: int = 3,
) -> list[tuple[_dt.datetime, _dt.datetime]]:
    """翌営業日から ``business_days`` 営業日の範囲で空き枠を最大 ``max_slots`` 件返す。

    ルール（初期値は Phase 2 パイロットで調整・指示書 §8）:
      - 営業時間 10:00-18:00 JST・1時間枠・正時グリッド
      - 土日を除く「翌営業日」以降（当日は提案しない＝相手への礼儀と準備時間）
      - **候補は別日優先**（1日1枠で埋め、足りなければ同日2枠目以降で補完）
      - busy と 1 分でも重なる枠は除外
    """
    now_jst = now.astimezone(_JST) if now.tzinfo else now.replace(tzinfo=_JST)
    busy_ranges: list[tuple[_dt.datetime, _dt.datetime]] = []
    for b in busy:
        bs, be = _parse(b.start), _parse(b.end)
        if bs and be and be > bs:
            busy_ranges.append((bs, be))

    dur = _dt.timedelta(minutes=duration_min)
    # 翌営業日から business_days 営業日ぶんの日付リスト。
    days: list[_dt.date] = []
    d = now_jst.date() + _dt.timedelta(days=1)
    while len(days) < business_days:
        if d.weekday() < 5:  # 月-金
            days.append(d)
        d += _dt.timedelta(days=1)

    def _free(start: _dt.datetime) -> bool:
        end = start + dur
        return all(not (start < be and bs < end) for bs, be in busy_ranges)

    per_day: list[list[tuple[_dt.datetime, _dt.datetime]]] = []
    for day in days:
        slots_of_day = []
        for hour in range(work_start_hour, work_end_hour - (duration_min - 1) // 60):
            start = _dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=_JST)
            if start + dur > _dt.datetime(
                day.year, day.month, day.day, work_end_hour, 0, tzinfo=_JST
            ):
                continue
            if _free(start):
                slots_of_day.append((start, start + dur))
        per_day.append(slots_of_day)

    out: list[tuple[_dt.datetime, _dt.datetime]] = []
    # 1st pass: 別日優先（各日の最初の空き枠）。
    for slots_of_day in per_day:
        if len(out) >= max_slots:
            break
        if slots_of_day:
            out.append(slots_of_day[0])
    # 2nd pass: 足りなければ同日の残り枠で補完（早い日から）。
    if len(out) < max_slots:
        taken = set(out)
        for slots_of_day in per_day:
            for slot in slots_of_day:
                if len(out) >= max_slots:
                    break
                if slot not in taken:
                    out.append(slot)
                    taken.add(slot)
    return out[:max_slots]


_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def format_candidates_ja(slots: list[tuple[_dt.datetime, _dt.datetime]]) -> str:
    """候補を「①7/15(水) 14:00〜15:00」形式の箇条書きにする（返信下書き用）。"""
    marks = "①②③④⑤⑥⑦⑧⑨"
    lines = []
    for i, (s, e) in enumerate(slots):
        m = marks[i] if i < len(marks) else f"({i + 1})"
        lines.append(
            f"{m}{s.month}/{s.day}({_WEEKDAY_JA[s.weekday()]}) "
            f"{s.strftime('%H:%M')}〜{e.strftime('%H:%M')}"
        )
    return "\n".join(lines)


def build_proposal_body(slots: list[tuple[_dt.datetime, _dt.datetime]]) -> str:
    """日程候補入りの返信本文（決定的・LLM不使用＝コストゼロ/ねつ造ゼロ）。

    署名・宛名は書かない（既存の下書き方針と同じ＝本人が送信前に整える）。
    """
    return (
        "お世話になっております。\n"
        "日程のご相談をいただきありがとうございます。\n"
        "以下の日程でご都合はいかがでしょうか。\n\n"
        f"{format_candidates_ja(slots)}\n\n"
        "上記でご都合が合わない場合は、候補をいくつかお知らせいただけますと幸いです。\n"
        "よろしくお願いいたします。"
    )
