"""実測の転送 URL・Markdown・supports の形で調査の失敗条件を再現する。"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from teamagent.adapters.gemini_client import GeminiGroundedResponse, GroundingSupport
from teamagent.adapters.tiktok_scraper import TikTokSearchResult
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.research import parse_gemini_research
from teamagent.skills.proposal_research.brief import ResearchBrief
from teamagent.skills.proposal_research.schema import ProposalResearchOutput
from teamagent.skills.proposal_research.skill import _SECTION_LABELS, ProposalResearchSkill

from .fakes import (
    BAD_URL,
    FIXTURE,
    GOOD_URL,
    REAL_TIKTOK_URL,
    SECTION_NAMES,
    UNREGISTERED_URL,
    FakeGemini,
    FakeTikTokSearcher,
    FakeUrlChecker,
    intermediate_payload,
)


def run_research(
    gemini: FakeGemini | None = None,
    checker: FakeUrlChecker | None = None,
    searcher: FakeTikTokSearcher | None = None,
    *,
    brief: ResearchBrief | None = None,
) -> ProposalResearchOutput:
    return ProposalResearchSkill(
        gemini=gemini or FakeGemini(),
        url_checker=checker or FakeUrlChecker(),
        tiktok_searcher=searcher or FakeTikTokSearcher(),
    ).run(
        brief or ResearchBrief(product_name="  ＡＣＭＥ メルティーキッス  ", brief="認知を広げる"),
        SkillContext(request_id="req-proposal-research-fixture"),
    )


def test_grounded_markdown_becomes_real_urls_and_scraped_tiktok() -> None:
    gemini, checker, searcher = FakeGemini(), FakeUrlChecker(), FakeTikTokSearcher()
    output = run_research(gemini, checker, searcher)
    payload = output.research_json
    assert payload["brand"] == "ACME メルティーキッス"
    assert payload["research_date"] == datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    assert payload["A_market_data"][0]["url"] == GOOD_URL
    assert payload["B_social_trend"][0]["url"] == GOOD_URL
    assert payload["F_competitor"][0]["url"] == GOOD_URL
    assert all(GOOD_URL in value for value in payload["G_insight"].values())
    assert payload["H_event"]["url"] == GOOD_URL
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "grounding-api-redirect" not in serialized
    assert UNREGISTERED_URL not in serialized
    assert "モデル捏造" not in serialized
    assert '"S1"' not in serialized
    assert "[S1]" not in serialized
    assert payload["C_tiktok"]
    for evidence in payload["C_tiktok"]:
        assert evidence["representative_post_url"] == REAL_TIKTOK_URL
        assert evidence["total_count"] == "取得不可（UI非表示）"
        assert "1" in evidence["search_demand_note"]
        assert "12345" in evidence["search_demand_note"].replace(",", "")
    for community in payload["E_community"]:
        assert community["tiktok_tags"]
        assert all(
            tag["representative_post_url"] == REAL_TIKTOK_URL for tag in community["tiktok_tags"]
        )
    assert output.summary.source_count == 1
    assert output.summary.discarded_count == 0
    assert output.summary.tiktok_search_count == len(searcher.calls)
    assert output.summary.gemini_cost_usd == pytest.approx(6 * 0.094 + 0.01)
    assert output.summary.elapsed_seconds >= 0
    assert Counter(gemini.grounding_calls) == Counter(dict.fromkeys(SECTION_NAMES, 1))
    assert len(gemini.text_calls) == 1
    assert gemini.text_calls[0]["json_mode"] is True
    parse_gemini_research(payload)


def test_same_original_url_is_numbered_once_across_sections() -> None:
    gemini, checker = FakeGemini(), FakeUrlChecker()
    run_research(gemini, checker)
    memo = gemini.text_calls[0]["prompt"]
    supported = FIXTURE["supports"][0]["text"]
    # 六区分とも支持された同じ断片の直後に同じ全体出典番号が入る。
    assert memo.count(f"{supported}[S1]") + memo.count(f"{supported} [S1]") >= 6
    assert GOOD_URL not in checker.resolve_calls
    assert Counter(checker.verify_calls) == Counter({GOOD_URL: 1, BAD_URL: 1})


def test_404_unknown_number_and_body_only_url_drop_individual_alternatives() -> None:
    payload = intermediate_payload()
    alternatives = payload["A_market_data"][0]["alt_data"]
    alternatives[0]["url"] = "S2"
    alternatives[0]["headline"] = "404で削除すべき補足"
    alternatives[1]["url"] = "S999"
    alternatives[1]["headline"] = "存在しない出典番号の補足"
    alternatives[2]["url"] = UNREGISTERED_URL
    alternatives[2]["headline"] = "モデルが本文URLを直接記載した補足"
    output = run_research(FakeGemini([payload]))
    assert output.research_json["A_market_data"][0]["alt_data"] == []
    # 親テーマや他テーマの補足をまとめて落とさず、一件単位で捨てる。
    assert len(output.research_json["A_market_data"]) == 3
    assert len(output.research_json["A_market_data"][1]["alt_data"]) == 3
    assert output.summary.discarded_count == 3
    assert output.summary.discarded_by_section["A_market_data"] == 3
    serialized = json.dumps(output.research_json, ensure_ascii=False)
    assert BAD_URL not in serialized
    assert UNREGISTERED_URL not in serialized


def test_source_without_support_in_memo_cannot_supply_evidence() -> None:
    payload = intermediate_payload()
    payload["A_market_data"][0]["alt_data"][0]["url"] = "S3"
    output = run_research(FakeGemini([payload], unsupported_source=True))
    assert len(output.research_json["A_market_data"][0]["alt_data"]) == 2
    assert "https://unsupported.example/unused" not in json.dumps(output.research_json)
    assert output.summary.discarded_count == 1


@pytest.mark.parametrize("section", SECTION_NAMES)
def test_stage_a_ungrounded_retries_once_and_succeeds(section: str) -> None:
    gemini = FakeGemini(grounding_failures={section: 1})
    output = run_research(gemini)
    assert output.research_json["A_market_data"][0]["url"] == GOOD_URL
    assert gemini.grounding_calls[section] == 2
    assert sum(gemini.grounding_calls.values()) == 7
    assert output.summary.gemini_cost_usd == pytest.approx(7 * 0.094 + 0.01)


@pytest.mark.parametrize("section", SECTION_NAMES)
def test_stage_a_ungrounded_twice_fails_with_section_name(section: str) -> None:
    gemini = FakeGemini(grounding_failures={section: 2})
    with pytest.raises(ValueError, match=re.escape(_SECTION_LABELS[section])):
        run_research(gemini)
    assert gemini.grounding_calls[section] == 2
    assert gemini.text_calls == []


def test_stage_a_no_usable_source_retries_once_then_fails() -> None:
    gemini = FakeGemini()
    with pytest.raises(ValueError, match="市場・インサイト"):
        run_research(gemini, FakeUrlChecker(all_unavailable=True))
    assert gemini.grounding_calls["A_market_data"] == 2
    assert gemini.text_calls == []


def test_stage_a_grounded_but_only_404_source_retries_and_recovers() -> None:
    class FirstGoneSourceGemini(FakeGemini):
        def generate_with_google_search(
            self, prompt: str, request_id: str, **kwargs: Any
        ) -> GeminiGroundedResponse:
            response = super().generate_with_google_search(prompt, request_id, **kwargs)
            if (
                prompt.startswith("SECTION: E_community")
                and self.grounding_calls["E_community"] == 1
            ):
                return replace(
                    response,
                    sources=(response.sources[1],),
                    supports=(
                        GroundingSupport(text=FIXTURE["supports"][0]["text"], source_indices=(0,)),
                    ),
                )
            return response

    gemini = FirstGoneSourceGemini()
    output = run_research(gemini)
    assert output.research_json["E_community"][0]["data_url"] == GOOD_URL
    assert gemini.grounding_calls["E_community"] == 2


def test_stage_a_search_concurrency_is_at_most_three() -> None:
    gemini = FakeGemini(pause=0.025)
    run_research(gemini)
    assert 1 < gemini.max_active <= 3


def test_stage_b_two_competitors_retries_with_shortage_then_succeeds() -> None:
    incomplete = intermediate_payload()
    incomplete["F_competitor"] = incomplete["F_competitor"][:2]
    gemini = FakeGemini([incomplete, intermediate_payload()])
    output = run_research(gemini)
    assert len(output.research_json["F_competitor"]) == 3
    assert len(gemini.text_calls) == 2
    assert "F_competitor" in gemini.text_calls[1]["prompt"]


def test_stage_b_two_competitors_twice_fails() -> None:
    incomplete = intermediate_payload()
    incomplete["F_competitor"] = incomplete["F_competitor"][:2]
    gemini = FakeGemini([incomplete])
    with pytest.raises(ValueError, match="競合"):
        run_research(gemini)
    assert len(gemini.text_calls) == 2


@pytest.mark.parametrize(
    "section",
    ["A_market_data", "B_social_trend", "D_publicity", "E_community", "G_insight", "H_event"],
)
def test_stage_b_section_losing_all_evidence_fails(section: str) -> None:
    payload = intermediate_payload()
    item = payload[section]
    if isinstance(item, list):
        for evidence in item:
            key = next(key for key in ("url", "evidence_url", "data_url") if key in evidence)
            evidence[key] = "S999"
    elif section == "G_insight":
        payload[section] = {key: "出典のない主張 [S999]" for key in item}
    else:
        item["url"] = "S999"
    gemini = FakeGemini([payload])
    with pytest.raises(ValueError, match=re.escape(_SECTION_LABELS[section])):
        run_research(gemini)
    assert len(gemini.text_calls) == 2


@pytest.mark.parametrize("bad_json", ["{broken", '{"A_market_data": "wrong-type"}'])
def test_stage_b_bad_json_or_type_retries_once_and_succeeds(bad_json: str) -> None:
    gemini = FakeGemini([bad_json, intermediate_payload()])
    output = run_research(gemini)
    assert output.research_json["A_market_data"][0]["url"] == GOOD_URL
    assert len(gemini.text_calls) == 2
    assert all(call["json_mode"] for call in gemini.text_calls)


def test_stage_b_broken_json_twice_fails_after_two_calls() -> None:
    gemini = FakeGemini(["{broken"])
    with pytest.raises(ValueError):
        run_research(gemini)
    assert len(gemini.text_calls) == 2


def test_stage_c_empty_hashtag_falls_back_to_keyword() -> None:
    searcher = FakeTikTokSearcher(empty_hashtag=True)
    output = run_research(searcher=searcher)
    assert output.research_json["C_tiktok"][0]["representative_post_url"] == REAL_TIKTOK_URL
    by_query: dict[str, list[str]] = {}
    for call in searcher.calls:
        by_query.setdefault(call["query"], []).append(call["search_type"])
    assert 1 <= len(by_query) <= 6
    assert all(types == ["hashtag", "keyword"] for types in by_query.values())
    assert output.summary.tiktok_search_count == len(searcher.calls)


def test_stage_c_all_searches_fail_names_c_section() -> None:
    searcher = FakeTikTokSearcher(all_failed=True)
    with pytest.raises(ValueError, match="TikTok の代表投稿"):
        run_research(searcher=searcher)
    assert searcher.calls
    assert len({call["query"] for call in searcher.calls}) <= 6


def test_stage_c_adapter_errors_fail_with_c_section() -> None:
    class FailedSearcher(FakeTikTokSearcher):
        def __call__(
            self, query: str, *, search_type: str = "keyword", **kwargs: Any
        ) -> TikTokSearchResult:
            super().__call__(query, search_type=search_type, **kwargs)
            raise ValueError("fake scraper unavailable")

    searcher = FailedSearcher()
    with pytest.raises(ValueError, match="TikTok の代表投稿"):
        run_research(searcher=searcher)
    assert all(call["search_type"] in {"hashtag", "keyword"} for call in searcher.calls)
    assert len(searcher.calls) <= 12


def test_stage_c_reuses_souvenir_report_depth_and_timeout_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMIYAGE_SEARCH_DEPTH", "15")
    monkeypatch.setenv("OMIYAGE_SEARCH_TIMEOUT_SECONDS", "200")
    searcher = FakeTikTokSearcher()
    run_research(searcher=searcher)
    assert all(call["max_videos"] == 15 for call in searcher.calls)
    assert all(call["timeout_s"] == 200 for call in searcher.calls)


def test_unreleased_product_is_omitted_from_search_prompts() -> None:
    gemini = FakeGemini()
    run_research(
        gemini,
        brief=ResearchBrief(
            product_name="機密未発表商材アルファ",
            unreleased=True,
            category_term="冬限定チョコレート",
            brief="新商材の認知を広げる",
        ),
    )
    assert all("冬限定チョコレート" in prompt for prompt in gemini.grounding_prompts)
    assert all("機密未発表商材アルファ" not in prompt for prompt in gemini.grounding_prompts)


@pytest.mark.parametrize(
    "values", [{"product_name": " "}, {"product_name": "商材", "unreleased": True}]
)
def test_invalid_brief_fails_before_network(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ResearchBrief.model_validate(values)
