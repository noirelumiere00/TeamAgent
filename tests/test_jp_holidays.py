"""祝日の表（teamagent.jp_holidays）のテスト。

表は内閣府の公表を写したもの。写し間違いを「もう一度同じ表を書く」テストで検出しても
意味が無いので、**祝日法の規則から表を導き直して**突き合わせる。外から与えるのは、
毎年 2 月 1 日の官報で決まる春分・秋分の日だけ（2026-09-29 に内閣府のページで照合）。
"""

from __future__ import annotations

import datetime as _dt

import pytest

from teamagent import jp_holidays

_D = _dt.date

#: 官報で決まる春分・秋分（内閣府「国民の祝日について」で照合）。
_EQUINOXES: dict[int, tuple[_dt.date, _dt.date]] = {
    2026: (_D(2026, 3, 20), _D(2026, 9, 23)),
    2027: (_D(2027, 3, 21), _D(2027, 9, 23)),
}


def _nth_monday(year: int, month: int, n: int) -> _dt.date:
    first = _D(year, month, 1)
    offset = (0 - first.weekday()) % 7  # 最初の月曜まで
    return first + _dt.timedelta(days=offset + 7 * (n - 1))


def _derive(year: int) -> dict[_dt.date, str]:
    """祝日法（2020 年以降の形）から 1 年分の祝日と休日を導く。"""
    spring, autumn = _EQUINOXES[year]
    base = {
        _D(year, 1, 1): "元日",
        _nth_monday(year, 1, 2): "成人の日",
        _D(year, 2, 11): "建国記念の日",
        _D(year, 2, 23): "天皇誕生日",
        spring: "春分の日",
        _D(year, 4, 29): "昭和の日",
        _D(year, 5, 3): "憲法記念日",
        _D(year, 5, 4): "みどりの日",
        _D(year, 5, 5): "こどもの日",
        _nth_monday(year, 7, 3): "海の日",
        _D(year, 8, 11): "山の日",
        _nth_monday(year, 9, 3): "敬老の日",
        autumn: "秋分の日",
        _nth_monday(year, 10, 2): "スポーツの日",
        _D(year, 11, 3): "文化の日",
        _D(year, 11, 23): "勤労感謝の日",
    }
    rest: dict[_dt.date, str] = {}
    # 3 条 2 項（振替休日）: 祝日が日曜なら、その後で最も近い「祝日でない日」。
    for d in sorted(base):
        if d.weekday() == 6:
            n = d + _dt.timedelta(days=1)
            while n in base or n in rest:
                n += _dt.timedelta(days=1)
            rest[n] = "休日"
    # 3 条 3 項（国民の休日）: 前日と翌日が「国民の祝日」である日（祝日でない日に限る）。
    d = _D(year, 1, 2)
    while d.year == year:
        if (
            d not in base
            and d not in rest
            and d - _dt.timedelta(days=1) in base
            and d + _dt.timedelta(days=1) in base
        ):
            rest[d] = "休日"
        d += _dt.timedelta(days=1)
    return {**base, **rest}


@pytest.mark.parametrize("year", [2026, 2027])
def test_table_matches_the_holiday_act(year: int) -> None:
    """表 = 祝日法の規則から導いた祝日（振替休日・国民の休日を含む）。1 日でも違えば赤。"""
    table = {
        d: jp_holidays.holiday_name(d)
        for d in jp_holidays.holidays_between(_D(year, 1, 1), _D(year, 12, 31))
    }
    assert table == _derive(year)


def test_counts_and_known_days() -> None:
    """内閣府の表どおりの日数（2026 年 18 日・2027 年 17 日）と、運用上大事な日。"""
    assert len(jp_holidays.holidays_between(_D(2026, 1, 1), _D(2026, 12, 31))) == 18
    assert len(jp_holidays.holidays_between(_D(2027, 1, 1), _D(2027, 12, 31))) == 17
    assert jp_holidays.holiday_name(_D(2026, 10, 12)) == "スポーツの日"
    assert jp_holidays.holiday_name(_D(2026, 11, 3)) == "文化の日"
    assert jp_holidays.holiday_name(_D(2026, 5, 6)) == "休日"  # 振替休日
    assert jp_holidays.holiday_name(_D(2026, 9, 22)) == "休日"  # 国民の休日
    assert jp_holidays.holiday_name(_D(2027, 3, 22)) == "休日"  # 振替休日
    assert jp_holidays.is_jp_holiday(_D(2026, 10, 13)) is False  # 明けの火曜
    assert jp_holidays.is_jp_holiday(_D(2026, 12, 31)) is False  # 大みそかは祝日ではない
    assert jp_holidays.is_jp_holiday(_D(2027, 1, 1)) is True


def test_out_of_range_is_unknown_not_false() -> None:
    """範囲外は None（分からない）。False と混ぜると「確かに平日」と言い切ってしまう。"""
    assert jp_holidays.is_jp_holiday(_D(2025, 12, 31)) is None
    assert jp_holidays.is_jp_holiday(_D(2028, 1, 1)) is None  # 実際は元日
    assert jp_holidays.holiday_name(_D(2028, 1, 1)) is None
    assert jp_holidays.is_covered(_D(2026, 1, 1)) and jp_holidays.is_covered(_D(2027, 12, 31))


@pytest.mark.parametrize(
    ("today", "state", "days_left"),
    [
        (_D(2026, 9, 29), "ok", 458),
        (_D(2027, 10, 31), "ok", 61),
        (_D(2027, 11, 1), "expiring", 60),  # 期限の 60 日前から知らせる
        (_D(2027, 12, 31), "expiring", 0),
        (_D(2028, 1, 1), "expired", -1),
        (_D(2025, 12, 31), "not_started", 730),
    ],
)
def test_coverage_status_boundaries(today: _dt.date, state: str, days_left: int) -> None:
    st = jp_holidays.coverage_status(today)
    assert (st.state, st.days_left) == (state, days_left)
    assert st.coverage_end == _D(2027, 12, 31)
    assert st.stale is (state != "ok")


def test_coverage_notice_is_one_line_only_when_stale() -> None:
    assert jp_holidays.coverage_notice(_D(2027, 10, 31)) is None
    expiring = jp_holidays.coverage_notice(_D(2027, 11, 1))
    assert expiring is not None and "あと 60 日" in expiring and "\n" not in expiring
    expired = jp_holidays.coverage_notice(_D(2028, 1, 5))
    assert expired is not None and "切れています" in expired and "\n" not in expired
