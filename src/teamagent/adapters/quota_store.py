"""動画分析クォータ台帳（v0.3 Task 10・migration 0017 の video_usage）。

Step 0 裁定:
  - 計数単位 = Gemini に投げた**実本数**（video_analysis=1/呼・video_algorithm=実分析本数）
  - 月次リセット = **JST の月初**（month 列は 'YYYY-MM'・アプリ側で JST 算出）
  - 障害時 = **fail-open**（クォータ DB が落ちても分析は通す・WARN＋ops 可視化。
    セキュリティ境界ではなくコスト制御のため可用性を優先＝統一原則)

原子性: 判定と加算を 1 文の条件付き UPSERT で行う（TOCTOU なし・リトライ二重計上は
ON CONFLICT の加算が「押した回数」に比例するため、呼び出し側は 1 API 呼び出しにつき
1 回だけ try_consume すること）。
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_JST = _dt.timezone(_dt.timedelta(hours=9))

# 条件付き原子 UPSERT: 上限内なら加算して used を返す。上限超過なら何も変えず 0 行。
# INSERT 側（月初回）も WHERE は効かないため VALUES 時点で count<=limit を呼び出し側で保証。
_CONSUME_SQL = """
INSERT INTO video_usage (user_email, month, used)
VALUES (%(email)s, %(month)s, %(count)s)
ON CONFLICT (user_email, month) DO UPDATE
    SET used = video_usage.used + EXCLUDED.used
    WHERE video_usage.used + EXCLUDED.used <= %(limit)s
RETURNING used
"""

_PEEK_SQL = "SELECT used FROM video_usage WHERE user_email = %(email)s AND month = %(month)s"


def current_month_jst(now: _dt.datetime | None = None) -> str:
    d = (now or _dt.datetime.now(tz=_JST)).astimezone(_JST)
    return f"{d.year:04d}-{d.month:02d}"


@dataclass(frozen=True)
class QuotaResult:
    allowed: bool
    used: int  # 消費後の使用数（blocked 時は現在値・-1 は「台帳を読めず不明」）
    limit: int
    requested: int = 0  # 要求本数（blocked の文面で「残数 vs 要求」を書き分けるため）

    @property
    def used_known(self) -> bool:
        """台帳から使用数を読めたか（fail-open/peek 失敗時は False）。"""

        return self.used >= 0

    @property
    def remaining(self) -> int:
        """今月あと何本ぶん消費できるか。不明なら 0（＝数字を語らない側に倒す）。"""

        if not self.used_known:
            return 0
        return max(0, self.limit - self.used)


_RESET_NOTE = "リセットは来月1日（JST）です。"


def quota_block_message(result: QuotaResult) -> str:
    """ブロック時の利用者向け文面。**事実に合わせて 2 通りに書き分ける**。

    - 残数 0: 本当に使い切った＝「上限に達しました」。
    - 残数 1 以上: 使い切ってはいない。要求本数が残数を超えただけなので、
      「残り N 本です。N 本で進めますか？」と**残数と選択肢**を出す（断らない）。
      2026-09-11 本番実測: 使用 14/上限 20 で「上限に達しました」と出て事実と食い違った。
    """

    remaining = result.remaining
    if remaining <= 0:
        usage = f"（使用 {result.used}本）" if result.used_known else ""
        return (
            f"VIDEO_QUOTA_EXCEEDED: 今月の動画分析上限（{result.limit}本）に達しました"
            f"{usage}。{_RESET_NOTE}"
            "お急ぎの場合は管理者に上限引き上げを依頼してください。"
        )
    requested = max(0, result.requested)
    asked = f"今回のご依頼は{requested}本でした。" if requested > remaining else ""
    return (
        f"VIDEO_QUOTA_PARTIAL_AVAILABLE: 今月の残りは{remaining}本です"
        f"（{result.limit}本中 {result.used}本使用）。{asked}"
        f"{remaining}本で進めますか？「{remaining}本で」とお答えいただければ、そのまま分析します。"
        f"{_RESET_NOTE}"
    )


class VideoQuotaStore:
    """video_usage への原子的 check-and-increment（RLS: 本人行のみ・fail-open）。"""

    def __init__(self, pg: Any | None = None, *, limit: int | None = None) -> None:
        self._pg = pg
        self._limit = limit if limit is not None else _env_limit()

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("VIDEO_QUOTA_ENABLED", "").strip().lower() in {"1", "true", "yes"}

    def _ensure_pg(self) -> Any:
        if self._pg is None:
            from teamagent.adapters.pgvector_client import PgVectorClient

            self._pg = PgVectorClient.from_env()
        return self._pg

    def try_consume(self, user_email: str, count: int, *, request_id: str) -> QuotaResult:
        """count 本ぶん消費を試みる。上限超過なら allowed=False（消費なし）。

        月初回の INSERT は ON CONFLICT の WHERE が効かないため count > limit は事前拒否。
        DB 障害は fail-open（allowed=True・WARN）＝分析を止めない。
        """
        email = (user_email or "").strip().lower()
        limit = self._limit
        if not email or count <= 0:
            return QuotaResult(allowed=True, used=0, limit=limit, requested=max(0, count))
        month = current_month_jst()
        if count > limit:
            # 月初回の INSERT は ON CONFLICT の WHERE が効かないため DB へ出す前に弾く。
            # ただし残数の実測は返す（文面が「残り N 本で進めますか？」を出せるように）。
            used = self._peek_used(email, month, request_id=request_id)
            result = QuotaResult(
                allowed=False,
                used=used if used is not None else -1,
                limit=limit,
                requested=count,
            )
            logger.info(
                "video_quota_blocked",
                request_id=request_id,
                count=count,
                requested=count,
                used=result.used,
                limit=limit,
                remaining=result.remaining,
            )
            return result
        start = time.perf_counter()
        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    _CONSUME_SQL,
                    {"email": email, "month": month, "count": count, "limit": limit},
                )
                row = cur.fetchone()
                if row is not None:
                    used = int(row["used"] if isinstance(row, dict) else row[0])
                    conn.commit()
                    logger.info(
                        "video_quota_consumed",
                        request_id=request_id,
                        count=count,
                        used=used,
                        limit=limit,
                        latency_ms=int((time.perf_counter() - start) * 1000),
                    )
                    return QuotaResult(allowed=True, used=used, limit=limit, requested=count)
                # 上限超過: 現在値を読んで返す（メッセージ用＝残数と要求の書き分けに使う）。
                cur.execute(_PEEK_SQL, {"email": email, "month": month})
                peek = cur.fetchone()
                conn.commit()
                used = int((peek["used"] if isinstance(peek, dict) else peek[0]) if peek else limit)
                result = QuotaResult(allowed=False, used=used, limit=limit, requested=count)
                logger.info(
                    "video_quota_blocked",
                    request_id=request_id,
                    count=count,
                    requested=count,
                    used=used,
                    limit=limit,
                    remaining=result.remaining,
                )
                return result
        except Exception as e:
            # fail-open（裁定）: コスト制御で業務を止めない。ops はこの WARN を監視する。
            logger.warning("video_quota_failed", request_id=request_id, error=type(e).__name__)
            return QuotaResult(allowed=True, used=-1, limit=limit, requested=count)

    def _peek_used(self, email: str, month: str, *, request_id: str) -> int | None:
        """当月の使用数を読むだけ（消費しない）。読めなければ None＝不明。"""

        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(_PEEK_SQL, {"email": email, "month": month})
                row = cur.fetchone()
                conn.commit()
                if row is None:
                    return 0
                return int(row["used"] if isinstance(row, dict) else row[0])
        except Exception as e:
            logger.warning("video_quota_peek_failed", request_id=request_id, error=type(e).__name__)
            return None


def _env_limit() -> int:
    try:
        return max(1, int(os.environ.get("VIDEO_MONTHLY_QUOTA", "20")))
    except ValueError:
        return 20


__all__ = [
    "QuotaResult",
    "VideoQuotaStore",
    "current_month_jst",
    "quota_block_message",
]
