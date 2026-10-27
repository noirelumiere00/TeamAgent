"""Existing TikTok actor with fake HTTP and real CostGuard over fake DynamoDB."""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.apify_client import ACTOR_TIKTOK_VIDEO, ApifyError
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from tests.adapters.test_apify_tiktok_video import _FakeTikTokApify
from tests.adapters.test_cost_guard import _FakeDdb


def test_search_uses_existing_actor_reserves_and_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "1")
    monkeypatch.delenv("COST_APIFY_PER_CALL_USD", raising=False)
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    items = [{"id": "9000000002"}, {"id": "9000000001"}]
    fake = _FakeTikTokApify({}, items=items)
    ddb = _FakeDdb()
    run = fake.client(CostGuard("test", ddb=ddb)).tiktok_search("合成 検索", max_items=100)
    assert run.items == items and run.actor_id == ACTOR_TIKTOK_VIDEO
    assert fake.post_bodies == [
        {
            "searchQueries": ["合成 検索"],
            "searchSection": "",
            "resultsPerPage": 100,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSlideshowImages": False,
        }
    ]
    assert next(iter(ddb.rows.values()))["cost_micro"] == 8000
    assert not any("/key-value-stores/" in call for call in fake.calls)


@pytest.mark.parametrize("monthly,ddb_failure", [("0.1", False), ("1", True), ("", False)])
def test_budget_or_ledger_unavailable_never_starts_actor(
    monkeypatch: pytest.MonkeyPatch, monthly: str, ddb_failure: bool
) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", monthly)
    monkeypatch.delenv("COST_APIFY_PER_CALL_USD", raising=False)
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    fake = _FakeTikTokApify({})
    with pytest.raises(CostLimitExceededError):
        fake.client(CostGuard("test", ddb=_FakeDdb(fail=ddb_failure))).tiktok_search("合成")
    assert fake.calls == []


@pytest.mark.parametrize(
    "kwargs", [{"query": ""}, {"query": "q", "max_items": 0}, {"query": "q", "max_items": 101}]
)
def test_invalid_search_never_starts_actor(kwargs: dict[str, Any]) -> None:
    fake = _FakeTikTokApify({})
    with pytest.raises(ValueError):
        fake.client().tiktok_search(**kwargs)
    assert fake.calls == []


def test_search_deadline_aborts_and_keeps_conservative_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "1")
    monkeypatch.delenv("COST_APIFY_PER_CALL_USD", raising=False)
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    fake = _FakeTikTokApify({})
    fake.final_status = "RUNNING"
    ddb = _FakeDdb()
    with pytest.raises(ApifyError, match="APIFY_TIMEOUT"):
        fake.client(CostGuard("test", ddb=ddb)).tiktok_search("合成", deadline_s=0.01)
    assert "POST /v2/actor-runs/run-1/abort" in fake.calls
    assert next(iter(ddb.rows.values()))["cost_micro"] == 400000
