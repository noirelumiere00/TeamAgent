"""配信日カレンダー（morning_digest/delivery_calendar.py）のテスト。

F0 の祝日スキップと祝日明けの走査範囲の拡張、F1/F2 が使う「次の配信日・前の配信日」。
日付は 2026-10-12（スポーツの日）・10-13（明け）・11-03・年末年始・大型連休で固定する。
純関数なので壁時計に依存しない（now_jst を読まない）。
"""

from __future__ import annotations

import datetime as _dt

import pytest

from teamagent.skills.morning_digest import delivery_calendar as dc

_D = _dt.date
ON = dc.DeliveryCalendar(skip_holidays=True)
OFF = dc.DeliveryCalendar()
YEAR_END = frozenset({_D(2026, 12, 29), _D(2026, 12, 30), _D(2026, 12, 31)})


def test_flag_off_keeps_todays_behaviour() -> None:
    """OFF（既定）: 祝日も配信日・走査は 3 日のまま＝今の本番と同じ。"""
    assert OFF.is_delivery_day(_D(2026, 10, 12))
    assert OFF.holiday_reason(_D(2026, 10, 12)) is None
    assert OFF.mail_lookback_days(_D(2026, 10, 13)) == 3
    assert OFF.prev_delivery_day(_D(2026, 10, 13)) == _D(2026, 10, 12)


def test_sports_day_is_skipped_and_the_next_day_scans_four_days() -> None:
    """10/12(月) スポーツの日は休み。10/13(火) は前の配信日 10/9(金) から 4 日。"""
    assert ON.skip_reason(_D(2026, 10, 12)) == dc.SKIP_JP_HOLIDAY
    assert ON.next_delivery_day(_D(2026, 10, 9)) == _D(2026, 10, 13)
    assert ON.prev_delivery_day(_D(2026, 10, 13)) == _D(2026, 10, 9)
    assert ON.mail_lookback_days(_D(2026, 10, 13)) == 4


def test_culture_day_and_the_following_wednesday() -> None:
    """11/3(火) は休み。11/4(水) は前の配信日 11/2(月) から 2 日＝最低の 3 日のまま。"""
    assert ON.skip_reason(_D(2026, 11, 3)) == dc.SKIP_JP_HOLIDAY
    assert ON.next_delivery_day(_D(2026, 11, 2)) == _D(2026, 11, 4)
    assert ON.mail_lookback_days(_D(2026, 11, 4)) == 3


def test_ordinary_days_keep_three_days() -> None:
    """ふだんの月曜（前の配信日が金曜）・火曜は今と同じ 3 日。"""
    assert ON.mail_lookback_days(_D(2026, 10, 19)) == 3  # 月曜
    assert ON.mail_lookback_days(_D(2026, 10, 20)) == 3  # 火曜


def test_long_holidays_2026() -> None:
    """大型連休明けは連休の前の配信日までさかのぼる。"""
    # 大型連休: 4/29(水) 5/4(月)〜5/6(水・振替休日)
    assert ON.next_delivery_day(_D(2026, 5, 1)) == _D(2026, 5, 7)
    assert ON.mail_lookback_days(_D(2026, 5, 7)) == 6
    assert ON.mail_lookback_days(_D(2026, 4, 30)) == 3  # 4/28 から 2 日→3
    # 9/21(月)〜9/23(水)（9/22 は国民の休日）
    assert ON.next_delivery_day(_D(2026, 9, 18)) == _D(2026, 9, 24)
    assert ON.mail_lookback_days(_D(2026, 9, 24)) == 6


def test_year_end_with_company_holidays() -> None:
    """年末年始: 会社休日 12/29〜12/31 を足すと 12/28(月) の次は 1/4(月)・走査 7 日。"""
    cal = dc.DeliveryCalendar(skip_holidays=True, extra_skip_dates=YEAR_END)
    assert cal.skip_reason(_D(2026, 12, 29)) == dc.SKIP_COMPANY_HOLIDAY
    assert cal.skip_reason(_D(2027, 1, 1)) == dc.SKIP_JP_HOLIDAY
    assert cal.next_delivery_day(_D(2026, 12, 28)) == _D(2027, 1, 4)
    assert cal.prev_delivery_day(_D(2027, 1, 4)) == _D(2026, 12, 28)
    assert cal.mail_lookback_days(_D(2027, 1, 4)) == 7


def test_year_end_without_company_holidays() -> None:
    """会社休日を足さなければ 12/29〜12/31 は配信日。1/4 は 12/31 から 4 日。"""
    assert ON.is_delivery_day(_D(2026, 12, 29))
    assert ON.next_delivery_day(_D(2026, 12, 31)) == _D(2027, 1, 4)
    assert ON.mail_lookback_days(_D(2027, 1, 4)) == 4


def test_company_holidays_do_nothing_when_the_flag_is_off() -> None:
    """会社休日は祝日スキップの一部。OFF のときは効かない（今と同じ）。"""
    cal = dc.DeliveryCalendar(skip_holidays=False, extra_skip_dates=YEAR_END)
    assert cal.is_delivery_day(_D(2026, 12, 29))
    assert cal.is_delivery_day(_D(2027, 1, 1))


def test_lookback_is_capped_at_the_schema_limit() -> None:
    """長い休み（3 週間）でも MorningDigestInput の上限 14 日を超えない。"""
    long_break = frozenset(_D(2026, 12, 1) + _dt.timedelta(days=i) for i in range(21))
    cal = dc.DeliveryCalendar(skip_holidays=True, extra_skip_dates=long_break)
    assert cal.mail_lookback_days(_D(2026, 12, 22)) == dc.MAX_LOOKBACK_DAYS == 14


def test_weekend_is_not_a_holiday_reason() -> None:
    """土日は cron の側で休み。祝日の理由には数えない（手動の土曜実行を止めない）。"""
    assert ON.holiday_reason(_D(2026, 10, 10)) is None  # 土曜
    assert ON.skip_reason(_D(2026, 10, 10)) == dc.SKIP_WEEKEND
    assert ON.next_delivery_day(_D(2026, 10, 10)) == _D(2026, 10, 13)  # 日→月(祝)→火


def test_days_outside_the_table_are_delivery_days() -> None:
    """表の範囲外（2028-01-10 は実際には成人の日）は配信を止めない側に倒す。"""
    assert ON.holiday_reason(_D(2028, 1, 10)) is None
    assert ON.is_delivery_day(_D(2028, 1, 10))
    assert ON.mail_lookback_days(_D(2028, 1, 11)) == 3


def test_parse_extra_skip_dates() -> None:
    dates, invalid = dc.parse_extra_skip_dates(
        " 2026-12-29, 2026-12-30 ,,2026-12-29, 20261231, 2026/12/31, 12-31, "
    )
    assert dates == frozenset({_D(2026, 12, 29), _D(2026, 12, 30)})
    assert invalid == 3  # 20261231・2026/12/31・12-31（重複と空要素は数えない）
    assert dc.parse_extra_skip_dates("") == (frozenset(), 0)
    assert dc.parse_extra_skip_dates(None) == (frozenset(), 0)


def test_parse_extra_skip_dates_caps_the_count() -> None:
    raw = ",".join((_D(2026, 1, 1) + _dt.timedelta(days=i)).isoformat() for i in range(70))
    dates, invalid = dc.parse_extra_skip_dates(raw)
    assert len(dates) == dc.MAX_EXTRA_SKIP_DATES == 60
    assert invalid == 10


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dc.HOLIDAY_SKIP_ENV, raising=False)
    monkeypatch.setenv(dc.EXTRA_SKIP_DATES_ENV, "2026-12-29")
    assert dc.DeliveryCalendar.from_env() == OFF  # OFF なら会社休日も読まない
    monkeypatch.setenv(dc.HOLIDAY_SKIP_ENV, "true")
    cal = dc.DeliveryCalendar.from_env()
    assert cal.skip_holidays is True
    assert cal.extra_skip_dates == frozenset({_D(2026, 12, 29)})
    for off in ("", "0", "false", "no", "off"):
        monkeypatch.setenv(dc.HOLIDAY_SKIP_ENV, off)
        assert dc.holiday_skip_enabled() is False
    for on in ("1", "true", "TRUE", " yes "):
        monkeypatch.setenv(dc.HOLIDAY_SKIP_ENV, on)
        assert dc.holiday_skip_enabled() is True
