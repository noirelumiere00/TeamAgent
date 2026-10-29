"""Saved 1084-post acquisition, named KVS pagination, and paid concurrency boundaries."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scripts.ingest_media_cases import main
from teamagent.adapters.apify_client import ApifyClient
from teamagent.adapters.cost_guard import CostLimitExceededError
from teamagent.skills.media_case_search.skill import accounts
from tests.adapters.test_apify_media_cases import png
from tests.skills.media_case_search.test_media_cases import (
    HANDLE,
    FakeGemini,
    MemoryStore,
    make_skill,
    raw_post,
)

DATASET = "p7MhpFwAJg1pF0M8g"
KVS = "aico-media-cases-newstv10-20261028"


class Snapshot:
    def __init__(self):
        self.posts = []
        self.calls = []
        self.observed = datetime.now(UTC).isoformat()
        for i in range(1084):
            handle = list(accounts())[i % 10]
            row = raw_post(i, text="国内旅行キャンペーン", isSponsored=False)
            row["webVideoUrl"] = row["webVideoUrl"].replace(HANDLE, handle)
            self.posts.append(row)
        self.keys = sorted(f"cover-{row['id']}.png" for row in self.posts[1:])

    def handler(self, request):
        assert request.method == "GET" and request.url.host == "api.apify.com"
        path, params = request.url.path, request.url.params
        self.calls.append((path, dict(params)))
        if path == f"/v2/datasets/{DATASET}":
            return httpx.Response(
                200, json={"data": {"itemCount": len(self.posts), "modifiedAt": self.observed}}
            )
        if path == f"/v2/datasets/{DATASET}/items":
            offset, limit = int(params["offset"]), int(params["limit"])
            return httpx.Response(200, json=self.posts[offset : offset + limit])
        if path == "/v2/key-value-stores":
            return httpx.Response(200, json={"data": {"items": [{"id": "savedkvs", "name": KVS}]}})
        if path == "/v2/key-value-stores/savedkvs/keys":
            offset = (
                self.keys.index(params["exclusiveStartKey"]) + 1
                if "exclusiveStartKey" in params
                else 0
            )
            page = self.keys[offset : offset + int(params["limit"])]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [{"key": key} for key in page],
                        "isTruncated": offset + len(page) < len(self.keys),
                        "nextExclusiveStartKey": page[-1],
                    }
                },
            )
        if path.startswith("/v2/key-value-stores/savedkvs/records/"):
            return httpx.Response(200, content=png())
        raise AssertionError(path)


def test_cli_imports_1084_saved_posts_over_100_per_account_without_actor(ledger, capsys):
    snapshot = Snapshot()
    model = FakeGemini()
    store = MemoryStore()
    store.states.clear()
    skill = make_skill(ledger, store=store, model=model)
    skill._apify = ApifyClient(
        "fake", http=httpx.Client(transport=httpx.MockTransport(snapshot.handler))
    )
    assert main(["--dataset", DATASET, "--kvs", KVS], skill=skill) == 0
    assert not snapshot.calls
    preview = json.loads(capsys.readouterr().out)
    assert preview["max_parallel_posts_per_account"] == 6
    assert preview["max_parallel_gemini_calls"] == 12
    assert preview["gemini_monthly_usd"] == "15"
    assert main(["--dataset", DATASET, "--kvs", KVS, "--commit"], skill=skill) == 0
    assert len(store.posts) == 1084 and len(model.calls) == 1084
    assert all(store.state(h)["initialized"] for h in accounts())
    assert all(100 < store.state(h)["result_count"] < 400 for h in accounts())
    assert store.post(snapshot.posts[0]["id"])["image_count"] == 0
    assert store.post(snapshot.posts[-1]["id"])["image_count"] == 1
    assert {p["metrics_observed_at"] for p in store.posts.values()} == {snapshot.observed}
    offsets = [params["offset"] for path, params in snapshot.calls if path.endswith("/items")]
    assert offsets == ["0", "1000"]
    assert any("exclusiveStartKey" in params for _, params in snapshot.calls)
    assert all(not key.startswith("apify#") for key in ledger._ddb.rows)
    newer = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    first = store.posts[snapshot.posts[0]["id"]]
    first["metrics_observed_at"] = newer
    first["metrics"]["playCount"] = 9999
    store.states[HANDLE]["watermark"] = newer
    store.states[HANDLE]["last_success"] = newer
    # Re-reading an older acquisition neither charges the model nor rewinds newer metrics.
    assert main(["--dataset", DATASET, "--kvs", KVS, "--commit"], skill=skill) == 0
    assert len(model.calls) == 1084 and len(store.posts) == 1084
    assert store.post(snapshot.posts[0]["id"])["metrics"]["playCount"] == 9999
    assert store.state(HANDLE)["watermark"] == newer
    assert store.state(HANDLE)["last_success"] == newer
    output = capsys.readouterr()
    assert '"posts": 1084' in output.out
    assert "処理済み=1084/1084 失敗=0 経過秒=" in output.err


def test_cli_rejects_over_400_and_unapproved_posts_before_model_cost(ledger):
    snapshot = Snapshot()
    snapshot.posts = [raw_post(i, text="国内旅行キャンペーン") for i in range(401)]
    model = FakeGemini()
    skill = make_skill(ledger, model=model)
    skill._apify = ApifyClient(
        "fake", http=httpx.Client(transport=httpx.MockTransport(snapshot.handler))
    )
    assert main(["--dataset", DATASET, "--accounts", HANDLE, "--commit"], skill=skill) == 1
    assert not model.calls and not skill.store.posts
    snapshot.posts = [raw_post(webVideoUrl="https://evil.example/video/1")]
    assert main(["--dataset", DATASET, "--accounts", HANDLE, "--commit"], skill=skill) == 1
    assert not model.calls


def test_saved_import_never_reconciles_runs_with_an_actor_start(ledger):
    skill = make_skill(ledger)
    skill.store.states[HANDLE] = {"intent": True}
    with pytest.raises(RuntimeError, match="reconcile"):
        skill.ingest_initial([HANDLE], dataset_id=DATASET, kvs=KVS)


def test_three_accounts_in_parallel_each_reserves_and_other_coordinator_is_blocked(ledger):
    ready, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    original_update = ledger._ddb.update_item

    def atomic_update(**kwargs):
        with lock:
            return original_update(**kwargs)

    ledger._ddb.update_item = atomic_update
    active, peak = 0, 0
    starts = []

    def handler(request):
        nonlocal active, peak
        path = request.url.path
        if request.method == "POST" and "/acts/" in path:
            with lock:
                assert sum(row["cost_micro"] for row in ledger._ddb.rows.values()) >= 1201000
                starts.append(json.loads(request.content))
                run_id = str(len(starts))
                active += 1
                peak = max(peak, active)
                if active == 3:
                    ready.set()
            return httpx.Response(201, json={"data": {"id": run_id, "status": "READY"}})
        if "/actor-runs/" in path:
            assert release.wait(10)
            with lock:
                active -= 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": path.rsplit("/", 1)[1],
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "ds",
                        "defaultKeyValueStoreId": "",
                    }
                },
            )
        if "/datasets/" in path:
            return httpx.Response(200, json=[])
        raise AssertionError(path)

    store = MemoryStore()
    store.states.clear()
    skill = make_skill(ledger, store=store)
    skill._apify = ApifyClient(
        "fake",
        ledger=ledger,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_s=0,
    )
    errors = []

    def run():
        try:
            skill.ingest_initial(list(accounts()))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(10)
        assert len(starts) == 3 and peak == 3
        with pytest.raises(RuntimeError, match="another media ingestion"):
            make_skill(ledger, store=store).ingest_initial([HANDLE])
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    assert len(starts) == 10 and peak == 3 and active == 0
    assert all(row["resultsPerPage"] == 400 for row in starts)
    assert next(iter(ledger._ddb.rows.values()))["cost_micro"] == 10000  # ten empty starts


def test_one_run_reservation_is_refused_before_actor_start(ledger, monkeypatch):
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "1.200")
    skill = make_skill(ledger)
    with pytest.raises(CostLimitExceededError):
        skill.ingest_initial([HANDLE])
    assert not skill.store.state(HANDLE)["intent"]
