"""SearchInput.include_answer（二段レスポンス #1）のテスト。

- 既定 True: 全既存呼び出し元と完全後方互換（要約が従来どおり生成される）
- False: _summarize（Bedrock converse）を一切呼ばず answer='' / cost 0 で hits を即返す
- False + 0 件: 0 件メッセージ（要約側の文言）も出さない

実 DB 0・実 Bedrock 0（tests/skills/test_search_skill.py と同じモック作法）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill


@pytest.fixture
def fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="要約テキスト [chunk_id: 1]",
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
    mock = MagicMock()
    cm_mock = MagicMock()
    cm_mock.__enter__ = MagicMock(return_value=MagicMock())
    cm_mock.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm_mock
    mock.search_similar.return_value = [
        SearchHit(
            chunk_id=1,
            content="PR代行は飲食・コスメ・教育で実績あり",
            score=0.91,
            metadata={"source": "proposal_2024_drink.pdf", "title": "飲料提案"},
        ),
    ]
    return mock


class FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _skill(fake_bedrock: MagicMock, fake_pgvector: MagicMock) -> SearchSkill:
    return SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
    )


def test_include_answer_defaults_true_in_schema() -> None:
    """既存のあらゆる SearchInput(...) 構築は include_answer=True になる（後方互換）。"""
    assert SearchInput(query="x").include_answer is True


def test_default_true_generates_answer_backward_compatible(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """include_answer 未指定（既定 True）では従来どおり Bedrock 要約が生成される。"""
    skill = _skill(fake_bedrock, fake_pgvector)
    out = skill.run(input=SearchInput(query="PR代行の実績"), ctx=SkillContext())

    fake_bedrock.converse.assert_called_once()
    # _strip_internal_markers が [chunk_id: N] を除去した本文が answer になる（従来どおり）
    assert out.answer == "要約テキスト"
    assert out.total_cost_usd == pytest.approx(0.0018)
    assert len(out.hits) == 1


def test_false_skips_summarize_and_returns_hits(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """include_answer=False は _summarize（Bedrock）を呼ばず answer='' / cost 0。

    hits と SearchHitOut 整形（content/score/source/title 等のメタ）は通常どおり。
    """
    skill = _skill(fake_bedrock, fake_pgvector)
    out = skill.run(
        input=SearchInput(query="PR代行の実績", include_answer=False),
        ctx=SkillContext(),
    )

    fake_bedrock.converse.assert_not_called()
    assert out.answer == ""
    assert out.total_cost_usd == 0.0
    # retrieval と整形は不変（fast path でも hits のメタは完全）
    assert len(out.hits) == 1
    assert out.hits[0].chunk_id == 1
    assert out.hits[0].content == "PR代行は飲食・コスメ・教育で実績あり"
    assert out.hits[0].score == pytest.approx(0.91)
    assert out.hits[0].source == "proposal_2024_drink.pdf"
    assert out.hits[0].title == "飲料提案"


def test_false_zero_hits_answer_stays_empty(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """include_answer=False + 0 件では 0 件メッセージも出さない（answer は空のまま）。

    「該当する資料が見つかりませんでした。」は _summarize 側の文言なのでスキップされる。
    フロントは include_answer=True の並行フェッチ（(b)）で従来文言を得る。
    """
    fake_pgvector.search_similar.return_value = []
    skill = _skill(fake_bedrock, fake_pgvector)
    out = skill.run(
        input=SearchInput(query="該当なし", include_answer=False),
        ctx=SkillContext(),
    )

    fake_bedrock.converse.assert_not_called()
    assert out.answer == ""
    assert out.hits == []
    assert out.total_cost_usd == 0.0


def test_true_zero_hits_keeps_legacy_message(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """include_answer=True（既定）+ 0 件は従来どおり 0 件メッセージ（回帰ガード）。"""
    fake_pgvector.search_similar.return_value = []
    skill = _skill(fake_bedrock, fake_pgvector)
    out = skill.run(input=SearchInput(query="該当なし"), ctx=SkillContext())

    fake_bedrock.converse.assert_not_called()  # 0 件時は従来から converse しない
    assert "見つかりません" in out.answer
