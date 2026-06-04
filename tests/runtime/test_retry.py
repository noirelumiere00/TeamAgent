"""call_with_retry の不変条件テスト（課金0・実スリープ0・決定論）。

sleep / jitter を注入して時間を排除し、「何回呼ぶ / いつ諦める / バックオフは増える」を検証する。
"""

from __future__ import annotations

import pytest

from teamagent.runtime.retry import RetryPolicy, backoff_cap, call_with_retry


class _TransientError(Exception):
    """テスト用の一過性エラー（リトライ対象）。"""


class _PermanentError(Exception):
    """テスト用の恒久エラー（リトライ非対象）。"""


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, _TransientError)


def _recorder() -> tuple[list[float], object]:
    """sleep 呼び出し秒を記録する関数を返す。"""
    slept: list[float] = []

    def _sleep(s: float) -> None:
        slept.append(s)

    return slept, _sleep


def test_succeeds_first_try_no_sleep() -> None:
    """初回成功なら fn は1回・sleep は0回。"""
    slept, sleep = _recorder()
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    out = call_with_retry(fn, is_retryable=_retryable, sleep=sleep)  # type: ignore[arg-type]
    assert out == "ok"
    assert calls == 1
    assert slept == []


def test_retries_then_succeeds() -> None:
    """2回失敗→3回目成功。fn は3回・sleep は2回・cap は base, base*2。"""
    slept, sleep = _recorder()
    policy = RetryPolicy(max_attempts=5, base_delay_s=0.5, max_delay_s=20.0)
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _TransientError("slow down")
        return "done"

    out = call_with_retry(
        fn,
        is_retryable=_retryable,
        policy=policy,
        sleep=sleep,  # type: ignore[arg-type]
        jitter=lambda cap: cap,  # フルジッタを無効化＝cap そのもの（決定論）
    )
    assert out == "done"
    assert calls == 3
    assert slept == [0.5, 1.0]  # base*2^0, base*2^1


def test_non_retryable_raises_immediately() -> None:
    """リトライ非対象の例外は即送出・sleep 0回・fn は1回。"""
    slept, sleep = _recorder()
    calls = 0

    def fn() -> None:
        nonlocal calls
        calls += 1
        raise _PermanentError("nope")

    with pytest.raises(_PermanentError):
        call_with_retry(fn, is_retryable=_retryable, sleep=sleep)  # type: ignore[arg-type]
    assert calls == 1
    assert slept == []


def test_exhausts_and_raises_last_exception() -> None:
    """全リトライ枯渇で最後の例外を送出。fn は max_attempts 回・on_retry は max-1 回。"""
    slept, sleep = _recorder()
    policy = RetryPolicy(max_attempts=4, base_delay_s=0.1, max_delay_s=10.0)
    calls = 0
    retries: list[int] = []

    def fn() -> None:
        nonlocal calls
        calls += 1
        raise _TransientError(f"fail-{calls}")

    with pytest.raises(_TransientError, match="fail-4"):
        call_with_retry(
            fn,
            is_retryable=_retryable,
            policy=policy,
            sleep=sleep,  # type: ignore[arg-type]
            jitter=lambda cap: cap,
            on_retry=lambda attempt, delay, exc: retries.append(attempt),
        )
    assert calls == 4  # 初回 + 3リトライ
    assert retries == [1, 2, 3]  # on_retry は各リトライ直前に1回ずつ
    assert len(slept) == 3


def test_backoff_cap_is_capped_and_grows() -> None:
    """cap は base*2^(n-1) で増え、max_delay_s で頭打ち。"""
    policy = RetryPolicy(max_attempts=10, base_delay_s=1.0, max_delay_s=8.0)
    caps = [backoff_cap(policy, n) for n in range(1, 7)]
    assert caps == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]  # 1,2,4,8 で頭打ち


def test_full_jitter_default_stays_within_bounds() -> None:
    """既定ジッタは [0, cap] に収まる（thundering herd 緩和）。"""
    policy = RetryPolicy(max_attempts=6, base_delay_s=0.5, max_delay_s=20.0)
    # 既定 jitter（実乱数）でも常に 0 <= delay <= cap
    slept, sleep = _recorder()
    calls = 0

    def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 6:  # 5回失敗 → 6回目成功（初回 + 5リトライ）
            raise _TransientError("x")
        return "ok"

    call_with_retry(fn, is_retryable=_retryable, policy=policy, sleep=sleep)  # type: ignore[arg-type]
    assert len(slept) == 5
    for n, delay in enumerate(slept, start=1):
        assert 0.0 <= delay <= backoff_cap(policy, n)


def test_invalid_policy_rejected() -> None:
    """max_attempts<1 や負の delay は構築時に弾く。"""
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_s=-1.0)
