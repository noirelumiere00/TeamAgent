"""Search admission survives independent parents, caller timeouts and stale CAS readers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.first_visit import sales25_analysis
from teamagent.skills.omiyage_report import sales25_wire
from teamagent.skills.omiyage_report.sales25 import StepResult, can_continue
from teamagent.skills.omiyage_report.sales25_wire import Wire


def case(now: float | None = None) -> dict[str, Any]:
    return {
        "owner_id": "T_SYNTH\x1fU_SYNTH",
        "job_id": "synthetic",
        "query": "合成ブランド",
        "started_at": datetime.fromtimestamp(now, UTC).isoformat()
        if now is not None
        else datetime.now(UTC).isoformat(),
    }


def completed() -> dict[str, Any]:
    return {
        "media_job_id": "mj_synthetic",
        "raw": {"fetched_at": datetime.now(UTC).isoformat()},
        "acquisition_status": {"accepted": True, "actual": 100},
    }


def test_two_global_slots_across_independent_wire_and_store_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks: dict[str, dict[str, Any]] = {}
    active = maximum = 0
    mutex = threading.Lock()
    two_started, waiting, release = threading.Event(), threading.Event(), threading.Event()
    original_sleep = time.sleep

    def sleep(seconds: float) -> None:
        assert 0 < seconds <= 1
        waiting.set()
        original_sleep(0.01)

    def search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal active, maximum
        with mutex:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_started.set()
        assert release.wait(5)
        with mutex:
            active -= 1
        return completed()

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(Wire, "_save_json", lambda *args: {})
    wires = [
        Wire(
            cast(MediaJobClient, SimpleNamespace(search_tiktok_sales25=search)),
            store=ProposalJobStore(table_name="", dedup_memory=locks),
        )
        for _ in range(3)
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = [
            pool.submit(w.search, case(), {}, f"job-{i}:search_1") for i, w in enumerate(wires[:2])
        ]
        assert two_started.wait(5)
        third = pool.submit(wires[2].search, case(), {}, "job-2:search_1")
        try:
            assert waiting.wait(5)
            assert not third.done()
        finally:
            release.set()
        assert all(future.result().status == "complete" for future in [*first, third])
    assert maximum == 2
    assert all(row["target_job_id"] == "" for row in locks.values())


@pytest.mark.parametrize("contended", [False, True])
def test_expired_slot_steal_and_release_are_compare_and_swap(
    monkeypatch: pytest.MonkeyPatch, contended: bool
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    store = ProposalJobStore(table_name="", memory={})
    first, second = sales25_wire._SEARCH_SLOT_NAMES
    stale = f"{int(now) - 1}:stale"
    assert store.put_dedup_lock(first, stale, expected_target=None)
    put = store.put_dedup_lock
    winner = f"{int(now) + 600}:winner"

    def compete(name: str, target: str, *, expected_target: str | None) -> bool:
        if contended and name == first and expected_target == stale:
            assert put(name, winner, expected_target=stale)
        return put(name, target, expected_target=expected_target)

    monkeypatch.setattr(store, "put_dedup_lock", compete)
    claimed, lease, timeout = Wire(store=store)._claim_search_slot(case(now))
    assert claimed == (second if contended else first)
    assert timeout == 780
    assert int(lease.split(":")[0]) == int(now) + 840
    assert not put(first, "", expected_target=stale)
    if contended:
        row = store.get_dedup_lock(first)
        assert row is not None and row["target_job_id"] == winner
    assert put(claimed, "", expected_target=lease)


@pytest.mark.parametrize("error", ["MEDIA_JOB_TIMEOUT", "MEDIA_JOB_DEADLINE_EXCEEDED"])
def test_caller_timeout_holds_slots_through_child_deadline_and_reserve(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    now = [1_800_000_000.0]
    started = now[0]
    sleeps: list[float] = []
    monkeypatch.setattr(time, "time", lambda: now[0])

    def sleep(seconds: float) -> None:
        assert 0 < seconds <= 1
        sleeps.append(seconds)
        now[0] += seconds

    def search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise MediaJobError(error)

    monkeypatch.setattr(time, "sleep", sleep)
    locks: dict[str, dict[str, Any]] = {}
    wires = [
        Wire(
            cast(MediaJobClient, SimpleNamespace(search_tiktok_sales25=search)),
            store=ProposalJobStore(table_name="", dedup_memory=locks),
        )
        for _ in range(3)
    ]
    for index, wire in enumerate(wires):
        result = wire.search(case(started), {}, f"job-{index}:search_1")
        assert result.status == "timeout" and result.reason == "timeout"
        if index < 2:
            assert now[0] == started
    assert sum(sleeps) == 840
    assert all(row["target_job_id"] for row in locks.values())


def test_slot_wait_is_bounded_by_parent_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1_800_000_000.0]
    monkeypatch.setenv("OMIYAGE_SALES25_PARENT_SECONDS", "300")
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    store = ProposalJobStore(table_name="", memory={})
    for name in sales25_wire._SEARCH_SLOT_NAMES:
        assert store.put_dedup_lock(name, f"{int(now[0]) + 1000}:occupied", expected_target=None)
    with pytest.raises(TimeoutError, match="parent deadline"):
        Wire(store=store)._claim_search_slot(case(now[0] - 299))


def test_uncertain_transport_failure_keeps_the_child_slot() -> None:
    def search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("synthetic transport disconnect after submission")

    store = ProposalJobStore(table_name="", memory={})
    wire = Wire(cast(MediaJobClient, SimpleNamespace(search_tiktok_sales25=search)), store=store)
    with pytest.raises(ConnectionError, match="disconnect"):
        wire.search(case(), {}, "job:search_1")
    row = store.get_dedup_lock(sales25_wire._SEARCH_SLOT_NAMES[0])
    assert row is not None and row["target_job_id"]


def test_cumulative_media_size_stops_before_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sales25_wire, "MAX_INPUT_BYTES", 1000)
    monkeypatch.setattr(sales25_wire, "_SNAPSHOT_METADATA_RESERVE", 100)
    monkeypatch.setattr(
        sales25_analysis, "selected_posts", lambda *args: [{"id": "1"}, {"id": "2"}]
    )
    monkeypatch.setattr(sales25_analysis, "media_operation", lambda post: post)
    monkeypatch.setattr(
        sales25_analysis,
        "validate_media",
        lambda *args: ({"assets": [{"bytes": 450}]}, {}),
    )
    saved: dict[str, Any] = {}
    acquired: list[str] = []

    def acquire(post: dict[str, Any], **kwargs: Any) -> dict[str, bytes]:
        acquired.append(post["id"])
        return {"media-manifest.json": b"{}", "media-bundle.zip": b"synthetic"}

    def save(case: Any, name: str, data: Any) -> dict[str, Any]:
        saved[name] = data
        return {"name": name}

    wire = Wire(
        cast(MediaJobClient, SimpleNamespace(acquire_sales25_media=acquire)),
        store=ProposalJobStore(table_name="", memory={}),
    )
    monkeypatch.setattr(wire, "_json", lambda *args: {})
    monkeypatch.setattr(wire, "_find_json", lambda *args: None)
    monkeypatch.setattr(wire, "_save", lambda *args: {})
    monkeypatch.setattr(wire, "_save_json", save)
    monkeypatch.setattr(wire, "_download", lambda *args: b"{}")
    monkeypatch.setattr(wire, "_review_name", lambda *args: "review.json")
    monkeypatch.setattr(sales25_analysis, "_review", lambda *args, **kwargs: {})
    result = wire.media(
        case(), {"relevance": StepResult(status="complete", refs=[{}])}, "job:media"
    )
    assert sorted(acquired) == ["1", "2"]
    assert result.status == "incomplete" and result.reason == "media_missing"
    assert not can_continue("media", result)
    assert saved["media-size-limit.json"]["reason"] == "snapshot_size_limit"
    assert saved["media-size-limit.json"]["bytes"] > 900
    assert "media.json" not in saved
