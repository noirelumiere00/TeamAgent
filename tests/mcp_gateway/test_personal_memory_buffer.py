"""揮発バッファ・日次枠・同時実行枠・observe の受付（I12・I16・I19）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from teamagent.adapters.personal_memory_store import Principal
from teamagent.mcp_gateway.personal_memory import buffer as pm_buffer
from teamagent.mcp_gateway.personal_memory.buffer import (
    DailyJobQuota,
    JobSlots,
    Sweeper,
    VolatileUtteranceBuffer,
    configured_max_jobs,
)
from teamagent.mcp_gateway.personal_memory.schemas import CommandInput, ObserveInput
from tests.personal_memory.fakes import FakeStore, HermesFake, ManualLauncher, make_runtime

A = Principal("T0123456789", "U0000000A1", "a@vectorinc.co.jp")
B = Principal("T0123456789", "U0000000B1", "b@vectorinc.co.jp")


def _fill(buf: VolatileUtteranceBuffer, p: Principal, n: int, *, start: int = 0, now: float = 0):
    out = None
    for i in range(start, start + n):
        out = buf.add(p, f"発話{i}", f"1784424000.{i:06d}", now)
    return out


# --- バッファ ------------------------------------------------------------------------------


def test_fifth_utterance_flushes_exactly_once() -> None:
    buf = VolatileUtteranceBuffer()
    assert _fill(buf, A, 4) is None
    batch = buf.add(A, "発話4", "1784424000.000004", 0)
    assert batch is not None
    assert batch.utterances == ("発話0", "発話1", "発話2", "発話3", "発話4")
    assert buf.pending_utterances(A) == 0
    # 6 件目は新しいバッファに入る
    assert buf.add(A, "発話5", "1784424000.000005", 1) is None
    assert buf.pending_utterances(A) == 1


def test_same_message_id_counts_once() -> None:
    buf = VolatileUtteranceBuffer()
    for _ in range(5):
        assert buf.add(A, "同じ", "1784424000.000001", 0) is None
    assert buf.pending_utterances(A) == 1


def test_sweep_flushes_after_480_seconds_not_before() -> None:
    buf = VolatileUtteranceBuffer()
    _fill(buf, A, 2, now=100.0)
    _fill(buf, B, 1, start=10, now=200.0)
    assert buf.sweep(100.0 + 479.0) == []
    flushed = buf.sweep(100.0 + 480.0)
    assert [b.principal for b in flushed] == [A]
    assert buf.pending_utterances(B) == 1


def test_discard_drops_pending() -> None:
    buf = VolatileUtteranceBuffer()
    _fill(buf, A, 3)
    buf.discard(A)
    assert buf.pending_utterances(A) == 0
    assert buf.sweep(10_000.0) == []


def test_batch_repr_hides_utterances() -> None:
    buf = VolatileUtteranceBuffer()
    batch = _fill(buf, A, 5)
    assert batch is not None
    assert "発話" not in repr(batch)
    assert "a@vectorinc" not in repr(batch)


def test_principal_cap_refuses_new_people() -> None:
    buf = VolatileUtteranceBuffer(max_principals=1)
    _fill(buf, A, 1)
    assert buf.add(B, "x", "1784424000.000009", 0) is None
    assert buf.pending_utterances(B) == 0


# --- 日次枠・同時実行枠 ---------------------------------------------------------------------


def test_daily_quota_resets_at_jst_midnight() -> None:
    moment = [datetime(2026, 9, 25, 14, 59, tzinfo=UTC)]  # JST 23:59
    quota = DailyJobQuota(limit=6, now=lambda: moment[0])
    assert all(quota.try_reserve(A.key) for _ in range(6))
    assert not quota.try_reserve(A.key)  # 7 本目
    assert quota.try_reserve(B.key)  # 人ごと
    moment[0] += timedelta(minutes=1)  # JST 0:00
    assert quota.try_reserve(A.key)


def test_daily_quota_is_jst_not_utc() -> None:
    moment = [datetime(2026, 9, 25, 23, 30, tzinfo=UTC)]  # JST 9/26 8:30
    quota = DailyJobQuota(limit=1, now=lambda: moment[0])
    assert quota.try_reserve(A.key)
    moment[0] = datetime(2026, 9, 26, 0, 30, tzinfo=UTC)  # UTC は日付が変わるが JST は同じ日
    assert not quota.try_reserve(A.key)


@pytest.mark.parametrize(("raw", "expected"), [("", 1), ("2", 2), ("9", 2), ("0", 1), ("x", 1)])
def test_max_jobs_clamped(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv(pm_buffer.MAX_JOBS_ENV, raw)
    assert configured_max_jobs() == expected


def test_slots_do_not_wait_and_detect_double_release() -> None:
    slots = JobSlots(limit=1)
    assert slots.try_acquire()
    assert not slots.try_acquire()
    slots.release()
    with pytest.raises(ValueError):
        slots.release()


def test_sweeper_starts_once_and_swallows_errors() -> None:
    launched: list[str] = []

    def launcher(target: Callable[[], None], name: str) -> None:
        launched.append(name)

    def tick() -> None:
        raise RuntimeError("boom")

    sweeper = Sweeper(tick, thread_launcher=launcher)
    sweeper.ensure_started()
    sweeper.ensure_started()
    assert launched == ["personal-memory-sweeper"]
    sweeper.run_once()  # 例外を外へ出さない


# --- observe の受付（service） ----------------------------------------------------------------


def _observe(runtime: Any, p: Principal, i: int, text: str | None = None, **kw: Any) -> str:
    payload = ObserveInput(utterance=text or f"資料は短めが好き{i}", **kw)
    return runtime.observe(p, f"1784424000.{i:06d}", payload)["status"]


def test_observe_launches_job_on_fifth(active_runtime: Any) -> None:
    runtime, launcher = active_runtime
    assert [_observe(runtime, A, i) for i in range(4)] == ["buffered"] * 4
    assert _observe(runtime, A, 4) == "queued"
    assert launcher.pending == 1
    assert runtime.slots.in_use() == 1
    launcher.run_all()
    assert runtime.slots.in_use() == 0
    assert len(runtime.hermes.calls) == 1
    assert len(runtime.hermes.calls[0]["utterances"]) == 5


def test_seventh_job_of_the_day_is_dropped(active_runtime: Any) -> None:
    runtime, launcher = active_runtime
    for job in range(7):
        statuses = [_observe(runtime, A, job * 5 + i) for i in range(5)]
        launcher.run_all()
        expected = "queued" if job < 6 else "dropped"
        assert statuses[-1] == expected, job
    assert len(runtime.hermes.calls) == 6
    assert runtime.buffer.pending_utterances(A) == 0  # 捨てた発話は残らない


def test_busy_slots_drop_batch_without_consuming_quota(active_runtime: Any) -> None:
    runtime, launcher = active_runtime
    assert runtime.slots.try_acquire()  # ほかのジョブが枠を使っている
    statuses = [_observe(runtime, A, i) for i in range(5)]
    assert statuses[-1] == "dropped"
    assert launcher.pending == 0
    assert runtime.quota.used(A.key) == 0
    assert runtime.buffer.pending_utterances(A) == 0


@pytest.mark.parametrize(
    ("text", "kw"),
    [
        # 発話の URL は 2 個以上で落とす（1 個なら通し、メモ側の検査で URL を弾く・guard の仕様）
        ("https://example.com と https://example.org を見比べて", {}),
        ("> 転送された本文です", {}),
        ("```\ncode\n```", {}),
        ("添付を見て", {"has_attachment": True}),
    ],
)
def test_rejected_utterances_are_not_buffered(
    active_runtime: Any, text: str, kw: dict[str, Any]
) -> None:
    runtime, _ = active_runtime
    assert _observe(runtime, A, 0, text, **kw) == "dropped"
    assert runtime.buffer.pending_utterances(A) == 0
    assert runtime.store.loads == 0  # guard で落ちたら DB も読まない


def test_utterances_before_notice_are_dropped() -> None:
    store = FakeStore()
    store.create(A, noticed=False)
    runtime, _ = make_runtime(store=store)
    assert _observe(runtime, A, 0) == "dropped"
    assert runtime.buffer.pending_utterances(A) == 0
    store2 = FakeStore()  # profile すら無い
    runtime2, _ = make_runtime(store=store2)
    assert _observe(runtime2, A, 0) == "dropped"


def test_frozen_drops_and_freeze_discards_buffer(active_runtime: Any) -> None:
    runtime, launcher = active_runtime
    for i in range(3):
        _observe(runtime, A, i)
    runtime.command(A, CommandInput(action="freeze"))
    assert runtime.buffer.pending_utterances(A) == 0
    assert _observe(runtime, A, 10) == "dropped"
    assert launcher.pending == 0


def test_erase_confirm_discards_buffer(active_runtime: Any) -> None:
    runtime, _ = active_runtime
    for i in range(3):
        _observe(runtime, A, i)
    runtime.command(A, CommandInput(action="erase_request"))
    runtime.command(A, CommandInput(action="erase_confirm"))
    assert runtime.buffer.pending_utterances(A) == 0


def test_learner_disabled_without_client() -> None:
    store = FakeStore()
    store.create(A)
    runtime, _ = make_runtime(store=store, client=None)
    assert _observe(runtime, A, 0) == "dropped"
    assert runtime.buffer.pending_utterances(A) == 0


def test_sweep_tick_launches_timed_out_batches(active_runtime: Any) -> None:
    runtime, launcher = active_runtime
    runtime.clock = lambda: 0.0
    _observe(runtime, A, 0)
    runtime.clock = lambda: 480.0
    runtime.sweep_tick()
    assert launcher.pending == 1
    launcher.run_all()
    assert runtime.hermes.calls[0]["utterances"] == ["資料は短めが好き0"]


@pytest.fixture
def active_runtime() -> tuple[Any, ManualLauncher]:
    store = FakeStore()
    store.create(A)
    store.create(B)
    return make_runtime(store=store, client=HermesFake())
