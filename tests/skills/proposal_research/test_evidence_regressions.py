"""複数の出典と、モデルの本文・転送失敗を使って出典保証を反証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.gemini_client import (
    GeminiGroundedResponse,
    GroundingSource,
    GroundingSupport,
)

from .fakes import (
    BAD_URL,
    FIXTURE,
    GOOD_URL,
    UNREGISTERED_URL,
    FakeGemini,
    FakeUrlChecker,
    intermediate_payload,
)
from .test_skill import run_research

MULTIPLE_SOURCES = json.loads(
    (Path(__file__).parent / "fixtures" / "grounded_multiple_sources.json").read_text()
)


class MultipleSourcesGemini(FakeGemini):
    def generate_with_google_search(
        self, prompt: str, request_id: str, **kwargs: Any
    ) -> GeminiGroundedResponse:
        response = super().generate_with_google_search(prompt, request_id, **kwargs)
        section = prompt.split("\n", 1)[0].removeprefix("SECTION:").strip()
        if section not in MULTIPLE_SOURCES:
            return response
        source = MULTIPLE_SOURCES[section]
        return replace(
            response,
            sources=(GroundingSource(title=source["title"], uri=source["uri"], domain="example"),),
            supports=(GroundingSupport(text=FIXTURE["supports"][0]["text"], source_indices=(0,)),),
        )


class MultipleSourcesChecker(FakeUrlChecker):
    def resolve_grounding_redirect(self, uri: str) -> str:
        for source in MULTIPLE_SOURCES.values():
            if uri == source["uri"]:
                with self._lock:
                    self.resolve_calls.append(uri)
                return str(source["resolved_url"])
        return super().resolve_grounding_redirect(uri)


@pytest.fixture
def multiple_sources_payload() -> dict[str, Any]:
    payload = intermediate_payload()
    for item in payload["B_social_trend"]:
        item["url"] = "S3"
    # 実在していても別区分の番号は、この項目の出典にできない。
    payload["B_social_trend"][0]["url"] = "S1"
    payload["B_social_trend"][1]["analysis"] += " [S3]"
    payload["G_insight"] = {
        key: value.replace("[S1]", "[S4]") for key, value in payload["G_insight"].items()
    }
    payload["H_event"]["url"] = "S4"
    return payload


def test_each_number_uses_its_own_url_and_only_its_sections_sources(
    multiple_sources_payload: dict[str, Any],
) -> None:
    gemini = MultipleSourcesGemini([multiple_sources_payload])
    output = run_research(gemini, MultipleSourcesChecker())
    data = output.research_json
    second = MULTIPLE_SOURCES["B_social_trend"]["resolved_url"]
    third = MULTIPLE_SOURCES["G_insight_H_event"]["resolved_url"]
    assert [item["url"] for item in data["B_social_trend"]] == [second, second]
    assert second in data["B_social_trend"][0]["analysis"]
    assert GOOD_URL not in data["B_social_trend"][0]["analysis"]
    assert output.summary.discarded_by_section == {"B_social_trend": 1}
    assert all(third in value and GOOD_URL not in value for value in data["G_insight"].values())
    assert data["H_event"]["url"] == third
    assert data["A_market_data"][0]["url"] == GOOD_URL
    assert output.summary.source_count == 3
    prompt = json.loads(gemini.text_calls[0]["prompt"])
    assert {source["number"] for source in prompt["usable_sources"]} == {"S1", "S3", "S4"}


def test_unavailable_inline_reference_drops_alternative_with_valid_url_field() -> None:
    payload = intermediate_payload()
    payload["A_market_data"][0]["alt_data"][0]["analysis"] = "削除された記事の主張 [S2]"
    output = run_research(FakeGemini([payload]))
    assert len(output.research_json["A_market_data"][0]["alt_data"]) == 2
    assert output.summary.discarded_by_section == {"A_market_data": 1}
    assert BAD_URL not in json.dumps(output.research_json, ensure_ascii=False)


def test_model_written_url_in_item_body_is_never_returned() -> None:
    payload = intermediate_payload()
    payload["A_market_data"][0]["analysis"] = f"気分転換になる {UNREGISTERED_URL}"
    output = run_research(FakeGemini([payload]))
    assert UNREGISTERED_URL not in json.dumps(output.research_json, ensure_ascii=False)
    assert output.research_json["A_market_data"][0]["url"] == GOOD_URL
    assert "気分転換になる" in output.research_json["A_market_data"][0]["analysis"]


@pytest.mark.parametrize("failure", ["exception", "another_grounding_redirect"])
def test_unresolved_grounding_redirect_never_becomes_an_output_source(failure: str) -> None:
    uri = FIXTURE["sources"][1]["uri"]

    class UnresolvedChecker(FakeUrlChecker):
        def resolve_grounding_redirect(self, url: str) -> str:
            if url == uri:
                if failure == "exception":
                    raise ValueError("grounding_redirect_unresolved")
                return url + "-again"
            return super().resolve_grounding_redirect(url)

    payload = intermediate_payload()
    payload["A_market_data"][0]["alt_data"][0]["url"] = "S2"
    checker = UnresolvedChecker()
    output = run_research(FakeGemini([payload]), checker)
    assert len(output.research_json["A_market_data"][0]["alt_data"]) == 2
    assert output.summary.discarded_by_section == {"A_market_data": 1}
    assert "grounding-api-redirect" not in json.dumps(output.research_json, ensure_ascii=False)
    assert all("grounding-api-redirect" not in url for url in checker.verify_calls)


def test_insight_without_any_reference_is_retried_and_rejected() -> None:
    payload = intermediate_payload()
    payload["G_insight"]["desire_example"] = "出典のない主張"
    gemini = FakeGemini([payload])
    with pytest.raises(ValueError, match="不満と欲求"):
        run_research(gemini)
    assert len(gemini.text_calls) == 2
