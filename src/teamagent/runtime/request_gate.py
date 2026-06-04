"""RequestGate — ボット全体の同時実行を ≤N に絞り、超過分は FIFO キューで待たせる総量規制。

40名同時利用での三大破綻（Bedrock 素 throttle 死 / DB 接続枯渇 / ThreadPool 飽和）を
「入口で総量を絞る」ことで根元から抑える。設計意図「同時≤4並列・それ以降はキュー」を実装。

ポイント:
- ``concurrency``: 同時実行スロット数（既定4）。Semaphore の待機列が FIFO キュー。
- ``queue_max``: 待機キューの最大長。超過は即時・明示拒否（無言ドロップせず「混雑中」）。
- ``acquire_timeout_s``: キューで待てる最大秒。超過はタイムアウト拒否（無限ハングしない）。
- **解放保証**: ``try/finally`` でスロットを必ず返す（実行中タスクが cancel されてもリークしない）。

使い方（Slack ハンドラ側）:
    gate = RequestGate(concurrency=4, queue_max=64)   # プロセスに1個（module-level）
    # ack / 受付メッセージは **Gate の外** で先に出す（Slack の3秒 ack を守るため）
    await say(build_ack_message(...))
    try:
        await gate.submit(dispatch_auto, text, ctx)   # 重い本処理だけ Gate に通す
    except QueueFullError:
        await say("ただいま混雑しています。少し待って再度お試しください。")
    except GateTimeoutError:
        await say("順番待ちが長くなっています。後ほどお試しください。")
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


class QueueFullError(Exception):
    """待機キューが満杯で受理できない（混雑）。ユーザに「混雑中、後ほど」を返す用。"""


class GateTimeoutError(Exception):
    """キューで ``acquire_timeout_s`` を超えて待った（タイムアウト拒否）。"""


@dataclass
class GateMetrics:
    """Gate の観測値（監視・テストの不変条件チェック用）。"""

    accepted: int = 0  # スロットを取得して実行に入った総数
    rejected_queue_full: int = 0  # キュー満杯で拒否
    rejected_timeout: int = 0  # 待機タイムアウトで拒否
    completed: int = 0  # 正常完了
    failed: int = 0  # 例外/キャンセルで終了
    in_flight: int = 0  # 現在実行中（≤ concurrency を常に満たす）
    peak_in_flight: int = 0  # 実行中の最大（= 実効並列度）
    waiting: int = 0  # 現在キューで待機中
    peak_waiting: int = 0  # 待機の最大（= 必要だったキュー深さ）


class RequestGate:
    """同時実行 ≤ concurrency・超過は FIFO キュー（待機上限とタイムアウト付き）。"""

    def __init__(
        self,
        *,
        concurrency: int = 4,
        queue_max: int = 64,
        acquire_timeout_s: float | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency は 1 以上が必要です")
        if queue_max < 0:
            raise ValueError("queue_max は 0 以上が必要です")
        self._concurrency = concurrency
        self._queue_max = queue_max
        self._acquire_timeout_s = acquire_timeout_s
        self._sem = asyncio.Semaphore(concurrency)
        self._m = GateMetrics()

    @property
    def metrics(self) -> GateMetrics:
        return self._m

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def queue_max(self) -> int:
        return self._queue_max

    async def submit(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """``fn(*args, **kwargs)`` を「同時≤concurrency」の制約下で実行する。

        キュー満杯なら QueueFullError、待機タイムアウトなら GateTimeoutError を送出。
        いずれの経路でもスロットはリークしない（取得した場合のみ確実に解放）。
        """
        m = self._m

        # 1) キュー満杯 → 即時・明示拒否（無言ドロップしない）。
        #    判定と waiting+=1 の間に await が無い＝原子的（asyncio は協調的スケジューリング）。
        if m.waiting >= self._queue_max:
            m.rejected_queue_full += 1
            raise QueueFullError(
                f"混雑中（待機 {m.waiting}/{self._queue_max}）。少し待って再度お試しください。"
            )

        # 2) キューに入って空きスロットを待つ（Semaphore の待機列＝FIFO）。
        m.waiting += 1
        if m.waiting > m.peak_waiting:
            m.peak_waiting = m.waiting
        acquired = False
        try:
            if self._acquire_timeout_s is not None:
                try:
                    await asyncio.wait_for(self._sem.acquire(), self._acquire_timeout_s)
                except TimeoutError:
                    m.rejected_timeout += 1
                    raise GateTimeoutError(
                        f"順番待ちがタイムアウトしました（>{self._acquire_timeout_s}s）。"
                    ) from None
            else:
                await self._sem.acquire()
            acquired = True
        finally:
            m.waiting -= 1

        # 3) スロット確保 → 実行。完了/失敗/キャンセルいずれでも必ず解放。
        m.accepted += 1
        m.in_flight += 1
        if m.in_flight > m.peak_in_flight:
            m.peak_in_flight = m.in_flight
        try:
            result = await fn(*args, **kwargs)
        except BaseException:  # CancelledError 含め必ず下の finally で解放
            m.failed += 1
            raise
        else:
            m.completed += 1
            return result
        finally:
            m.in_flight -= 1
            if acquired:
                self._sem.release()


__all__ = [
    "GateMetrics",
    "GateTimeoutError",
    "QueueFullError",
    "RequestGate",
]
