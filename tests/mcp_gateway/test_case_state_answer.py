"""案件状態フラグの直接配信と同じ回答の復旧（実接続なし）。"""

from __future__ import annotations

import json
from importlib import import_module
from typing import Any

import pytest

from teamagent.mcp_gateway import detached_jobs, server
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search.schema import SearchInput, SearchOutput

fixtures = import_module("tests.mcp_gateway.test_direct_search_answer")
ME, OTHER, DM, CHANNEL, STUB = (
    fixtures.ME,
    fixtures.OTHER,
    fixtures.DM,
    fixtures.CHANNEL,
    fixtures.STUB,
)


class FixedSearch(BaseSkill[SearchInput, SearchOutput]):
    input_schema = SearchInput
    output_schema = SearchOutput

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        return SearchOutput(
            answer=self.answer,
            case_state_answer=self.answer,
            direct_answer=self.answer,
            direct_answer_kind="case_state",
            total_cost_usd=0,
        )


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (
        "SEARCH_DIRECT_ANSWER",
        "SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS",
        "SEARCH_DIRECT_ANSWER_KINDS",
        "ENABLE_PROGRESS_NOTIFY",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "false")
    monkeypatch.setattr(server, "_record_usage", lambda **kwargs: None)
    monkeypatch.setattr(server, "_inject_search_web_links", lambda data: None)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0)


async def dispatch(
    answer: str, *, email: str = ME, channel: str = DM, verified: bool = True
) -> dict[str, Any]:
    contents = await server.dispatch_tool(
        fixtures._spec(FixedSearch(answer), "search"),
        "search",
        {"query": "案件の状態"},
        identity_resolver=fixtures._resolver_for(email),
        caller_claim_verifier=fixtures._Verifier(fixtures._claim(channel=channel))
        if verified
        else None,
    )
    result = json.loads(contents[0].text)
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("outcome", ["posted", "failed", "uncertain"])
async def test_case_state_flag_delivers_or_returns_the_same_fixed_answer(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", ME)
    answer = "2026-08 時点の正式状態: 提案\n出典: https://example.test/source"
    posts = []

    def post(text: str, *args: Any, **kwargs: Any) -> str:
        posts.append(text)
        return outcome

    monkeypatch.setattr(detached_jobs, "post_to_origin_status", post)
    result = await dispatch(answer)
    assert posts == [answer]
    assert result["case_state_answer"] == answer
    assert result["direct_delivery"] == {"answer_kind": "case_state", "answer": answer}
    assert result["answer"] == (STUB if outcome == "posted" else answer)


@pytest.mark.parametrize(
    "email,channel,verified", [(OTHER, DM, True), (ME, CHANNEL, True), (ME, DM, False)]
)
async def test_case_state_direct_delivery_keeps_identity_and_dm_boundary(
    monkeypatch: pytest.MonkeyPatch, email: str, channel: str, verified: bool
) -> None:
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", ME)
    monkeypatch.setattr(
        detached_jobs, "post_to_origin_status", lambda *a, **kw: pytest.fail("outside verified DM")
    )
    result = await dispatch("正式状態は不明", email=email, channel=channel, verified=verified)
    assert "delivery" not in result
