"""実測形の grounding を本物の URL アダプタ＋HTTP/DNSフェイクで通す。"""

from __future__ import annotations

import json
from collections import Counter

import httpx
import pytest

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.adapters.source_url_check import SourceUrlChecker
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_research.brief import ResearchBrief
from teamagent.skills.proposal_research.skill import ProposalResearchSkill

from .fakes import (
    BAD_URL,
    FIXTURE,
    GOOD_URL,
    UNREGISTERED_URL,
    FakeGemini,
    FakeTikTokSearcher,
    FakeUrlChecker,
    intermediate_payload,
)


def test_grounding_302_and_original_404_flow_through_to_item_deletion() -> None:
    redirects = {source["uri"]: source["resolved_url"] for source in FIXTURE["sources"]}
    requests: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        url = str(request.url)
        requests[url] += 1
        if url in redirects:
            return httpx.Response(302, headers={"location": redirects[url]})
        assert url in {GOOD_URL, BAD_URL}
        return httpx.Response(404 if url == BAD_URL else 200)

    payload = intermediate_payload()
    payload["A_market_data"][0]["alt_data"][0]["url"] = "S2"
    gemini = FakeGemini([payload])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(client=client, resolver=lambda _host: ["8.8.8.8"])
        output = ProposalResearchSkill(
            gemini=gemini, url_checker=checker, tiktok_searcher=FakeTikTokSearcher()
        ).run(ResearchBrief(product_name="明治 メルティーキッス"), SkillContext())
    assert output.research_json["A_market_data"][0]["url"] == GOOD_URL
    assert len(output.research_json["A_market_data"][0]["alt_data"]) == 2
    assert output.summary.discarded_by_section == {"A_market_data": 1}
    assert requests == Counter(dict.fromkeys([*redirects, GOOD_URL, BAD_URL], 1))
    assert BAD_URL not in json.dumps(output.research_json)


def test_model_url_in_tag_candidate_is_neither_searched_nor_returned() -> None:
    payload = intermediate_payload()
    for community in payload["E_community"]:
        community["tiktok_tags"].append({"tag": UNREGISTERED_URL, "representative_post_url": ""})
    searcher = FakeTikTokSearcher()
    output = ProposalResearchSkill(
        gemini=FakeGemini([payload]),
        url_checker=FakeUrlChecker(),
        tiktok_searcher=searcher,
    ).run(ResearchBrief(product_name="明治 メルティーキッス"), SkillContext())
    assert all(UNREGISTERED_URL not in call["query"] for call in searcher.calls)
    assert UNREGISTERED_URL not in json.dumps(output.research_json)


def test_default_gemini_uses_configured_adapter_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    gemini = FakeGemini()
    factory_calls: list[bool] = []

    def from_env() -> FakeGemini:
        factory_calls.append(True)
        return gemini

    monkeypatch.setattr(GeminiClient, "from_env", from_env)
    output = ProposalResearchSkill(
        url_checker=FakeUrlChecker(), tiktok_searcher=FakeTikTokSearcher()
    ).run(ResearchBrief(product_name="明治 メルティーキッス"), SkillContext())
    assert factory_calls == [True]
    assert output.research_json["A_market_data"][0]["url"] == GOOD_URL
