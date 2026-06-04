"""RequestGate / 接続プールの定期スナップショット（管理画面 runtime_metrics 用）。

GateMetrics / PoolStats は Bot プロセスのメモリ内・揮発値で、別プロセスの管理画面からは
直接読めない。本モジュールの常駐タスクが一定間隔（既定15秒）でスナップショットを
``runtime_metrics`` に1行ずつ INSERT し、画面はそれを read する。

- 失敗してもユーザ処理を止めない（best-effort・ログのみ）。
- 同期 DB 書込は ``run_in_executor`` でワーカースレッドへ逃がしイベントループを塞がない。
- スナップショットは「メトリクス読取り→DB接続借用→書込」の順。借用は読取りの後なので、
  スナップショット自身の接続が in_use を1膨らませて自分を観測することはない。
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


_INSERT_SQL = """
INSERT INTO runtime_metrics
    (instance_id, gate_in_flight, gate_peak_in_flight, gate_waiting, gate_peak_waiting,
     gate_accepted, gate_completed, gate_failed, gate_rejected_queue_full,
     gate_rejected_timeout, gate_concurrency, gate_queue_max,
     pool_max_size, pool_in_use, pool_idle, pool_open_total, pool_created, pool_closed,
     pool_timeouts, pool_reset_failures)
VALUES
    (%(instance_id)s, %(gate_in_flight)s, %(gate_peak_in_flight)s, %(gate_waiting)s,
     %(gate_peak_waiting)s, %(gate_accepted)s, %(gate_completed)s, %(gate_failed)s,
     %(gate_rejected_queue_full)s, %(gate_rejected_timeout)s, %(gate_concurrency)s,
     %(gate_queue_max)s, %(pool_max_size)s, %(pool_in_use)s, %(pool_idle)s,
     %(pool_open_total)s, %(pool_created)s, %(pool_closed)s, %(pool_timeouts)s,
     %(pool_reset_failures)s)
"""


def build_snapshot_row(gate: Any, pool_stats: Any, instance_id: str) -> dict[str, Any]:
    """gate.metrics + PoolStats(or None) を runtime_metrics の1行 dict へ変換（純粋関数）。"""
    g = gate.metrics
    row: dict[str, Any] = {
        "instance_id": instance_id,
        "gate_in_flight": g.in_flight,
        "gate_peak_in_flight": g.peak_in_flight,
        "gate_waiting": g.waiting,
        "gate_peak_waiting": g.peak_waiting,
        "gate_accepted": g.accepted,
        "gate_completed": g.completed,
        "gate_failed": g.failed,
        "gate_rejected_queue_full": g.rejected_queue_full,
        "gate_rejected_timeout": g.rejected_timeout,
        "gate_concurrency": gate.concurrency,
        "gate_queue_max": gate.queue_max,
        # プール無効（直結）時は None（DB 列は NULL 許容）
        "pool_max_size": None,
        "pool_in_use": None,
        "pool_idle": None,
        "pool_open_total": None,
        "pool_created": None,
        "pool_closed": None,
        "pool_timeouts": None,
        "pool_reset_failures": None,
    }
    if pool_stats is not None:
        row.update(
            pool_max_size=pool_stats.max_size,
            pool_in_use=pool_stats.in_use,
            pool_idle=pool_stats.idle,
            pool_open_total=pool_stats.open_total,
            pool_created=pool_stats.created,
            pool_closed=pool_stats.closed,
            pool_timeouts=pool_stats.timeouts,
            pool_reset_failures=pool_stats.reset_failures,
        )
    return row


def default_instance_id() -> str:
    """host:pid（複数プロセス集約用の識別子）。"""
    return f"{socket.gethostname()}:{os.getpid()}"


class MetricsSnapshotter:
    """gate/pool のスナップショットを定期 INSERT する常駐タスク。"""

    def __init__(
        self,
        gate: Any,
        pgvector: Any,
        *,
        interval_s: float = 15.0,
        instance_id: str | None = None,
        app_role: str = "teamagent_app",
    ) -> None:
        self._gate = gate
        self._pg = pgvector
        self._interval_s = interval_s
        self._instance_id = instance_id or default_instance_id()
        self._app_role = app_role
        self._stopped = False

    def snapshot_once(self) -> None:
        """1スナップショットを同期 INSERT（テスト可能な実体）。読取り→借用→書込の順。"""
        pool_stats = self._pg.pool_stats() if hasattr(self._pg, "pool_stats") else None
        row = build_snapshot_row(self._gate, pool_stats, self._instance_id)
        with self._pg.connection(app_role=self._app_role) as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, row)

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """停止されるまで interval ごとにスナップショットを取る（best-effort）。"""
        while not self._stopped:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.snapshot_once)
            except Exception:
                logger.warning("runtime_metrics_snapshot_failed")
            await asyncio.sleep(self._interval_s)


__all__ = ["MetricsSnapshotter", "build_snapshot_row", "default_instance_id"]
