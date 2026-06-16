"""指数バックオフ + フルジッタの同期リトライ（Bedrock 等の一過性エラー対策）。

40名同時利用では、入口の RequestGate で同時実行を ≤4 に絞っても、Bedrock 側の
ThrottlingException / 5xx / 一時的な接続断は依然起こりうる。これらを「一過性」と分類して
指数バックオフ + フルジッタで自動リトライし、ユーザに無用なエラーを見せない。

設計:
- ``is_retryable(exc) -> bool`` で「リトライすべき例外か」を呼び出し側が決める
  （Bedrock 固有のエラーコード分類は bedrock_client 側に置き、本モジュールは汎用・依存ゼロ）。
- フルジッタ（AWS 推奨）: ``delay = uniform(0, min(cap, base * 2^n))``。同時多発リトライの
  同期（thundering herd）を散らす。
- ``sleep`` / ``jitter`` を注入可能にしてテストを決定論にする（実スリープ0・課金0）。
- リトライ枯渇時は **最後の例外をそのまま送出**（上位の ClientError ハンドラを壊さない）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """リトライの上限・バックオフ係数。"""

    max_attempts: int = 5  # 初回 + リトライ を含む総試行回数（スリープは最大 max_attempts-1 回）
    base_delay_s: float = 0.5  # 1回目リトライ前の基準待ち
    max_delay_s: float = 20.0  # 1回の待ちの上限（cap）
    # 既定(5/0.5/20)での最悪追加待ち＝cap系列 0.5+1+2+4 の和 = 7.5s（cap20には届かず実質効かない）

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts は 1 以上が必要です")
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("delay は 0 以上が必要です")


def _full_jitter(cap: float) -> float:
    """フルジッタ: [0, cap] の一様乱数。"""
    return random.uniform(0.0, cap)


def backoff_cap(policy: RetryPolicy, attempt: int) -> float:
    """``attempt`` 回目（1-based）の失敗後に待つ上限（cap）= min(max, base * 2^(n-1))。"""
    return min(policy.max_delay_s, policy.base_delay_s * (2.0 ** (attempt - 1)))


def call_with_retry(
    fn: Callable[[], T],
    *,
    is_retryable: Callable[[BaseException], bool],
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float], float] = _full_jitter,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> T:
    """``fn()`` を実行し、``is_retryable`` が True の例外なら指数バックオフで再試行する。

    Args:
        fn: 引数なしで呼べる呼び出し（``lambda: client.converse(**kwargs)`` 等）。
        is_retryable: 例外を見て「一過性＝リトライ可」を返す述語。
        policy: 上限・バックオフ係数（省略時は既定 RetryPolicy）。
        sleep: 待機関数（テストで差し替え）。
        jitter: cap を渡すと実待ち秒を返す（テストで決定論化）。
        on_retry: リトライ直前フック ``(attempt, delay_s, exc)``（ログ用）。

    Returns:
        ``fn()`` の戻り値。

    Raises:
        リトライ不可の例外、またはリトライを使い切った後の最後の例外をそのまま送出。
    """
    pol = policy or RetryPolicy()
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:  # 分類は is_retryable に委譲し、対象外/枯渇は必ず再送出
            if attempt >= pol.max_attempts or not is_retryable(exc):
                raise
            delay = jitter(backoff_cap(pol, attempt))
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep(delay)


__all__ = ["RetryPolicy", "backoff_cap", "call_with_retry"]
