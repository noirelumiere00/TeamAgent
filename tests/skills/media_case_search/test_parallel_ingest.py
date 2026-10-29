"""Exercise the real scheduler, reservations, and Gemini retry boundary without I/O."""

from __future__ import annotations

import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from scripts.ingest_media_cases import report_progress
from teamagent.adapters.apify_client import ApifyClient
from teamagent.adapters.cost_guard import CostLimitExceededError
from teamagent.adapters.gemini_client import GeminiClient
from teamagent.adapters.retry import call_with_retry
from teamagent.skills.media_case_search.distill import CaseDistiller
from teamagent.skills.media_case_search.skill import IngestProgress, accounts
from tests.adapters.test_apify_tiktok_video import _FakeTikTokApify
from tests.skills.media_case_search.test_cli import DATASET, KVS, Snapshot
from tests.skills.media_case_search.test_media_cases import (
    HANDLE,
    FakeGemini,
    MemoryStore,
    extraction,
    make_skill,
    raw_post,
)


def snapshot_skill(ledger, model, handles, *, store=None):
    snapshot = Snapshot()
    snapshot.posts = []
    for index, handle in enumerate(handles):
        for number in range(8):
            row = raw_post(index * 100 + number, text=f"国内旅行キャンペーン {index} {number}")
            row["webVideoUrl"] = row["webVideoUrl"].replace(HANDLE, handle)
            snapshot.posts.append(row)
    skill = make_skill(ledger, model=model, store=store)
    skill._apify = ApifyClient(
        "fake", http=httpx.Client(transport=httpx.MockTransport(snapshot.handler))
    )
    return skill, snapshot


def test_dataset_parallel_limits_and_coordinator_only_post_writes(ledger):
    reached, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = Counter()
    peaks = [0, 0, 0]

    class Model(FakeGemini):
        def _reply(self, prompt, images, cap):
            account = json.loads(prompt.split("投稿データ: ")[1])["caption"].split()[-2]
            with lock:
                active[account] += 1
                peaks[0] = max(peaks[0], sum(active.values()))
                peaks[1] = max(peaks[1], max(active.values()))
                peaks[2] = max(peaks[2], sum(n > 0 for n in active.values()))
                if peaks[0] == 12:
                    reached.set()
            try:
                assert release.wait(10)
                return SimpleNamespace(text=json.dumps(extraction()), cost_usd=0.0001)
            finally:
                with lock:
                    active[account] -= 1

    class Store(MemoryStore):
        def save_post(self, metadata, text, request_id):
            assert threading.current_thread().name.startswith("media-profile")
            super().save_post(metadata, text, request_id)

    handles = list(accounts())[:4]
    skill, _ = snapshot_skill(ledger, Model(), handles, store=Store())
    progress = IngestProgress()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            skill.ingest_initial, handles, dataset_id=DATASET, kvs=KVS, progress=progress
        )
        try:
            assert reached.wait(10)
            assert progress.snapshot() == (0, 32, 0)
        finally:
            release.set()
        assert future.result(timeout=15)["failed"] == 0
    assert peaks[0] == 12 and peaks[1] <= 6 and peaks[2] <= 3
    assert progress.snapshot() == (32, 32, 0)
    assert len(skill.store.posts) == 32


def test_parallel_reservations_never_exceed_monthly_cap(ledger, monkeypatch):
    monkeypatch.setenv("COST_MEDIA_CASE_GEMINI_MONTHLY_USD", "0.045")
    release, denied_all = threading.Event(), threading.Event()
    barrier = threading.Barrier(20)
    lock = threading.Lock()
    calls = denied = peak = 0
    original = ledger._ddb.update_item

    def update(**kwargs):
        nonlocal peak
        with lock:
            result = original(**kwargs)
            peak = max(peak, sum(row["cost_micro"] for row in ledger._ddb.rows.values()))
            return result

    ledger._ddb.update_item = update

    class Model(FakeGemini):
        def _reply(self, prompt, images, cap):
            nonlocal calls
            with lock:
                calls += 1
                assert sum(row["cost_micro"] for row in ledger._ddb.rows.values()) >= 15000
            assert release.wait(10)
            return SimpleNamespace(text=json.dumps(extraction()), cost_usd=0.015)

    distiller = CaseDistiller(Model(), ledger)

    def run():
        nonlocal denied
        barrier.wait(timeout=10)
        try:
            distiller._call("data", [], "budget-test")
        except CostLimitExceededError:
            with lock:
                denied += 1
                if denied == 17:
                    denied_all.set()

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(run) for _ in range(20)]
        try:
            assert denied_all.wait(10)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=10)
    assert calls == 3 and denied == 17 and peak == 45000
    assert next(iter(ledger._ddb.rows.values()))["cost_micro"] == 45000


def test_budget_stop_drains_and_saves_inflight_posts(ledger, monkeypatch):
    monkeypatch.setenv("COST_MEDIA_CASE_GEMINI_MONTHLY_USD", "0.045")

    class Model(FakeGemini):
        def _reply(self, prompt, images, cap):
            return SimpleNamespace(text=json.dumps(extraction()), cost_usd=0.015)

    skill, _ = snapshot_skill(ledger, Model(), [HANDLE])
    progress = IngestProgress()
    with pytest.raises(CostLimitExceededError):
        skill.ingest_initial([HANDLE], dataset_id=DATASET, kvs=KVS, progress=progress)
    failed = skill.store.state(HANDLE)["failed_ids"]
    assert len(skill.store.posts) == 3 and failed
    assert not skill.store.state(HANDLE).get("initialized")
    assert progress.snapshot() == (3 + len(failed), 8, len(failed))
    assert skill.store.lock.acquire(blocking=False)
    skill.store.lock.release()


def test_429_backoff_reserves_every_retry_and_keeps_failed_post(ledger, monkeypatch):
    monkeypatch.setenv("GEMINI_RATE_LIMIT_RETRY_MAX_ATTEMPTS", "3")
    waits = []

    def retry(fn, **kwargs):
        return call_with_retry(fn, **kwargs, sleep=waits.append, jitter=lambda cap: cap)

    monkeypatch.setattr("teamagent.skills.media_case_search.distill.call_with_retry", retry)
    calls = Counter()
    lock = threading.Lock()

    class LimitedError(Exception):
        code = 429

    class Models:
        def count_tokens(self, **kwargs):
            return SimpleNamespace(total_tokens=1000)

        def generate_content(self, **kwargs):
            prompt = next(p.text for p in kwargs["contents"][0].parts if p.text)
            number = json.loads(prompt.split("投稿データ: ")[1])["caption"].split()[-1]
            with lock:
                calls[number] += 1
                attempt = calls[number]
            if number == "1" or (number == "2" and attempt == 1):
                raise LimitedError("RESOURCE_EXHAUSTED")
            return SimpleNamespace(text=json.dumps(extraction()), usage_metadata=None)

    model = GeminiClient(api_key="fake", client=SimpleNamespace(models=Models()))
    skill, snapshot = snapshot_skill(ledger, model, [HANDLE])
    progress = IngestProgress()
    result = skill.ingest_initial([HANDLE], dataset_id=DATASET, kvs=KVS, progress=progress)
    assert result["failed"] == 1 and len(skill.store.posts) == 7
    assert skill.store.state(HANDLE)["failed_ids"] == [snapshot.posts[1]["id"]]
    assert calls["1"] == 3 and calls["2"] == 2 and sum(calls.values()) == 11
    assert sorted(waits) == [1.0, 1.0, 2.0]
    assert next(iter(ledger._ddb.rows.values()))["units"] == 11
    assert progress.snapshot() == (8, 8, 1)


def test_progress_reports_while_waiting_and_on_failure(capsys):
    progress = IngestProgress(total=8)
    printed = threading.Event()
    original = progress.snapshot

    def snapshot():
        printed.set()
        return original()

    progress.snapshot = snapshot
    with pytest.raises(RuntimeError), report_progress(progress, interval_s=0.01):
        assert printed.wait(5)
        progress.add(failed=True)
        raise RuntimeError("stop")
    lines = capsys.readouterr().err.splitlines()
    assert "処理済み=0/8 失敗=0 経過秒=" in lines[0]
    assert "処理済み=1/8 失敗=1 経過秒=" in lines[-1]


def test_fetch_progress_counts_unique_posts(ledger):
    rows = [raw_post(1), raw_post(1), raw_post(2)]
    skill = make_skill(ledger, _FakeTikTokApify({}, rows))
    progress = IngestProgress()
    skill.ingest_initial([HANDLE], progress=progress)
    assert progress.snapshot() == (2, 2, 0)
    assert len(skill.store.posts) == 2
