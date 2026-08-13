"""UsageRecorder のテスト（実DB0・課金0）。

best-effort 記録の不変条件: 正しい列で INSERT・未知statusはokに補正・DB失敗でも例外を投げない・
disabled で no-op・書込ロールは teamagent_app（admin GUC を立てない＝書くだけ）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from teamagent.runtime.usage_recorder import UsageEvent, UsageRecorder


class _FakeCursor:
    def __init__(self, rec: list[tuple[str, Any]]) -> None:
        self._rec = rec

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._rec.append((sql, params))


class _FakeConn:
    def __init__(self, rec: list[tuple[str, Any]]) -> None:
        self._rec = rec

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rec)


class _FakePg:
    """PgVectorClient.connection の最小 fake。connection kwargs と実行SQLを記録。"""

    def __init__(self, *, raise_on_connect: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.executed: list[tuple[str, Any]] = []
        self._raise = raise_on_connect

    @contextmanager
    def connection(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if self._raise:
            raise RuntimeError("db down")
        yield _FakeConn(self.executed)


def test_write_inserts_row_with_app_role() -> None:
    pg = _FakePg()
    rec = UsageRecorder(pg)
    rec.write(
        UsageEvent(
            request_id="r1",
            skill="search",
            status="ok",
            user_email="a@x.com",
            user_id="U1",
            cost_usd=0.0123,
            latency_ms=120,
            query_chars=5,
            query_text="hello",
            via="mention",
        )
    )
    # 書込は teamagent_app（admin GUC を立てない）
    assert pg.calls[0] == {"app_role": "teamagent_app"}
    sql, params = pg.executed[0]
    assert "INSERT INTO usage_events" in sql
    assert "ON CONFLICT (request_id) DO NOTHING" in sql
    assert params["request_id"] == "r1"
    assert params["skill"] == "search"
    assert params["cost_usd"] == 0.0123
    assert params["user_email"] == "a@x.com"
    assert params["status"] == "ok"
    assert params["query_chars"] == 5
    assert params["query_text"] == "hello"
    assert "query_text" in sql


def test_write_truncates_query_text_to_2000_characters() -> None:
    pg = _FakePg()
    rec = UsageRecorder(pg)
    rec.write(UsageEvent(request_id="r1-long", skill="search", query_text="あ" * 2001))
    _sql, params = pg.executed[-1]
    assert params["query_text"] == "あ" * 2000


def test_write_preserves_none_query_text() -> None:
    pg = _FakePg()
    rec = UsageRecorder(pg)
    rec.write(UsageEvent(request_id="r1-none", skill="search", query_text=None))
    _sql, params = pg.executed[-1]
    assert params["query_text"] is None


def test_write_coerces_unknown_status_to_ok() -> None:
    """CHECK 制約違反で全行落とさないよう、未知 status は ok に倒す。"""
    pg = _FakePg()
    rec = UsageRecorder(pg)
    rec.write(UsageEvent(request_id="r2", skill="x", status="bogus"))
    _sql, params = pg.executed[-1]
    assert params["status"] == "ok"


def test_write_preserves_valid_error_status() -> None:
    pg = _FakePg()
    rec = UsageRecorder(pg)
    rec.write(UsageEvent(request_id="r2b", skill="x", status="queue_full"))
    _sql, params = pg.executed[-1]
    assert params["status"] == "queue_full"


async def test_record_writes_via_executor() -> None:
    pg = _FakePg()
    rec = UsageRecorder(pg)
    await rec.record(UsageEvent(request_id="r4", skill="search", cost_usd=0.01))
    assert pg.executed[0][1]["request_id"] == "r4"


async def test_record_swallows_db_errors() -> None:
    """DB 失敗でも例外を投げない（ユーザ処理を止めない）。"""
    pg = _FakePg(raise_on_connect=True)
    rec = UsageRecorder(pg)
    await rec.record(UsageEvent(request_id="r3", skill="x"))  # 例外が漏れないこと
    assert pg.executed == []


async def test_record_disabled_is_noop() -> None:
    pg = _FakePg()
    rec = UsageRecorder(pg, enabled=False)
    await rec.record(UsageEvent(request_id="r5", skill="x"))
    assert pg.executed == []
    assert pg.calls == []
