"""日次上限と費用 cap（計画 §4 論点 7 の初期値）。

初期値: **1 人 2 本/日・全体 20 本/日・``CLIP_COST_CAP_USD=1.0``（1 依頼あたり）**。
``CLIP_DAILY_COST_CAP_USD`` は全体の 1 日の上限で、既定は 1 依頼 cap × 全体本数。

規律（「できないことを利用者にやらせない」）:
- 上限に当たっても**断らない**。順番待ち（``busy``）か「明日以降」（``deferred``）の
  案内にし、枠が空く時刻と急ぎの連絡先まで文言に書く。
- 日次カウンタは **submit 受理時に加算し、失敗しても戻さない**（計画 §2-2 費用欄 ⑤）。
  戻すと「失敗させ続ければ無限に課金できる」経路になる。
- 日付は Asia/Tokyo の暦日で切る（UTC 日付だと朝 9 時にリセットされる）。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

JST = timezone(timedelta(hours=9), "JST")

#: 1 人 1 日あたりの本数（計画 §4 論点 7）。
DEFAULT_PER_USER_PER_DAY = 2
#: 全体 1 日あたりの本数。
DEFAULT_GLOBAL_PER_DAY = 20
#: 1 依頼あたりの推論費用上限（USD）。
DEFAULT_COST_CAP_USD = 1.0

#: 急ぎの連絡先（上限文言に必ず書く。連絡先を伏せると利用者が詰む）。
ESCALATION_CONTACT = "小俣さん"

DecisionKind = Literal["accept", "deferred"]


def _envint(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = default if not raw else int(raw)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _envfloat(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = default if not raw else float(raw)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def configured_per_user_per_day() -> int:
    return _envint(
        "CLIP_PROPOSAL_MAX_JOBS_PER_DAY", DEFAULT_PER_USER_PER_DAY, minimum=1, maximum=50
    )


def configured_global_per_day() -> int:
    return _envint(
        "CLIP_PROPOSAL_MAX_JOBS_PER_DAY_TOTAL",
        DEFAULT_GLOBAL_PER_DAY,
        minimum=1,
        maximum=500,
    )


def configured_cost_cap_usd() -> float:
    return _envfloat("CLIP_COST_CAP_USD", DEFAULT_COST_CAP_USD, minimum=0.01, maximum=50.0)


def configured_daily_cost_cap_usd() -> float:
    """全体の 1 日の費用上限。未設定なら 1 依頼 cap × 全体本数で導出する。"""

    default = configured_cost_cap_usd() * configured_global_per_day()
    return _envfloat("CLIP_DAILY_COST_CAP_USD", default, minimum=0.01, maximum=5000.0)


def jst_date_key(now: datetime | None = None) -> str:
    """JST の暦日キー（``YYYYMMDD``）。tz 無し datetime は UTC とみなす。"""

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(JST).strftime("%Y%m%d")


def next_reset_label(now: datetime | None = None) -> str:
    """次に枠が空く時刻の案内文（「明朝 6:00 以降」等ではなく暦日で言い切る）。"""

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    tomorrow = (moment.astimezone(JST) + timedelta(days=1)).date()
    return f"{tomorrow.month}/{tomorrow.day} 0:00（JST）"


@dataclass(frozen=True)
class QuotaDecision:
    """日次上限の判定結果。``kind='deferred'`` でもジョブは作らず、断りもしない。"""

    kind: DecisionKind
    reason: str = ""
    used_by_user: int = 0
    limit_per_user: int = 0
    used_total: int = 0
    limit_total: int = 0
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.kind == "accept"


class DailyQuota:
    """JST 暦日で切る本数カウンタ（プロセス内・mcp は desiredCount=1）。

    mcp タスクが 1 本なので、分散カウンタは不要。増やすと「台帳が読めないと受付
    できない」fail-closed 面が増えるだけ（omiyage の ``JobAdmission`` と同じ判断）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._date_key = ""
        self._per_user: dict[str, int] = {}
        self._total = 0
        self._cost_usd = 0.0

    def _roll_locked(self, date_key: str) -> None:
        if date_key != self._date_key:
            self._date_key = date_key
            self._per_user = {}
            self._total = 0
            self._cost_usd = 0.0

    def snapshot(self, user_key: str, *, now: datetime | None = None) -> tuple[int, int, float]:
        """(その人の本数, 全体の本数, 全体の実測費用)。日付が変わっていればリセットする。"""

        with self._lock:
            self._roll_locked(jst_date_key(now))
            return (self._per_user.get(user_key, 0), self._total, self._cost_usd)

    def try_reserve(self, user_key: str, *, now: datetime | None = None) -> QuotaDecision:
        """受理できるなら 1 本ぶん確保する。**失敗しても戻さない**（返す API を持たない）。"""

        per_user_limit = configured_per_user_per_day()
        total_limit = configured_global_per_day()
        daily_cost_cap = configured_daily_cost_cap_usd()
        with self._lock:
            self._roll_locked(jst_date_key(now))
            used_user = self._per_user.get(user_key, 0)
            used_total = self._total
            spent = self._cost_usd
            reason = ""
            if used_user >= per_user_limit:
                reason = "per_user"
            elif used_total >= total_limit:
                reason = "global"
            elif spent >= daily_cost_cap:
                reason = "daily_cost"
            if reason:
                return QuotaDecision(
                    kind="deferred",
                    reason=reason,
                    used_by_user=used_user,
                    limit_per_user=per_user_limit,
                    used_total=used_total,
                    limit_total=total_limit,
                    message=build_deferred_message(reason, now=now),
                )
            self._per_user[user_key] = used_user + 1
            self._total = used_total + 1
            return QuotaDecision(
                kind="accept",
                used_by_user=used_user + 1,
                limit_per_user=per_user_limit,
                used_total=used_total + 1,
                limit_total=total_limit,
            )

    def add_cost(self, cost_usd: float, *, now: datetime | None = None) -> float:
        """実測費用を全体カウンタへ加算する（リトライ分も呼び出し側で足して渡す）。"""

        with self._lock:
            self._roll_locked(jst_date_key(now))
            self._cost_usd += max(0.0, float(cost_usd))
            return self._cost_usd


_QUOTA = DailyQuota()


def shared_quota() -> DailyQuota:
    """プロセス共有の日次カウンタ。Skill は呼び出しのたびに生成されるので共有が要る。"""

    return _QUOTA


def reset_daily_quota() -> DailyQuota:
    """共有カウンタを作り直す（テスト用・本番経路からは呼ばない）。"""

    global _QUOTA
    _QUOTA = DailyQuota()
    return _QUOTA


def build_deferred_message(reason: str, *, now: datetime | None = None) -> str:
    """日次上限に当たったときの案内。**「できません」で終わらせない。**

    枠が空く時刻と急ぎの連絡先まで書く（計画 §2-2「混雑時は再依頼を求めない」）。
    """

    reset = next_reset_label(now)
    if reason == "per_user":
        head = f"今日ぶんの枠（お一人 {configured_per_user_per_day()} 本/日）を使い切りました。"
    elif reason == "global":
        head = f"今日は全体の枠（{configured_global_per_day()} 本/日）が埋まりました。"
    else:
        head = "今日は全体の費用の上限に達しました。"
    return (
        f"{head}枠が空くのは {reset} です。"
        f"そのタイミングで同じようにこのスレッドへ動画を貼っていただければ着手します。"
        f"急ぎでしたら {ESCALATION_CONTACT} へご連絡ください。"
    )


def build_queued_message(*, client_name: str, eta_minutes: int) -> str:
    """受付直後の即答（クライアント名をエコーバックして取り違えを止める）。"""

    who = client_name.strip() or "（クライアント名が取れなかったため、分かり次第反映します）"
    return (
        f"切り抜き提案の作成を始めました。「{who}」の素材として進めます。"
        "違っていればこのスレッドで教えてください。"
        f"目安 {eta_minutes} 分前後です。できたらこのスレッドに資料を添付します。"
    )


def build_busy_message(*, position: int, wait_minutes: int) -> str:
    """混雑時。**再依頼を求めず**、順番が来たら自動で着手すると言い切る。"""

    return (
        f"{position} 番目で受け付けました。順番が来たら自動で始めます"
        f"（目安 {wait_minutes} 分）。もう一度送っていただく必要はありません。"
        "できたらこのスレッドに資料を添付します。"
    )


@dataclass(frozen=True)
class CostLedger:
    """1 依頼の推論費用の実測。``spent`` は **リトライ分も含む累計**。"""

    cap_usd: float
    spent_usd: float = 0.0
    calls: int = 0

    def add(self, cost_usd: float) -> CostLedger:
        return CostLedger(
            cap_usd=self.cap_usd,
            spent_usd=self.spent_usd + max(0.0, float(cost_usd)),
            calls=self.calls + 1,
        )

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.cap_usd


__all__ = [
    "DEFAULT_COST_CAP_USD",
    "DEFAULT_GLOBAL_PER_DAY",
    "DEFAULT_PER_USER_PER_DAY",
    "ESCALATION_CONTACT",
    "JST",
    "CostLedger",
    "DailyQuota",
    "QuotaDecision",
    "build_busy_message",
    "build_deferred_message",
    "build_queued_message",
    "configured_cost_cap_usd",
    "configured_daily_cost_cap_usd",
    "configured_global_per_day",
    "configured_per_user_per_day",
    "jst_date_key",
    "next_reset_label",
    "reset_daily_quota",
    "shared_quota",
]
