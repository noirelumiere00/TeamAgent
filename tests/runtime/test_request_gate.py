"""RequestGate の負荷/不変条件テスト（課金0・外部依存0・決定論）。

設計意図「同時≤4並列・超過はFIFOキュー」を asyncio 上で実証する。
asyncio は協調的スケジューリングなので、タイミング数値ではなく**不変条件**で検証する。
"""

from __future__ import annotations

import asyncio

import pytest

from teamagent.runtime.request_gate import (
    GateTimeoutError,
    QueueFullError,
    RequestGate,
)


async def test_bounds_concurrency_and_completes_all() -> None:
    """40件同時投入で 同時≤4・全件完了・ドロップ0・リークなし（P0-1）。"""
    gate = RequestGate(concurrency=4, queue_max=64)
    current = 0
    peak = 0
    completed: list[int] = []
    lock = asyncio.Lock()

    async def work(i: int) -> None:
        nonlocal current, peak
        async with lock:
            current += 1
            peak = max(peak, current)
            assert current <= 4, f"並列 {current} が上限4を超過"
        await asyncio.sleep(0.02)
        async with lock:
            current -= 1
        completed.append(i)

    await asyncio.gather(*[gate.submit(work, i) for i in range(40)])

    assert peak == 4  # 上限4を実際に使い切る（過小直列化でない）
    assert sorted(completed) == list(range(40))  # 全件完了・ドロップ0
    m = gate.metrics
    assert m.peak_in_flight == 4
    assert m.in_flight == 0  # リークなし
    assert m.completed == 40
    assert m.rejected_queue_full == 0
    assert m.peak_waiting == 40 - 4  # 36 が待機の最大


async def test_rejects_when_queue_full() -> None:
    """4実行+10キュー=14受理、超過6件は即時・明示拒否（P0-3）。無言ドロップ禁止。"""
    gate = RequestGate(concurrency=4, queue_max=10)
    release = asyncio.Event()

    async def slow(i: int) -> None:
        await release.wait()

    tasks = [asyncio.create_task(gate.submit(slow, i)) for i in range(20)]
    await asyncio.sleep(0.05)  # 4 acquire + 10 queue が落ち着く

    assert gate.metrics.peak_in_flight == 4
    assert gate.metrics.rejected_queue_full == 6  # 20 - (4+10)

    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rejected = [r for r in results if isinstance(r, QueueFullError)]
    assert len(rejected) == 6
    assert gate.metrics.in_flight == 0  # 拒否してもリークなし
    assert gate.metrics.completed == 14


async def test_cancel_releases_slot() -> None:
    """実行中タスクを cancel してもスロットが解放される（P0-4・リーク防止）。"""
    gate = RequestGate(concurrency=2, queue_max=10)
    release = asyncio.Event()

    async def slow(i: int) -> None:
        await release.wait()

    tasks = [asyncio.create_task(gate.submit(slow, i)) for i in range(2)]
    await asyncio.sleep(0.02)
    assert gate.metrics.in_flight == 2

    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    await asyncio.sleep(0.01)
    assert gate.metrics.in_flight == 1  # cancel した分のスロットが解放された

    release.set()
    await tasks[1]
    assert gate.metrics.in_flight == 0


async def test_fifo_fairness() -> None:
    """concurrency=1 なら厳密 FIFO（スタベーションなし・P0-2）。"""
    gate = RequestGate(concurrency=1, queue_max=20)
    order: list[int] = []

    async def work(i: int) -> None:
        await asyncio.sleep(0.005)
        order.append(i)

    await asyncio.gather(*[gate.submit(work, i) for i in range(10)])
    assert order == list(range(10))


async def test_acquire_timeout_rejects() -> None:
    """空きスロットを待ちきれない場合はタイムアウト拒否（無限ハングしない）。"""
    gate = RequestGate(concurrency=1, queue_max=10, acquire_timeout_s=0.05)
    release = asyncio.Event()

    async def slow(i: int) -> None:
        await release.wait()

    holder = asyncio.create_task(gate.submit(slow, 0))
    await asyncio.sleep(0.02)  # holder が唯一のスロットを保持

    with pytest.raises(GateTimeoutError):
        await gate.submit(slow, 1)  # 0.05s 待ってタイムアウト
    assert gate.metrics.rejected_timeout == 1

    release.set()
    await holder
    assert gate.metrics.in_flight == 0


def test_invalid_params_rejected() -> None:
    """不正な引数は構築時に弾く。"""
    with pytest.raises(ValueError):
        RequestGate(concurrency=0)
    with pytest.raises(ValueError):
        RequestGate(queue_max=-1)
