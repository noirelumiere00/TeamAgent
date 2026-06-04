"""ConnectionPool の不変条件テスト（P0①・実DB0・課金0・決定論）。

fake 接続を注入して「総数上限・再利用・返却時 RESET ROLE・壊れた接続の破棄・待ちタイムアウト」を
実 DB 無しで検証する。スレッド上限はワーカーを並走させてピーク同時貸出 ≤ max_size を実証する。
"""

from __future__ import annotations

import threading
import time

import pytest

from teamagent.adapters.pg_pool import (
    ConnectionPool,
    PoolClosedError,
    PoolTimeoutError,
)


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.executed.append(sql)


class _FakeConn:
    """psycopg 接続のうちプールが使う部分だけ模した fake。"""

    def __init__(self, cid: int) -> None:
        self.cid = cid
        self.closed = False
        self.broken = False  # True にすると rollback/cursor が失敗＝reset 不能（壊れた接続）
        self.executed: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        if self.broken:
            raise RuntimeError("connection broken")
        return _FakeCursor(self)

    def rollback(self) -> None:
        if self.broken:
            raise RuntimeError("connection broken")
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def _factory() -> tuple[list[_FakeConn], object]:
    """生成された接続を記録する connect ファクトリを返す。"""
    created: list[_FakeConn] = []

    def connect() -> _FakeConn:
        conn = _FakeConn(cid=len(created))
        created.append(conn)
        return conn

    return created, connect


# -----------------------------------------------------------
# 返却時リセット・再利用
# -----------------------------------------------------------
def test_reset_role_issued_and_committed_on_return() -> None:
    """返却時に rollback → RESET ROLE → commit が走り、idle へ戻る（RLS リセット保証）。"""
    _created, connect = _factory()
    pool: ConnectionPool[_FakeConn] = ConnectionPool(connect, max_size=2)  # type: ignore[arg-type]

    with pool.connection() as conn:
        assert isinstance(conn, _FakeConn)
    # 返却後: reset の痕跡
    assert "RESET ROLE" in conn.executed
    assert conn.rollbacks >= 1
    assert conn.commits >= 1
    s = pool.stats()
    assert s.in_use == 0
    assert s.idle == 1
    assert s.open_total == 1


def test_reuses_idle_connection() -> None:
    """連続借用は同一物理接続を再利用する（接続storm を起こさない）。"""
    created, connect = _factory()
    pool: ConnectionPool[_FakeConn] = ConnectionPool(connect, max_size=4)  # type: ignore[arg-type]

    with pool.connection() as c1:
        first = c1
    with pool.connection() as c2:
        assert c2 is first  # 同じ接続が返ってくる
    assert len(created) == 1  # 物理接続は1本だけ
    assert pool.stats().created == 1


# -----------------------------------------------------------
# 総数上限・待ちタイムアウト
# -----------------------------------------------------------
def test_bounds_total_connections_and_times_out() -> None:
    """max_size を超える同時貸出は待ち、timeout 内に空かなければ PoolTimeoutError。"""
    created, connect = _factory()
    pool: ConnectionPool[_FakeConn] = ConnectionPool(
        connect,
        max_size=3,
        timeout=0.05,
    )  # type: ignore[arg-type]
    cms = [pool.connection() for _ in range(3)]
    held = [cm.__enter__() for cm in cms]  # 3本すべて貸出中
    try:
        assert pool.stats().in_use == 3
        with pytest.raises(PoolTimeoutError):
            with pool.connection():  # 4本目は空き無し → タイムアウト
                pass
        assert pool.stats().timeouts == 1
        assert len(created) == 3  # 上限を超えて作らない
    finally:
        for cm in cms:
            cm.__exit__(None, None, None)
    # 解放後は貸出可能に戻る
    with pool.connection() as conn:
        assert conn in held  # 既存接続を再利用
    assert pool.stats().in_use == 0


# -----------------------------------------------------------
# 壊れた接続の扱い
# -----------------------------------------------------------
def test_broken_connection_discarded_on_return() -> None:
    """返却時 reset に失敗した接続はプールに戻さず破棄する（汚染を持ち越さない）。"""
    created, connect = _factory()
    pool: ConnectionPool[_FakeConn] = ConnectionPool(connect, max_size=2)  # type: ignore[arg-type]

    with pool.connection() as conn:
        conn.broken = True  # 使用中に壊れた想定 → 返却時 reset が失敗する
    assert conn.closed is True  # 破棄された
    s = pool.stats()
    assert s.idle == 0
    assert s.in_use == 0
    assert s.open_total == 0
    assert s.reset_failures == 1

    # 次の借用は新しい接続を作る
    with pool.connection() as conn2:
        assert conn2 is not conn
    assert len(created) == 2


def test_idle_connection_closed_externally_is_evicted() -> None:
    """アイドル中に切れた接続（RDS が idle を切断）は借用時に捨てて作り直す。"""
    created, connect = _factory()
    pool: ConnectionPool[_FakeConn] = ConnectionPool(connect, max_size=2)  # type: ignore[arg-type]

    with pool.connection() as conn:
        first = conn
    # アイドルの間に外部要因でクローズされた
    first.closed = True

    with pool.connection() as conn2:
        assert conn2 is not first  # 壊れた idle は使わず
    assert len(created) == 2
    assert first.closed is True


# -----------------------------------------------------------
# 構築・ウォームアップ・close
# -----------------------------------------------------------
def test_invalid_params_rejected() -> None:
    _, connect = _factory()
    with pytest.raises(ValueError):
        ConnectionPool(connect, max_size=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ConnectionPool(connect, max_size=2, min_size=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ConnectionPool(connect, max_size=2, timeout=0)  # type: ignore[arg-type]


def test_min_size_warmup_pre_opens_connections() -> None:
    """min_size 指定で起動時に接続を事前確保（初回レイテンシ低減）。"""
    created, connect = _factory()
    pool: ConnectionPool[_FakeConn] = ConnectionPool(connect, max_size=4, min_size=2)  # type: ignore[arg-type]
    s = pool.stats()
    assert s.idle == 2
    assert s.in_use == 0
    assert s.open_total == 2
    assert len(created) == 2


def test_close_disposes_idle_and_blocks_new_borrows() -> None:
    """close でアイドル接続を全て閉じ、以後の借用は PoolClosedError。"""
    created, connect = _factory()
    pool: ConnectionPool[_FakeConn] = ConnectionPool(connect, max_size=4, min_size=2)  # type: ignore[arg-type]
    pool.close()
    assert all(c.closed for c in created)
    with pytest.raises(PoolClosedError):
        with pool.connection():
            pass


# -----------------------------------------------------------
# スレッド上限（負荷の核）
# -----------------------------------------------------------
def test_concurrency_capped_under_threads() -> None:
    """40スレッド同時投入でも同時貸出は ≤ max_size、全件完了・接続数も ≤ max_size。"""
    created, connect = _factory()
    pool: ConnectionPool[_FakeConn] = ConnectionPool(connect, max_size=4, timeout=5.0)  # type: ignore[arg-type]

    current = 0
    peak = 0
    done = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal current, peak, done
        with pool.connection():
            with lock:
                current += 1
                peak = max(peak, current)
                assert current <= 4, f"同時貸出 {current} が上限4を超過"
            time.sleep(0.005)
            with lock:
                current -= 1
                done += 1

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert done == 40  # 全件完了（ドロップ無し）
    assert peak == 4  # 上限を使い切る
    assert len(created) <= 4  # 物理接続は上限以内（storm にならない）
    assert pool.stats().in_use == 0  # リーク無し
