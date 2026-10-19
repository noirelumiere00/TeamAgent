"""Search の確定回答は MCP が 1 回投稿し、会話の書き足しを止める。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.mcp_gateway import detached_jobs, server
from teamagent.mcp_gateway import direct_video_metrics as video
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search.schema import SearchInput, SearchOutput
from tests.mcp_gateway.test_direct_video_metrics import ANSWER, SECTION
from tests.mcp_gateway.test_surface_video_followup import (
    CHANNEL,
    DM,
    ME,
    OTHER,
    _claim,
    _FakeSlack,
    _resolver_for,
    _spec,
    _Verifier,
)

STUB = "回答はこの会話に投稿済みです。同じ内容の要約・補足は書かないでください。"


class _Search(BaseSkill[SearchInput, SearchOutput]):
    input_schema = SearchInput
    output_schema = SearchOutput

    def __init__(self, kind: str, answer: str) -> None:
        self.kind, self.answer = kind, answer

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        return SearchOutput(
            answer=self.answer + "\nモデル向けの旧要約。",
            direct_answer=self.answer,
            direct_answer_kind=self.kind,
            total_cost_usd=0,
            found=self.kind != "not_found",
        )


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        "SEARCH_DIRECT_ANSWER",
        "SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS",
        "SEARCH_DIRECT_ANSWER_KINDS",
        video.ENABLED_ENV,
        video.ALLOWED_EMAILS_ENV,
        "ENABLE_PROGRESS_NOTIFY",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "false")
    monkeypatch.setattr(server, "_record_usage", lambda **kwargs: None)
    monkeypatch.setattr(server, "_inject_search_web_links", lambda data: None)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0)


def enable(monkeypatch: pytest.MonkeyPatch, emails: str = ME) -> None:
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER", "1")
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS", emails)
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", "not_found,comparison,list_fast,video_metrics")


async def dispatch(
    kind: str = "not_found",
    answer: str = "宅配の事例は見つかりませんでした。",
    *,
    email: str = ME,
    channel: str = DM,
    verified: bool = True,
) -> dict[str, Any]:
    contents = await server.dispatch_tool(
        _spec(_Search(kind, answer), "search"),
        "search",
        {"query": "宅配の事例"},
        identity_resolver=_resolver_for(email),
        caller_claim_verifier=_Verifier(_claim(channel=channel)) if verified else None,
    )
    return json.loads(contents[0].text)


@pytest.mark.parametrize(
    ("kind", "answer"),
    [
        ("not_found", "宅配の事例は見つかりませんでした。"),
        ("comparison", "# 施策比較表\nA社 1,000\n📎 資料 <https://example.test/report>"),
        ("list_fast", "資料の一覧です。\n<https://example.test/file>"),
        ("video_metrics", ANSWER),
    ],
)
async def test_confirmed_search_answer_is_delivered_once_with_suppression_marker(
    monkeypatch: pytest.MonkeyPatch, kind: str, answer: str
) -> None:
    enable(monkeypatch)
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    result = await dispatch(kind, answer)
    assert len(slack.posts) == 1
    assert "モデル向けの旧要約" not in slack.posts[0]["text"]
    assert result["answer"] == STUB
    assert result["status"] == "posted" and result["delivered"] is True
    assert result["delivery"] == {"kind": "search_direct_answer", "answer_kind": kind}
    assert result["direct_delivery"] == {"answer_kind": kind, "answer": answer}
    assert "hits" not in result
    if kind == "video_metrics":
        assert result["video_metrics_evidence"]["text"] == SECTION
        assert result["video_metrics_evidence"]["urls"] == [
            "https://example.test/video/1",
            "https://example.test/video/2",
        ]


@pytest.mark.parametrize("failure", ["failed", "uncertain", "exception"])
async def test_failed_direct_answer_keeps_old_answer_and_does_not_suppress(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    enable(monkeypatch)
    monkeypatch.setenv(video.ENABLED_ENV, "1")
    monkeypatch.setenv(video.ALLOWED_EMAILS_ENV, ME)
    calls = []

    def post(*args: Any, **kwargs: Any) -> str:
        calls.append(args[0])
        if failure == "exception":
            raise RuntimeError("Slack failure")
        return failure

    monkeypatch.setattr(detached_jobs, "post_to_origin_status", post)
    result = await dispatch("video_metrics", ANSWER)
    assert result["answer"] == ANSWER + "\nモデル向けの旧要約。"
    assert result["delivery"] == {
        "kind": "search_direct_answer",
        "answer_kind": "video_metrics",
        "status": "failed" if failure == "exception" else failure,
    }
    assert result.get("delivered") is not True and result.get("status") != "posted"
    assert calls == [ANSWER]


@pytest.mark.parametrize(
    ("emails", "email", "channel", "verified"),
    [("", ME, DM, True), (ME, OTHER, DM, True), (ME, ME, CHANNEL, True), (ME, ME, DM, False)],
)
async def test_direct_answer_gate_is_byte_identical_to_off(
    monkeypatch: pytest.MonkeyPatch, emails: str, email: str, channel: str, verified: bool
) -> None:
    baseline = await dispatch(email=email, channel=channel, verified=verified)
    enable(monkeypatch, emails)
    monkeypatch.setattr(
        detached_jobs,
        "post_to_origin_status",
        lambda *a, **kw: pytest.fail("not an allowed verified DM"),
    )
    assert await dispatch(email=email, channel=channel, verified=verified) == baseline


async def test_wildcard_still_requires_verified_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    enable(monkeypatch, "*")
    monkeypatch.setattr(detached_jobs, "post_to_origin_status", lambda *a, **kw: "posted")
    assert (await dispatch(email=OTHER))["delivered"] is True
    assert "delivery" not in await dispatch(email=OTHER, verified=False)


async def test_direct_answer_keeps_clickable_source_and_escapes_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable(monkeypatch)
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    await dispatch("list_fast", "資料: [提案 & <@U999>](https://example.test/report)")
    assert slack.posts[0]["text"] == (
        "資料: 提案 &amp; &lt;@U999&gt; <https://example.test/report>"
    )


async def test_existing_partial_video_post_retains_evidence_only_for_new_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.mcp_gateway.test_direct_video_metrics import _dispatch

    monkeypatch.setenv(video.ENABLED_ENV, "1")
    monkeypatch.setenv(video.ALLOWED_EMAILS_ENV, ME)
    monkeypatch.setattr(detached_jobs, "post_to_origin_status", lambda *a, **kw: "posted")
    old = await _dispatch()
    assert "video_metrics_evidence" not in old
    enable(monkeypatch)
    current = await _dispatch()
    assert current["video_metrics_evidence"]["text"] == SECTION
    assert current["answer"] == old["answer"]


async def test_signed_gateway_runs_real_search_and_posts_canonical_campaign_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.skills.search.test_campaign_totals import _campaign_hit
    from tests.skills.search.test_composite_search import _bedrock, _converse, _pg, _skill

    enable(monkeypatch)
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "0")
    campaign = _campaign_hit()
    campaign.metadata["source_uri"] = "gdrive://CAMPAIGN_SHEET"
    skill, _ = _skill(_bedrock(), _pg([campaign]))
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    summary = "施策は完了し、商談は検討中です。11月に連絡予定です。"
    skill._bedrock.converse.return_value = _converse(summary)
    slack = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    result = await server.dispatch_tool(
        _spec(skill, "search"),
        "search",
        {"query": "施策実績"},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=_Verifier(_claim()),
    )
    payload = json.loads(result[0].text)
    assert payload["delivered"] is True
    assert payload["delivery"]["answer_kind"] == "video_metrics"
    assert len(slack.posts) == 1
    assert slack.posts[0]["text"].startswith(summary)
    assert "いいね 合計 2,844・保存 合計 310" in slack.posts[0]["text"]
    assert "https://drive.google.com/file/d/CAMPAIGN_SHEET/view" in slack.posts[0]["text"]
    assert "進めますか" not in slack.posts[0]["text"]
    skill._bedrock.converse.assert_called_once()


@pytest.mark.parametrize("kinds", [None, "", "unknown", "not_found,unknown", ","])
async def test_missing_empty_or_unknown_kinds_deny_all_direct_delivery(
    monkeypatch: pytest.MonkeyPatch, kinds: str | None
) -> None:
    enable(monkeypatch)
    if kinds is None:
        monkeypatch.delenv("SEARCH_DIRECT_ANSWER_KINDS")
    else:
        monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", kinds)
    monkeypatch.setenv(video.ENABLED_ENV, "1")
    monkeypatch.setenv(video.ALLOWED_EMAILS_ENV, ME)
    posts: list[Any] = []
    monkeypatch.setattr(detached_jobs, "post_to_origin_status", lambda *a, **kw: posts.append(a))
    for kind, answer in (
        ("not_found", "宅配の事例は見つかりませんでした。"),
        ("video_metrics", ANSWER),
    ):
        result = await dispatch(kind, answer)
        assert "direct_delivery" not in result
        assert result.get("delivered") is not True
    assert posts == []


@pytest.mark.parametrize("kind", ["comparison", "list_fast", "video_metrics"])
async def test_not_found_only_blocks_other_kinds_and_legacy_partial_post(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    enable(monkeypatch)
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", "not_found")
    monkeypatch.setenv(video.ENABLED_ENV, "1")
    monkeypatch.setenv(video.ALLOWED_EMAILS_ENV, ME)
    posts: list[Any] = []
    monkeypatch.setattr(detached_jobs, "post_to_origin_status", lambda *a, **kw: posts.append(a))
    result = await dispatch(kind, ANSWER)
    assert "direct_delivery" not in result
    assert result["answer"] == ANSWER + "\nモデル向けの旧要約。"
    assert posts == []


async def test_verified_candidate_survives_no_mcp_post_for_zero_tool_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.mcp_gateway import direct_search_answer

    enable(monkeypatch)
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", "not_found")
    monkeypatch.setattr(direct_search_answer, "deliver", lambda *a, **kw: None)
    result = await dispatch()
    assert result["direct_delivery"] == {
        "answer_kind": "not_found",
        "answer": "宅配の事例は見つかりませんでした。",
    }
