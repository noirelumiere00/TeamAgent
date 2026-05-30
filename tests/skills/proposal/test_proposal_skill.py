"""ProposalDraftSkill の単体テスト (SearchSkill.retrieve_hits / bedrock をモック)。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal.schema import ProposalDraftInput
from teamagent.skills.proposal.skill import ProposalDraftSkill


@pytest.fixture
def fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="### 1. 提案の方向性\nUGC拡散で認知→来店 [chunk_id: 1]",
        usage=TokenUsage(
            input_tokens=400,
            output_tokens=200,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.008,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=500,
        stop_reason="end_turn",
    )
    return mock


def _hits() -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=1,
            content="グルメインフルエンサー×グルメメディアで来店動線を作った提案",
            score=0.71,
            metadata={"source_type": "gdrive", "title": "施策②グルメ", "client_name": None},
        ),
        SearchHit(
            chunk_id=2,
            content="ナノインフルエンサーUGCで自然拡散",
            score=0.55,
            metadata={"source_type": "slack", "client_name": "阪急阪神"},
        ),
    ]


def test_proposal_generates_draft_from_similar(fake_bedrock: MagicMock) -> None:
    search = MagicMock()
    search.retrieve_hits.return_value = _hits()

    skill = ProposalDraftSkill(search=search, bedrock=fake_bedrock)
    out = skill.run(
        ProposalDraftInput(brief="飲食チェーンのTikTok集客", top_k=8), ctx=SkillContext()
    )

    assert out.source_count == 2
    assert "提案の方向性" in out.draft
    assert out.total_cost_usd == pytest.approx(0.008)
    assert out.sources[0].chunk_id == 1
    assert out.sources[1].client_name == "阪急阪神"
    # 検索基盤に brief / top_k / industry が渡る
    kwargs = search.retrieve_hits.call_args.kwargs
    assert kwargs["top_k"] == 8


def test_proposal_passes_industry_filter(fake_bedrock: MagicMock) -> None:
    search = MagicMock()
    search.retrieve_hits.return_value = _hits()
    skill = ProposalDraftSkill(search=search, bedrock=fake_bedrock)
    skill.run(ProposalDraftInput(brief="コスメのPR", industry="化粧品"), ctx=SkillContext())
    kwargs = search.retrieve_hits.call_args.kwargs
    assert kwargs["filter_industry"] == "化粧品"


def test_proposal_no_hits_skips_bedrock(fake_bedrock: MagicMock) -> None:
    """類似提案 0 件なら Bedrock を呼ばず案内文を返す。"""
    search = MagicMock()
    search.retrieve_hits.return_value = []
    skill = ProposalDraftSkill(search=search, bedrock=fake_bedrock)
    out = skill.run(ProposalDraftInput(brief="前例のない新規業態"), ctx=SkillContext())

    assert out.source_count == 0
    assert out.total_cost_usd == 0.0
    assert "見つかりません" in out.draft
    fake_bedrock.converse.assert_not_called()
