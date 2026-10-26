"""Delayed offline work proves bounded overlap, stable identities and resumable evidence."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.omiyage_report import sales25
from teamagent.skills.omiyage_report.sales25_wire import Wire, _Model, production_stages
from tests.skills.first_visit.test_sales25_analysis import reply
from tests.skills.omiyage_report.test_sales25_parent import (
    Pipeline,
    ctx,
    flags,
    request,
    result,
)
from tests.skills.omiyage_report.test_sales25_wire import Transport, build, model

__all__ = ["flags"]


def test_delayed_search_is_faster_bounded_and_pipelines_before_last_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def trial(concurrency: int) -> float:
        monkeypatch.setenv("OMIYAGE_SALES25_SEARCH_CONCURRENCY", str(concurrency))
        store = ProposalJobStore(table_name="", memory={})
        pipeline = Pipeline()
        active = maximum = completed = 0
        mutex = threading.Lock()
        early = threading.Event()

        def search(case: Any, previous: Any, key: str) -> sales25.StepResult:
            nonlocal active, maximum, completed
            with mutex:
                active += 1
                maximum = max(maximum, active)
            # Different delays force completion to differ from query order.
            time.sleep(0.08 if key.endswith("1") else 0.06)
            outcome = pipeline.step(case, previous, key)
            with mutex:
                active -= 1
                completed += 1
            return outcome

        def prefetch(case: Any, previous: Any, key: str) -> sales25.StepResult:
            if key.endswith("1"):
                with mutex:
                    assert completed < 6
                early.set()
            return sales25.StepResult(status="complete")

        started = time.perf_counter()
        accepted = build(
            store, sales25_stages=replace(pipeline.stages(), search=search, prefetch=prefetch)
        ).run(request(), ctx())
        duration = time.perf_counter() - started
        saved = result(store, accepted.job_id)
        assert saved.status == "ready" and early.is_set()
        assert maximum == concurrency
        for index in range(1, 7):
            assert saved.checkpoints[f"search_{index}"].refs == [
                {"raw": f"{accepted.job_id}:search_{index}"}
            ]
        return duration

    elapsed = [trial(concurrency) for concurrency in (1, 2)]
    assert elapsed[1] < elapsed[0] * 0.85
    print(f"search serial={elapsed[0]:.3f}s parallel={elapsed[1]:.3f}s")


def test_post_pipeline_overlap_progress_and_restart_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ProposalJobStore(table_name="", memory={})
    client = Transport()
    approved = model()
    acquire = client.acquire_sales25_media
    batch = approved.analyze_images_bytes.side_effect
    mutex = threading.Lock()
    acquiring = reviewing = maximum = 0
    overlap = threading.Event()
    second_acquiring = threading.Event()
    first_reviewing = threading.Event()
    progress: list[tuple[int, int]] = []
    calls = 0

    def slow_acquire(*args: Any, **kwargs: Any) -> Any:
        nonlocal acquiring, maximum, calls
        with mutex:
            acquiring += 1
            calls += 1
            index = calls
            maximum = max(maximum, acquiring + reviewing)
        if index == 1:
            assert second_acquiring.wait(5)
        elif index == 2:
            second_acquiring.set()
            assert first_reviewing.wait(5)
        time.sleep(0.005)
        value = acquire(*args, **kwargs)
        with mutex:
            acquiring -= 1
        return value

    def slow_review(*args: Any, **kwargs: Any) -> Any:
        nonlocal reviewing, maximum
        with mutex:
            reviewing += 1
            maximum = max(maximum, acquiring + reviewing)
            if acquiring:
                overlap.set()
            first_reviewing.set()
        time.sleep(0.02)
        value = batch(*args, **kwargs)
        with mutex:
            reviewing -= 1
        return value

    monkeypatch.setattr(client, "acquire_sales25_media", slow_acquire)
    approved.analyze_images_bytes.side_effect = slow_review
    wire = Wire(client, lambda: approved, store=store)
    media = wire.media

    def record_progress(case: Any, previous: Any, key: str) -> sales25.StepResult:
        callback = case["_progress"]

        def update(done: int, total: int) -> None:
            callback(done, total)
            progress.append((done, total))
            row = store.get_job(case["job_id"])
            assert row is not None
            checkpoint = json.loads(row["checkpoint_json"])
            assert checkpoint["completed"] == done
            assert (
                f"{done}/{total}"
                in sales25.Parent(store).status(case["job_id"], row, ctx()).message
            )

        return media({**case, "_progress": update}, previous, key)

    stages = replace(production_stages(wire), prefetch=None, media=record_progress)
    accepted = build(store, sales25_stages=stages).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.delivery_status == "pending", saved.model_dump_json()
    assert calls == 30 and maximum == 2 and overlap.is_set()
    assert progress == [(i, 30) for i in range(1, 31)]
    assert approved.analyze_images_bytes.call_count == 30
    approved.analyze_image_bytes.assert_not_called()
    # Re-enter the acquisition stage with a new adapter and identical owner/job.
    case = {
        **json.loads(store.get_job(accepted.job_id)["request_summary"])["case"],
        "owner_id": json.loads(store.get_job(accepted.job_id)["request_summary"])["owner"],
        "job_id": accepted.job_id,
        "started_at": datetime.now(UTC).isoformat(),
    }
    resumed = Wire(client, lambda: approved, store=store)
    before = approved.analyze_images_bytes.call_count
    assert resumed.media(case, saved.checkpoints, accepted.job_id + ":media").actual == 30
    assert calls == 30 and approved.analyze_images_bytes.call_count == before


def test_model_call_budget_is_atomic_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMIYAGE_SALES25_MODEL_CALLS_PER_STAGE", "1")
    client = model()
    wire = Wire(Transport(), lambda: client)
    case = {
        "owner_id": "synthetic-owner",
        "job_id": "synthetic-budget",
        "started_at": datetime.now(UTC).isoformat(),
    }
    budget = _Model(wire, case, "analysis")

    def invoke(index: int) -> str:
        try:
            budget.analyze_image_bytes(b"synthetic", "image/jpeg", "Observe", str(index))
            return "ok"
        except TimeoutError:
            return "limited"

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(invoke, range(4))) == ["limited", "limited", "limited", "ok"]
    client.analyze_image_bytes.assert_called_once()


@pytest.mark.parametrize(
    "name,value",
    [
        ("OMIYAGE_SALES25_SEARCH_CONCURRENCY", "3"),
        ("OMIYAGE_SALES25_SEARCH_CONCURRENCY", "0"),
        ("OMIYAGE_SALES25_POST_CONCURRENCY", "5"),
        ("OMIYAGE_SALES25_POST_CONCURRENCY", "no"),
    ],
)
def test_invalid_concurrency_fails_closed(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        sales25.search_concurrency() if "SEARCH" in name else Wire(Transport())


def test_batch_adapter_retains_order_bytes_and_bounds() -> None:
    from unittest.mock import MagicMock

    client = GeminiClient(api_key="offline")
    client._generate_video = MagicMock(return_value=reply({}))  # type: ignore[method-assign]
    client.analyze_images_bytes([(b"one", "image/jpeg"), (b"two", "image/png")], "observe", "id")
    parts = client._generate_video.call_args.args[0]
    assert [part.inline_data.data for part in parts[:2]] == [b"one", b"two"]
    assert client._generate_video.call_args.kwargs["media_resolution"] == "high"
    for images in ([], [(b"x", "text/plain")], [(b"x", "image/jpeg")] * 5):
        with pytest.raises(ValueError):
            client.analyze_images_bytes(images, "observe", "id")


def test_concurrent_same_id_is_acquired_once_and_mismatch_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.skills.first_visit import sales25_analysis as analysis
    from tests.skills.first_visit.sales25_fixture import inputs

    client = Transport()
    approved = model()
    acquire = client.acquire_sales25_media
    count = 0

    def slow_acquire(*args: Any, **kwargs: Any) -> Any:
        nonlocal count
        count += 1
        time.sleep(0.02)
        return acquire(*args, **kwargs)

    monkeypatch.setattr(client, "acquire_sales25_media", slow_acquire)
    wire = Wire(client, lambda: approved)
    case = {
        "owner_id": "synthetic-owner",
        "job_id": "synthetic-dedup",
        "started_at": datetime.now(UTC).isoformat(),
    }
    post = inputs()[1]["q0"]["videos"][0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: wire._acquire_post(case, post), range(4)))
    assert count == 1 and all(record == records[0] for record in records)
    # Reusing the ID cannot bypass the manifest URL check before model invocation.
    read_json = wire._json
    monkeypatch.setattr(wire, "_json", lambda case, ref: {} if ref == {} else read_json(case, ref))
    monkeypatch.setattr(
        analysis,
        "selected_posts",
        lambda *args: [{**post, "url": post["url"].replace("@synthetic", "@different")}],
    )
    with pytest.raises(ValueError, match="media identity/status mismatch"):
        wire.media(
            case, {"relevance": sales25.StepResult(status="complete", refs=[{}])}, "job:media"
        )
    approved.analyze_images_bytes.assert_not_called()
