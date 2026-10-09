"""区分の粒度と、モデル由来 URL・未発表名の境界を追加で検証する。"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from typing import Any

import pytest

from teamagent.adapters.tiktok_scraper import TikTokSearchResult
from teamagent.skills.proposal_research.brief import ResearchBrief
from teamagent.skills.proposal_research.skill import _SECTION_LABELS

from .fakes import (
    GOOD_URL,
    REAL_TIKTOK_URL,
    UNREGISTERED_URL,
    FakeGemini,
    FakeTikTokSearcher,
    intermediate_payload,
)
from .test_skill import run_research

_CARDINALITY_CASES = [
    ("A_market_data", 2),
    ("A_market_data", 4),
    ("B_social_trend", 2),
    ("B_social_trend", 4),
    ("D_publicity", 5),
    ("D_publicity", 9),
    ("E_community", 2),
    ("E_community", 4),
]


def _resize(payload: dict[str, Any], section: str, count: int) -> None:
    items = payload[section]
    payload[section] = [copy.deepcopy(items[index % len(items)]) for index in range(count)]


@pytest.mark.parametrize(("section", "count"), _CARDINALITY_CASES)
def test_intermediate_section_cardinality_retries_and_recovers(section: str, count: int) -> None:
    incomplete = intermediate_payload()
    _resize(incomplete, section, count)
    gemini = FakeGemini([incomplete, intermediate_payload()])
    output = run_research(gemini)
    assert len(gemini.text_calls) == 2
    assert section in gemini.text_calls[1]["prompt"]
    expected = 6 if section == "D_publicity" else 3
    assert len(output.research_json[section]) == expected


@pytest.mark.parametrize(("section", "count"), _CARDINALITY_CASES)
def test_intermediate_section_cardinality_twice_fails_with_section(
    section: str, count: int
) -> None:
    incomplete = intermediate_payload()
    _resize(incomplete, section, count)
    gemini = FakeGemini([incomplete])
    with pytest.raises(ValueError, match=re.escape(_SECTION_LABELS[section])):
        run_research(gemini)
    assert len(gemini.text_calls) == 2


@pytest.mark.parametrize(
    ("section", "count"), [("A_market_data", 2), ("A_market_data", 4), ("B_social_trend", 4)]
)
def test_intermediate_alternative_cardinality_retries_and_recovers(
    section: str, count: int
) -> None:
    incomplete = intermediate_payload()
    alternative = copy.deepcopy(incomplete["A_market_data"][0]["alt_data"][0])
    incomplete[section][0]["alt_data"] = [copy.deepcopy(alternative) for _ in range(count)]
    gemini = FakeGemini([incomplete, intermediate_payload()])
    output = run_research(gemini)
    assert len(gemini.text_calls) == 2
    assert section in gemini.text_calls[1]["prompt"]
    assert output.research_json[section][0]["url"] == GOOD_URL


@pytest.mark.parametrize(
    ("section", "count"), [("A_market_data", 2), ("A_market_data", 4), ("B_social_trend", 4)]
)
def test_intermediate_alternative_cardinality_twice_fails(section: str, count: int) -> None:
    incomplete = intermediate_payload()
    alternative = copy.deepcopy(incomplete["A_market_data"][0]["alt_data"][0])
    incomplete[section][0]["alt_data"] = [copy.deepcopy(alternative) for _ in range(count)]
    gemini = FakeGemini([incomplete])
    with pytest.raises(ValueError, match=re.escape(_SECTION_LABELS[section])):
        run_research(gemini)
    assert len(gemini.text_calls) == 2


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_social_alternative_count_accepts_zero_through_three(count: int) -> None:
    payload = intermediate_payload()
    alternative = payload["A_market_data"][0]["alt_data"][0]
    payload["B_social_trend"][0]["alt_data"] = [copy.deepcopy(alternative) for _ in range(count)]
    gemini = FakeGemini([payload])
    output = run_research(gemini)
    assert len(gemini.text_calls) == 1
    assert len(output.research_json["B_social_trend"][0]["alt_data"]) == count


@pytest.mark.parametrize(
    ("section", "key", "expected"),
    [
        ("A_market_data", "url", 2),
        ("B_social_trend", "url", 2),
        ("D_publicity", "evidence_url", 5),
        ("E_community", "data_url", 2),
    ],
)
def test_discarding_one_unverified_item_keeps_nonempty_section_without_retry(
    section: str, key: str, expected: int
) -> None:
    payload = intermediate_payload()
    payload[section][0][key] = "S999"
    gemini = FakeGemini([payload])
    output = run_research(gemini)
    assert len(output.research_json[section]) == expected
    assert len(gemini.text_calls) == 1
    assert output.summary.discarded_by_section[section] >= 1


def test_raw_model_url_in_product_meta_is_removed() -> None:
    payload = intermediate_payload()
    payload["product_meta"]["moment"] = "冬 " + UNREGISTERED_URL
    output = run_research(FakeGemini([payload]))
    assert output.research_json["product_meta"]["moment"].strip() == "冬"
    assert UNREGISTERED_URL not in json.dumps(output.research_json, ensure_ascii=False)


def test_removing_meta_url_leaves_blank_field_retries_and_recovers() -> None:
    payload = intermediate_payload()
    payload["product_meta"]["moment"] = UNREGISTERED_URL
    gemini = FakeGemini([payload, intermediate_payload()])
    output = run_research(gemini)
    assert output.research_json["product_meta"]["moment"] == "冬"
    assert len(gemini.text_calls) == 2
    assert "moment" in gemini.text_calls[1]["prompt"]


def test_removing_meta_url_leaves_blank_field_twice_fails() -> None:
    payload = intermediate_payload()
    payload["product_meta"]["moment"] = UNREGISTERED_URL
    gemini = FakeGemini([payload])
    with pytest.raises(ValueError, match="提案書の形にまとめられませんでした"):
        run_research(gemini)
    assert len(gemini.text_calls) == 2


@pytest.mark.parametrize("port", ["not-a-port", "65536", "444"])
def test_tiktok_invalid_port_is_skipped_before_choosing_representative(port: str) -> None:
    class InvalidPortFirstSearcher(FakeTikTokSearcher):
        def __call__(
            self, query: str, *, search_type: str = "keyword", **kwargs: Any
        ) -> TikTokSearchResult:
            result = super().__call__(query, search_type=search_type, **kwargs)
            valid_video = result.videos[0]
            invalid_video = replace(
                valid_video, url=REAL_TIKTOK_URL.replace("www.tiktok.com", f"www.tiktok.com:{port}")
            )
            return replace(result, videos=(invalid_video, valid_video))

    output = run_research(searcher=InvalidPortFirstSearcher())
    assert all(
        item["representative_post_url"] == REAL_TIKTOK_URL
        for item in output.research_json["C_tiktok"]
    )
    assert all(
        "上位 1 本" in item["search_demand_note"] for item in output.research_json["C_tiktok"]
    )


def test_unreleased_fullwidth_name_in_brief_is_normalized_and_omitted_from_search() -> None:
    gemini = FakeGemini()
    output = run_research(
        gemini,
        brief=ResearchBrief(
            product_name="ＡＣＭＥ　新商品",
            unreleased=True,
            category_term="冬限定チョコレート",
            brief="ＡＣＭＥ　新商品で認知を広げる",
        ),
    )
    assert output.research_json["brand"] == "ACME 新商品"
    assert all("冬限定チョコレート" in prompt for prompt in gemini.grounding_prompts)
    assert all(
        "ＡＣＭＥ" not in prompt and "ACME 新商品" not in prompt
        for prompt in gemini.grounding_prompts
    )
