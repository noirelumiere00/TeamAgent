"""MetricsSnapshotter のテスト（実DB0・課金0）。

GateMetrics/PoolStats → runtime_metrics 行への正しいマッピング、直結時の pool_* NULL、
snapshot_once の INSERT、run ループの停止を検証する。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any

from teamagent.adapters.pg_pool import PoolStats
from teamagent.runtime.metrics_snapshot import (
    MetricsSnapshotter,
    build_snapshot_row,
    default_instance_id,
)
from teamagent.runtime.request_gate import GateMetrics


class _FakeGate:
    def __init__(self, metrics: GateMetrics, *, concurrency: int = 4, queue_max: int = 64) -> None:
        self._m = metrics
        self._c = concurrency
        self._q = queue_max

    @property
    def metrics(self) -> GateMetrics:
        return self._m

    @property
    def concurrency(self) -> int:
        return self._c

    @property
    def queue_max(self) -> int:
        return self._q


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
    def __init__(self, pool_stats: PoolStats | None) -> None:
        self._pool_stats = pool_stats
        self.executed: list[tuple[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def pool_stats(self) -> PoolStats | None:
        return self._pool_stats

    @contextmanager
    def connection(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        yield _FakeConn(self.executed)


def _gate_metrics() -> GateMetrics:
    return GateMetrics(
        accepted=10,
        rejected_queue_full=2,
        rejected_timeout=1,
        completed=8,
        failed=1,
        in_flight=3,
        peak_in_flight=4,
        waiting=5,
        peak_waiting=7,
    )


def _pool_stats() -> PoolStats:
    return PoolStats(
        max_size=8,
        in_use=3,
        idle=2,
        open_total=5,
        created=9,
        closed=4,
        timeouts=1,
        reset_failures=0,
    )


def test_build_row_maps_gate_and_pool() -> None:
    gate = _FakeGate(_gate_metrics(), concurrency=4, queue_max=64)
    row = build_snapshot_row(gate, _pool_stats(), "host:123")
    assert row["instance_id"] == "host:123"
    assert row["gate_in_flight"] == 3
    assert row["gate_peak_in_flight"] == 4
    assert row["gate_waiting"] == 5
    assert row["gate_peak_waiting"] == 7
    assert row["gate_rejected_queue_full"] == 2
    assert row["gate_rejected_timeout"] == 1
    assert row["gate_concurrency"] == 4
    assert row["gate_queue_max"] == 64
    assert row["pool_max_size"] == 8
    assert row["pool_in_use"] == 3
    assert row["pool_timeouts"] == 1


def test_build_row_pool_none_is_null() -> None:
    """直結モード（pool_stats=None）では pool_* がすべて None。"""
    gate = _FakeGate(_gate_metrics())
    row = build_snapshot_row(gate, None, "h:1")
    for key in (
        "pool_max_size",
        "pool_in_use",
        "pool_idle",
        "pool_open_total",
        "pool_created",
        "pool_closed",
        "pool_timeouts",
        "pool_reset_failures",
    ):
        assert row[key] is None


def test_snapshot_once_inserts_row() -> None:
    gate = _FakeGate(_gate_metrics())
    pg = _FakePg(_pool_stats())
    snap = MetricsSnapshotter(gate, pg, instance_id="h:9")
    snap.snapshot_once()
    assert pg.calls[0] == {"app_role": "teamagent_app"}
    sql, params = pg.executed[0]
    assert "INSERT INTO runtime_metrics" in sql
    assert params["gate_peak_in_flight"] == 4
    assert params["pool_in_use"] == 3
    assert params["instance_id"] == "h:9"


def test_default_instance_id_has_host_and_pid() -> None:
    iid = default_instance_id()
    assert ":" in iid
    assert iid.rsplit(":", 1)[1].isdigit()


async def test_run_loop_stops_after_stop_called() -> None:
    """snapshot_once が stop を呼んだら run は1回で抜ける（無限ループしない）。"""
    gate = _FakeGate(_gate_metrics())
    pg = _FakePg(_pool_stats())
    snap = MetricsSnapshotter(gate, pg, interval_s=0.01)

    calls = 0
    real_stop = snap.stop

    def once() -> None:
        nonlocal calls
        calls += 1
        real_stop()

    snap.snapshot_once = once  # type: ignore[method-assign]
    await asyncio.wait_for(snap.run(), timeout=1.0)
    assert calls == 1
