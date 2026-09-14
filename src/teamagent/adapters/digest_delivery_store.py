"""朝ダイジェストの「その日もう送った」印（二重配信の唯一の止め口）。

migration 0026 の ``digest_delivery`` を ``teamagent_app`` ロール＋``app.user_email``
GUC で読み書きする。

設計の芯:
  - 判定は **DB の一意制約** に委ねる。``INSERT ... ON CONFLICT DO NOTHING`` の rowcount
    が 1 のときだけ「自分が送る」。アプリ側の if 文で調停しないので、planner 再実行・
    予約重複・一括実行との同時走行でも 2 通は物理的に出ない。
  - **fail-closed**: 印を取れなかった／確認できなかったときは **送らない**。ここを
    fail-open（送る）に倒すと、DB 障害の日に 29 名へ 2 通届く。無音は監視で拾えるが、
    誤配信は取り返せない。障害は ``error`` レベルで出し、既存の ErrorCount alarm へ流す。
  - ログは件数と request_id だけ（メールアドレスを出さない）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_TTL_DAYS = 14  # 診断の猶予だけ取る（長期保持しない）

_CLAIM_SQL = """
INSERT INTO digest_delivery (user_email, digest_date, origin, expires_at)
VALUES (%(email)s, %(day)s, %(origin)s, NOW() + make_interval(days => %(ttl_days)s))
ON CONFLICT (user_email, digest_date) DO NOTHING
"""

_RELEASE_SQL = """
DELETE FROM digest_delivery WHERE user_email = %(email)s AND digest_date = %(day)s
"""

# 期限切れ行の掃除。claim と **同じトランザクション** で流す（掃除ジョブ・cron を
# 増やさない）。RLS が効いているので消えるのは本人行だけ＝他人の印を落とさない。
_PURGE_SQL = """
DELETE FROM digest_delivery WHERE expires_at < NOW()
"""


def _normalise_email(user_email: str) -> str:
    return (user_email or "").strip().lower()


class DigestDeliveryStore:
    """(user, date) を 1 回だけ通す claim ストア。"""

    def __init__(self, pg: Any | None = None) -> None:
        self._pg = pg

    def _ensure_pg(self) -> Any:
        if self._pg is None:
            from teamagent.adapters.pgvector_client import PgVectorClient

            self._pg = PgVectorClient.from_env()
        return self._pg

    def claim(
        self,
        user_email: str,
        day: _dt.date,
        *,
        origin: str,
        request_id: str,
    ) -> bool:
        """その日の配信権を取る。**取れたときだけ True**（＝送ってよい）。

        ⚠️ 例外は False（fail-closed）。「確認できないから送っておく」は二重配信の道。

        同一トランザクションで期限切れ行（14 日）も掃除する。掃除の経路がどこにも
        無いと、SQL のコメントが宣言している保持期間が実装されていないことになる。
        """
        email = _normalise_email(user_email)
        if not email or "@" not in email or origin not in ("scheduled", "bulk"):
            return False
        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    _CLAIM_SQL,
                    {
                        "email": email,
                        "day": day,
                        "origin": origin,
                        "ttl_days": _TTL_DAYS,
                    },
                )
                claimed = int(cur.rowcount) == 1
                # ⚠️ rowcount を読んでから掃除する（順序を入れ替えると claim の
                #   判定が DELETE の rowcount になり、毎回 False＝1 通も出なくなる）。
                cur.execute(_PURGE_SQL)
                conn.commit()
            logger.info(
                "digest_delivery_claim", request_id=request_id, claimed=claimed, origin=origin
            )
            return claimed
        except Exception:
            # fail-closed。error レベルで出して既存の ErrorCount alarm へ流す
            # （無音配信停止を「正常」に見せない）。
            logger.error("digest_delivery_claim_failed", request_id=request_id, origin=origin)
            return False

    def release(self, user_email: str, day: _dt.date, *, request_id: str) -> bool:
        """配信に失敗したときに印を戻す（次の経路に再挑戦させる）。

        取り消せなくても呼び出し側は続行する（その日 1 通も出ない方向に倒れるだけで、
        二重配信側へは倒れない）。
        """
        email = _normalise_email(user_email)
        if not email or "@" not in email:
            return False
        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(_RELEASE_SQL, {"email": email, "day": day})
                removed = int(cur.rowcount) > 0
                conn.commit()
            logger.info("digest_delivery_release", request_id=request_id, removed=removed)
            return removed
        except Exception:
            logger.warning("digest_delivery_release_failed", request_id=request_id)
            return False


__all__ = ["DigestDeliveryStore"]
