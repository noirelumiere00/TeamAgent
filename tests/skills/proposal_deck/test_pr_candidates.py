"""元47頁の理由付き他候補。外部モデルは呼ばない。"""

import json
from unittest.mock import MagicMock

import pytest

from teamagent.media.proposal_template import BASE_AUXILIARY_KEYS
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_deck.contract import LENGTH_RULES
from teamagent.skills.proposal_deck.provenance import ProvenanceValidationError
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill
from tests.skills.proposal_deck.test_proposal_deck_skill import _full_composer_json


@pytest.mark.parametrize("count", [3, 4, 5, 7])
def test_fallback_retains_names_and_reasons_per_line(count: int) -> None:
    data = json.loads(_full_composer_json())
    names = [f"合成案{chr(65 + index)}" for index in range(count)]
    data["placeholders"]["47"] = "\n".join(
        f"①「{name}」—— " + "生活者の迷いに寄り添い商品の使い方を想像しやすくする" * 5
        for name in names
    )
    bedrock = MagicMock(model_id="unknown-model")
    _, warnings = ProposalDeckSkill(bedrock=bedrock)._repair_lengths(
        data,
        ProposalDeckInput(
            product_name="合成商材", goal="認知", target_persona="成人", max_repair=0
        ),
        SkillContext(),
        spent=0,
    )
    lines = data["placeholders"]["47"].splitlines()
    assert LENGTH_RULES[47] == (1, 404)  # 80字×5行＋改行4個
    assert len(lines) == min(count, 5)
    for line, name in zip(lines, names, strict=False):
        assert line.startswith(f"「{name}」— 生活者の迷いに寄り添い")
        assert len(line) == 80 and line.endswith("…")
    assert warnings and "{47}" in warnings[0]
    bedrock.converse.assert_not_called()


def test_per_line_limit_triggers_repair_even_when_total_is_under_limit() -> None:
    data = json.loads(_full_composer_json())
    data["placeholders"]["47"] = (
        "「案Ａ」— " + "あ" * 90 + "\n「案Ｂ」— 日常に結びつく。\n「案Ｃ」— 気持ちを言葉にする。"
    )
    assert len(data["placeholders"]["47"]) < 404
    skill = ProposalDeckSkill(bedrock=MagicMock(model_id="unknown-model"))
    _, warnings = skill._repair_lengths(
        data,
        ProposalDeckInput(
            product_name="合成商材", goal="認知", target_persona="成人", max_repair=0
        ),
        SkillContext(),
        spent=0,
    )
    assert warnings and len(data["placeholders"]["47"].splitlines()[0]) == 80
    assert data["placeholders"]["47"].splitlines()[0].endswith("…")


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
@pytest.mark.parametrize("with_reasons", [False, True])
def test_integrated_candidates_require_three_to_five_reasoned_lines(
    count: int, with_reasons: bool
) -> None:
    data = json.loads(_full_composer_json())
    data["placeholders"]["47"] = "\n".join(
        f"「合成案{chr(65 + index)}」" + ("— 生活場面を想像しやすくする。" if with_reasons else "")
        for index in range(count)
    )
    request = ProposalDeckInput(
        product_name="合成商材",
        goal="認知",
        target_persona="成人",
        template_profile="proposal-builder-v1",
        auxiliary_placeholders={key: "設定値" for key in BASE_AUXILIARY_KEYS},
    )
    if with_reasons and 3 <= count <= 5:
        assert ProposalDeckSkill._finalize_composer(data, request).placeholders[47]
    else:
        with pytest.raises((ProvenanceValidationError, ValueError)):
            ProposalDeckSkill._finalize_composer(data, request)


def test_long_quoted_name_leaves_room_for_reason() -> None:
    from teamagent.skills.proposal_deck.skill import _fit_pr_candidates

    source = "\n".join(
        f"「{chr(65 + i)}" + "名" * 40 + "」—— " + "迷いに寄り添う理由" * 20 for i in range(3)
    )
    lines = _fit_pr_candidates(source).splitlines()
    assert len(lines) == 3
    assert all(len(line) == 80 and "…」— 迷いに寄り添う理由" in line for line in lines)
    assert all(line.endswith("…") for line in lines)


def test_local_repair_prompt_and_patch_keep_reasons() -> None:
    from tests.skills.proposal_deck.test_proposal_deck_skill import _resp

    data = json.loads(_full_composer_json())
    data["placeholders"]["47"] = "\n".join(
        f"「合成案{chr(65 + i)}」— " + "理由" * 50 for i in range(3)
    )
    repaired = "\n".join(f"「合成案{chr(65 + i)}」— 使う場面を想像しやすくする。" for i in range(3))
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.side_effect = [
        _resp(json.dumps(data)),
        _resp(json.dumps({"placeholders": {"47": repaired}})),
    ]
    output, _, warnings = ProposalDeckSkill(bedrock=bedrock)._compose(
        ProposalDeckInput(
            product_name="合成商材", goal="認知", target_persona="成人", max_repair=1
        ),
        SkillContext(),
    )
    assert output.placeholders[47] == repaired
    prompt = bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "各行80字以内" in prompt and "理由を省略しない" in prompt
    assert warnings and "再生成で修正" in warnings[0]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_prompt_matches_reasoned_candidate_limits(version: str) -> None:
    from teamagent.prompts.loader import load_prompt

    prompt = load_prompt("proposal_deck", version, "system")
    assert "3〜5案" in prompt and "1案1行" in prompt
    assert "各行80字以内" in prompt and "全体404字以内" in prompt
    assert "理由を省略しない" in prompt and "250〜450" in prompt
    assert "500 文字程度" not in prompt


def test_fallback_does_not_treat_separator_as_a_missing_reason() -> None:
    from teamagent.skills.proposal_deck.skill import _fit_pr_candidates

    text = "\n".join(f"①「合成案{chr(65 + i)}」—— " for i in range(3))
    assert _fit_pr_candidates(text) == text


@pytest.mark.parametrize("length,accepted", [(249, False), (250, True), (450, True), (451, False)])
def test_background_length_uses_keynote_verified_limits(length: int, accepted: bool) -> None:
    from pydantic import ValidationError

    from teamagent.skills.proposal_deck.contract import ComposerOutput

    data = json.loads(_full_composer_json())
    data["placeholders"]["56"] = "背" * length
    if accepted:
        assert ComposerOutput.model_validate(data).placeholders[56] == "背" * length
    else:
        with pytest.raises(ValidationError, match=r"\{56\} length"):
            ComposerOutput.model_validate(data)
