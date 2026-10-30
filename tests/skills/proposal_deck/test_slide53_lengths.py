"""統合FMT 53枚目の境界と自己修正。機密PPTXを使わないフェイクのみ。"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_deck.contract import LENGTH_RULES, VALID_IDS, ComposerOutput
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill

# 実装のLENGTH_RULESから期待値を作らず、23枠全ての規則欠落・80への退行を検出する。
EXPECTED_LIMITS = {
    61: 18,
    62: 30,
    63: 18,
    64: 30,
    66: 18,
    67: 30,
    68: 18,
    69: 30,
    73: 18,
    74: 30,
    75: 18,
    76: 30,
    78: 18,
    79: 30,
    80: 18,
    81: 30,
    85: 18,
    86: 30,
    87: 18,
    88: 30,
    90: 18,
    91: 30,
    92: 18,
}


def _placeholders() -> dict[int, str]:
    return {pid: "あ" * LENGTH_RULES.get(pid, (1, 1))[0] for pid in VALID_IDS}


@pytest.mark.parametrize(("pid", "hi"), EXPECTED_LIMITS.items())
@pytest.mark.parametrize("at_limit", [False, True])
def test_accepts_one_character_and_upper_boundary(pid: int, hi: int, at_limit: bool) -> None:
    placeholders = _placeholders()
    placeholders[pid] = "あ" * (hi if at_limit else 1)
    assert ComposerOutput(placeholders=placeholders).placeholders == placeholders


@pytest.mark.parametrize(("pid", "hi"), EXPECTED_LIMITS.items())
def test_rejects_one_over_limit_with_id_and_upper_bound(pid: int, hi: int) -> None:
    placeholders = _placeholders()
    placeholders[pid] = "あ" * (hi + 1)
    with pytest.raises(ValidationError) as caught:
        ComposerOutput(placeholders=placeholders)
    assert f"{{{pid}}} length {hi + 1} out of [1, {hi}]" in str(caught.value)


def test_all_23_violations_are_reported_together() -> None:
    placeholders = _placeholders()
    placeholders.update({pid: "あ" * (hi + 1) for pid, hi in EXPECTED_LIMITS.items()})
    with pytest.raises(ValidationError) as caught:
        ComposerOutput(placeholders=placeholders)
    for pid, hi in EXPECTED_LIMITS.items():
        assert f"{{{pid}}} length {hi + 1} out of [1, {hi}]" in str(caught.value)


def test_other_slide53_rules_are_preserved() -> None:
    for pid in (60, 65, 72, 77, 84, 89):
        assert LENGTH_RULES[pid] == (1, 28)
    for pid in (58, 59, 70, 71, 82, 83):
        assert LENGTH_RULES[pid] == (1, 18)
    for pid in (7, 9, 11):
        assert LENGTH_RULES[pid] == (1, 20)
    assert LENGTH_RULES[47] == (1, 60)


def _response(placeholders: dict[int, str], *, skipped_id: int | None = None) -> ConverseResponse:
    return ConverseResponse(
        text=json.dumps(
            {
                "placeholders": placeholders,
                "skipped_placeholders": (
                    [{"id": skipped_id, "reason": "要確認（データ未検出）"}]
                    if skipped_id is not None
                    else []
                ),
            },
            ensure_ascii=False,
        ),
        usage=TokenUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.01,
        ),
        model_id="fake",
        latency_ms=0,
        stop_reason="end_turn",
    )


def test_self_repair_only_changes_bad_slots_and_receives_limits() -> None:
    invalid = _placeholders()
    invalid[64] = "あ" * 31
    invalid[61] = "あ" * 19
    repaired = {64: "あ" * 30, 61: "あ" * 18, 1: "勝手な書き換え"}
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.side_effect = [_response(invalid), _response(repaired)]
    skill = ProposalDeckSkill(bedrock=bedrock)
    output, cost, warnings = skill._compose(
        ProposalDeckInput(product_name="テスト", goal="認知", target_persona="成人", max_repair=1),
        SkillContext(),
    )
    assert bedrock.converse.call_count == 2
    repair_call = bedrock.converse.call_args.kwargs
    assert len(repair_call["messages"]) == 1
    repair_text = repair_call["messages"][0]["content"][0]["text"]
    assert "{64} length 31 out of [1, 30]" in repair_text
    assert "{61} length 19 out of [1, 18]" in repair_text
    assert "{2}" not in repair_text
    assert repair_call["max_tokens"] == 2048
    assert cost == pytest.approx(0.02)
    assert output.placeholders == {**invalid, 64: repaired[64], 61: repaired[61]}
    assert len(warnings) == 2 and all("再生成で修正" in line for line in warnings)


@pytest.mark.parametrize("max_repair,expected_calls", [(0, 1), (1, 2), (2, 3), (4, 5)])
def test_exhausted_length_repair_completes_with_safe_text(
    max_repair: int, expected_calls: int
) -> None:
    placeholders = _placeholders()
    placeholders[64] = "あ" * 31
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.return_value = _response(placeholders)
    output, cost, warnings = ProposalDeckSkill(bedrock=bedrock)._compose(
        ProposalDeckInput(
            product_name="テスト", goal="認知", target_persona="成人", max_repair=max_repair
        ),
        SkillContext(),
    )
    assert bedrock.converse.call_count == expected_calls
    assert output.placeholders[64] == "あ" * 30
    assert output.placeholder_warnings[64] == warnings[0]
    assert len(warnings) == 1 and "{64}" in warnings[0] and "切り詰め" in warnings[0]
    assert cost == pytest.approx(0.01 * expected_calls)


@pytest.mark.parametrize("budget,calls", [(0.0, 1), (0.04, 2)])
def test_repair_cost_is_reserved_before_each_call(
    monkeypatch: pytest.MonkeyPatch, budget: float, calls: int
) -> None:
    from teamagent.skills.proposal_deck import skill as module

    monkeypatch.setattr(module, "_LENGTH_REPAIR_BUDGET_USD", budget)
    placeholders = _placeholders()
    placeholders[87] = "あ" * 21
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.return_value = _response(placeholders)
    output, cost, warnings = ProposalDeckSkill(bedrock=bedrock)._compose(
        ProposalDeckInput(product_name="テスト", goal="認知", target_persona="成人"), SkillContext()
    )
    assert bedrock.converse.call_count == calls
    assert cost == pytest.approx(0.01 * calls)
    assert output.placeholders[87] == "あ" * 18
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "reply", ["not-json", "[]", '{"placeholders": []}', '{"placeholders": {"87": ""}}']
)
def test_malformed_patch_falls_back_without_losing_valid_slots(reply: str) -> None:
    placeholders = _placeholders()
    placeholders[87] = "あ" * 21
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.side_effect = [
        _response(placeholders),
        replace(_response({}), text=reply),
    ]
    output, _, _ = ProposalDeckSkill(bedrock=bedrock)._compose(
        ProposalDeckInput(product_name="テスト", goal="認知", target_persona="成人", max_repair=1),
        SkillContext(),
    )
    assert output.placeholders == {**placeholders, 87: "あ" * 18}


@pytest.mark.parametrize("model", ["unknown-model", "jp.anthropic.claude-sonnet-4-6"])
def test_unknown_price_or_repair_outage_uses_safe_text(model: str) -> None:
    placeholders = _placeholders()
    placeholders[2] = "あ" * 141
    bedrock = MagicMock(model_id=model)
    bedrock.converse.side_effect = [_response(placeholders), RuntimeError("service unavailable")]
    output, cost, _ = ProposalDeckSkill(bedrock=bedrock)._compose(
        ProposalDeckInput(product_name="テスト", goal="認知", target_persona="成人"), SkillContext()
    )
    assert bedrock.converse.call_count == (1 if model == "unknown-model" else 2)
    assert cost == pytest.approx(0.01)
    assert output.placeholders[2] == "あ" * 140


@pytest.mark.parametrize("replacement", ["10万人", "PRIVATE-BRAND"])
def test_length_patch_still_passes_provenance_and_confidentiality_checks(replacement: str) -> None:
    from teamagent.skills.proposal_deck.provenance import ProvenanceValidationError

    data = {"placeholders": {str(pid): text for pid, text in _placeholders().items()}}
    data["placeholders"]["87"] = "あ" * 21
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.return_value = _response({87: replacement})
    skill = ProposalDeckSkill(bedrock=bedrock)
    input = ProposalDeckInput(
        product_name="テスト",
        goal="認知",
        target_persona="成人",
        max_repair=1,
        enforce_provenance=True,
        forbidden_output_terms=["PRIVATE-BRAND"],
    )
    skill._repair_lengths(data, input, SkillContext(), spent=0)
    with pytest.raises(ProvenanceValidationError):
        skill._finalize_composer(data, input)


def test_second_patch_only_requests_the_remaining_bad_slot() -> None:
    placeholders = _placeholders()
    placeholders.update({87: "あ" * 21, 90: "い" * 23})
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.side_effect = [
        _response(placeholders),
        _response({87: "短い訴求", 90: "い" * 20}),
        _response({87: "変更禁止", 90: "短い訴求の続き"}),
    ]
    output, cost, warnings = ProposalDeckSkill(bedrock=bedrock)._compose(
        ProposalDeckInput(product_name="テスト", goal="認知", target_persona="成人"), SkillContext()
    )
    assert output.placeholders == {**placeholders, 87: "短い訴求", 90: "短い訴求の続き"}
    second_prompt = bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "{87}" not in second_prompt
    assert "{90} length 20 out of [1, 18]" in second_prompt
    assert cost == pytest.approx(0.03) and len(warnings) == 2


def test_budget_is_shared_across_other_contract_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.skills.proposal_deck import skill as module
    from teamagent.skills.proposal_deck.provenance import ProvenanceValidationError

    monkeypatch.setattr(module, "_LENGTH_REPAIR_BUDGET_USD", 0.04)
    placeholders = _placeholders()
    placeholders[87] = "あ" * 21
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.return_value = _response(placeholders)
    skill = ProposalDeckSkill(bedrock=bedrock)
    finalize = skill._finalize_composer
    calls = 0

    def fail_once(data, input):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProvenanceValidationError(["confidential term remains"])
        return finalize(data, input)

    monkeypatch.setattr(skill, "_finalize_composer", fail_once)
    output, cost, _ = skill._compose(
        ProposalDeckInput(product_name="テスト", goal="認知", target_persona="成人", max_repair=1),
        SkillContext(),
    )
    assert bedrock.converse.call_count == 3  # 全体2回 + 予算内の局所修復1回
    assert cost == pytest.approx(0.03)
    assert output.placeholders[87] == "あ" * 18


def test_clipped_body_keeps_its_original_citation() -> None:
    url = "https://example.com/evidence"
    placeholders = _placeholders()
    original = "口コミ10万件を参考にする利用場面" + "あ" * 10
    placeholders[87] = original
    data = {
        "placeholders": {str(pid): text for pid, text in placeholders.items()},
        "citations_per_placeholder": {"87": [url]},
    }
    request = ProposalDeckInput(
        product_name="合成商材",
        goal="認知",
        target_persona="成人",
        max_repair=0,
        enforce_provenance=True,
        urls=[url],
        quantitative_evidence={"10万件": [url]},
    )
    skill = ProposalDeckSkill(bedrock=MagicMock(model_id="unknown-model"))
    skill._repair_lengths(data, request, SkillContext(), spent=0)
    output = skill._finalize_composer(data, request)
    assert output.placeholders[87] == original[:18]
    assert output.citations_per_placeholder[87] == [url]
