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

#: ``claim_result`` の返り値。送ってよいのは CLAIM_CLAIMED だけ。
CLAIM_CLAIMED = "claimed"
CLAIM_TAKEN = "taken"
CLAIM_FAILED = "failed"
#: 一括実行が当たった行が、planner の「後で個別に送る」予約だった（送らない・正常）。
CLAIM_RESERVED = "reserved"

# planner が「この人には後で個別に送る」と予約する印（migration 0031 で origin に追加）。
# 一括実行（既定時刻）はこの行に当たって送らない。予約の発火（scheduled）は UPDATE で
# 印を「送った」に変えて送る。⚠️ 印を作ったのに予約（Scheduler）が作れなかったら、
# planner が必ず印を消す（消さないとその日は誰も送らない）。
_RESERVE_SQL = """
INSERT INTO digest_delivery (user_email, digest_date, origin, expires_at)
VALUES (%(email)s, %(day)s, 'reserved', NOW() + make_interval(days => %(ttl_days)s))
ON CONFLICT (user_email, digest_date) DO NOTHING
"""

# 予約の発火: 自分の予約印だけを「送った」に変える（1 行なら送ってよい）。
_TAKE_RESERVED_SQL = """
UPDATE digest_delivery SET origin = 'scheduled', claimed_at = NOW()
WHERE user_email = %(email)s AND digest_date = %(day)s AND origin = 'reserved'
"""

_ORIGIN_SQL = """
SELECT origin FROM digest_delivery WHERE user_email = %(email)s AND digest_date = %(day)s
"""

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
        return (
            self.claim_result(user_email, day, origin=origin, request_id=request_id)
            == CLAIM_CLAIMED
        )

    def claim_result(
        self,
        user_email: str,
        day: _dt.date,
        *,
        origin: str,
        request_id: str,
    ) -> str:
        """``claim`` と同じ処理で、結果を 3 通りに分けて返す（送ってよいのは CLAIM_CLAIMED だけ）。

        - ``CLAIM_CLAIMED``: 自分が取った（送ってよい）
        - ``CLAIM_TAKEN``: 既に別の経路が取っている（送らない・正常）
        - ``CLAIM_FAILED``: DB 障害・入力不正で確かめられなかった（送らない＝fail-closed）

        F0: 呼び出し側が「送信済み」と「確認できず止めた」を数え分けるための入口。
        後者を「送信済み」と数えると、DB 障害で誰にも届かなかった朝を管理者 DM が
        「問題なし」と報告してしまう。
        """
        email = _normalise_email(user_email)
        if not email or "@" not in email or origin not in ("scheduled", "bulk"):
            return CLAIM_FAILED
        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                claimed = False
                if origin == "scheduled":
                    # 予約の発火: まず自分の予約印を「送った」に変える。
                    cur.execute(_TAKE_RESERVED_SQL, {"email": email, "day": day})
                    claimed = int(cur.rowcount) == 1
                if not claimed:
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
                held_by = ""
                if not claimed:
                    # 誰が持っているか（予約印なら「後で個別に送る」＝一括は正常に見送る）。
                    cur.execute(_ORIGIN_SQL, {"email": email, "day": day})
                    row = cur.fetchone()
                    if row is not None:
                        held_by = str(row["origin"] if isinstance(row, dict) else row[0])
                # ⚠️ rowcount を読んでから掃除する（順序を入れ替えると claim の
                #   判定が DELETE の rowcount になり、毎回 False＝1 通も出なくなる）。
                cur.execute(_PURGE_SQL)
                conn.commit()
            logger.info(
                "digest_delivery_claim",
                request_id=request_id,
                claimed=claimed,
                origin=origin,
                held_by=held_by,
            )
            if claimed:
                return CLAIM_CLAIMED
            return CLAIM_RESERVED if held_by == "reserved" else CLAIM_TAKEN
        except Exception:
            # fail-closed。error レベルで出して既存の ErrorCount alarm へ流す
            # （無音配信停止を「正常」に見せない）。
            logger.error("digest_delivery_claim_failed", request_id=request_id, origin=origin)
            return CLAIM_FAILED

    def reserve(self, user_email: str, day: _dt.date, *, request_id: str) -> bool:
        """planner が「この人には後で個別に送る」と印を付ける。付けられたときだけ True。

        False（既に行がある・DB 障害）なら planner は予約を作らない＝一括実行に残す
        （予約と印の片方だけが残る状態を作らない）。
        """
        email = _normalise_email(user_email)
        if not email or "@" not in email:
            return False
        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(_RESERVE_SQL, {"email": email, "day": day, "ttl_days": _TTL_DAYS})
                reserved = int(cur.rowcount) == 1
                conn.commit()
            logger.info("digest_delivery_reserve", request_id=request_id, reserved=reserved)
            return reserved
        except Exception:
            logger.error("digest_delivery_reserve_failed", request_id=request_id)
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


__all__ = ["CLAIM_CLAIMED", "CLAIM_FAILED", "CLAIM_RESERVED", "CLAIM_TAKEN", "DigestDeliveryStore"]
