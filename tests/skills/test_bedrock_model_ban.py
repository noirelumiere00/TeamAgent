"""本番と同じ用途別 Sonnet env から、実際の分析・資料原稿を Haiku で生成する。"""

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import DEFAULT_MODEL_ID
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.skill import ProposalBuilderSkill
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.x_research.schema import XNeedsMiningInput
from teamagent.skills.x_research.skill import XNeedsMiningSkill
from tests.adapters.test_bedrock_client import _fake_bedrock_response
from tests.adapters.test_bedrock_model_ban import SONNET_MODELS
from tests.adapters.test_bedrock_model_ban import runtime as bedrock_runtime
from tests.skills.proposal_deck.test_proposal_deck_skill import _full_composer_json
from tests.skills.x_research.test_x_research_skills import _ctx, _FakeApify, _post, _publisher

runtime = bedrock_runtime


@pytest.mark.parametrize("model_id", [*SONNET_MODELS, DEFAULT_MODEL_ID])
def test_x_analysis_env_sonnet_uses_haiku(
    monkeypatch: pytest.MonkeyPatch, runtime: MagicMock, model_id: str
) -> None:
    monkeypatch.setenv("X_ANALYSIS_MODEL_ID", model_id)
    monkeypatch.setenv("X_RESEARCH_ALLOWED_EMAILS", "a@vectorinc.co.jp")
    response = _fake_bedrock_response()
    response["output"]["message"]["content"][0]["text"] = (
        '{"clusters": [{"label": "ニーズ", "post_ids": ["1"], "insight": "洞察"}],'
        '"hypothesis_summary": "仮説"}'
    )
    runtime.converse.return_value = response
    skill = XNeedsMiningSkill(apify=_FakeApify([_post("1")]), publisher=_publisher)
    out = skill.run(XNeedsMiningInput(theme="白湯", emotion_words=["好き"]), _ctx())
    assert out.hypothesis_summary == "仮説"
    assert out.clusters[0].label == "ニーズ"
    assert runtime.converse.call_count == 1
    assert runtime.converse.call_args.kwargs["modelId"] == DEFAULT_MODEL_ID
    assert runtime.converse.call_args.kwargs["inferenceConfig"]["maxTokens"] == 3072


@pytest.mark.parametrize("model_id", [*SONNET_MODELS, DEFAULT_MODEL_ID, None])
def test_proposal_builder_env_sonnet_uses_haiku(
    monkeypatch: pytest.MonkeyPatch, runtime: MagicMock, model_id: str | None
) -> None:
    if model_id is None:
        monkeypatch.delenv("PROPOSAL_BUILDER_MODEL_ID", raising=False)
        monkeypatch.delenv("BEDROCK_MODEL_ID")
    else:
        monkeypatch.setenv("PROPOSAL_BUILDER_MODEL_ID", model_id)
    monkeypatch.setenv("PROPOSAL_BUILDER_MAX_TOKENS", "999999")
    response = _fake_bedrock_response()
    response["output"]["message"]["content"][0]["text"] = _full_composer_json()
    runtime.converse.return_value = response
    deck = ProposalBuilderSkill._build_deck()
    composed, cost, warnings = deck._compose(
        ProposalDeckInput(product_name="白湯", goal="認知", target_persona="20代"),
        SkillContext(),
    )
    assert len(composed.placeholders) == 95
    assert cost > 0
    assert not warnings
    assert runtime.converse.call_count == 1
    assert runtime.converse.call_args.kwargs["modelId"] == DEFAULT_MODEL_ID
    assert runtime.converse.call_args.kwargs["inferenceConfig"]["maxTokens"] == 32_000
