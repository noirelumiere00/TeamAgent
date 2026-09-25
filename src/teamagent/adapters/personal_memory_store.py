"""DM 本人メモ v1 の保存。

migration 0029 の personal_memory_* を personal_memory_app ロールで読み書きする。

設計: docs/architecture/hermes_migration_design.md §10b。

設計の芯:
  - 本人の行だけを扱う。RLS の鍵は GUC ``app.pm_principal``（'T…:U…'）で、各トランザクションの冒頭で
    txn-local に入れる。既存ロール（teamagent_app・master）にはこの表の権限が無い（0029）。
  - 変更系は「本人の profile 行を FOR UPDATE → 版と状態を照合 → 変更 → 版を +1 → 監査 INSERT」を
    1 トランザクションで行う。ON CONFLICT と RETURNING は使わない
    （0024 の地雷: 最小権限ロールで落ちる）。
    初回の profile 作成の競合は SAVEPOINT で受け止める。
  - 書き込む内容は DB の CHECK に頼る前にアプリ側で検査する。
    CHECK 違反のエラー詳細には行の中身が載り、RDS のエラーログに残るため。
  - 1.2 秒の予算を守るため、専用の小さいプール（待ち 0.5 秒）と statement_timeout 800ms を使う。
  - 例外は ``PersonalMemoryStoreError(code)`` に包む。ログは event 名・code・型名・sha16 だけで、
    email・Slack ID・本文は出さない。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal

import structlog

logger = structlog.get_logger(__name__)

APP_ROLE: Final = "personal_memory_app"
MAX_ENTRY_CHARS: Final = 200
MAX_ENTRIES_PER_TARGET: Final = 40
# Hermes の上限と同じ（区切り "\n§\n" を含む合計字数）。
# hermes_runtime/aico_hermes/schema.py の値と一致させる
ENTRY_DELIMITER: Final = "\n§\n"
TARGET_CHAR_LIMIT: Final = {"user": 1375, "memory": 2200}
ERASE_WINDOW_S: Final = 600
STATEMENT_TIMEOUT_MS: Final = 800

_TEAM_RE: Final = re.compile(r"T[A-Z0-9]{8,}")
_USER_RE: Final = re.compile(r"U[A-Z0-9]{8,}")
_REASON_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")

Target = Literal["user", "memory"]
State = Literal["active", "frozen"]


class PersonalMemoryStoreError(RuntimeError):
    """保存の失敗。code は応答・ログに載せてよい固定の識別子（中身を含めない）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Principal:
    """本人（署名済み caller claim と resolver で確定した値だけから作る）。"""

    team_id: str
    slack_user_id: str
    user_email: str

    def __post_init__(self) -> None:
        if _TEAM_RE.fullmatch(self.team_id or "") is None:
            raise PersonalMemoryStoreError("bad_principal")
        if _USER_RE.fullmatch(self.slack_user_id or "") is None:
            raise PersonalMemoryStoreError("bad_principal")
        email = (self.user_email or "").strip()
        if not email or "@" not in email:
            raise PersonalMemoryStoreError("bad_principal")

    @property
    def key(self) -> str:
        return f"{self.team_id}:{self.slack_user_id}"

    @property
    def sha16(self) -> str:
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:16]

    def __repr__(self) -> str:  # ログや例外に Slack ID・email を出さない
        return f"Principal(sha16={self.sha16})"


@dataclass(frozen=True, slots=True)
class EntryRow:
    entry_id: str
    target: Target
    content: str
    created_at: _dt.datetime


@dataclass(frozen=True, slots=True)
class Snapshot:
    state: State
    noticed: bool
    version: int
    admin_view_count: int
    erase_confirm_until: _dt.datetime | None
    entries: tuple[EntryRow, ...]

    def contents(self, target: Target) -> tuple[str, ...]:
        return tuple(e.content for e in self.entries if e.target == target)


def valid_content(content: object) -> bool:
    """DB の CHECK と同じ規則（1〜200 字・前後に空白なし・§ を含まない）。"""
    return (
        isinstance(content, str)
        and 1 <= len(content) <= MAX_ENTRY_CHARS
        and content == content.strip()
        and "§" not in content
    )


def within_limits(entries: Sequence[tuple[str, str]]) -> bool:
    """target ごとの件数と合計字数（区切り込み）が Hermes の上限以内か。"""
    for target, limit in TARGET_CHAR_LIMIT.items():
        contents = [c for t, c in entries if t == target]
        if len(contents) > MAX_ENTRIES_PER_TARGET:
            return False
        if len(ENTRY_DELIMITER.join(contents)) > limit:
            return False
    return True


_SELECT_PROFILE_SQL = """
SELECT state, noticed_at IS NOT NULL AS noticed, version, admin_view_count, erase_confirm_until
  FROM personal_memory_profiles
 WHERE team_id = %(team)s AND slack_user_id = %(user)s
"""
_LOCK_PROFILE_SQL = _SELECT_PROFILE_SQL + " FOR UPDATE"
_SELECT_ENTRIES_SQL = """
SELECT entry_id::text, target, content, created_at
  FROM personal_memory_entries
 WHERE team_id = %(team)s AND slack_user_id = %(user)s
 ORDER BY target, created_at, entry_id
"""
_INSERT_PROFILE_SQL = """
INSERT INTO personal_memory_profiles (team_id, slack_user_id, user_email)
VALUES (%(team)s, %(user)s, %(email)s)
"""
_INSERT_ENTRY_SQL = """
INSERT INTO personal_memory_entries (team_id, slack_user_id, target, content)
VALUES (%(team)s, %(user)s, %(target)s, %(content)s)
"""
_DELETE_ENTRIES_SQL = """
DELETE FROM personal_memory_entries
 WHERE team_id = %(team)s AND slack_user_id = %(user)s AND entry_id = ANY(%(ids)s::uuid[])
"""
_DELETE_ALL_ENTRIES_SQL = """
DELETE FROM personal_memory_entries WHERE team_id = %(team)s AND slack_user_id = %(user)s
"""
_BUMP_SQL = """
UPDATE personal_memory_profiles
   SET version = version + 1, updated_at = NOW(), user_email = %(email)s
 WHERE team_id = %(team)s AND slack_user_id = %(user)s
"""
_NOTICE_SQL = """
UPDATE personal_memory_profiles
   SET noticed_at = COALESCE(noticed_at, NOW()), version = version + 1, updated_at = NOW(),
       user_email = %(email)s
 WHERE team_id = %(team)s AND slack_user_id = %(user)s
"""
_SET_STATE_SQL = """
UPDATE personal_memory_profiles
   SET state = %(state)s, erase_confirm_until = NULL, version = version + 1, updated_at = NOW()
 WHERE team_id = %(team)s AND slack_user_id = %(user)s
"""
_REQUEST_ERASE_SQL = """
UPDATE personal_memory_profiles
   SET erase_confirm_until = NOW() + make_interval(secs => %(window)s), updated_at = NOW()
 WHERE team_id = %(team)s AND slack_user_id = %(user)s
"""
_ERASED_SQL = """
UPDATE personal_memory_profiles
   SET state = 'frozen', erase_confirm_until = NULL, version = version + 1, updated_at = NOW()
 WHERE team_id = %(team)s AND slack_user_id = %(user)s
"""
_AUDIT_SQL = """
INSERT INTO personal_memory_audit (actor_kind, profile_sha16, action, item_count, reason_code)
VALUES (%(actor)s, %(sha16)s, %(action)s, %(count)s, %(reason)s)
"""


def _default_connection_factory() -> Callable[[], AbstractContextManager[Any]]:
    """本人メモ専用の小さいプール（既定のプールは待ちが 10 秒で 1.2 秒の予算を守れない）。"""
    # 返却時に RESET ROLE する既存の最小プール（pg_pool.py）を、本人メモ専用に小さく作る
    from teamagent.adapters.pg_pool import ConnectionPool
    from teamagent.adapters.pgvector_client import PgVectorClient, _connect_pg

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise PersonalMemoryStoreError("no_database_url")
    pool: ConnectionPool[Any] = ConnectionPool(
        connect=lambda: _connect_pg(dsn),
        max_size=2,
        min_size=0,
        timeout=0.5,
    )
    client = PgVectorClient(dsn=dsn, pool=pool)

    def factory() -> AbstractContextManager[Any]:
        return client.connection(app_role=APP_ROLE, application_name="personal_memory")

    return factory


class PersonalMemoryStore:
    """本人メモの読み書き。1 メソッド = 1 トランザクション。"""

    def __init__(
        self, connection_factory: Callable[[], AbstractContextManager[Any]] | None = None
    ) -> None:
        self._factory = connection_factory
        self._factory_lock = threading.Lock()

    # --- 接続とトランザクション -------------------------------------------------------------

    @contextmanager
    def _txn(self, principal: Principal, op: str) -> Iterator[Any]:
        with self._factory_lock:
            if self._factory is None:
                self._factory = _default_connection_factory()
        try:
            with self._factory() as conn, conn.cursor() as cur:
                cur.execute("SELECT set_config('app.pm_principal', %s, true)", (principal.key,))
                cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
                cur.execute("SET LOCAL lock_timeout = '500ms'")
                yield conn, cur
        except PersonalMemoryStoreError as exc:
            logger.warning(
                "personal_memory_store_rejected", op=op, code=exc.code, sha16=principal.sha16
            )
            raise
        except Exception as exc:
            # 例外文に値が載りうるので型名だけを出す
            logger.error(
                "personal_memory_store_failed",
                op=op,
                error_type=type(exc).__name__,
                sha16=principal.sha16,
            )
            raise PersonalMemoryStoreError("store_unavailable") from None

    @staticmethod
    def _params(principal: Principal, **extra: Any) -> dict[str, Any]:
        return {"team": principal.team_id, "user": principal.slack_user_id, **extra}

    @staticmethod
    def _ensure_profile(conn: Any, cur: Any, principal: Principal) -> None:
        """profile が無ければ作る（競合は SAVEPOINT で受け止める・ON CONFLICT は使わない）。"""
        from psycopg import errors

        cur.execute(_SELECT_PROFILE_SQL, PersonalMemoryStore._params(principal))
        if cur.fetchone() is not None:
            return
        try:
            with conn.transaction():  # SAVEPOINT
                cur.execute(
                    _INSERT_PROFILE_SQL,
                    PersonalMemoryStore._params(principal, email=principal.user_email.lower()),
                )
        except errors.UniqueViolation:
            pass  # 同時に作られた。ロールバック済みの SAVEPOINT の外で読み直す

    @staticmethod
    def _lock(cur: Any, principal: Principal) -> tuple[Any, ...]:
        cur.execute(_LOCK_PROFILE_SQL, PersonalMemoryStore._params(principal))
        row = cur.fetchone()
        if row is None:
            raise PersonalMemoryStoreError("no_profile")
        return tuple(row.values()) if isinstance(row, dict) else tuple(row)

    @staticmethod
    def _audit(cur: Any, principal: Principal, action: str, count: int, reason: str) -> None:
        if _REASON_RE.fullmatch(reason) is None:
            raise PersonalMemoryStoreError("bad_reason")
        cur.execute(
            _AUDIT_SQL,
            {
                "actor": "self" if action != "learn_applied" else "system",
                "sha16": principal.sha16,
                "action": action,
                "count": count,
                "reason": reason,
            },
        )

    @staticmethod
    def _entries(cur: Any, principal: Principal) -> tuple[EntryRow, ...]:
        cur.execute(_SELECT_ENTRIES_SQL, PersonalMemoryStore._params(principal))
        out = []
        for row in cur.fetchall():
            values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            out.append(
                EntryRow(
                    entry_id=values[0], target=values[1], content=values[2], created_at=values[3]
                )
            )
        return tuple(out)

    # --- 読み ---------------------------------------------------------------------------

    def load(self, principal: Principal) -> Snapshot | None:
        with self._txn(principal, "load") as (_conn, cur):
            cur.execute(_SELECT_PROFILE_SQL, self._params(principal))
            row = cur.fetchone()
            if row is None:
                return None
            state, noticed, version, views, erase_until = (
                tuple(row.values()) if isinstance(row, dict) else tuple(row)
            )
            return Snapshot(
                state=state,
                noticed=bool(noticed),
                version=int(version),
                admin_view_count=int(views),
                erase_confirm_until=erase_until,
                entries=self._entries(cur, principal),
            )

    # --- 書き ---------------------------------------------------------------------------

    def mark_noticed(self, principal: Principal) -> None:
        """初回告知を送った印（これより前の発話は学習に使わない）。"""
        with self._txn(principal, "mark_noticed") as (conn, cur):
            self._ensure_profile(conn, cur, principal)
            self._lock(cur, principal)
            cur.execute(_NOTICE_SQL, self._params(principal, email=principal.user_email.lower()))
            self._audit(cur, principal, "notice_ack", 0, "notice_sent")

    def apply_learned(
        self,
        principal: Principal,
        *,
        expected_version: int,
        adds: Sequence[tuple[Target, str]],
        remove_ids: Sequence[str],
    ) -> int:
        """学習結果を反映する。版が進んでいたら（本人が途中でコマンドを使った等）反映しない。"""
        for target, content in adds:
            if target not in TARGET_CHAR_LIMIT or not valid_content(content):
                raise PersonalMemoryStoreError("bad_entry")
        with self._txn(principal, "apply_learned") as (_conn, cur):
            state, noticed, version, _views, _erase = self._lock(cur, principal)
            if state != "active" or not noticed:
                raise PersonalMemoryStoreError("not_active")
            if int(version) != expected_version:
                raise PersonalMemoryStoreError("version_conflict")
            current = self._entries(cur, principal)
            known = {e.entry_id for e in current}
            if any(rid not in known for rid in remove_ids):
                raise PersonalMemoryStoreError("unknown_entry")
            remaining = [
                (e.target, e.content) for e in current if e.entry_id not in set(remove_ids)
            ]
            if not within_limits([*remaining, *adds]):
                raise PersonalMemoryStoreError("over_limit")
            if remove_ids:
                cur.execute(_DELETE_ENTRIES_SQL, self._params(principal, ids=list(remove_ids)))
            for target, content in adds:
                cur.execute(
                    _INSERT_ENTRY_SQL, self._params(principal, target=target, content=content)
                )
            cur.execute(_BUMP_SQL, self._params(principal, email=principal.user_email.lower()))
            self._audit(
                cur, principal, "learn_applied", len(adds) + len(remove_ids), "hermes_learn"
            )
            return int(version) + 1

    def forget(
        self, principal: Principal, entry_id: str, *, expected_version: int | None = None
    ) -> int | None:
        """1 項目を消して新しい版を返す。見つからなければ None。

        ``expected_version`` を渡すと、一覧を出した時点から版が進んでいれば
        ``version_conflict`` にする（番号がずれた一覧で別の項目を消さない）。
        """
        with self._txn(principal, "forget") as (_conn, cur):
            _state, _noticed, version, _views, _erase = self._lock(cur, principal)
            if expected_version is not None and int(version) != expected_version:
                raise PersonalMemoryStoreError("version_conflict")
            known = {e.entry_id for e in self._entries(cur, principal)}
            if entry_id not in known:
                return None
            cur.execute(_DELETE_ENTRIES_SQL, self._params(principal, ids=[entry_id]))
            cur.execute(_BUMP_SQL, self._params(principal, email=principal.user_email.lower()))
            self._audit(cur, principal, "forget", 1, "user_command")
            return int(version) + 1

    def set_state(self, principal: Principal, state: State) -> None:
        """「覚えるのを止めて」（frozen）と「記憶を再開して」（active）。"""
        if state not in ("active", "frozen"):
            raise PersonalMemoryStoreError("bad_state")
        with self._txn(principal, "set_state") as (conn, cur):
            self._ensure_profile(conn, cur, principal)
            self._lock(cur, principal)
            cur.execute(_SET_STATE_SQL, self._params(principal, state=state))
            self._audit(
                cur, principal, "freeze" if state == "frozen" else "resume", 0, "user_command"
            )

    def request_erase(self, principal: Principal, *, window_s: int = ERASE_WINDOW_S) -> None:
        """「覚えたことを全部消して」の確認待ちにする（window_s 秒以内の確認で消す）。"""
        with self._txn(principal, "request_erase") as (_conn, cur):
            self._lock(cur, principal)
            cur.execute(_REQUEST_ERASE_SQL, self._params(principal, window=int(window_s)))

    def confirm_erase(self, principal: Principal) -> int:
        """確認が期限内なら全項目を消して凍結する。消した件数を返す。"""
        with self._txn(principal, "confirm_erase") as (_conn, cur):
            _state, _noticed, _version, _views, erase_until = self._lock(cur, principal)
            cur.execute("SELECT NOW()")
            row = cur.fetchone()
            now = next(iter(row.values())) if isinstance(row, dict) else row[0]
            if erase_until is None or erase_until < now:
                raise PersonalMemoryStoreError("erase_not_requested")
            cur.execute(_DELETE_ALL_ENTRIES_SQL, self._params(principal))
            deleted = int(cur.rowcount)
            cur.execute(_ERASED_SQL, self._params(principal))
            self._audit(cur, principal, "erase_all", deleted, "user_command")
            return deleted
