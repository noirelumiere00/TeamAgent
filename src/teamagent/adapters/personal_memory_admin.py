"""本人メモの管理者閲覧と退職・ゲスト化の掃除（M6・設計 §10b.5 / §10b.6）。

DB では SECURITY DEFINER の関数だけを使う（migration 0029）。表を直接読むことはしない。

- 管理者閲覧: ``personal_memory_admin_reader`` へ SET ROLE して
  ``personal_memory_admin_view`` を呼ぶ。
  関数は「監査 INSERT → 行を返す」を 1 本で行うので、監査に失敗すれば 1 行も返らない。
  呼び出し側（connect_web）は例外なら 503 で何も表示しない。
- 退職・ゲスト化: ``personal_memory_retirer`` へ SET ROLE して、対象の一覧（team と U… だけ）を
  取り、Slack の ``users.info`` を **保存した U… で直接**引く。``deleted=true`` を直接確認できた
  ときだけ消し、ゲスト（is_restricted / is_ultra_restricted）は凍結だけ。API の失敗は退職と
  見なさない。

管理者は本人メモ専用の allowlist ``PERSONAL_MEMORY_ADMIN_EMAILS``（利用状況画面の
``CONNECT_ADMIN_EMAILS`` とは共用しない）。空・未設定は誰も見られない。v1 はちょうど 1 名
（契約テストで固定）。
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol

import structlog

logger = structlog.get_logger(__name__)

ADMIN_EMAILS_ENV: Final = "PERSONAL_MEMORY_ADMIN_EMAILS"
ADMIN_ROLE: Final = "personal_memory_admin_reader"
RETIRER_ROLE: Final = "personal_memory_retirer"
STATEMENT_TIMEOUT_MS: Final = 3000
_TEAM_RE: Final = re.compile(r"T[A-Z0-9]{8,}")
_USER_RE: Final = re.compile(r"U[A-Z0-9]{8,}")
_REASON_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")


class PersonalMemoryAdminError(RuntimeError):
    """閲覧・掃除の失敗。``code`` は固定語（値は載せない）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def admin_emails() -> frozenset[str]:
    """本人メモを閲覧できる管理者。空・未設定は誰もいない（利用状況画面と違い既定に倒さない）。"""
    raw = os.environ.get(ADMIN_EMAILS_ENV, "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_admin(email: str | None) -> bool:
    return bool(email) and str(email).strip().lower() in admin_emails()


def valid_principal(team_id: str, slack_user_id: str) -> bool:
    return bool(_TEAM_RE.fullmatch(team_id or "")) and bool(_USER_RE.fullmatch(slack_user_id or ""))


@dataclass(frozen=True)
class AdminRow:
    no: int
    target: str
    content: str
    updated_at: _dt.datetime | None


ConnectionFactory = Callable[[str], AbstractContextManager[Any]]


def _default_connection_factory() -> ConnectionFactory:
    from teamagent.adapters.pgvector_client import PgVectorClient

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise PersonalMemoryAdminError("no_database_url")
    client = PgVectorClient(dsn=dsn)

    def factory(role: str) -> AbstractContextManager[Any]:
        return client.connection(app_role=role, application_name="personal_memory_admin")

    return factory


class PersonalMemoryAdmin:
    """1 メソッド = 1 トランザクション。例外は型名だけをログに出し、固定語の例外にして投げ直す。"""

    def __init__(self, connection_factory: ConnectionFactory | None = None) -> None:
        self._factory = connection_factory

    @contextmanager
    def _txn(self, role: str, op: str) -> Iterator[Any]:
        if self._factory is None:
            self._factory = _default_connection_factory()
        try:
            with self._factory(role) as conn, conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
                yield cur
        except PersonalMemoryAdminError:
            raise
        except Exception as exc:
            logger.error("personal_memory_admin_failed", op=op, error=type(exc).__name__)
            raise PersonalMemoryAdminError("db_failed") from None

    def view(
        self, *, admin_email: str, team_id: str, slack_user_id: str, reason: str = "admin_page"
    ) -> list[AdminRow]:
        """監査を確定してから行を返す。管理者でない・形が不正なら DB に触れずに拒否。"""
        if not is_admin(admin_email):
            raise PersonalMemoryAdminError("not_admin")
        if not valid_principal(team_id, slack_user_id) or not _REASON_RE.fullmatch(reason):
            raise PersonalMemoryAdminError("bad_principal")
        with self._txn(ADMIN_ROLE, "view") as cur:
            cur.execute(
                "SELECT entry_no, entry_target, entry_content, entry_updated_at "
                "FROM public.personal_memory_admin_view(%s, %s, %s, %s)",
                (admin_email.strip().lower(), team_id, slack_user_id, reason),
            )
            rows = [AdminRow(int(r[0]), str(r[1]), str(r[2]), r[3]) for r in cur.fetchall()]
        logger.info("personal_memory_admin_viewed", items=len(rows))
        return rows

    def candidates(self) -> list[tuple[str, str]]:
        with self._txn(RETIRER_ROLE, "candidates") as cur:
            cur.execute(
                "SELECT team_id, slack_user_id FROM public.personal_memory_retire_candidates()"
            )
            return [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    def retire_delete(self, team_id: str, slack_user_id: str, reason: str = "slack_deleted") -> int:
        if not valid_principal(team_id, slack_user_id):
            raise PersonalMemoryAdminError("bad_principal")
        with self._txn(RETIRER_ROLE, "retire_delete") as cur:
            cur.execute(
                "SELECT public.personal_memory_retire_delete(%s, %s, %s)",
                (team_id, slack_user_id, reason),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def retire_freeze(self, team_id: str, slack_user_id: str, reason: str = "slack_guest") -> bool:
        if not valid_principal(team_id, slack_user_id):
            raise PersonalMemoryAdminError("bad_principal")
        with self._txn(RETIRER_ROLE, "retire_freeze") as cur:
            cur.execute(
                "SELECT public.personal_memory_retire_freeze(%s, %s, %s)",
                (team_id, slack_user_id, reason),
            )
            row = cur.fetchone()
        return bool(row and row[0])


class SlackUserInfo(Protocol):
    def users_info(self, *, user: str) -> Any: ...


@dataclass(frozen=True)
class SweepResult:
    checked: int = 0
    deleted: int = 0
    frozen: int = 0
    unknown: int = 0


def sweep_retired(admin: PersonalMemoryAdmin, slack: SlackUserInfo) -> SweepResult:
    """退職（deleted=true）は削除・ゲスト化は凍結。Slack の失敗は何もしない（退職と見なさない）。"""
    checked = deleted = frozen = unknown = 0
    for team_id, slack_user_id in admin.candidates():
        checked += 1
        try:
            resp = slack.users_info(user=slack_user_id)
            data = resp.data if hasattr(resp, "data") else resp
            user = data.get("user") if isinstance(data, dict) and data.get("ok") else None
        except Exception as exc:
            logger.warning("personal_memory_sweep_lookup_failed", error=type(exc).__name__)
            user = None
        if not isinstance(user, dict):
            unknown += 1
            continue
        if user.get("deleted") is True:
            admin.retire_delete(team_id, slack_user_id, "slack_deleted")
            deleted += 1
        elif user.get("is_restricted") is True or user.get("is_ultra_restricted") is True:
            if admin.retire_freeze(team_id, slack_user_id, "slack_guest"):
                frozen += 1
    result = SweepResult(checked=checked, deleted=deleted, frozen=frozen, unknown=unknown)
    logger.info(
        "personal_memory_sweep_done",
        checked=checked,
        deleted=deleted,
        frozen=frozen,
        unknown=unknown,
    )
    return result


__all__ = [
    "ADMIN_EMAILS_ENV",
    "ADMIN_ROLE",
    "RETIRER_ROLE",
    "AdminRow",
    "PersonalMemoryAdmin",
    "PersonalMemoryAdminError",
    "SweepResult",
    "admin_emails",
    "is_admin",
    "sweep_retired",
    "valid_principal",
]
