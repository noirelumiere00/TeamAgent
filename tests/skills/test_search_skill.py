"""SearchSkill の happy path テスト。

embedder と adapters をモックして3層分離が機能することを確認する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill


@pytest.fixture
def fake_bedrock() -> MagicMock:
    """Bedrock のモック。常に同じテキストを返す。"""
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="提案書では業界別に PR 代行実績が記載されています [chunk_id: 1]",
        usage=TokenUsage(
            input_tokens=200,
            output_tokens=80,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0018,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=300,
        stop_reason="end_turn",
    )
    return mock


@pytest.fixture
def fake_pgvector() -> MagicMock:
    """pgvector のモック。固定の SearchHit を返す。"""
    mock = MagicMock()
    # connection() はコンテキストマネージャ
    cm_mock = MagicMock()
    cm_mock.__enter__ = MagicMock(return_value=MagicMock())
    cm_mock.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm_mock

    mock.search_similar.return_value = [
        SearchHit(
            chunk_id=1,
            content="PR代行は飲食・コスメ・教育で実績あり",
            score=0.91,
            metadata={"source": "proposal_2024_drink.pdf", "industry": "飲食"},
        ),
        SearchHit(
            chunk_id=2,
            content="化粧品業界向けの提案テンプレートあり",
            score=0.84,
            metadata={"source": "proposal_cosme.pdf", "industry": "コスメ"},
        ),
    ]
    return mock


class FakeEmbedder:
    """1024次元のダミー埋め込みを返すスタブ。"""

    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def test_search_happy_path(fake_bedrock: MagicMock, fake_pgvector: MagicMock) -> None:
    """SearchSkill が SearchOutput を返すこと、コストが集計されること。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
    )
    ctx = SkillContext()
    out = skill.run(
        input=SearchInput(query="PR代行の業界別実績は？", top_k=2),
        ctx=ctx,
    )

    assert "業界別" in out.answer
    assert len(out.hits) == 2
    assert out.hits[0].chunk_id == 1
    assert out.hits[0].score == pytest.approx(0.91)
    assert out.hits[0].source == "proposal_2024_drink.pdf"
    assert out.total_cost_usd == pytest.approx(0.0018)


def test_search_zero_hits_skips_bedrock(fake_bedrock: MagicMock, fake_pgvector: MagicMock) -> None:
    """ヒット 0 件のとき Bedrock を呼ばずスキップする。"""
    fake_pgvector.search_similar.return_value = []

    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
    )
    out = skill.run(
        input=SearchInput(query="該当なし", top_k=5),
        ctx=SkillContext(),
    )

    assert out.hits == []
    assert out.total_cost_usd == 0.0
    assert "見つかりません" in out.answer
    fake_bedrock.converse.assert_not_called()


def test_search_filter_industry_added_to_where(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """filter_industry を指定したとき、metadata 列がある場合のみ WHERE 句が pgvector に渡る。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
        content_col="content",
        metadata_col="metadata",
    )
    skill.run(
        input=SearchInput(query="飲食事例", top_k=3, filter_industry="飲食"),
        ctx=SkillContext(),
    )

    call_kwargs: dict[str, Any] = fake_pgvector.search_similar.call_args.kwargs
    assert call_kwargs["where"] == "metadata->>'industry' = '飲食'"
    assert call_kwargs["limit"] == 3
    assert call_kwargs["metadata_col"] == "metadata"
    assert call_kwargs["content_col"] == "content"


def test_search_filter_industry_ignored_without_metadata_col(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """metadata 列を持たないテーブルでは filter_industry が無視されること。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposals_chunks",
        content_col="text",
        metadata_col=None,
    )
    skill.run(
        input=SearchInput(query="飲食事例", top_k=3, filter_industry="飲食"),
        ctx=SkillContext(),
    )
    call_kwargs: dict[str, Any] = fake_pgvector.search_similar.call_args.kwargs
    assert call_kwargs["where"] is None
    assert call_kwargs["content_col"] == "text"
    assert call_kwargs["metadata_col"] is None
