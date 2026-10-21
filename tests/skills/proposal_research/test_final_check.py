"""Stage C の実在しうるスキーマ差と、区分限定の復旧を合成入力で検証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.research import parse_gemini_research
from teamagent.skills.proposal_builder.skill import _research_summary_lines
from teamagent.skills.proposal_research import skill as skill_module
from teamagent.skills.proposal_research.brief import ResearchBrief
from teamagent.skills.proposal_research.skill import ResearchError

from .fakes import FakeGemini, FakeTikTokSearcher, intermediate_payload
from .test_section_source_integrity import SECOND_REDIRECT, TwoUsableChecker, TwoUsableInB
from .test_skill import run_research

BAD_SOURCE = "https://b-second.example/report...pdf"


class BadSecondSource(TwoUsableChecker):
    def resolve_grounding_redirect(self, uri: str) -> str:
        return BAD_SOURCE if uri == SECOND_REDIRECT else super().resolve_grounding_redirect(uri)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    class Logger:
        def info(self, event: str, **kwargs: Any) -> None:
            recorded.append((event, kwargs))

        warning = info

    monkeypatch.setattr(SkillContext, "bind_logger", lambda *_: Logger())
    monkeypatch.setattr(
        "teamagent.skills.proposal_builder.research.logger", Logger(), raising=False
    )
    return recorded


def test_bad_optional_source_drops_only_one_alternative_and_marks_review(events: Any) -> None:
    payload = intermediate_payload()
    payload["B_social_trend"][0]["alt_data"] = [
        {"headline": "補足", "analysis": "根拠", "url": ref} for ref in ("S3", "S1", "S3")
    ]
    gemini = TwoUsableInB([payload])
    output = run_research(gemini, BadSecondSource())
    assert len(output.research_json["B_social_trend"][0]["alt_data"]) == 1
    assert len(output.research_json["B_social_trend"]) == 3
    assert len(gemini.text_calls) == 1
    assert len(output.summary.review_required) == 2
    assert output.summary.discarded_by_section == {"B_social_trend": 2}
    assert "要確認" in _research_summary_lines(output)
    assert "ソーシャルの動き" in _research_summary_lines(output)
    assert BAD_SOURCE not in json.dumps(events, ensure_ascii=False)
    assert any(name == "gemini_research_validation_failed" for name, _ in events)


def test_required_source_regenerates_only_b_once_without_searching_again(events: Any) -> None:
    expected = run_research().research_json
    events.clear()
    payload = intermediate_payload()
    payload["B_social_trend"][0]["url"] = "S3"
    fixed = intermediate_payload()["B_social_trend"]
    gemini = TwoUsableInB([payload, {"B_social_trend": fixed}])
    output = run_research(gemini, BadSecondSource())
    assert output.research_json == expected
    assert len(gemini.text_calls) == 2
    retry = gemini.text_calls[1]
    request = json.loads(retry["prompt"])
    assert set(request["annotated_memos"]) == {"B_social_trend"}
    assert set(request["schema"]["properties"]) == {"B_social_trend"}
    assert {source["number"] for source in request["usable_sources"]} == {"S1"}
    assert 0 < retry["max_cost_usd"] <= 0.1
    assert sum(gemini.grounding_calls.values()) == 6
    assert output.summary.gemini_cost_usd == pytest.approx(6 * 0.094 + 0.02)
    assert len([e for e, _ in events if e == "proposal_research_tiktok_done"]) == 1


@pytest.mark.parametrize("reply", ["broken json", {}, {"B_social_trend": []}])
def test_required_retry_failure_names_section_and_stops_after_one(reply: Any) -> None:
    payload = intermediate_payload()
    payload["B_social_trend"][0]["url"] = "S3"
    gemini = TwoUsableInB([payload, reply])
    with pytest.raises(ResearchError, match="ソーシャルの動き") as caught:
        run_research(gemini, BadSecondSource())
    assert BAD_SOURCE not in str(caught.value)
    assert len(gemini.text_calls) == 2


def test_tiktok_url_with_ellipsis_is_filtered_before_final_validation(events: Any) -> None:
    class EllipsisFirst(FakeTikTokSearcher):
        def __call__(self, query: str, **kwargs: Any) -> Any:
            result = super().__call__(query, **kwargs)
            bad = replace(
                result.videos[0], url=result.videos[0].url.replace("fake_creator", "a...b")
            )
            return replace(result, videos=(bad, *result.videos))

    output = run_research(searcher=EllipsisFirst())
    assert all("上位 1 本" in row["search_demand_note"] for row in output.research_json["C_tiktok"])
    searches = [data for name, data in events if name == "proposal_research_tiktok_search_done"]
    assert len(searches) == output.summary.tiktok_search_count
    assert all(data["latency_ms"] >= 0 and data["video_count"] == 2 for data in searches)
    assert "fake_creator" not in json.dumps(events)


def test_final_parser_logs_only_locations_and_types_including_extra_keys(events: Any) -> None:
    payload = run_research().research_json
    payload["research_date"] = "SECRET_DATE"
    payload["brand"] = "SECRET_BRAND" * 30
    payload["SECRET_UNKNOWN_FIELD"] = "SECRET_VALUE"
    with pytest.raises(ValidationError):
        parse_gemini_research(payload)
    failures = [data for name, data in events if name == "gemini_research_validation_failed"]
    assert failures
    serialized = json.dumps(failures)
    assert "SECRET" not in serialized
    assert "research_date" in serialized and "brand" in serialized
    assert "string_too_long" in serialized


@pytest.mark.parametrize("raw", ["{SECRET_JSON", 123, [], '{"SECRET_KEY": "SECRET_VALUE"}'])
def test_final_parser_syntax_and_root_type_logs_do_not_echo_input(raw: Any, events: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_gemini_research(raw)
    assert events[-1][0] == "gemini_research_validation_failed"
    assert "SECRET" not in json.dumps(events)


def test_tiktok_failed_attempts_and_total_are_logged(events: Any) -> None:
    class FailedSearcher:
        def __call__(self, *_: Any, **kwargs: Any) -> Any:
            raise ValueError("SECRET_TOKEN")

    with pytest.raises(ResearchError, match="TikTok"):
        run_research(searcher=FailedSearcher())
    failures = [data for name, data in events if name == "proposal_research_tiktok_search_failed"]
    assert len(failures) == 12
    assert all(data["error_type"] == "ValueError" and data["latency_ms"] >= 0 for data in failures)
    assert events[-1][0] == "proposal_research_tiktok_failed"
    assert events[-1][1]["search_count"] == 12
    assert "SECRET" not in json.dumps(events)


def test_section_retry_cannot_adopt_another_sections_source() -> None:
    from .test_evidence_regressions import (
        MULTIPLE_SOURCES,
        MultipleSourcesChecker,
        MultipleSourcesGemini,
    )

    payload = intermediate_payload()
    # S3 belongs only to B; S1 belongs to the other sections.
    for item in payload["B_social_trend"]:
        item["url"] = "S3"
    payload["G_insight"] = {k: v.replace("[S1]", "[S4]") for k, v in payload["G_insight"].items()}
    payload["H_event"]["url"] = "S4"

    class BadB(MultipleSourcesChecker):
        def resolve_grounding_redirect(self, uri: str) -> str:
            resolved = super().resolve_grounding_redirect(uri)
            return (
                resolved + "..."
                if resolved == MULTIPLE_SOURCES["B_social_trend"]["resolved_url"]
                else resolved
            )

    gemini = MultipleSourcesGemini(
        [payload, {"B_social_trend": intermediate_payload()["B_social_trend"]}]
    )
    with pytest.raises(ResearchError, match="ソーシャルの動き"):
        run_research(gemini, BadB())
    assert len(gemini.text_calls) == 2


def test_bounded_gemini_caps_thinking_and_output_and_disables_retries(monkeypatch: Any) -> None:
    configs: list[Any] = []
    count_configs: list[Any] = []

    class Models:
        def count_tokens(self, **kwargs: Any) -> Any:
            count_configs.append(kwargs["config"])
            return SimpleNamespace(total_tokens=1000)

        def generate_content(self, **kwargs: Any) -> Any:
            configs.append(kwargs["config"])
            return SimpleNamespace(text="{}", usage_metadata=None)

    def once(fn: Any, **kwargs: Any) -> Any:
        assert kwargs["policy"].max_attempts == 1
        assert kwargs["rate_limit_policy"].max_attempts == 1
        return fn()

    monkeypatch.setattr("teamagent.adapters.gemini_client.call_with_retry", once)
    client = GeminiClient(client=SimpleNamespace(models=Models()))
    client.generate_text("補修", "req-budget", system="指示", json_mode=True, max_cost_usd=0.05)
    assert count_configs[0].system_instruction == "指示"
    assert count_configs[0].http_options.retry_options.attempts == 1
    assert configs[0].http_options.retry_options.attempts == 1
    assert 256 <= configs[0].max_output_tokens <= 8192
    assert (1000 + 1024) * 1.5 / 1_000_000 + configs[0].max_output_tokens * 9 / 1_000_000 <= 0.05


@pytest.mark.parametrize(
    "model,tokens,budget",
    [
        ("unknown-model", 1000, 0.1),
        ("gemini-3.5-flash", 100_000, 0.1),
        ("gemini-3.5-flash", None, 0.1),
        ("gemini-3.5-flash", 1000, float("nan")),
    ],
)
def test_bounded_gemini_rejects_unpriceable_or_exhausted_budget(
    model: str,
    tokens: Any,
    budget: float,
) -> None:
    client = GeminiClient(
        model_id=model,
        client=SimpleNamespace(
            models=SimpleNamespace(
                count_tokens=lambda **_: SimpleNamespace(total_tokens=tokens),
                generate_content=lambda **_: pytest.fail("must not issue a billable request"),
            )
        ),
    )
    with pytest.raises(ValueError):
        client.generate_text("補修", "req-budget", max_cost_usd=budget)


def test_required_retry_still_invalid_is_not_generated_twice() -> None:
    payload = intermediate_payload()
    for item in payload["B_social_trend"]:
        item["url"] = "S3"
    gemini = TwoUsableInB([payload, {"B_social_trend": payload["B_social_trend"]}])
    with pytest.raises(ResearchError, match="ソーシャルの動き"):
        run_research(gemini, BadSecondSource())
    assert len(gemini.text_calls) == 2


def test_multiple_required_sections_reserve_at_most_thirty_cents(monkeypatch: Any) -> None:
    materialize = skill_module._materialize
    materializations = 0

    def break_first(*args: Any) -> Any:
        nonlocal materializations
        data, discarded, used = materialize(*args)
        materializations += 1
        if materializations == 1:
            for section, field in [
                ("A_market_data", "url"),
                ("B_social_trend", "url"),
                ("D_publicity", "evidence_url"),
                ("E_community", "data_url"),
            ]:
                data[section][0][field] = "https://source.example/truncated..."
        return data, discarded, used

    monkeypatch.setattr(skill_module, "_materialize", break_first)
    gemini = FakeGemini(
        [intermediate_payload()]
        + [
            {section: intermediate_payload()[section]}
            for section in ("A_market_data", "B_social_trend", "D_publicity")
        ]
    )
    with pytest.raises(ResearchError, match="界隈"):
        run_research(gemini)
    assert len(gemini.text_calls) == 4
    assert sum(call["max_cost_usd"] for call in gemini.text_calls[1:]) == pytest.approx(0.30)
    assert sum(gemini.grounding_calls.values()) == 6


@pytest.mark.parametrize(
    "section,field,invalid",
    [
        ("A_market_data", "alt_data", None),
        ("E_community", "tiktok_tags", [{"tag": "x"}]),
        ("D_publicity", "recommended_media", [123, "残す媒体", None]),
    ],
)
def test_optional_type_or_missing_child_drops_only_broken_supplement(
    monkeypatch: Any,
    section: str,
    field: str,
    invalid: Any,
) -> None:
    materialize = skill_module._materialize

    def break_optional(*args: Any) -> Any:
        data, discarded, used = materialize(*args)
        data[section][0][field] = invalid
        return data, discarded, used

    monkeypatch.setattr(skill_module, "_materialize", break_optional)
    # E tags are assigned by the scraper after materialization; break only final input.
    if section == "E_community":
        fill = skill_module.ProposalResearchSkill._fill_tiktok

        def break_tags(self: Any, data: Any, *args: Any) -> int:
            count = fill(self, data, *args)
            data[section][0][field] = invalid
            return count

        monkeypatch.setattr(skill_module.ProposalResearchSkill, "_fill_tiktok", break_tags)
    gemini = FakeGemini()
    output = run_research(gemini)
    expected = ["残す媒体"] if section == "D_publicity" else []
    assert output.research_json[section][0][field] == expected
    assert output.summary.review_required
    assert len(gemini.text_calls) == 1


def test_nfkc_expansion_of_authoritative_brand_is_reported_without_truncation() -> None:
    # ResearchBrief preserves symbols; final NFKC expands this 100-character name to 400.
    brief = ResearchBrief(product_name="㍿" * 100)
    gemini = FakeGemini([intermediate_payload(), {"brand": "勝手な短縮名"}])
    with pytest.raises(ResearchError, match="商材名"):
        run_research(gemini, brief=brief)
    assert len(gemini.text_calls) == 2


def test_optional_review_survives_builder_and_keeps_proposal_draft(
    monkeypatch: Any, tmp_path: Any
) -> None:
    from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput, ResearchIssue
    from teamagent.skills.proposal_builder.skill import ProposalBuilderStatusSkill
    from tests.skills.proposal_builder import test_research_delivery_recovery as integration

    result = integration._result()
    result.summary.review_required = [
        ResearchIssue(
            path="$.B_social_trend[0].alt_data[0]",
            code="invalid_optional_field",
            message="ソーシャルの動きの補足を除外（形式・出典の確認が必要）",
        )
    ]
    monkeypatch.setattr(integration, "_result", lambda: result)
    job_id, ctx, _, _, store, _ = integration._build_job(
        monkeypatch,
        tmp_path,
        deferred=False,
        ready=True,
    )
    state = ProposalBuilderStatusSkill(store=store).run(
        ProposalBuilderStatusInput(job_id=job_id), ctx
    )
    assert state.status == "done" and state.proposal_status == "draft"
    assert "要確認" in state.result_message and "ソーシャルの動き" in state.result_message
    assert any(
        "invalid_optional_field:$.B_social_trend" in issue for issue in state.verification_issues
    )
