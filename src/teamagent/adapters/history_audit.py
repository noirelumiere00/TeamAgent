"""履歴の管理者閲覧の監査（上司 5・第 1 段）。migration 0033 の SECURITY DEFINER 関数だけを使う。

``history_view_auditor`` へ SET ROLE して ``public.history_view_record`` を 1 回呼ぶ。
関数は監査行を INSERT するだけ（本文・email 以外の値は持たない）。**Slack を読む前に**呼び、
失敗したら呼び出し側（connect_web）は 503 で何も表示しない（本人メモの管理者閲覧と同じ型）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Final

import structlog

from teamagent.adapters.dm_history import (
    HISTORY_DAYS_MAX,
    is_admin,
    valid_team_id,
    valid_user_id,
)

logger = structlog.get_logger(__name__)

AUDITOR_ROLE: Final = "history_view_auditor"
STATEMENT_TIMEOUT_MS: Final = 3000
_REASON_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")


class HistoryAuditError(RuntimeError):
    """監査を確定できなかった。``code`` は固定語（値は載せない）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


ConnectionFactory = Callable[[str], AbstractContextManager[Any]]


def _default_connection_factory() -> ConnectionFactory:
    from teamagent.adapters.pgvector_client import PgVectorClient

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise HistoryAuditError("no_database_url")
    client = PgVectorClient(dsn=dsn)

    def factory(role: str) -> AbstractContextManager[Any]:
        return client.connection(app_role=role, application_name="history_view_audit")

    return factory


class HistoryAudit:
    """1 呼び出し = 1 トランザクション。例外は型名だけをログに出し、固定語の例外にして投げ直す。"""

    def __init__(self, connection_factory: ConnectionFactory | None = None) -> None:
        self._factory = connection_factory

    @contextmanager
    def _txn(self) -> Iterator[Any]:
        if self._factory is None:
            self._factory = _default_connection_factory()
        try:
            with self._factory(AUDITOR_ROLE) as conn, conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
                yield cur
        except HistoryAuditError:
            raise
        except Exception as exc:
            logger.error("history_audit_failed", error=type(exc).__name__)
            raise HistoryAuditError("db_failed") from None

    def record(
        self,
        *,
        admin_email: str,
        team_id: str,
        slack_user_id: str,
        days: int,
        reason: str = "admin_page",
    ) -> None:
        """監査を確定する。管理者でない・形が不正なら DB に触れずに拒否する。"""
        if not is_admin(admin_email):
            raise HistoryAuditError("not_admin")
        if not valid_team_id(team_id) or not valid_user_id(slack_user_id):
            raise HistoryAuditError("bad_principal")
        if not 1 <= int(days) <= HISTORY_DAYS_MAX or not _REASON_RE.fullmatch(reason):
            raise HistoryAuditError("bad_argument")
        with self._txn() as cur:
            cur.execute(
                "SELECT public.history_view_record(%s, %s, %s, %s, %s) AS ok",
                (admin_email.strip().lower(), team_id, slack_user_id, int(days), reason),
            )
            row = cur.fetchone()
            # 本番の PgVectorClient は row_factory=dict_row（行は dict）。
            # tuple の接続でも読めるようにする。
            ok = row.get("ok") if isinstance(row, dict) else (row[0] if row else None)
            if ok is not True:
                # トランザクションの中で投げる＝監査が確定していない扱い（読みに進ませない）
                logger.error("history_audit_not_confirmed")
                raise HistoryAuditError("not_confirmed")
        logger.info("history_audit_recorded")


__all__ = ["AUDITOR_ROLE", "HistoryAudit", "HistoryAuditError"]
