"""個人別配信時刻の決定（純関数・JST 固定）— DELTA §1 の唯一の実装。

    送信時刻 = clamp(その日の最初の「時刻つき」予定の開始 − 60分, 下限 06:00, 上限 <既定時刻>)
    予定が 1 件も無い日 = <既定時刻>

- **終日予定は「最初の予定」の計算から除外**（終日だけの日は「予定なし」扱い）。
- 分は 5 分単位に **切り下げ**（08:47 → 08:45）。
- 下限 06:00 に張り付いたら、冒頭に 1 行添える（``clamped_to_floor``）。
- 上限は現行の既定時刻。最初の予定が 15:00 の人に 14:00 送信だとメール下書きの価値が
  消えるため、遅い日は既定時刻のまま。

planner 実行後に予定が追加・変更されても当日の送信時刻は追随しない（既存リマインドと
同じ制約）。祝日・休暇の判定はしない（予定が無ければ既定時刻）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from teamagent.skills.morning_digest import calendar_window as _calwin

#: 配信の下限（これより早くは送らない）。
FLOOR_HHMM = (6, 0)
#: 予定の何分前に送るか。
LEAD_MINUTES = 60
#: 分の丸め単位（切り下げ）。
ROUND_MINUTES = 5


@dataclass(frozen=True)
class SendPlan:
    """1 人ぶんの配信計画。``fire_at`` は JST aware。"""

    fire_at: _dt.datetime
    #: 下限 06:00 に張り付いたか（＝冒頭に「通常より短い間隔」の 1 行を添える）
    clamped_to_floor: bool = False
    #: 上限（既定時刻）に張り付いたか。予定なし・遅い予定の日は True
    clamped_to_default: bool = False
    #: 時刻つき予定が 1 件も無かったか
    no_timed_event: bool = False


def _floor_to_step(value: _dt.datetime, step: int = ROUND_MINUTES) -> _dt.datetime:
    """分を step 単位へ **切り下げ**（秒・マイクロ秒も落とす）。"""
    step = max(1, int(step))
    return value.replace(minute=(value.minute // step) * step, second=0, microsecond=0)


def parse_hhmm(raw: str | None, default: tuple[int, int]) -> tuple[int, int]:
    """``"09:30"`` → ``(9, 30)``。読めなければ default（例外を投げない）。"""
    text = (raw or "").strip()
    if not text or ":" not in text:
        return default
    head, _, tail = text.partition(":")
    try:
        hour, minute = int(head), int(tail)
    except ValueError:
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return (hour, minute)


def first_timed_start(starts: list[str | None], day: _dt.date) -> _dt.datetime | None:
    """当日の「時刻つき」予定のうち最も早い開始時刻（JST aware）。無ければ None。

    ⚠️ 終日予定（``date`` のみ / all_day）は呼び出し側で除外済みであること。日付が
    当日でないものはここで落とす（窓が翌日にはみ出す日の混入を防ぐ）。
    """
    best: _dt.datetime | None = None
    for raw in starts:
        parsed = _calwin.parse_jst_datetime(raw)
        if parsed is None:
            continue
        jst = parsed.astimezone(_calwin.JST)
        if jst.date() != day:
            continue
        if best is None or jst < best:
            best = jst
    return best


def compute_send_time(
    day: _dt.date,
    first_start: _dt.datetime | None,
    *,
    default_hhmm: tuple[int, int],
    floor_hhmm: tuple[int, int] = FLOOR_HHMM,
    lead_minutes: int = LEAD_MINUTES,
) -> SendPlan:
    """DELTA §1 の式そのもの。``first_start`` は時刻つき予定の最初の開始（JST）。"""
    default_at = _dt.datetime.combine(
        day, _dt.time(default_hhmm[0], default_hhmm[1]), tzinfo=_calwin.JST
    )
    floor_at = _dt.datetime.combine(day, _dt.time(floor_hhmm[0], floor_hhmm[1]), tzinfo=_calwin.JST)
    if first_start is None:
        return SendPlan(
            fire_at=default_at,
            clamped_to_default=True,
            no_timed_event=True,
        )
    lead = _dt.timedelta(minutes=lead_minutes)
    target = _floor_to_step(first_start.astimezone(_calwin.JST) - lead)
    # ⚠️ 境界は **厳密不等号**。最初の予定がちょうど 07:00 の日は target == floor_at
    #   ＝リードタイムは通常どおり 60 分なので、「通常より短い間隔で」の 1 行を
    #   付けてはいけない（嘘の注記になる）。張り付き扱いは target < floor_at だけ。
    if target < floor_at:
        return SendPlan(fire_at=floor_at, clamped_to_floor=True)
    if target >= default_at:
        return SendPlan(fire_at=default_at, clamped_to_default=True)
    return SendPlan(fire_at=target)


__all__ = [
    "FLOOR_HHMM",
    "LEAD_MINUTES",
    "ROUND_MINUTES",
    "SendPlan",
    "compute_send_time",
    "first_timed_start",
    "parse_hhmm",
]
