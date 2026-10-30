"""sales25 search input, retry budget and incomplete DM regressions; all services are fake."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from teamagent.adapters import apify_client, tiktok_search_fallback
from teamagent.adapters.apify_client import ACTOR_TIKTOK_VIDEO, ApifyClient, ApifyRunResult
from teamagent.adapters.cost_guard import CostGuard
from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_search_fallback import apply_sales25_fallback
from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.mcp_gateway import detached_jobs
from teamagent.mcp_gateway.detached_jobs import post_to_origin_status
from teamagent.skills._shared.long_jobs import ORIGIN_KEY
from teamagent.skills.omiyage_report import sales25
from teamagent.skills.omiyage_report.skill import OmiyageReportStatusSkill
from tests.adapters.test_apify_client import _FakeApify, _ReservationLedger
from tests.adapters.test_apify_tiktok_video import _FakeTikTokApify
from tests.skills.omiyage_report.test_sales25_apify import QUERIES, Worker, actor_items, apify
from tests.skills.omiyage_report.test_sales25_parent import (
    Pipeline,
    build,
    ctx,
    flags,
    request,
    result,
)

__all__ = ["apify", "flags"]


def test_actor_input_requests_fifty_results_without_downloads() -> None:
    fake = _FakeTikTokApify({}, items=[{"id": "9000000001"}])
    ledger = _ReservationLedger()
    ledger.token = SimpleNamespace(global_reserved=True)
    fake.client(ledger).tiktok_search("合成 検索", max_items=50)
    assert fake.post_bodies == [
        {
            "searchQueries": ["合成 検索"],
            "searchSection": "",
            "resultsPerPage": 50,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSlideshowImages": False,
        }
    ]
    assert ledger.reserved == [0.2]


def test_empty_retry_keeps_first_thirteen_rows(apify: Mock) -> None:
    client = Worker({0: "TIKTOK_BOT_WALL"})
    original = client.search_tiktok_sales25(
        QUERIES[0], owner_id="synthetic", request_fingerprint="empty-retry"
    )
    apify.tiktok_search.side_effect = [
        ApifyRunResult(actor_items(0, 13), ACTOR_TIKTOK_VIDEO, "first", "SUCCEEDED", 0.052),
        ApifyRunResult([], ACTOR_TIKTOK_VIDEO, "retry", "SUCCEEDED", 0),
    ]
    recovered = apply_sales25_fallback(
        original, media_client=client, deadline_s=240, request_id="synthetic"
    )
    assert apify.tiktok_search.call_count == 2
    assert recovered["acquisition_status"]["actual"] == len(recovered["raw"]["videos"]) == 13
    assert "APIFY_RETRY_FAILED:" in recovered["apify_retry_warning"]


def test_shortfall_retry_shares_deadline_and_aborts_with_conservative_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_TIKTOK_APIFY_FALLBACK", "1")
    monkeypatch.setenv("OMIYAGE_SALES25_SEARCH_DEPTH", "50")
    monkeypatch.setenv("TIKTOK_APIFY_FALLBACK_DEADLINE_S", "240")
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    clock = SimpleNamespace(time=lambda: 1000, monotonic=lambda: elapsed[0], sleep=sleep)
    monkeypatch.setattr(tiktok_search_fallback, "time", clock)
    monkeypatch.setattr(apify_client, "time", clock)
    ledger = _ReservationLedger()
    ledger.token = SimpleNamespace(global_reserved=True)
    monkeypatch.setattr(CostGuard, "from_env", lambda: ledger)
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_TIKTOK_VIDEO] = actor_items(0, 13)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/acts/" in request.url.path:
            bodies.append(json.loads(request.content))
            if len(bodies) == 2:
                fake.final_status = "RUNNING"
        if request.method == "GET" and "/actor-runs/" in request.url.path and len(bodies) == 1:
            elapsed[0] += 218  # The first poll already consumed three seconds.
        return fake.handler(request)

    actor = ApifyClient(
        "synthetic", ledger=ledger, http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    search = Mock(wraps=actor.tiktok_search)
    monkeypatch.setattr(actor, "tiktok_search", search)
    monkeypatch.setattr(ApifyClient, "from_env", lambda **kwargs: actor)
    client = Worker({0: "TIKTOK_BOT_WALL"})
    original = client.search_tiktok_sales25(
        QUERIES[0], owner_id="synthetic", request_fingerprint="deadline", max_videos=50
    )
    recovered = apply_sales25_fallback(
        original, media_client=client, deadline_s=300, request_id="synthetic"
    )
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert bodies[0]["resultsPerPage"] == 50
    assert search.call_args.kwargs["deadline_s"] == 19
    assert search.call_args.kwargs["request_id"] == "synthetic:retry"
    assert elapsed[0] == 240 and len(fake.aborted) == 1
    assert ledger.reserved == [0.2, 0.2]
    assert [(cost, units) for _, cost, units in ledger.settled] == [(0.052, 13), (0.2, 0)]
    assert recovered["acquisition_status"]["actual"] == 13
    assert "APIFY_RETRY_FAILED:APIFY_TIMEOUT" in recovered["apify_retry_warning"]


@pytest.mark.parametrize("channel", ["D_SELF", "C_TEAM"])
@pytest.mark.parametrize("outcome", ["posted", "failed", "uncertain"])
def test_incomplete_notifies_owner_via_existing_watcher(
    monkeypatch: pytest.MonkeyPatch, channel: str, outcome: str
) -> None:
    from teamagent.mcp_gateway import server
    from teamagent.skills.omiyage_report import skill as module

    store = ProposalJobStore(table_name="", memory={})
    accepted = build(store, Pipeline(outcomes={"analysis": "timeout"})).run(
        request(), ctx(channel=channel)
    )
    parent = sales25.Parent(store, reader=lambda ref: pytest.fail("no attachment"))
    status = OmiyageReportStatusSkill(store=store, sales25_parent=parent)
    monkeypatch.setattr(module, "OmiyageReportStatusSkill", lambda: status)
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    posts: list[str] = []

    def post(message: str, destination: Any, **kwargs: Any) -> str:
        assert result(store, accepted.job_id).status == "incomplete"
        assert kwargs.get("dm_only") is True
        assert kwargs["fallback_user_id"] == "U_SELF"
        posts.append(message)
        return outcome

    monkeypatch.setattr(detached_jobs, "post_to_origin_status", post)
    for attempt in range(2):
        context = ctx(channel=channel)
        poll = server._build_async_job_poll("omiyage_report_submit", accepted.job_id, context)
        state, message = poll()
        delivered = notify.publish_notice(
            message,
            origin=context.metadata[ORIGIN_KEY],
            request_id="fake",
            job_id=accepted.job_id,
            completed=state == "done",
        )
        assert delivered is (outcome == "posted" and attempt == 0)
    assert len(posts) == (2 if outcome == "failed" else 1)
    assert all(
        word in posts[0] for word in ("作れませんでした", "理由:", "時間切れ", "もう一度同じ依頼")
    )
    assert "private" not in posts[0]
    saved = result(store, accepted.job_id)
    assert saved.status == "incomplete"
    assert saved.slack_delivered is (outcome == "posted")
    assert saved.delivery_status == ("delivered" if outcome == "posted" else "not_ready")
    assert saved.delivery_target == ("dm" if outcome == "posted" else "none")
    assert "添付しました" not in saved.message


@pytest.mark.parametrize("outcome", ["posted", "failed", "uncertain"])
def test_dm_only_notice_uses_verified_user_without_posting_to_origin(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    users: list[str] = []

    async def post(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("must not post to the request channel")

    async def dm(text: str, *, user_id: str, request_id: str, timeout_s: int) -> bool:
        users.append(user_id)
        if outcome == "uncertain":
            raise TimeoutError
        return outcome == "posted"

    monkeypatch.setattr(detached_jobs, "_post_once", post)
    monkeypatch.setattr(detached_jobs, "_post_to_user_dm", dm)
    assert (
        post_to_origin_status(
            "作れませんでした。理由: 時間切れ。もう一度ご依頼ください。",
            detached_jobs.Destination("C12345678", "111.222"),
            request_id="synthetic",
            fallback_user_id="U12345678",
            dm_only=True,
        )
        == outcome
    )
    assert users == ["U12345678"]
