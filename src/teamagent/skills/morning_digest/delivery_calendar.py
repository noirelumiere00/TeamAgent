"""配信日カレンダー（朝ダイジェストが届く日の唯一の判定・純関数・JST の暦日）。

「その日に朝ダイジェストが届くか」「次に届く日」「前に届いた日」を 1 か所で決める。
F0 の祝日スキップと祝日明けの走査範囲の拡張がここを使い、F1（期限の前の配信日に
知らせる）・F2 も同じものを使う（2 か所が別々の祝日判定を持たない）。

配信日の定義:
  - 土日は配信しない（EventBridge の cron が MON-FRI）。
  - ``MORNING_DIGEST_HOLIDAY_SKIP`` が ON のときだけ、祝日（``teamagent.jp_holidays``）と
    会社休日（``MORNING_DIGEST_EXTRA_SKIP_DATES``）も配信しない。
  - OFF（既定）のときは祝日も配信日＝今の本番と同じ。会社休日の env も読まない。

表の範囲外（2028 年以降など）の平日は **配信日** として扱う（分からない日に止めない）。

走査範囲（``mail_lookback_days``）:
  ``max(3, 今日 − 前の配信日)``。ふだんは今と同じ 3 日で、祝日明けだけ広がる
  （例: 10/12 月が祝日 → 10/13 火は前の配信日が 10/9 金なので 4 日）。
  ⚠️ 個人別配信（MORNING_DIGEST_PERSONALIZED）で前の配信日の送信が 9:30 より早かった場合、
     その時刻差（最大 3.5 時間）に届いたメールは拾えない。上限（max_messages=30）も変えない。
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass, field

import structlog

from teamagent import jp_holidays

logger = structlog.get_logger(__name__)

#: 祝日スキップと祝日明けの走査範囲の拡張（既定 OFF）。
HOLIDAY_SKIP_ENV = "MORNING_DIGEST_HOLIDAY_SKIP"
#: 会社休日（YYYY-MM-DD のカンマ区切り）。祝日スキップが ON のときだけ効く。
EXTRA_SKIP_DATES_ENV = "MORNING_DIGEST_EXTRA_SKIP_DATES"

#: ``MorningDigestInput.lookback_days`` の既定と上限（schema.py と同じ値）。
BASE_LOOKBACK_DAYS = 3
MAX_LOOKBACK_DAYS = 14

#: 会社休日として受け付ける最大件数（env の書き間違いで探索が伸びないように）。
MAX_EXTRA_SKIP_DATES = 60
#: 次／前の配信日を探す最大日数（到達しないはずの安全弁）。
_SEARCH_LIMIT_DAYS = 400

SKIP_WEEKEND = "weekend"
SKIP_JP_HOLIDAY = "jp_holiday"
SKIP_COMPANY_HOLIDAY = "company_holiday"

_TRUE = frozenset({"1", "true", "yes"})


def holiday_skip_enabled() -> bool:
    """``MORNING_DIGEST_HOLIDAY_SKIP`` が ON か（既定 OFF）。"""
    return os.environ.get(HOLIDAY_SKIP_ENV, "").strip().lower() in _TRUE


def parse_extra_skip_dates(raw: str | None) -> tuple[frozenset[_dt.date], int]:
    """``"2026-12-29,2026-12-30"`` → (日付の集合, 読めなかった件数)。

    空白・空要素は無視する。読めない要素と上限を超えた分は捨てて件数だけ返す
    （値そのものはログに出さない＝何が書かれていても漏らさない）。
    """
    dates: set[_dt.date] = set()
    invalid = 0
    for token in (raw or "").split(","):
        s = token.strip()
        if not s:
            continue
        try:
            d = _dt.date.fromisoformat(s)
        except ValueError:
            invalid += 1
            continue
        if len(s) != 10:  # "20261229" 形などの曖昧な書き方は受け付けない
            invalid += 1
            continue
        if d in dates:
            continue
        if len(dates) >= MAX_EXTRA_SKIP_DATES:
            invalid += 1
            continue
        dates.add(d)
    return frozenset(dates), invalid


@dataclass(frozen=True)
class DeliveryCalendar:
    """朝ダイジェストの配信日の判定。``skip_holidays=False`` なら土日だけが休み。"""

    skip_holidays: bool = False
    extra_skip_dates: frozenset[_dt.date] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> DeliveryCalendar:
        """env から作る。祝日スキップが OFF なら会社休日の env も読まない（今と同じ）。"""
        if not holiday_skip_enabled():
            return cls()
        dates, invalid = parse_extra_skip_dates(os.environ.get(EXTRA_SKIP_DATES_ENV, ""))
        if invalid:
            logger.warning("morning_digest_extra_skip_dates_invalid", invalid=invalid)
        return cls(skip_holidays=True, extra_skip_dates=dates)

    def holiday_reason(self, day: _dt.date) -> str | None:
        """祝日・会社休日なら理由（``jp_holiday`` / ``company_holiday``）。土日は見ない。

        祝日スキップが OFF なら常に ``None``。表の範囲外の日は祝日とみなさない。
        """
        if not self.skip_holidays:
            return None
        if jp_holidays.is_jp_holiday(day) is True:
            return SKIP_JP_HOLIDAY
        if day in self.extra_skip_dates:
            return SKIP_COMPANY_HOLIDAY
        return None

    def skip_reason(self, day: _dt.date) -> str | None:
        """配信しない日なら理由（``weekend`` / ``jp_holiday`` / ``company_holiday``）。"""
        if day.weekday() >= 5:
            return SKIP_WEEKEND
        return self.holiday_reason(day)

    def is_delivery_day(self, day: _dt.date) -> bool:
        """その日に朝ダイジェストが届くか。"""
        return self.skip_reason(day) is None

    def next_delivery_day(self, day: _dt.date) -> _dt.date:
        """``day`` より **後** の最初の配信日。"""
        return self._step(day, +1)

    def prev_delivery_day(self, day: _dt.date) -> _dt.date:
        """``day`` より **前** の最後の配信日。"""
        return self._step(day, -1)

    def mail_lookback_days(self, day: _dt.date) -> int:
        """その日のメール走査範囲（日数）。``max(3, day − 前の配信日)``・上限 14。"""
        gap = (day - self.prev_delivery_day(day)).days
        return min(MAX_LOOKBACK_DAYS, max(BASE_LOOKBACK_DAYS, gap))

    def _step(self, day: _dt.date, direction: int) -> _dt.date:
        cur = day
        for _ in range(_SEARCH_LIMIT_DAYS):
            cur = cur + _dt.timedelta(days=direction)
            if self.is_delivery_day(cur):
                return cur
        # 会社休日は最大 60 件・祝日は年 20 件未満なので 400 日以内に必ず平日が残る。
        raise RuntimeError("delivery day not found within search limit")


__all__ = [
    "BASE_LOOKBACK_DAYS",
    "EXTRA_SKIP_DATES_ENV",
    "HOLIDAY_SKIP_ENV",
    "MAX_EXTRA_SKIP_DATES",
    "MAX_LOOKBACK_DAYS",
    "SKIP_COMPANY_HOLIDAY",
    "SKIP_JP_HOLIDAY",
    "SKIP_WEEKEND",
    "DeliveryCalendar",
    "holiday_skip_enabled",
    "parse_extra_skip_dates",
]
