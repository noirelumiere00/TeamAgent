"""朝ダイジェストの本人ごとの設定（migration 0030 ``digest_preferences``）を読み書きするストア。

``teamagent_app`` ロール＋``app.user_email`` の RLS で本人行だけを触る（0025 と同じ作法）。
中身（JSON）の妥当性はここでは見ない（adapters は skills を import しない＝レイヤ規約）。
検査は ``skills.morning_digest.preferences`` が書く前・読んだ後に行う。

障害の伝え方は **例外**（読めなかった・書けなかったを「設定なし」「書けた」に潰さない）。
倒し方は呼び出し側が決める: 配信側は既定へ倒し、設定ツールは本人に「変えられなかった」と返す。
ログには request_id と結果だけを残し、メールアドレスや設定内容を含めない。
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_GET_SQL = """
SELECT prefs, version FROM digest_preferences WHERE user_email = %(email)s
"""

# 新規行: 同時に 2 か所から「初めての設定」をしたら、後の方は 0 行＝競合として返す。
_INSERT_SQL = """
INSERT INTO digest_preferences (user_email, prefs, version, updated_at)
VALUES (%(email)s, %(prefs)s::jsonb, 1, NOW())
ON CONFLICT (user_email) DO NOTHING
"""

# 既存行: 読んだ時の版と一致するときだけ書く（楽観ロック）。
_UPDATE_SQL = """
UPDATE digest_preferences
SET prefs = %(prefs)s::jsonb, version = version + 1, updated_at = NOW()
WHERE user_email = %(email)s AND version = %(expected)s
"""

_DELETE_SQL = "DELETE FROM digest_preferences WHERE user_email = %(email)s"


def _normalise_email(user_email: str) -> str:
    return (user_email or "").strip().lower()


def _valid_email(email: str) -> bool:
    return bool(email) and "@" in email


class DigestPreferencesStore:
    """本人行に限定して ``digest_preferences`` を読み書きするストア。"""

    def __init__(self, pg: Any | None = None) -> None:
        self._pg = pg

    def _ensure_pg(self) -> Any:
        if self._pg is None:
            from teamagent.adapters.pgvector_client import PgVectorClient

            self._pg = PgVectorClient.from_env()
        return self._pg

    def get(self, user_email: str, *, request_id: str) -> tuple[dict[str, Any] | None, int]:
        """``(prefs, version)`` を返す。行が無ければ ``(None, 0)``。障害は例外。"""
        email = _normalise_email(user_email)
        if not _valid_email(email):
            raise ValueError("user_email is required")
        with (
            self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(_GET_SQL, {"email": email})
            row = cur.fetchone()
            conn.commit()
        if row is None:
            logger.info("digest_prefs_get", request_id=request_id, found=False)
            return None, 0
        if isinstance(row, dict):
            prefs, version = row["prefs"], row["version"]
        else:
            prefs, version = row
        if isinstance(prefs, str):  # jsonb を文字列で返すドライバ設定でも読めるように
            prefs = json.loads(prefs)
        version = int(version)
        if not isinstance(prefs, dict) or version < 1:
            raise ValueError("invalid digest_preferences row")
        logger.info("digest_prefs_get", request_id=request_id, found=True)
        return prefs, version

    def save(
        self,
        user_email: str,
        prefs: dict[str, Any],
        *,
        expected_version: int,
        request_id: str,
    ) -> int | None:
        """書いて新しい版を返す。読んだ後に他で書き換わっていたら ``None``（競合）。障害は例外。

        ``expected_version=0`` は「行がまだ無い」と読んだ場合（新規作成）。
        """
        email = _normalise_email(user_email)
        if not _valid_email(email):
            raise ValueError("user_email is required")
        if not isinstance(prefs, dict) or expected_version < 0:
            raise ValueError("invalid arguments")
        payload = json.dumps(prefs, ensure_ascii=False, sort_keys=True)
        with (
            self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
            conn.cursor() as cur,
        ):
            if expected_version == 0:
                cur.execute(_INSERT_SQL, {"email": email, "prefs": payload})
            else:
                cur.execute(
                    _UPDATE_SQL, {"email": email, "prefs": payload, "expected": expected_version}
                )
            written = int(cur.rowcount)
            conn.commit()
        if written != 1:
            logger.info("digest_prefs_save_conflict", request_id=request_id)
            return None
        logger.info("digest_prefs_saved", request_id=request_id)
        return expected_version + 1

    def delete(self, user_email: str, *, request_id: str) -> bool:
        """本人の設定行を消す（＝既定に戻す）。消す行が無くても True。障害は例外。"""
        email = _normalise_email(user_email)
        if not _valid_email(email):
            raise ValueError("user_email is required")
        with (
            self._ensure_pg().connection(app_role="teamagent_app", user_email=email) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(_DELETE_SQL, {"email": email})
            deleted = max(0, int(cur.rowcount))
            conn.commit()
        logger.info("digest_prefs_deleted", request_id=request_id, count=deleted)
        return True


__all__ = ["DigestPreferencesStore"]
