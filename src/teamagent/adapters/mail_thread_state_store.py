"""メールサマリーのボタン操作状態ストア（per-user・RLS・migration 0015）。

インタラクティブなメールサマリーの `[対応する][対応済み][後で][…]` 押下結果を
スレッド単位で保持する。状態語彙は migration 0015 の CHECK と一致させること:
  open（未対応）/ done（対応済み）/ snoozed（後で・再通知待ち）/ muted（今後通知しない）

per-user 分離は RLS（`app.user_email` GUC）で DB 側担保。reminder スキャンだけは
admin ロール（`app.user_role='admin'`）で全ユーザーの期限到来分を走査する
（morning_digest と同じ信頼境界のバックエンドジョブ）。

⚠️ 生件名・生 From は保存しない（subject_scrubbed / counterpart_masked は DLP マスク後）。
thread_id は Gmail の不透明 ID であり生 messageId ではない。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# 状態語彙（migration 0015 の CHECK と一致）。
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_SNOOZED = "snoozed"
STATUS_MUTED = "muted"
VALID_STATUS = frozenset({STATUS_OPEN, STATUS_DONE, STATUS_SNOOZED, STATUS_MUTED})


@dataclass(frozen=True)
class MailThreadState:
    """1 スレッドの操作状態。"""

    user_email: str
    thread_id: str
    status: str
    snooze_until: _dt.datetime | None = None
    subject_scrubbed: str = ""
    counterpart_masked: str = ""
    last_notified_at: _dt.datetime | None = None


@runtime_checkable
class MailThreadStateStore(Protocol):
    """差し替え可能な状態ストア（本番=RDS・test=InMemory）。"""

    def get(self, user_email: str, thread_id: str) -> MailThreadState | None: ...

    def set_status(
        self,
        user_email: str,
        thread_id: str,
        status: str,
        *,
        snooze_until: _dt.datetime | None = None,
        subject_scrubbed: str = "",
        counterpart_masked: str = "",
    ) -> None: ...

    def list_due(self, now: _dt.datetime) -> list[MailThreadState]: ...

    def reopen_after_reminder(self, user_email: str, thread_id: str, now: _dt.datetime) -> None: ...


def _norm(email: str) -> str:
    return email.strip().lower()


class InMemoryMailThreadStateStore:
    """dev/test 用のメモリ実装（RLS 相当の分離はキー (email, thread_id) で表現）。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], MailThreadState] = {}

    def get(self, user_email: str, thread_id: str) -> MailThreadState | None:
        return self._rows.get((_norm(user_email), thread_id))

    def set_status(
        self,
        user_email: str,
        thread_id: str,
        status: str,
        *,
        snooze_until: _dt.datetime | None = None,
        subject_scrubbed: str = "",
        counterpart_masked: str = "",
    ) -> None:
        if status not in VALID_STATUS:
            raise ValueError(f"invalid status: {status!r}")
        email = _norm(user_email)
        prev = self._rows.get((email, thread_id))
        self._rows[(email, thread_id)] = MailThreadState(
            user_email=email,
            thread_id=thread_id,
            status=status,
            snooze_until=snooze_until,
            # 空で来たら既存値を保つ（再通知描画メタを失わない）。
            subject_scrubbed=subject_scrubbed or (prev.subject_scrubbed if prev else ""),
            counterpart_masked=counterpart_masked or (prev.counterpart_masked if prev else ""),
            last_notified_at=prev.last_notified_at if prev else None,
        )

    def list_due(self, now: _dt.datetime) -> list[MailThreadState]:
        return [
            r
            for r in self._rows.values()
            if r.status == STATUS_SNOOZED and r.snooze_until is not None and r.snooze_until <= now
        ]

    def reopen_after_reminder(self, user_email: str, thread_id: str, now: _dt.datetime) -> None:
        email = _norm(user_email)
        prev = self._rows.get((email, thread_id))
        if prev is None:
            return
        self._rows[(email, thread_id)] = MailThreadState(
            user_email=email,
            thread_id=thread_id,
            status=STATUS_OPEN,
            snooze_until=None,
            subject_scrubbed=prev.subject_scrubbed,
            counterpart_masked=prev.counterpart_masked,
            last_notified_at=now,
        )


class RdsMailThreadStateStore:
    """RDS(mail_thread_state) 実装（per-user RLS・migration 0015）。

    通常操作は `connection(app_role, user_email)` で本人行 RLS。reminder スキャンは
    `connection(app_role, user_role='admin')` で全行を走査/更新する。
    """

    def __init__(self, pgvector: Any, *, app_role: str = "teamagent_app") -> None:
        self._pgvector = pgvector
        self._app_role = app_role

    def get(self, user_email: str, thread_id: str) -> MailThreadState | None:
        email = _norm(user_email)
        with self._pgvector.connection(app_role=self._app_role, user_email=email) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_email, thread_id, status, snooze_until, subject_scrubbed,"
                    " counterpart_masked, last_notified_at"
                    " FROM mail_thread_state WHERE user_email = %s AND thread_id = %s",
                    (email, thread_id),
                )
                row = cur.fetchone()
        return _row_to_state(row) if row else None

    def set_status(
        self,
        user_email: str,
        thread_id: str,
        status: str,
        *,
        snooze_until: _dt.datetime | None = None,
        subject_scrubbed: str = "",
        counterpart_masked: str = "",
    ) -> None:
        if status not in VALID_STATUS:
            raise ValueError(f"invalid status: {status!r}")
        email = _norm(user_email)
        with self._pgvector.connection(app_role=self._app_role, user_email=email) as conn:
            with conn.cursor() as cur:
                # subject/counterpart は空で来たら既存値を保つ（NULLIF + COALESCE）。
                cur.execute(
                    """
                    INSERT INTO mail_thread_state
                        (user_email, thread_id, status, snooze_until,
                         subject_scrubbed, counterpart_masked)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_email, thread_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        snooze_until = EXCLUDED.snooze_until,
                        subject_scrubbed = COALESCE(
                            NULLIF(EXCLUDED.subject_scrubbed, ''),
                            mail_thread_state.subject_scrubbed),
                        counterpart_masked = COALESCE(
                            NULLIF(EXCLUDED.counterpart_masked, ''),
                            mail_thread_state.counterpart_masked)
                    """,
                    (email, thread_id, status, snooze_until, subject_scrubbed, counterpart_masked),
                )
            conn.commit()

    def list_due(self, now: _dt.datetime) -> list[MailThreadState]:
        with self._pgvector.connection(app_role=self._app_role, user_role="admin") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_email, thread_id, status, snooze_until, subject_scrubbed,"
                    " counterpart_masked, last_notified_at"
                    " FROM mail_thread_state"
                    " WHERE status = %s AND snooze_until IS NOT NULL AND snooze_until <= %s"
                    " ORDER BY snooze_until",
                    (STATUS_SNOOZED, now),
                )
                rows = cur.fetchall()
        return [_row_to_state(r) for r in rows]

    def reopen_after_reminder(self, user_email: str, thread_id: str, now: _dt.datetime) -> None:
        email = _norm(user_email)
        with self._pgvector.connection(app_role=self._app_role, user_role="admin") as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE mail_thread_state"
                    " SET status = %s, snooze_until = NULL, last_notified_at = %s"
                    " WHERE user_email = %s AND thread_id = %s",
                    (STATUS_OPEN, now, email, thread_id),
                )
            conn.commit()


def _row_to_state(row: dict[str, Any]) -> MailThreadState:
    return MailThreadState(
        user_email=str(row["user_email"]),
        thread_id=str(row["thread_id"]),
        status=str(row["status"]),
        snooze_until=row.get("snooze_until"),
        subject_scrubbed=str(row.get("subject_scrubbed") or ""),
        counterpart_masked=str(row.get("counterpart_masked") or ""),
        last_notified_at=row.get("last_notified_at"),
    )


__all__ = [
    "STATUS_DONE",
    "STATUS_MUTED",
    "STATUS_OPEN",
    "STATUS_SNOOZED",
    "VALID_STATUS",
    "InMemoryMailThreadStateStore",
    "MailThreadState",
    "MailThreadStateStore",
    "RdsMailThreadStateStore",
]
