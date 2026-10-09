"""本番の例外包装、未発表名の表記ゆれ、複数Partの出典位置を固定する。"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import httpx
import pytest
from google.genai.errors import ClientError, ServerError

from teamagent.adapters import retry
from teamagent.adapters.gemini_client import (
    GeminiGroundedResponse,
    GroundingSupport,
    _is_retryable_vertex,
    _parse_grounding,
)
from teamagent.skills.proposal_research.brief import ResearchBrief
from teamagent.skills.proposal_research.skill import (
    _Sources,
    _strip_urls,
)

from .fakes import (
    SECTION_NAMES,
    FakeGemini,
    FakeTikTokSearcher,
    FakeUrlChecker,
    intermediate_payload,
)
from .test_confirmed_regressions import _response
from .test_skill import run_research


@pytest.mark.parametrize(
    "inner",
    [
        ClientError(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}),
        ServerError(500, {"error": {"status": "INTERNAL"}}),
        ServerError(503, {"error": {"status": "UNAVAILABLE"}}),
        httpx.ConnectError("reset"),
        httpx.RemoteProtocolError("disconnected"),
    ],
)
def test_production_wrapped_stage_a_error_retries_only_failed_section(
    inner: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LONG_JOB_RETRY", "1")
    waits: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", waits.append)

    class WrappedGemini(FakeGemini):
        def __init__(self) -> None:
            super().__init__()
            self.attempts: Counter[str] = Counter()

        def generate_with_google_search(
            self, prompt: str, request_id: str, **kwargs: Any
        ) -> GeminiGroundedResponse:
            section = prompt.splitlines()[0].split(": ", 1)[1]
            self.attempts[section] += 1
            if section == "F_competitor" and self.attempts[section] == 1:
                try:
                    raise inner
                except Exception as exc:
                    raise RuntimeError(
                        f"Gemini Web 検索に失敗しました: {type(exc).__name__}"
                    ) from exc
            return super().generate_with_google_search(prompt, request_id, **kwargs)

    gemini = WrappedGemini()
    assert run_research(gemini).research_json["F_competitor"]
    assert gemini.attempts == Counter({**dict.fromkeys(SECTION_NAMES, 1), "F_competitor": 2})
    assert len(waits) == 1 and waits[0] > 0


@pytest.mark.parametrize("status", ["RESOURCE_EXHAUSTED", "UNAVAILABLE", 429, 500, 503])
def test_retry_classifier_follows_nested_causes_and_status(status: str | int) -> None:
    class StatusError(Exception):
        pass

    inner = StatusError()
    inner.status = status  # type: ignore[attr-defined]
    middle = RuntimeError("ServerError")
    middle.__cause__ = inner
    outer = RuntimeError("wrapped")
    outer.__cause__ = middle
    assert _is_retryable_vertex(outer)
    inner.__cause__ = outer  # cycle is bounded
    assert not _is_retryable_vertex(RuntimeError("invalid request"))


@pytest.mark.parametrize(
    "name,variant",
    [
        ("ガーナ 生チョコ", "ガーナ・生チョコ"),
        ("Acme Shake", "Acme-Shake"),
        ("Acme Shake", "acme_shake"),
        ("Acme Shake", "Acme\u200bShake"),
        ("Acme Shake", "Acme\u00adShake"),
        ("Acme Shake（仮称）", "Acme Shake"),
        ("Acme Shake®™", "Acme.Shake"),
        ("Acme Shake(仮)", "Acme&Shake"),
        ("Acme Shake", "Acme\u2060Shake"),
        ("AcmeⓇShake", "AcmeShake"),
        ("Acme Shake（開発中）", "Acme(開発中)Shake"),
    ],
)
def test_unreleased_brief_and_name_never_reach_any_prompt(name: str, variant: str) -> None:
    """10-09 方針: 未発表のときは与件（自由文）も商品名も、検索にもまとめにも渡さない。"""
    gemini, searcher = FakeGemini(), FakeTikTokSearcher()
    run_research(
        gemini,
        searcher=searcher,
        brief=ResearchBrief(
            product_name=name,
            unreleased=True,
            category_term="冬限定チョコ",
            brief=variant + "の認知を広げる",
        ),
    )
    assert len(gemini.grounding_prompts) == 6 and len(gemini.text_calls) == 1
    sent = json.loads(gemini.grounding_prompts[0].splitlines()[-1])
    assert sent["brief"] == "" and sent["product_name"] == "冬限定チョコ"
    for prompt in [*gemini.grounding_prompts, gemini.text_calls[0]["prompt"]]:
        assert "の認知を広げる" not in prompt
        assert variant not in prompt and name not in prompt
    assert all(variant.casefold() not in call["query"].casefold() for call in searcher.calls)


@pytest.mark.parametrize("name", ["IT", "ON", "Rin", "Tea", "Sea", "Pro", "Url", "Moi"])
def test_short_latin_unreleased_name_never_aborts_the_research(name: str) -> None:
    gemini = FakeGemini()
    run_research(
        gemini,
        brief=ResearchBrief(
            product_name=name,
            unreleased=True,
            category_term="冬限定チョコ",
            brief="Promote moisture with seasonal ingredients",
        ),
    )
    assert len(gemini.grounding_prompts) == 6 and len(gemini.text_calls) == 1
    assert "Promote moisture with seasonal ingredients" not in gemini.grounding_prompts[0]


def test_category_with_backslash_is_sent_literally_and_brief_is_not_sent() -> None:
    gemini = FakeGemini()
    run_research(
        gemini,
        brief=ResearchBrief(
            product_name="Acme Shake",
            unreleased=True,
            category_term=r"チョコ\d",
            brief="Acme-Shake and UPPER",
        ),
    )
    sent = json.loads(gemini.grounding_prompts[0].splitlines()[-1])
    assert sent["category_term"] == r"チョコ\d" and sent["brief"] == ""
    assert "UPPER" not in gemini.grounding_prompts[0]


@pytest.mark.parametrize(
    "url", ["https://日本語.jp/report", "http://ｗｗｗ.example.com/a", "https://[2001:db8::1]/x"]
)
def test_model_idn_and_ipv6_url_never_becomes_evidence(url: str) -> None:
    payload = intermediate_payload()
    payload["A_market_data"][0]["analysis"] = f"市場は拡大（{url}）。"
    payload["G_insight"]["complaint_pattern"] = f"切り替えにくい（{url}）[S1]"
    output = run_research(FakeGemini([payload]))
    assert url not in json.dumps(output.research_json, ensure_ascii=False)
    assert _strip_urls(f"調査（{url}）日本語") == "調査（）日本語"


@pytest.mark.parametrize("part_key", ["partIndex", "part_index"])
def test_multipart_grounding_uses_offsets_relative_to_its_part(part_key: str) -> None:
    parts = ["競合A:20代女性\n", "競合B:20代女性\n"]
    fragment = "20代女性"
    start = len("競合B:".encode())
    candidate = {
        "content": {"parts": [{"text": text} for text in parts]},
        "groundingMetadata": {
            "groundingChunks": [
                {"web": {"uri": "https://first.example/evidence"}},
                {"web": {"uri": "https://second.example/evidence"}},
            ],
            "groundingSupports": [
                {
                    "segment": {
                        "text": fragment,
                        "startIndex": start,
                        "endIndex": start + len(fragment.encode()),
                        part_key: 1,
                    },
                    "groundingChunkIndices": [1],
                }
            ],
        },
    }
    _, supports, _ = _parse_grounding(candidate)
    response = _response("".join(parts), supports)
    memos, refs = _Sources(FakeUrlChecker()).prepare({"F_competitor": response})
    assert memos["F_competitor"] == "競合A:20代女性\n競合B:20代女性[S2]\n"
    assert refs["F_competitor"] == {"S2"}


def test_ambiguous_ungrounded_duplicate_never_steals_later_source() -> None:
    response = _response("競合A:20代女性\n競合B:20代女性", (GroundingSupport("20代女性", (1,)),))
    memos, refs = _Sources(FakeUrlChecker()).prepare({"F_competitor": response})
    assert "[S" not in memos["F_competitor"] and not refs["F_competitor"]


def test_out_of_order_substring_supports_keep_distinct_sources() -> None:
    response = _response(
        "国内のSNS市場は拡大している。\n市場は拡大している。",
        (
            GroundingSupport("市場は拡大している。", (1,)),
            GroundingSupport("国内のSNS市場は拡大している。", (0,)),
        ),
    )
    memos, _ = _Sources(FakeUrlChecker()).prepare({"F_competitor": response})
    assert memos["F_competitor"] == "国内のSNS市場は拡大している。[S1]\n市場は拡大している。[S2]"


def test_name_inside_category_is_rejected_at_intake() -> None:
    with pytest.raises(ValueError, match="category_term に商品名"):
        ResearchBrief(
            product_name="Acme Shake", unreleased=True, category_term="チョコ（Acme Shake）"
        )
