"""個人別配信時刻（DELTA §1）の式を固定する。"""

from __future__ import annotations

import datetime as _dt

from teamagent.skills.morning_digest.calendar_window import JST
from teamagent.skills.morning_digest.send_window import (
    compute_send_time,
    first_timed_start,
    parse_hhmm,
)

DAY = _dt.date(2026, 9, 11)
DEFAULT = (9, 30)


def _at(hour: int, minute: int) -> _dt.datetime:
    return _dt.datetime(2026, 9, 11, hour, minute, tzinfo=JST)


def test_typical_case_is_one_hour_before() -> None:
    plan = compute_send_time(DAY, _at(10, 0), default_hhmm=DEFAULT)
    assert plan.fire_at == _at(9, 0)
    assert plan.clamped_to_floor is False
    assert plan.clamped_to_default is False


def test_minutes_floor_to_five() -> None:
    """08:47 の予定 → 07:47 → **切り下げ** 07:45（切り上げにすると赤）。"""
    plan = compute_send_time(DAY, _at(8, 47), default_hhmm=DEFAULT)
    assert plan.fire_at == _at(7, 45)


def test_early_meeting_clamps_to_floor_and_flags() -> None:
    """06:30 の予定は 05:30 でなく 06:00 へ。冒頭の 1 行を出す印が立つ。"""
    plan = compute_send_time(DAY, _at(6, 30), default_hhmm=DEFAULT)
    assert plan.fire_at == _at(6, 0)
    assert plan.clamped_to_floor is True


def test_late_meeting_stays_at_default() -> None:
    """15:00 の予定に 14:00 送信だとメール下書きの価値が消える＝既定時刻のまま。"""
    plan = compute_send_time(DAY, _at(15, 0), default_hhmm=DEFAULT)
    assert plan.fire_at == _at(9, 30)
    assert plan.clamped_to_default is True


def test_no_event_uses_default() -> None:
    plan = compute_send_time(DAY, None, default_hhmm=DEFAULT)
    assert plan.fire_at == _at(9, 30)
    assert plan.no_timed_event is True


def test_exactly_at_floor_is_not_flagged() -> None:
    """最初の予定がちょうど 07:00 の日は **通常どおり 60 分前**。嘘の注記を付けない。

    変異: ``compute_send_time`` の境界を ``target <= floor_at`` に戻すと
    ``clamped_to_floor`` が True になり赤（冒頭に「通常より短い間隔で」が出る）。
    """
    plan = compute_send_time(DAY, _at(7, 0), default_hhmm=DEFAULT)
    assert plan.fire_at == _at(6, 0)
    assert plan.clamped_to_floor is False


def test_below_floor_is_flagged() -> None:
    """06:59 始まりの日は 06:00 へ張り付く＝リードが短いので注記を出す。"""
    plan = compute_send_time(DAY, _at(6, 59), default_hhmm=DEFAULT)
    assert plan.fire_at == _at(6, 0)
    assert plan.clamped_to_floor is True


def test_first_timed_start_picks_earliest_of_the_day() -> None:
    starts = [
        "2026-09-11T14:00:00+09:00",
        "2026-09-11T10:30:00+09:00",
        "2026-09-11T16:00:00+09:00",
    ]
    assert first_timed_start(starts, DAY) == _at(10, 30)


def test_first_timed_start_drops_other_days() -> None:
    """窓が翌日にはみ出した日の予定を「最初の予定」にしない。"""
    starts = ["2026-09-12T08:00:00+09:00", "2026-09-11T11:00:00+09:00"]
    assert first_timed_start(starts, DAY) == _at(11, 0)


def test_first_timed_start_none_when_empty() -> None:
    assert first_timed_start([], DAY) is None
    assert first_timed_start(["こわれた値"], DAY) is None


def test_parse_hhmm_falls_back_without_raising() -> None:
    assert parse_hhmm("09:30", (0, 0)) == (9, 30)
    assert parse_hhmm("", (9, 30)) == (9, 30)
    assert parse_hhmm("25:00", (9, 30)) == (9, 30)
    assert parse_hhmm("abc", (9, 30)) == (9, 30)
