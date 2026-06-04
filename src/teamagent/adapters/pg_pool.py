"""依存ゼロの最小コネクションプール（RDS 接続枯渇 / 接続storm 対策・P0①）。

40名同時利用で ``connection()`` が毎回 ``psycopg.connect`` → ``close`` すると、
(1) TLS+認証の往復が毎リクエスト発生して遅く、(2) RDS の ``max_connections`` を一気に
圧迫する。入口の RequestGate で同時実行を ≤4 に絞っても ingest 等の非ゲート経路もあるため、
**総接続数の上限をプールで物理的に固定** し、確立済み接続を使い回す。

セキュリティ上の肝（RLS との両立）:
- ``SET ROLE`` は **セッション持続**（commit を跨ぐ）。プールで使い回す接続に前の借用者の
  ロールが残ると越権の温床になる。よって **返却時に必ず ``RESET ROLE`` して確定（commit）** する。
- ``set_config(..., is_local=true)`` の GUC（``app.user_email`` 等）は txn-local なので借用側の
  commit/rollback で消えるが、保険として返却時の reset でも rollback してから掃除する。
- 返却時の reset が失敗した接続（＝壊れている）は **プールに戻さず破棄**（汚染を持ち越さない）。

本番では psycopg_pool.ConnectionPool（``configure=register_vector`` / ``reset=RESET ROLE`` /
``check=ConnectionPool.check_connection``）へ置換可能。本クラスは依存を増やさず同じ不変条件を
満たす最小実装＝設計の実証（drop-in spec 参照）。

スレッド安全: ``threading.Semaphore`` で総貸出数を上限管理し、``threading.Lock`` で idle 集合を
保護する。各接続は同時に1借用者にのみ渡す（psycopg 接続のスレッド共有はしない）。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import structlog

logger = structlog.get_logger(__name__)

C = TypeVar("C")  # 接続型（psycopg.Connection を想定するが、本体は依存しない）


class PoolTimeoutError(Exception):
    """空き接続を timeout 内に確保できなかった（RDS 飽和 or 接続リーク疑い）。"""


class PoolClosedError(Exception):
    """close 済みプールから接続を借りようとした。"""


@dataclass(frozen=True)
class PoolStats:
    """プールの観測スナップショット（監視・テスト・管理画面用）。"""

    max_size: int
    in_use: int  # 現在貸出中
    idle: int  # 現在アイドル（即時再利用可）
    open_total: int  # 現存する物理接続数（in_use + idle）
    created: int  # 累計で開いた接続数
    closed: int  # 累計で閉じた接続数
    timeouts: int  # 貸出待ちタイムアウト累計
    reset_failures: int  # 返却時 reset 失敗（＝破棄）累計


def _default_is_broken(conn: Any) -> bool:
    """psycopg 接続が閉じている/壊れているか（既定）。"""
    return bool(getattr(conn, "closed", False))


def _default_close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # close 失敗は致命ではない（既に切断済み等）
        logger.debug("pg_pool_close_ignored", exc_info=True)


def _default_reset(conn: Any) -> None:
    """返却時リセット: 開いた txn を捨て、``RESET ROLE`` を確定する（RLS 取りこぼし防止）。

    - ``rollback()``: 借用中に開いた txn を破棄（txn-local GUC もここで消える）。
    - ``RESET ROLE``: セッション持続のロールを既定（ログインロール）へ戻す。
    - ``commit()``: ``RESET ROLE`` を確定（rollback だと巻き戻り、次の借用者へ持ち越す）。
    例外はそのまま送出し、呼び出し側（_release）が「壊れた接続」として破棄する。
    """
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("RESET ROLE")
    conn.commit()


class ConnectionPool(Generic[C]):
    """総貸出数 ≤ max_size の最小スレッドセーフプール（返却時 RLS リセット付き）。"""

    def __init__(
        self,
        connect: Callable[[], C],
        *,
        max_size: int = 8,
        min_size: int = 0,
        timeout: float = 10.0,
        reset: Callable[[C], None] = _default_reset,
        is_broken: Callable[[C], bool] = _default_is_broken,
        close: Callable[[C], None] = _default_close,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size は 1 以上が必要です")
        if min_size < 0 or min_size > max_size:
            raise ValueError("min_size は 0..max_size の範囲が必要です")
        if timeout <= 0:
            raise ValueError("timeout は正の秒数が必要です")
        self._connect = connect
        self._max_size = max_size
        self._timeout = timeout
        self._reset = reset
        self._is_broken = is_broken
        self._close = close

        self._sem = threading.Semaphore(max_size)
        self._lock = threading.Lock()
        self._idle: deque[C] = deque()
        self._closed = False
        # 統計（_lock 下で更新）
        self._in_use = 0
        self._open_total = 0
        self._created = 0
        self._closed_count = 0
        self._timeouts = 0
        self._reset_failures = 0

        # 任意のウォームアップ（best-effort: 失敗しても遅延生成に委ねる）
        for _ in range(min_size):
            try:
                conn = self._make()
                with self._lock:
                    self._idle.append(conn)
            except Exception:
                logger.warning("pg_pool_warmup_failed", exc_info=True)
                break

    # ---- public ---------------------------------------------------
    @property
    def max_size(self) -> int:
        return self._max_size

    def stats(self) -> PoolStats:
        with self._lock:
            return PoolStats(
                max_size=self._max_size,
                in_use=self._in_use,
                idle=len(self._idle),
                open_total=self._open_total,
                created=self._created,
                closed=self._closed_count,
                timeouts=self._timeouts,
                reset_failures=self._reset_failures,
            )

    @contextmanager
    def connection(self) -> Iterator[C]:
        """接続を借りて使い、終了時に reset して返す（壊れていれば破棄）。"""
        conn = self._acquire()
        try:
            yield conn
        finally:
            self._release(conn)

    def close(self) -> None:
        """以後の貸出を拒否し、アイドル接続をすべて閉じる（貸出中は返却時に破棄される）。

        注: close と _acquire の間に極小の競合窓があり、close 確定の直後に進行中の _acquire が
        新規接続を1本作り得るが、その接続は ``_release`` 時に（_closed のため）必ず dispose される。
        よって **接続/permit のリークは起きない**（最終会計は整合）。close 直後の一瞬だけ
        ``stats().open_total`` が 0 に下がらない過渡があるが、運用上の実害は無い（意図的挙動）。
        """
        with self._lock:
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
        for conn in idle:
            self._dispose(conn)

    # ---- internal -------------------------------------------------
    def _make(self) -> C:
        conn = self._connect()
        with self._lock:
            self._open_total += 1
            self._created += 1
        return conn

    def _dispose(self, conn: C) -> None:
        with self._lock:
            self._open_total -= 1
            self._closed_count += 1
        self._close(conn)

    def _acquire(self) -> C:
        if self._closed:
            raise PoolClosedError("pool は close 済みです")
        if not self._sem.acquire(timeout=self._timeout):
            with self._lock:
                self._timeouts += 1
            raise PoolTimeoutError(
                f"接続待ちタイムアウト（{self._timeout}s, max_size={self._max_size}）。"
                "RDS 飽和か接続リークの可能性。"
            )
        try:
            if self._closed:  # 待っている間に close された
                raise PoolClosedError("pool は close 済みです")
            while True:
                with self._lock:
                    conn = self._idle.popleft() if self._idle else None
                if conn is None:
                    conn = self._make()  # 新規物理接続（permit は確保済み）
                elif self._is_broken(conn):
                    self._dispose(conn)  # アイドル中に切れた → 捨てて作り直す
                    continue
                with self._lock:
                    self._in_use += 1
                return conn
        except BaseException:
            self._sem.release()  # permit 確保後の失敗は必ず返す（リーク防止）
            raise

    def _release(self, conn: C) -> None:
        keep = False
        try:
            if not self._closed and not self._is_broken(conn):
                self._reset(conn)  # rollback + RESET ROLE + commit
                keep = True
        except Exception:
            with self._lock:
                self._reset_failures += 1
            logger.warning("pg_pool_reset_failed", exc_info=True)
            keep = False
        with self._lock:
            self._in_use -= 1
            if keep:
                self._idle.append(conn)
        if not keep:
            self._dispose(conn)
        self._sem.release()


__all__ = [
    "ConnectionPool",
    "PoolClosedError",
    "PoolStats",
    "PoolTimeoutError",
]
