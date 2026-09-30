"""日本の祝日の表（内閣府「国民の祝日」2026〜2027 年を写したもの・純関数・依存なし）。

朝ダイジェストの祝日スキップ（F0）と、F1/F2 が使う「配信日カレンダー」
（``teamagent.skills.morning_digest.delivery_calendar``）の唯一の祝日の出どころ。

出典: 内閣府「国民の祝日について」https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html
（同ページの syukujitsu.csv と同じ内容。2026-09-29 に 2026・2027 年の全 35 日を照合済み）。
内閣府は振替休日（祝日法 3 条 2 項）と国民の休日（同 3 項）をどちらも「休日」と書くので、
名前もそれに合わせる（どちらなのかは表の行末コメントに書く）。

⚠️ 外部依存（jpholiday 等）は入れない裁定（calendar_freebusy/free_windows.py 参照）。
   静的な表の鮮度の心配は ``coverage_status`` で見張る:
   期限（``COVERAGE_END``）の 60 日前から runner が ``jp_holiday_table_stale`` を出す。
⚠️ 表の範囲外の日は ``is_jp_holiday`` が ``None``（＝分からない）を返す。呼び出し側は
   「祝日ではない」として扱い、配信を止めない（止める側に倒すと全員に届かない日が出る）。

表を延ばすとき（2028 年以降）:
  1. 内閣府の同ページで翌年分を確かめる（春分・秋分は前年 2 月 1 日の官報で決まる）。
  2. ``_HOLIDAYS`` に足し、``COVERAGE_END`` をその年の 12/31 にする。
  3. tests/test_jp_holidays.py の「法の規則から導いた表と一致する」テストに、
     その年の春分・秋分の日付を足す（それ以外の祝日は規則から自動で導かれる）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

_D = _dt.date

#: 表が正しいと言える最初の日と最後の日（両端を含む）。
COVERAGE_START: _dt.date = _D(2026, 1, 1)
COVERAGE_END: _dt.date = _D(2027, 12, 31)

#: 期限の何日前から「期限が近い」と知らせるか。
STALE_WARN_DAYS = 60

_HOLIDAYS: dict[_dt.date, str] = {
    # ── 2026 年（令和 8 年）18 日 ──
    _D(2026, 1, 1): "元日",
    _D(2026, 1, 12): "成人の日",
    _D(2026, 2, 11): "建国記念の日",
    _D(2026, 2, 23): "天皇誕生日",
    _D(2026, 3, 20): "春分の日",
    _D(2026, 4, 29): "昭和の日",
    _D(2026, 5, 3): "憲法記念日",
    _D(2026, 5, 4): "みどりの日",
    _D(2026, 5, 5): "こどもの日",
    _D(2026, 5, 6): "休日",  # 振替休日（5/3 が日曜）
    _D(2026, 7, 20): "海の日",
    _D(2026, 8, 11): "山の日",
    _D(2026, 9, 21): "敬老の日",
    _D(2026, 9, 22): "休日",  # 国民の休日（敬老の日と秋分の日に挟まれた日）
    _D(2026, 9, 23): "秋分の日",
    _D(2026, 10, 12): "スポーツの日",
    _D(2026, 11, 3): "文化の日",
    _D(2026, 11, 23): "勤労感謝の日",
    # ── 2027 年（令和 9 年）17 日 ──
    _D(2027, 1, 1): "元日",
    _D(2027, 1, 11): "成人の日",
    _D(2027, 2, 11): "建国記念の日",
    _D(2027, 2, 23): "天皇誕生日",
    _D(2027, 3, 21): "春分の日",
    _D(2027, 3, 22): "休日",  # 振替休日（3/21 が日曜）
    _D(2027, 4, 29): "昭和の日",
    _D(2027, 5, 3): "憲法記念日",
    _D(2027, 5, 4): "みどりの日",
    _D(2027, 5, 5): "こどもの日",
    _D(2027, 7, 19): "海の日",
    _D(2027, 8, 11): "山の日",
    _D(2027, 9, 20): "敬老の日",
    _D(2027, 9, 23): "秋分の日",
    _D(2027, 10, 11): "スポーツの日",
    _D(2027, 11, 3): "文化の日",
    _D(2027, 11, 23): "勤労感謝の日",
}


def is_covered(day: _dt.date) -> bool:
    """その日が表の範囲内か。"""
    return COVERAGE_START <= day <= COVERAGE_END


def is_jp_holiday(day: _dt.date) -> bool | None:
    """祝日（振替休日・国民の休日を含む）なら True。表の範囲外は ``None``（分からない）。"""
    if not is_covered(day):
        return None
    return day in _HOLIDAYS


def holiday_name(day: _dt.date) -> str | None:
    """祝日の名前（内閣府の表記）。祝日でない日・表の範囲外は ``None``。"""
    return _HOLIDAYS.get(day) if is_covered(day) else None


def holidays_between(start: _dt.date, end: _dt.date) -> list[_dt.date]:
    """[start, end]（両端を含む）にある祝日を昇順で返す。範囲外の部分は含まれない。"""
    return sorted(d for d in _HOLIDAYS if start <= d <= end)


@dataclass(frozen=True)
class CoverageStatus:
    """表の鮮度。``state`` は ok / expiring / expired / not_started のどれか。"""

    state: str
    #: 期限（``COVERAGE_END``）までの日数。期限を過ぎていれば負。
    days_left: int
    coverage_end: _dt.date

    @property
    def stale(self) -> bool:
        """知らせるべき状態か（期限が近い・切れた・始まっていない）。"""
        return self.state != "ok"


def coverage_status(today: _dt.date, *, warn_days: int = STALE_WARN_DAYS) -> CoverageStatus:
    """今日の時点での表の鮮度。期限の ``warn_days`` 日前（当日を含む）から expiring。"""
    days_left = (COVERAGE_END - today).days
    if today < COVERAGE_START:
        state = "not_started"
    elif days_left < 0:
        state = "expired"
    elif days_left <= warn_days:
        state = "expiring"
    else:
        state = "ok"
    return CoverageStatus(state=state, days_left=days_left, coverage_end=COVERAGE_END)


def coverage_notice(today: _dt.date, *, warn_days: int = STALE_WARN_DAYS) -> str | None:
    """管理者向けの 1 行（知らせる必要が無ければ ``None``）。

    PR-0a の管理者 DM がこの 1 行をそのまま足す想定（利用者の DM には出さない）。
    """
    st = coverage_status(today, warn_days=warn_days)
    end = st.coverage_end.isoformat()
    if st.state == "expiring":
        return (
            f"⚠️ 祝日の表の期限（{end}）まであと {st.days_left} 日です。"
            "内閣府の翌年分を src/teamagent/jp_holidays.py に足してください"
            "（期限を過ぎても配信は止まらず、祝日にも届くようになります）。"
        )
    if st.state == "expired":
        return (
            f"⚠️ 祝日の表の期限（{end}）が切れています。祝日にも朝ダイジェストが届いています。"
            "内閣府の翌年分を src/teamagent/jp_holidays.py に足してください。"
        )
    if st.state == "not_started":
        return f"⚠️ 祝日の表は {COVERAGE_START.isoformat()} からです（今日は表の範囲外です）。"
    return None


__all__ = [
    "COVERAGE_END",
    "COVERAGE_START",
    "STALE_WARN_DAYS",
    "CoverageStatus",
    "coverage_notice",
    "coverage_status",
    "holiday_name",
    "holidays_between",
    "is_covered",
    "is_jp_holiday",
]
