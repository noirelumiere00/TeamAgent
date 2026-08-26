"""朝ダイジェストの確認済み項目を保持する PostgreSQL ストア。

migration 0025 の ``digest_ack`` を、``app.user_email`` による RLS を有効にした
``teamagent_app`` ロールで読み書きする。Gmail thread ID や Slack channel/permalink は
保存せず、ユーザーも材料に含めた 16 桁の SHA-256 鍵だけを扱う（G3）。

確認済み情報は配信項目を隠すための補助情報であり、読めない場合に項目を隠すと見逃しを
生む。したがって読み取り・期限切れ削除の障害だけは fail-open とし、書き込み障害は件数 0
で呼び出し側へ失敗を伝える。ログには request_id と件数だけを残し、メールアドレスや項目鍵を
含めない。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

import structlog

from teamagent.skills.morning_digest.ack_token import AckItem

logger = structlog.get_logger(__name__)

_ACK_TTL_DAYS = 30  # 保持期間（裁定済み）
_ITEM_KEY_RE = re.compile(r"^[0-9a-f]{16}$")

_ACK_SQL = f"""
INSERT INTO digest_ack (user_email, item_kind, item_key, anchor, expires_at)
VALUES (%(email)s, %(kind)s, %(key)s, %(anchor)s, NOW() + INTERVAL '{_ACK_TTL_DAYS} days')
ON CONFLICT (user_email, item_kind, item_key) DO UPDATE SET
    anchor = EXCLUDED.anchor,
    acked_at = NOW(),
    expires_at = EXCLUDED.expires_at
"""

_UNACK_SQL = """
DELETE FROM digest_ack
WHERE user_email = %(email)s AND item_kind = %(kind)s AND item_key = %(key)s
"""

_ACTIVE_SQL = """
SELECT item_kind, item_key, anchor
FROM digest_ack
WHERE user_email = %(email)s AND expires_at > NOW()
"""

_PURGE_SQL = "DELETE FROM digest_ack WHERE expires_at <= NOW()"


def _normalise_email(user_email: str) -> str:
    return (user_email or "").strip().lower()


def _valid_identity(item: AckItem) -> bool:
    return item.item_kind in {"m", "s"} and _ITEM_KEY_RE.fullmatch(item.item_key) is not None


class DigestAckStore:
    """本人行に限定して ``digest_ack`` を読み書きするストア。"""

    def __init__(self, pg: Any | None = None) -> None:
        self._pg = pg

    def _ensure_pg(self) -> Any:
        if self._pg is None:
            from teamagent.adapters.pgvector_client import PgVectorClient

            self._pg = PgVectorClient.from_env()
        return self._pg

    @staticmethod
    def item_key(item_kind: str, user_email: str, raw_id: str) -> str:
        """生 ID を保存しないための鍵化。

        ``sha256("digestack:" + kind + ":" + email_norm + ":" + raw_id)[:16]``
        """
        email = _normalise_email(user_email)
        material = f"digestack:{item_kind}:{email}:{raw_id}".encode()
        return hashlib.sha256(material).hexdigest()[:16]

    def ack(self, user_email: str, items: Sequence[AckItem], *, request_id: str) -> int:
        """確認済み項目を UPSERT し、書けた件数を返す。"""
        email = _normalise_email(user_email)
        pending = tuple(items)
        if (
            not email
            or "@" not in email
            or not pending
            or not all(
                _valid_identity(item)
                and type(item.anchor) is int
                and item.anchor >= 0
                for item in pending
            )
        ):
            return 0

        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                written = 0
                for item in pending:
                    cur.execute(
                        _ACK_SQL,
                        {
                            "email": email,
                            "kind": item.item_kind,
                            "key": item.item_key,
                            "anchor": item.anchor,
                        },
                    )
                    written += max(0, int(cur.rowcount))
                conn.commit()
            logger.info("digest_ack_acked", request_id=request_id, count=written)
            return written
        except Exception:
            # 書き込みは fail-open にしない。0 件を返し、呼び出し側に失敗を通知させる。
            logger.warning("digest_ack_ack_failed", request_id=request_id, count=0)
            return 0

    def unack(self, user_email: str, items: Sequence[AckItem], *, request_id: str) -> int:
        """指定された確認済み項目を削除し、削除できた件数を返す。"""
        email = _normalise_email(user_email)
        pending = tuple(items)
        if (
            not email
            or "@" not in email
            or not pending
            or not all(_valid_identity(item) for item in pending)
        ):
            return 0

        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                deleted = 0
                for item in pending:
                    cur.execute(
                        _UNACK_SQL,
                        {"email": email, "kind": item.item_kind, "key": item.item_key},
                    )
                    deleted += max(0, int(cur.rowcount))
                conn.commit()
            logger.info("digest_ack_unacked", request_id=request_id, count=deleted)
            return deleted
        except Exception:
            # 削除も成功扱いにはせず、0 件を呼び出し側の失敗判定に使わせる。
            logger.warning("digest_ack_unack_failed", request_id=request_id, count=0)
            return 0

    def active(self, user_email: str, *, request_id: str) -> dict[tuple[str, str], int]:
        """期限内の確認済み項目を ``(kind, key) -> anchor`` で返す。"""
        email = _normalise_email(user_email)
        if not email or "@" not in email:
            return {}

        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(_ACTIVE_SQL, {"email": email})
                rows = cur.fetchall()
                conn.commit()

            active: dict[tuple[str, str], int] = {}
            for row in rows:
                if isinstance(row, dict):
                    kind, key, anchor = row["item_kind"], row["item_key"], row["anchor"]
                else:
                    kind, key, anchor = row
                item = AckItem(item_kind=str(kind), item_key=str(key), anchor=int(anchor))
                if not _valid_identity(item) or item.anchor < 0:
                    raise ValueError("invalid digest_ack row")
                active[(item.item_kind, item.item_key)] = item.anchor
            logger.info("digest_ack_active", request_id=request_id, count=len(active))
            return active
        except Exception:
            # fail-open: 隠す側に倒れる障害は見逃しを生むので、読めなければ何も隠さない。
            logger.warning("digest_ack_active_failed", request_id=request_id, count=0)
            return {}

    def purge_expired(self, user_email: str, *, request_id: str) -> int:
        """RLS で本人行に限定し、期限切れ行を削除する。"""
        email = _normalise_email(user_email)
        if not email or "@" not in email:
            return 0

        try:
            with (
                self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(_PURGE_SQL)
                deleted = max(0, int(cur.rowcount))
                conn.commit()
            logger.info("digest_ack_purged", request_id=request_id, count=deleted)
            return deleted
        except Exception:
            # fail-open: 保守処理に失敗しても配信判定を止めず、active() は読取不能時に全件を出す。
            logger.warning("digest_ack_purge_failed", request_id=request_id, count=0)
            return 0


__all__ = ["DigestAckStore"]
