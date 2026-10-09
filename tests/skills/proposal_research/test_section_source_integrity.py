"""同一区分の複数出典・区分をまたぐ参照・入れ子URLの回帰テスト。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from teamagent.adapters.gemini_client import (
    GeminiGroundedResponse,
    GroundingSource,
    GroundingSupport,
)

from .fakes import (
    FIXTURE,
    GOOD_URL,
    UNREGISTERED_URL,
    FakeGemini,
    FakeUrlChecker,
    intermediate_payload,
)
from .test_evidence_regressions import (
    MultipleSourcesChecker,
    MultipleSourcesGemini,
)
from .test_skill import run_research

SECOND_REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/BSECOND"
SECOND_URL = "https://b-second.example/other-report"
B_TEXT = FIXTURE["sections"]["B_social_trend"]
B_FRAG2 = "掲載が削除された調査では回答者の割合は３割だった。"


class TwoUsableInB(FakeGemini):
    def generate_with_google_search(
        self, prompt: str, request_id: str, **kwargs: Any
    ) -> GeminiGroundedResponse:
        response = super().generate_with_google_search(prompt, request_id, **kwargs)
        if not prompt.startswith("SECTION: B_social_trend"):
            return response
        return replace(
            response,
            sources=(
                GroundingSource(title="first", uri=FIXTURE["sources"][0]["uri"], domain="a"),
                GroundingSource(title="second", uri=SECOND_REDIRECT, domain="b"),
            ),
            supports=(
                GroundingSupport(text=FIXTURE["supports"][0]["text"], source_indices=(0,)),
                GroundingSupport(text=B_FRAG2, source_indices=(1,)),
            ),
        )


class TwoUsableChecker(FakeUrlChecker):
    def resolve_grounding_redirect(self, uri: str) -> str:
        if uri == SECOND_REDIRECT:
            return SECOND_URL
        return super().resolve_grounding_redirect(uri)


def test_two_usable_in_same_section_keep_their_own_urls() -> None:
    payload = intermediate_payload()
    # S1 = GOOD (first), S2 = BAD (other sections), S3 = SECOND_URL (B only)
    payload["B_social_trend"][0]["url"] = "S1"
    payload["B_social_trend"][1]["url"] = "S3"
    payload["B_social_trend"][2]["url"] = "S3"
    payload["B_social_trend"][2]["analysis"] += " [S1]"
    output = run_research(TwoUsableInB([payload]), TwoUsableChecker())
    b = output.research_json["B_social_trend"]
    assert [item["url"] for item in b] == [GOOD_URL, SECOND_URL, SECOND_URL]
    assert output.summary.source_count == 2
    assert f"(出典: {GOOD_URL})" in b[2]["analysis"]


def test_insight_cites_other_sections_number_is_rejected() -> None:
    payload = intermediate_payload()
    for item in payload["B_social_trend"]:
        item["url"] = "S3"
    payload["G_insight"] = {k: v.replace("[S1]", "[S4]") for k, v in payload["G_insight"].items()}
    payload["G_insight"]["desire_example"] = (
        "手軽なおやつがあるとうれしい [S3]"  # S3 belongs to B only
    )
    payload["H_event"]["url"] = "S4"
    gemini = MultipleSourcesGemini([payload])
    with pytest.raises(ValueError):
        run_research(gemini, MultipleSourcesChecker())


def test_inline_ref_from_other_section_drops_item() -> None:
    payload = intermediate_payload()
    for item in payload["B_social_trend"]:
        item["url"] = "S3"
    payload["B_social_trend"][1]["analysis"] += " [S1]"  # S1 usable but belongs to A..F, not B
    payload["G_insight"] = {k: v.replace("[S1]", "[S4]") for k, v in payload["G_insight"].items()}
    payload["H_event"]["url"] = "S4"
    output = run_research(MultipleSourcesGemini([payload]), MultipleSourcesChecker())
    assert len(output.research_json["B_social_trend"]) == 2
    assert GOOD_URL not in json.dumps(output.research_json["B_social_trend"], ensure_ascii=False)


def test_model_url_in_nested_list_field_is_removed() -> None:
    payload = intermediate_payload()
    payload["D_publicity"][0]["recommended_media"] = [f"食品媒体 {UNREGISTERED_URL}"]
    output = run_research(FakeGemini([payload]))
    assert UNREGISTERED_URL not in json.dumps(output.research_json, ensure_ascii=False)


def test_two_insight_sources_keep_each_inline_number_and_count() -> None:
    class TwoSourcesInInsight(TwoUsableInB):
        def generate_with_google_search(
            self, prompt: str, request_id: str, **kwargs: Any
        ) -> GeminiGroundedResponse:
            if prompt.startswith("SECTION: G_insight_H_event"):
                prompt = prompt.replace("SECTION: G_insight_H_event", "SECTION: B_social_trend", 1)
            elif prompt.startswith("SECTION: B_social_trend"):
                return FakeGemini.generate_with_google_search(self, prompt, request_id, **kwargs)
            return super().generate_with_google_search(prompt, request_id, **kwargs)

    payload = intermediate_payload()
    payload["G_insight"]["desire_example"] = "需要がある [S3]。別の調査 [S1]"
    output = run_research(TwoSourcesInInsight([payload]), TwoUsableChecker())
    text = output.research_json["G_insight"]["desire_example"]
    assert text == f"需要がある (出典: {SECOND_URL})。別の調査 (出典: {GOOD_URL})"
    assert output.summary.source_count == 2
