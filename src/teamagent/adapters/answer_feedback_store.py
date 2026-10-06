"""回答評価ボタン（👍/👎）の保存先 ``search_feedback``（migration 0015 / 0022）。

- teamagent_app ロールで INSERT するだけ（0022 で app の SELECT/UPDATE/DELETE は取り消し済み）。
  ON CONFLICT・RETURNING は使わない（最小権限ロールでは落ちる・0024 の地雷）。
- 「同じ人の二度押しは最後の値で上書き」は追記で表し、集計側（dashboard の
  ``answer_feedback_summary``）が (user_email, answer_id) ごとに最新の 1 行だけを数える。
- 本文（回答）は保存しない。query は検索語だけ（0015 の契約）。
- 例外は ``AnswerFeedbackStoreError`` に包む（例外文に行の中身を載せない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Protocol

APP_ROLE: Final = "teamagent_app"
_ANSWER_ID_RE: Final = re.compile(r"[0-9a-f]{16}")
_SESSION_RE: Final = re.compile(r"[A-Za-z0-9-]{1,64}")
# 列名は固定。値はすべてプレースホルダ。
INSERT_SQL: Final = (
    "INSERT INTO search_feedback "
    "(user_email, query, target_type, rating, search_session_id, answer_id) "
    "VALUES (%s, %s, 'answer', %s, %s, %s)"
)


class AnswerFeedbackStoreError(RuntimeError):
    """保存の失敗。code は固定の識別子（行の中身を含めない）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AnswerFeedbackRow:
    """search_feedback に入れる 1 行（target_type は常に 'answer'・score は NULL）。"""

    user_email: str
    query: str
    rating: int
    answer_id: str
    search_session_id: str

    def __post_init__(self) -> None:
        email = self.user_email.strip()
        if not email or "@" not in email or email != email.lower():
            raise AnswerFeedbackStoreError("bad_email")
        if not self.query.strip():
            raise AnswerFeedbackStoreError("bad_query")
        if isinstance(self.rating, bool) or self.rating not in (-1, 1):
            raise AnswerFeedbackStoreError("bad_rating")
        if _ANSWER_ID_RE.fullmatch(self.answer_id) is None:
            raise AnswerFeedbackStoreError("bad_answer_id")
        if _SESSION_RE.fullmatch(self.search_session_id) is None:
            raise AnswerFeedbackStoreError("bad_session_id")


class AnswerFeedbackStore(Protocol):
    def insert(self, row: AnswerFeedbackRow) -> None: ...


class PgAnswerFeedbackStore:
    """PgVectorClient（同期 API）で 1 行 INSERT する。"""

    def __init__(self, pgvector: Any, *, app_role: str = APP_ROLE) -> None:
        self._pg = pgvector
        self._app_role = app_role

    @classmethod
    def from_env(cls) -> PgAnswerFeedbackStore:
        from teamagent.adapters.pgvector_client import PgVectorClient

        return cls(PgVectorClient.from_env())

    def insert(self, row: AnswerFeedbackRow) -> None:
        try:
            with self._pg.connection(app_role=self._app_role, user_email=row.user_email) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        INSERT_SQL,
                        [
                            row.user_email,
                            row.query,
                            row.rating,
                            row.search_session_id,
                            row.answer_id,
                        ],
                    )
        except Exception as error:
            raise AnswerFeedbackStoreError(f"insert_failed:{type(error).__name__}") from None


__all__ = [
    "AnswerFeedbackRow",
    "AnswerFeedbackStore",
    "AnswerFeedbackStoreError",
    "PgAnswerFeedbackStore",
]
