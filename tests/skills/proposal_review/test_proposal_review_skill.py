"""ProposalReviewSkill の単体テスト (SearchSkill.retrieve_hits / bedrock をモック)。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_review.schema import ProposalReviewInput
from teamagent.skills.proposal_review.skill import ProposalReviewSkill


@pytest.fixture
def fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="### 1. 総評\n訴求は強いが失注理由への備えが薄い [chunk_id: 9]",
        usage=TokenUsage(
            input_tokens=600,
            output_tokens=250,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.009,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=600,
        stop_reason="end_turn",
    )
    return mock


def _hits() -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=9,
            content="失注理由: テレビCM予算との競合で見送り",
            score=0.7,
            metadata={"source_type": "slack", "client_name": "東芝"},
        ),
    ]


def test_review_generates_with_grounding(fake_bedrock: MagicMock) -> None:
    search = MagicMock()
    search.retrieve_hits.return_value = _hits()
    skill = ProposalReviewSkill(search=search, bedrock=fake_bedrock)

    out = skill.run(
        ProposalReviewInput(proposal_text="飲食チェーン向けTikTok提案。訴求はUGC。"),
        ctx=SkillContext(),
    )
    assert out.source_count == 1
    assert "総評" in out.review
    assert out.total_cost_usd == pytest.approx(0.009)
    assert out.sources[0].client_name == "東芝"
    # 提案文の先頭がクエリとして検索に渡る
    search.retrieve_hits.assert_called_once()


def test_review_works_without_past_examples(fake_bedrock: MagicMock) -> None:
    """類似事例 0 件でもレビュー自体は実行する (一般原則で診断)。"""
    search = MagicMock()
    search.retrieve_hits.return_value = []
    skill = ProposalReviewSkill(search=search, bedrock=fake_bedrock)

    out = skill.run(
        ProposalReviewInput(proposal_text="前例のない新規業態への提案"),
        ctx=SkillContext(),
    )
    assert out.source_count == 0
    # 0 件でも Bedrock は呼ばれる (レビューは生成する)
    fake_bedrock.converse.assert_called_once()
    assert out.total_cost_usd == pytest.approx(0.009)


def test_review_passes_industry_filter(fake_bedrock: MagicMock) -> None:
    search = MagicMock()
    search.retrieve_hits.return_value = _hits()
    skill = ProposalReviewSkill(search=search, bedrock=fake_bedrock)
    skill.run(
        ProposalReviewInput(proposal_text="コスメ提案", industry="化粧品"),
        ctx=SkillContext(),
    )
    kwargs = search.retrieve_hits.call_args.kwargs
    assert kwargs["filter_industry"] == "化粧品"
