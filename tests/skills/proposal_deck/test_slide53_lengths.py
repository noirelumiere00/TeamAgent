"""統合FMT 53枚目の境界と自己修正。機密PPTXを使わないフェイクのみ。"""

from __future__ import annotations

import json
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
    for pid in (60, 70, 71, 77, 82, 84, 89):
        assert LENGTH_RULES[pid] == (1, 80)
    for pid in (58, 59, 65, 72, 83):
        assert pid not in LENGTH_RULES


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


@pytest.mark.parametrize("skip", [False, True])
def test_self_repair_receives_limits_and_can_shorten_or_explicitly_skip(skip: bool) -> None:
    invalid = _placeholders()
    invalid[64] = "あ" * 31
    invalid[61] = "あ" * 19
    repaired = _placeholders()
    repaired[64] = "あ" * 30
    repaired[61] = "あ" * 18
    if skip:
        del repaired[64]
    bedrock = MagicMock()
    bedrock.converse.side_effect = [
        _response(invalid),
        _response(repaired, skipped_id=64 if skip else None),
    ]
    skill = ProposalDeckSkill(bedrock=bedrock)
    output, cost = skill._compose(
        ProposalDeckInput(product_name="テスト", goal="認知", target_persona="成人", max_repair=1),
        SkillContext(),
    )
    assert bedrock.converse.call_count == 2
    repair_text = bedrock.converse.call_args.kwargs["messages"][-1]["content"][0]["text"]
    assert "{64} length 31 out of [1, 30]" in repair_text
    assert "{61} length 19 out of [1, 18]" in repair_text
    assert cost == pytest.approx(0.02)
    assert output.placeholders == repaired
    assert [item.id for item in output.skipped_placeholders] == ([64] if skip else [])


def test_exhausted_length_repair_still_fails_without_automatic_skip() -> None:
    placeholders = _placeholders()
    placeholders[64] = "あ" * 31
    bedrock = MagicMock()
    bedrock.converse.return_value = _response(placeholders)
    skill = ProposalDeckSkill(bedrock=bedrock)
    with pytest.raises(ValueError, match="compose failed after 3 attempts") as caught:
        skill._compose(
            ProposalDeckInput(
                product_name="テスト", goal="認知", target_persona="成人", max_repair=2
            ),
            SkillContext(),
        )
    assert "{64} length 31 out of [1, 30]" in str(caught.value)
    assert bedrock.converse.call_count == 3
