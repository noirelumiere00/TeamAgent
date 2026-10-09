"""PR #572 の実在指摘を、既存フェイクの土台を変更せず再現する。"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import replace
from typing import Any, ClassVar

import httpx
import pytest

from teamagent.adapters.gemini_client import (
    GeminiGroundedResponse,
    GroundingSource,
    GroundingSupport,
    _parse_grounding,
)
from teamagent.adapters.source_url_check import UrlCheckResult
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_research import skill as research_module
from teamagent.skills.proposal_research.brief import ResearchBrief
from teamagent.skills.proposal_research.skill import (
    ResearchError,
    _memo_text,
    _Sources,
    _strip_urls,
)

from .fakes import (
    BAD_URL,
    GOOD_URL,
    SECTION_NAMES,
    FakeGemini,
    FakeTikTokSearcher,
    FakeUrlChecker,
    intermediate_payload,
)
from .test_skill import run_research


@pytest.mark.parametrize(
    "variant",
    ["acme shake", "ACMEShake", "ＡＣＭＥ　ＳＨＡＫＥ", "A C M E S h a k e"],
)
def test_unreleased_name_variants_never_reach_search_prompts_or_tiktok(variant: str) -> None:
    payload = intermediate_payload()
    payload["product_meta"]["kaiwai_keywords"][0] = variant
    for community in payload["E_community"]:
        community["tiktok_tags"].insert(0, {"tag": "#" + variant, "representative_post_url": ""})
    gemini = FakeGemini([payload])
    searcher = FakeTikTokSearcher()
    run_research(
        gemini,
        searcher=searcher,
        brief=ResearchBrief(
            product_name="Acme Shake",
            unreleased=True,
            category_term="冬限定チョコ",
            brief=variant + "の認知を広げる",
        ),
    )
    prompts = [*gemini.grounding_prompts, gemini.text_calls[0]["prompt"]]
    assert len(gemini.grounding_prompts) == 6
    assert all(
        "acmeshake" not in re.sub(r"\s+", "", unicodedata.normalize("NFKC", prompt)).casefold()
        for prompt in prompts
    )
    assert all(
        "acmeshake" not in re.sub(r"\s+", "", call["query"]).casefold() for call in searcher.calls
    )


def test_unreleased_name_left_in_category_is_rejected_at_intake() -> None:
    with pytest.raises(ValueError, match="category_term に商品名"):
        ResearchBrief(product_name="Acme Shake", unreleased=True, category_term="ACMESHAKE飲料")


def test_unreleased_name_from_model_output_is_never_sent_to_tiktok() -> None:
    """名前は検索にもまとめにも渡さないが、万一モデルの出力に現れても TikTok の検索語には使わない。"""
    payload = intermediate_payload()
    payload["product_meta"]["kaiwai_keywords"][0] = "acme shake"
    gemini, searcher = FakeGemini([payload]), FakeTikTokSearcher()
    run_research(
        gemini,
        searcher=searcher,
        brief=ResearchBrief(product_name="Acme Shake", unreleased=True, category_term="チョコ"),
    )
    assert searcher.calls
    assert all(
        "acmeshake" not in call["query"].casefold().replace(" ", "") for call in searcher.calls
    )


class RateLimitError(RuntimeError):
    status_code = 429


class ServiceUnavailableError(RuntimeError):
    status_code = 503


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timeout"),
        httpx.ReadTimeout("timeout"),
        RateLimitError("429"),
        ServiceUnavailableError("503"),
        ConnectionError("reset"),
    ],
)
def test_stage_a_transient_exception_retries_only_failed_section(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LONG_JOB_RETRY", "1")

    class TransientGemini(FakeGemini):
        attempts: ClassVar[Counter[str]] = Counter()

        def generate_with_google_search(
            self, prompt: str, request_id: str, **kwargs: Any
        ) -> GeminiGroundedResponse:
            section = prompt.splitlines()[0].split(": ", 1)[1]
            self.attempts[section] += 1
            if section == "F_competitor" and self.attempts[section] == 1:
                raise error
            return super().generate_with_google_search(prompt, request_id, **kwargs)

    gemini = TransientGemini()
    output = run_research(gemini)
    assert output.research_json["F_competitor"]
    assert gemini.attempts == Counter({**dict.fromkeys(SECTION_NAMES, 1), "F_competitor": 2})


@pytest.mark.parametrize("retry_enabled, expected_attempts", [("1", 2), ("0", 1)])
def test_stage_a_exception_retry_has_existing_long_job_limit(
    retry_enabled: str, expected_attempts: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LONG_JOB_RETRY", retry_enabled)

    class FailingGemini(FakeGemini):
        attempts = 0

        def generate_with_google_search(
            self, prompt: str, request_id: str, **kwargs: Any
        ) -> GeminiGroundedResponse:
            if prompt.startswith("SECTION: F_competitor\n"):
                self.attempts += 1
                raise TimeoutError("timeout")
            return super().generate_with_google_search(prompt, request_id, **kwargs)

    gemini = FailingGemini()
    with pytest.raises(ResearchError, match="競合"):
        run_research(gemini)
    assert gemini.attempts == expected_attempts


def _response(raw: str, supports: tuple[GroundingSupport, ...]) -> GeminiGroundedResponse:
    base = FakeGemini().generate_with_google_search("SECTION: F_competitor", "req-local")
    return replace(
        base,
        text=raw,
        sources=(
            GroundingSource("出典1", "https://first.example/evidence", "first.example"),
            GroundingSource("出典2", "https://second.example/evidence", "second.example"),
        ),
        supports=supports,
    )


def test_grounding_byte_offsets_select_distinct_repeated_japanese_fragments() -> None:
    raw = "競合A:20代女性\n競合B:20代女性"
    fragment = "20代女性"
    first_start = raw.index(fragment)
    second_start = raw.rindex(fragment)
    response = _response(
        raw,
        (
            GroundingSupport(fragment, (1,), len(raw[:second_start].encode()), len(raw.encode())),
            GroundingSupport(
                fragment,
                (0,),
                len(raw[:first_start].encode()),
                len(raw[: first_start + len(fragment)].encode()),
            ),
        ),
    )
    memos, refs = _Sources(FakeUrlChecker()).prepare({"F_competitor": response})
    assert memos["F_competitor"] == "競合A:20代女性[S1]\n競合B:20代女性[S2]"
    assert refs["F_competitor"] == {"S1", "S2"}


@pytest.mark.parametrize("has_invalid_offset", [False, True])
def test_grounding_missing_or_invalid_offsets_reject_ambiguous_occurrences(
    has_invalid_offset: bool,
) -> None:
    response = _response(
        "競合A:20代女性\n競合B:20代女性",
        (
            GroundingSupport("20代女性", (0,), end_byte=1 if has_invalid_offset else None),
            GroundingSupport("20代女性", (1,)),
        ),
    )
    memos, refs = _Sources(FakeUrlChecker()).prepare({"F_competitor": response})
    assert memos["F_competitor"] == "競合A:20代女性\n競合B:20代女性"
    assert not refs["F_competitor"]


def test_grounding_substring_does_not_credit_previously_supported_longer_claim() -> None:
    response = _response(
        "SNS市場は拡大。\n市場は拡大。",
        (GroundingSupport("SNS市場は拡大。", (0,)), GroundingSupport("市場は拡大。", (1,))),
    )
    memos, _ = _Sources(FakeUrlChecker()).prepare({"A_market_data": response})
    assert memos["A_market_data"] == "SNS市場は拡大。[S1]\n市場は拡大。[S2]"


@pytest.mark.parametrize("snake_case", [False, True])
def test_adapter_keeps_segment_utf8_byte_indices(snake_case: bool) -> None:
    segment = {
        "text": "日本語",
        "start_index" if snake_case else "startIndex": 6,
        "end_index" if snake_case else "endIndex": 15,
    }
    candidate = {
        "groundingMetadata": {
            "groundingChunks": [{"web": {"uri": GOOD_URL}}],
            "groundingSupports": [{"segment": segment, "groundingChunkIndices": [0]}],
        }
    }
    _, supports, _ = _parse_grounding(candidate)
    assert supports[0].start_byte == 6
    assert supports[0].end_byte == 15


def test_transient_origin_check_is_retried_and_record_becomes_usable() -> None:
    class RecoveringChecker(FakeUrlChecker):
        def verify_public_url(self, url: str) -> UrlCheckResult:
            result = super().verify_public_url(url)
            if url == GOOD_URL and self.verify_calls.count(GOOD_URL) == 1:
                return result.model_copy(
                    update={"ok": False, "status_code": None, "reason": "timeout"}
                )
            return result

    checker = RecoveringChecker()
    gemini = FakeGemini()
    output = run_research(gemini, checker)
    assert output.summary.source_count == 1
    assert output.summary.unconfirmed_count == 0
    assert checker.verify_calls.count(GOOD_URL) == 2
    assert checker.verify_calls.count(BAD_URL) == 1
    assert sum(gemini.grounding_calls.values()) == 12


def test_url_removal_preserves_japanese_following_text_and_citation() -> None:
    assert (
        _memo_text("公式（https://example.test/a）によると売上は3割増。")
        == "公式（）によると売上は3割増。"
    )
    assert (
        _strip_urls("市場は拡大（https://example.test/a）。特に20代で顕著。")
        == "市場は拡大（）。特に20代で顕著。"
    )
    payload = intermediate_payload()
    payload["G_insight"]["complaint_pattern"] = (
        "気分を切り替えにくい（https://model.example/survey）[S1]。対象は20代。"
    )
    output = run_research(FakeGemini([payload]))
    insight = output.research_json["G_insight"]["complaint_pattern"]
    assert "https://model.example" not in insight
    assert "対象は20代。" in insight
    assert f"(出典: {GOOD_URL})" in insight


def test_grounding_inline_url_does_not_remove_following_supported_claim() -> None:
    response = _response(
        "[公式](https://model.example/)によると市場は拡大。",
        (GroundingSupport("市場は拡大。", (0,)),),
    )
    memos, refs = _Sources(FakeUrlChecker()).prepare({"A_market_data": response})
    assert memos["A_market_data"] == "[公式]()によると市場は拡大。[S1]"
    assert refs["A_market_data"] == {"S1"}


def test_meta_tags_and_community_names_drop_source_markers_before_search() -> None:
    payload = intermediate_payload()
    payload["product_meta"]["moment"] += "[S1]"
    payload["product_meta"]["kaiwai_keywords"][0] += "[S1]"
    for community in payload["E_community"]:
        community["name"] += "[S1]"
        community["tiktok_tags"][0]["tag"] += "[S1]"
    searcher = FakeTikTokSearcher()
    output = run_research(FakeGemini([payload]), searcher=searcher)
    assert "[S1]" not in json.dumps(output.research_json)
    assert all("[S1]" not in call["query"] for call in searcher.calls)
    assert all(community["tiktok_tags"] for community in output.research_json["E_community"])


def test_bot_blocked_sources_are_counted_separately() -> None:
    class BlockedChecker(FakeUrlChecker):
        def verify_public_url(self, url: str) -> UrlCheckResult:
            result = super().verify_public_url(url)
            return (
                result.model_copy(update={"status_code": 403, "bot_blocked": True})
                if result.ok
                else result
            )

    output = run_research(checker=BlockedChecker())
    assert output.summary.source_count == 1
    assert output.summary.unconfirmed_count == 1


def test_source_stage_deadline_does_not_wait_for_blocked_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(research_module, "SOURCE_STAGE_DEADLINE_S", 0.02)
    released = threading.Event()

    class BlockedChecker(FakeUrlChecker):
        def resolve_grounding_redirect(self, uri: str) -> str:
            released.wait(timeout=1)
            return uri

    response = _response("市場は拡大。", (GroundingSupport("市場は拡大。", (0,)),))
    sources = _Sources(BlockedChecker())
    started = time.monotonic()
    try:
        _, refs = sources.prepare({"A_market_data": response})
        assert time.monotonic() - started < 0.3
        assert not sources.allowed(refs["A_market_data"])
    finally:
        released.set()


def test_research_aggregate_log_does_not_repeat_adapter_cost_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: dict[str, dict[str, Any]] = {}

    class Logger:
        def info(self, event: str, **values: Any) -> None:
            events[event] = values

    monkeypatch.setattr(SkillContext, "bind_logger", lambda _self, _name: Logger())
    output = run_research()
    assert "cost_usd" not in events["proposal_research_done"]
    assert events["proposal_research_done"]["research_cost_usd"] == output.summary.gemini_cost_usd


def test_two_products_of_the_same_company_do_not_count_as_three_competitors() -> None:
    """10-09 の実機で「ロッテ ラミー / バッカス」と「ロッテ シャルロッテ」が 2 社と数えられた。"""
    from teamagent.skills.proposal_research.skill import _company_of, _missing_sections

    assert _company_of("ロッテ ラミー / バッカス") == _company_of("ロッテ シャルロッテ")
    assert _company_of("江崎グリコ「冬のくちどけポッキー」") == "江崎グリコ"
    data = {
        key: [{}] for key in ("A_market_data", "B_social_trend", "D_publicity", "E_community")
    } | {
        "G_insight": {"x": "y"},
        "H_event": {"x": "y"},
        "F_competitor": [
            {"name": "ロッテ ラミー / バッカス"},
            {"name": "江崎グリコ「冬のくちどけポッキー」"},
            {"name": "ロッテ シャルロッテ"},
        ],
    }
    assert _missing_sections(data) == ["F_competitor"]
    data["F_competitor"][2] = {"name": "ブルボン 生チョコトリュフ"}
    assert _missing_sections(data) == []
