"""お知らせ系 DM の「その日もう送った」印（``DigestDeliveryStore`` と同型）。

migration 0027 の ``digest_notice`` を ``teamagent_app`` ロール＋``app.user_email``
GUC で読み書きする。

なぜ ``digest_delivery``（0026）へ相乗りしないか:
  - 0026 の主キーは ``(user_email, digest_date)``＝「その日の **本文** を送る権」。
    未連携者もダイジェスト本文は一括実行で受け取るので、お知らせが先に claim を
    取ると **その人のその日のダイジェストが丸ごと消える**。
  - 0026 の ``origin`` は ``CHECK (origin IN ('scheduled','bulk'))``。
    ``'unlinked_notice'`` はそもそも INSERT できない。

設計の芯（0026 と同じ）:
  - 判定は **DB の一意制約** に委ねる。``INSERT ... ON CONFLICT DO NOTHING`` の
    rowcount が 1 のときだけ「自分が送る」。planner が途中で落ちて再実行されても
    （Scheduler の ``maximum_retry_attempts = 1``）2 通目は物理的に出ない。
  - **fail-closed**: 印を取れなかった／確認できなかったときは **送らない**。
    お知らせは週 1 回なので、1 回落ちても翌週に出る（誤配信は取り返せない）。
  - ログは件数と request_id だけ（メールアドレスを出さない）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_TTL_DAYS = 14  # 診断の猶予だけ取る（長期保持しない）

#: 種類。増やすときは migration 0027 の CHECK も同時に足す（片方だけだと INSERT が落ちる）。
NOTICE_CALENDAR_UNLINKED = "calendar_unlinked"
_KINDS = (NOTICE_CALENDAR_UNLINKED,)

_CLAIM_SQL = """
INSERT INTO digest_notice (user_email, notice_kind, notice_date, expires_at)
VALUES (%(email)s, %(kind)s, %(day)s, NOW() + make_interval(days => %(ttl_days)s))
ON CONFLICT (user_email, notice_kind, notice_date) DO NOTHING
"""

# 期限切れ行の掃除。claim と **同じトランザクション** で流す（掃除ジョブを増やさない）。
# RLS が効いているので消えるのは本人行だけ＝他人の印を落とさない。
_PURGE_SQL = """
DELETE FROM digest_notice WHERE expires_at < NOW()
"""


def _normalise_email(user_email: str) -> str:
    return (user_email or "").strip().lower()


class DigestNoticeStore:
    """(user, kind, date) を 1 回だけ通す claim ストア。"""

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
        kind: str,
        request_id: str,
    ) -> bool:
        """その日そのお知らせの送信権を取る。**取れたときだけ True**（＝送ってよい）。

        ⚠️ 例外は False（fail-closed）。「確認できないから送っておく」は二重配信の道。
        """
        email = _normalise_email(user_email)
        if not email or "@" not in email or kind not in _KINDS:
            return False
        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    _CLAIM_SQL,
                    {"email": email, "kind": kind, "day": day, "ttl_days": _TTL_DAYS},
                )
                claimed = int(cur.rowcount) == 1
                # ⚠️ rowcount を読んでから掃除する（順序を入れ替えると claim の判定が
                #   DELETE の rowcount になり、毎回 False＝1 通も出なくなる）。
                cur.execute(_PURGE_SQL)
                conn.commit()
            logger.info("digest_notice_claim", request_id=request_id, claimed=claimed, kind=kind)
            return claimed
        except Exception:
            # fail-closed。error レベルで出して既存の ErrorCount alarm へ流す。
            logger.error("digest_notice_claim_failed", request_id=request_id, kind=kind)
            return False


__all__ = ["NOTICE_CALENDAR_UNLINKED", "DigestNoticeStore"]
