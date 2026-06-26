"""SearchSkill のナレッジ・ルーティング（資料種別フィルタ＋フォールバック＋分類タグ露出）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="要約 [chunk_id: 1]",
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0001,
        ),
        model_id="m",
        latency_ms=1,
        stop_reason="end_turn",
    )
    return mock


def _hit(**meta: object) -> SearchHit:
    return SearchHit(chunk_id=1, content="本文", score=0.9, metadata=dict(meta))


def _pgvector(return_value: object = None, *, side_effect: object = None) -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    if side_effect is not None:
        mock.search_similar_new_schema.side_effect = side_effect
    else:
        mock.search_similar_new_schema.return_value = return_value or [_hit()]
    return mock


def _skill(pgvector: MagicMock, *, use_knowledge_filters: bool) -> SearchSkill:
    return SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pgvector,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=False,
        use_knowledge_filters=use_knowledge_filters,
    )


def test_knowledge_filter_passed_when_enabled() -> None:
    pg = _pgvector()
    _skill(pg, use_knowledge_filters=True).run(
        input=SearchInput(query="食品業界の提案事例を教えて"), ctx=SkillContext()
    )
    kwargs = pg.search_similar_new_schema.call_args.kwargs
    assert kwargs["metadata_filters"] == {"cls_doc_type": "提案書"}


def test_no_filter_when_disabled() -> None:
    pg = _pgvector()
    _skill(pg, use_knowledge_filters=False).run(
        input=SearchInput(query="食品業界の提案事例を教えて"), ctx=SkillContext()
    )
    kwargs = pg.search_similar_new_schema.call_args.kwargs
    assert kwargs["metadata_filters"] is None


def test_no_filter_when_no_doc_type_word() -> None:
    pg = _pgvector()
    _skill(pg, use_knowledge_filters=True).run(
        input=SearchInput(query="アース製薬の過去資料を見せて"), ctx=SkillContext()
    )
    kwargs = pg.search_similar_new_schema.call_args.kwargs
    assert kwargs["metadata_filters"] is None


def test_fallback_to_unfiltered_when_zero_hits() -> None:
    # 1 回目（種別フィルタあり）は 0 件 → 2 回目（フィルタ無し）で拾う。
    pg = _pgvector(side_effect=[[], [_hit(cls_doc_type="提案書")]])
    out = _skill(pg, use_knowledge_filters=True).run(
        input=SearchInput(query="提案事例ある？"), ctx=SkillContext()
    )
    assert pg.search_similar_new_schema.call_count == 2
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    second = pg.search_similar_new_schema.call_args_list[1].kwargs
    assert first["metadata_filters"] == {"cls_doc_type": "提案書"}
    assert second.get("metadata_filters") is None  # フォールバックは種別フィルタ無し
    assert len(out.hits) == 1


def test_no_fallback_when_first_has_hits() -> None:
    pg = _pgvector(side_effect=[[_hit(cls_doc_type="提案書")], [_hit()]])
    _skill(pg, use_knowledge_filters=True).run(
        input=SearchInput(query="提案書見せて"), ctx=SkillContext()
    )
    assert pg.search_similar_new_schema.call_count == 1  # 1 回目で取れたら再検索しない


def test_classification_tags_surfaced_in_output() -> None:
    pg = _pgvector(
        return_value=[_hit(cls_project="アース製薬", cls_industry="日用品", cls_doc_type="提案書")]
    )
    out = _skill(pg, use_knowledge_filters=False).run(
        input=SearchInput(query="アース製薬の資料"), ctx=SkillContext()
    )
    assert out.hits[0].project == "アース製薬"
    assert out.hits[0].industry == "日用品"
    assert out.hits[0].doc_type == "提案書"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
