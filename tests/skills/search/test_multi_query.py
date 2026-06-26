"""SearchSkill のエージェント検索（query planner → multi-query/HyDE → RRF 融合）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.knowledge_query import extract_knowledge_filters
from teamagent.skills.search.query_planner import QueryPlan
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="要約",
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


def _hit(chunk_id: int) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, content="本文", score=0.9, metadata={})


def _pgvector(*, side_effect: object = None, return_value: object = None) -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    if side_effect is not None:
        mock.search_similar_new_schema.side_effect = side_effect
    else:
        mock.search_similar_new_schema.return_value = return_value or [_hit(1)]
    return mock


class _FakePlanner:
    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan
        self.calls = 0

    def plan(self, query: str, request_id: str) -> QueryPlan:
        self.calls += 1
        return self._plan


def _skill(pg: MagicMock, planner: object, *, use_knowledge_filters: bool = True) -> SearchSkill:
    return SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=False,
        use_knowledge_filters=use_knowledge_filters,
        query_planner=planner,  # type: ignore[arg-type]
    )


def test_multi_query_searches_each_subquery_and_fuses() -> None:
    plan = QueryPlan(
        paraphrases=["言い換えA", "言い換えB"],
        hyde_answer="想定回答テキスト",
        industry="食品",
        doc_type="提案書",
        client_names=[],
        is_aggregation=False,
    )
    pg = _pgvector(side_effect=[[_hit(1)], [_hit(2)], [_hit(3)], [_hit(4)]])
    planner = _FakePlanner(plan)
    out = _skill(pg, planner).run(input=SearchInput(query="元クエリ"), ctx=SkillContext())

    assert planner.calls == 1
    # 元query + 言い換え2 + HyDE = 4 サブクエリ分 検索される
    assert pg.search_similar_new_schema.call_count == 4
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    # use_knowledge_filters=True（既定）のとき plan 由来の業界/資料種別フィルタが効く
    assert first["metadata_filters"] == {"cls_doc_type": "提案書"}
    assert first["filter_industry"] == "食品"
    # RRF 融合で 4 chunk すべてが出る
    assert {h.chunk_id for h in out.hits} == {1, 2, 3, 4}


def test_planner_filters_gated_by_knowledge_flag() -> None:
    # USE_KNOWLEDGE_FILTERS=OFF のときは plan 由来のメタフィルタを適用しない
    # （cls_* が疎なうちに doc_type ハードフィルタで取りこぼすのを防ぐ＝単一クエリ経路と一貫）。
    plan = QueryPlan(
        paraphrases=["言い換えA", "言い換えB"],
        hyde_answer="想定回答テキスト",
        industry="食品",
        doc_type="提案書",
        client_names=[],
        is_aggregation=False,
    )
    pg = _pgvector(side_effect=[[_hit(1)], [_hit(2)], [_hit(3)], [_hit(4)]])
    out = _skill(pg, _FakePlanner(plan), use_knowledge_filters=False).run(
        input=SearchInput(query="元クエリ"), ctx=SkillContext()
    )
    # multi-query/RRF 自体は動く（4 サブクエリ → 4 chunk）
    assert pg.search_similar_new_schema.call_count == 4
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["metadata_filters"] is None
    assert first["filter_industry"] is None
    assert {h.chunk_id for h in out.hits} == {1, 2, 3, 4}


def test_planner_dedupes_duplicate_paraphrases() -> None:
    # Haiku が元クエリや言い換え同士で重複文を返しても embed/検索を増やさない。
    plan = QueryPlan(
        paraphrases=["元クエリ", "言い換えA", "言い換えA"],  # 元と重複1 + 自己重複1
        hyde_answer="言い換えA",  # さらに重複
        industry="",
        doc_type="",
        client_names=[],
        is_aggregation=False,
    )
    pg = _pgvector(side_effect=[[_hit(1)], [_hit(2)]])
    out = _skill(pg, _FakePlanner(plan)).run(
        input=SearchInput(query="元クエリ"), ctx=SkillContext()
    )
    # ユニークは「元クエリ」「言い換えA」の 2 つだけ
    assert pg.search_similar_new_schema.call_count == 2
    assert {h.chunk_id for h in out.hits} == {1, 2}


def test_planner_off_is_single_query() -> None:
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg, None).run(input=SearchInput(query="元クエリ"), ctx=SkillContext())
    assert pg.search_similar_new_schema.call_count == 1  # 単一クエリ（後方互換）


def test_extract_knowledge_filters_composite_phase() -> None:
    assert extract_knowledge_filters("受注した提案書を見せて") == {
        "cls_doc_type": "提案書",
        "cls_phase": "受注",
    }
    assert extract_knowledge_filters("失注の議事録") == {
        "cls_doc_type": "議事録",
        "cls_phase": "失注",
    }
    assert extract_knowledge_filters("普通の質問") is None
