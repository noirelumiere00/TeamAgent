"""client / budget フィルタの SearchSkill 配線テスト（設計 v2 §4 の blocker 回帰）。

検証項目:
- fail-open 再検索で metadata_contains / sticky_filters が保持される（明示 client/budget は
  外れない）一方で filter_industry / metadata_filters は解除される（blocker 2）
- exclusion_rescue 再検索でも明示フィルタが保持される
- query_planner 経路の各 _pool_search にも client/budget が渡る（blocker 3）
- filter_client 明示時は _apply_client_boost をスキップ
- SEARCH_BUDGET_SORT 既定 OFF / ON の挙動（並べ替えが env-gate される）
- SearchHitOut.budget に cls_budget が射影される
- include_unknown_budget=True で soft sentinel（__budget_or_unknown__）が使われる
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
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


def _hit(chunk_id: int, budget: str | None = None) -> SearchHit:
    meta: dict[str, object] = {"document_id": chunk_id}
    if budget is not None:
        meta["cls_budget"] = budget
    return SearchHit(chunk_id=chunk_id, content="本文", score=0.9, metadata=meta)


def _pgvector(*, side_effect: object = None, return_value: object = None) -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    if side_effect is not None:
        mock.search_similar_new_schema.side_effect = side_effect
    else:
        mock.search_similar_new_schema.return_value = (
            return_value if return_value is not None else [_hit(1)]
        )
    return mock


class _FakePlanner:
    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan

    def plan(self, query: str, request_id: str) -> QueryPlan:
        return self._plan


def _skill(
    pg: MagicMock,
    *,
    use_client_boost: bool = False,
    planner: object = None,
    use_knowledge_filters: bool = False,
) -> SearchSkill:
    return SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=use_client_boost,
        use_knowledge_filters=use_knowledge_filters,
        query_planner=planner,  # type: ignore[arg-type]
    )


# --- blocker 2: fail-open re-injection -------------------------------------------------


def test_single_query_passes_client_and_budget() -> None:
    """単一クエリ経路の _pool_search が __client__ / cls_budget を pgvector へ渡す。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg).run(
        input=SearchInput(query="ACMEの提案", filter_client="ACME", filter_budget="500万〜"),
        ctx=SkillContext(),
    )
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["metadata_contains"] == {"__client__": "ACME"}
    assert first["sticky_filters"] == {"cls_budget": "500万〜"}


def test_fail_open_keeps_sticky_and_contains_drops_industry() -> None:
    """fail-open 再検索で client/budget は保持・industry/metadata_filters は解除（blocker 2）。"""
    # 1 回目（filter_industry 付き）0 件 → fail-open で 2 回目ヒット。
    pg = _pgvector(side_effect=[[], [_hit(1)]])
    _skill(pg).run(
        input=SearchInput(
            query="アパレルのACME提案",
            filter_industry="アパレル",
            filter_client="ACME",
            filter_budget="500万〜",
        ),
        ctx=SkillContext(),
    )
    assert pg.search_similar_new_schema.call_count == 2
    reopen = pg.search_similar_new_schema.call_args_list[1].kwargs
    # 明示 client/budget は再注入される
    assert reopen["metadata_contains"] == {"__client__": "ACME"}
    assert reopen["sticky_filters"] == {"cls_budget": "500万〜"}
    # 自動付与の業界フィルタは外れる
    assert reopen["filter_industry"] is None


def test_exclusion_rescue_keeps_explicit_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """exclusion_rescue 再検索でも明示 client/budget が保持される。"""
    monkeypatch.setenv("BOILERPLATE_EXCLUDE_SEARCH", "1")  # exclude 系を起動
    monkeypatch.setenv("SEARCH_EXCLUSION_RESCUE", "true")
    # filter_industry なし & metadata_filters なし → fail-open 条件は偽でスキップ。
    # 1 回目 0 件 → exclusion_rescue（exclude 全外し）が 2 回目でヒット。
    pg = _pgvector(side_effect=[[], [_hit(1)]])
    _skill(pg).run(
        input=SearchInput(query="ACME提案", filter_client="ACME", filter_budget="〜100万"),
        ctx=SkillContext(),
    )
    assert pg.search_similar_new_schema.call_count == 2
    rescue = pg.search_similar_new_schema.call_args_list[1].kwargs
    assert rescue["metadata_contains"] == {"__client__": "ACME"}
    assert rescue["sticky_filters"] == {"cls_budget": "〜100万"}
    # rescue は exclude を全外し
    assert rescue["exclude_boilerplate"] is False
    assert rescue["exclude_duplicates"] is False


# --- blocker 3: query_planner branch wiring --------------------------------------------


def test_query_planner_branch_wires_client_and_budget() -> None:
    """query_planner 経路の全 _pool_search にも client/budget が渡る（blocker 3）。"""
    plan = QueryPlan(
        paraphrases=["言い換えA"],
        hyde_answer="想定回答",
        industry=None,
        doc_type=None,
        client_names=[],
        is_aggregation=False,
    )
    # 元クエリ + 言い換え + HyDE = 3 サブクエリ
    pg = _pgvector(side_effect=[[_hit(1)], [_hit(2)], [_hit(3)]])
    _skill(pg, planner=_FakePlanner(plan)).run(
        input=SearchInput(query="元", filter_client="日本ガイシ", filter_budget="100〜500万"),
        ctx=SkillContext(),
    )
    assert pg.search_similar_new_schema.call_count == 3
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs["metadata_contains"] == {"__client__": "日本ガイシ"}
        assert call.kwargs["sticky_filters"] == {"cls_budget": "100〜500万"}


# --- boost skip ------------------------------------------------------------------------


def test_client_boost_skipped_when_filter_client_set() -> None:
    """filter_client 明示時は _apply_client_boost をスキップ（別 client 混入防止）。"""
    pg = _pgvector(return_value=[_hit(1)])
    skill = _skill(pg, use_client_boost=True)
    skill._apply_client_boost = MagicMock()  # type: ignore[method-assign]
    skill.run(
        input=SearchInput(query="ACMEの提案", filter_client="ACME"),
        ctx=SkillContext(),
    )
    skill._apply_client_boost.assert_not_called()


def test_client_boost_runs_when_no_filter_client() -> None:
    """filter_client 未指定なら従来どおり boost が走る（後方互換）。"""
    pg = _pgvector(return_value=[_hit(1)])
    skill = _skill(pg, use_client_boost=True)
    skill._apply_client_boost = MagicMock(return_value=[_hit(1)])  # type: ignore[method-assign]
    skill.run(input=SearchInput(query="ACMEの提案"), ctx=SkillContext())
    skill._apply_client_boost.assert_called_once()


# --- env-gate budget sort --------------------------------------------------------------


def test_budget_sort_env_off_no_reorder(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEARCH_BUDGET_SORT 既定 OFF → sort_budget_near 指定でも並べ替えない。"""
    monkeypatch.delenv("SEARCH_BUDGET_SORT", raising=False)
    # 不明(末尾候補) が先頭・同帯が後ろ。OFF なら順序保持。
    hits = [_hit(1, "不明"), _hit(2, "〜100万")]
    pg = _pgvector(return_value=hits)
    out = _skill(pg).run(
        input=SearchInput(query="x", sort_budget_near="〜100万"),
        ctx=SkillContext(),
    )
    assert [h.chunk_id for h in out.hits] == [1, 2]  # 並べ替えなし


def test_budget_sort_env_on_reorders(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEARCH_BUDGET_SORT=1 → sort_budget_near に近い順へ並べ替える。"""
    monkeypatch.setenv("SEARCH_BUDGET_SORT", "1")
    hits = [_hit(1, "不明"), _hit(2, "〜100万")]
    pg = _pgvector(return_value=hits)
    out = _skill(pg).run(
        input=SearchInput(query="x", sort_budget_near="〜100万"),
        ctx=SkillContext(),
    )
    assert [h.chunk_id for h in out.hits] == [2, 1]  # 同帯が先頭へ


def test_budget_sort_on_but_no_target_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """env ON でも sort_budget_near 未指定なら並べ替えない。"""
    monkeypatch.setenv("SEARCH_BUDGET_SORT", "1")
    hits = [_hit(1, "不明"), _hit(2, "〜100万")]
    pg = _pgvector(return_value=hits)
    out = _skill(pg).run(input=SearchInput(query="x"), ctx=SkillContext())
    assert [h.chunk_id for h in out.hits] == [1, 2]


# --- output projection / soft ----------------------------------------------------------


def test_budget_projected_to_output() -> None:
    """SearchHitOut.budget に cls_budget が射影される。"""
    pg = _pgvector(return_value=[_hit(1, "500万〜")])
    out = _skill(pg).run(input=SearchInput(query="x"), ctx=SkillContext())
    assert out.hits[0].budget == "500万〜"


def test_budget_projected_none_when_absent() -> None:
    pg = _pgvector(return_value=[_hit(1, None)])
    out = _skill(pg).run(input=SearchInput(query="x"), ctx=SkillContext())
    assert out.hits[0].budget is None


def test_include_unknown_uses_soft_sentinel() -> None:
    """include_unknown_budget=True で __budget_or_unknown__ sentinel が使われる。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg).run(
        input=SearchInput(query="x", filter_budget="500万〜", include_unknown_budget=True),
        ctx=SkillContext(),
    )
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["sticky_filters"] == {"__budget_or_unknown__": "500万〜"}


def test_strict_budget_when_unknown_flag_false() -> None:
    """include_unknown_budget=False（既定）は strict（cls_budget 等価）。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg).run(
        input=SearchInput(query="x", filter_budget="500万〜"),
        ctx=SkillContext(),
    )
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["sticky_filters"] == {"cls_budget": "500万〜"}


def test_no_explicit_filters_passes_none() -> None:
    """client/budget 未指定なら metadata_contains / sticky_filters は None（後方互換）。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg).run(input=SearchInput(query="x"), ctx=SkillContext())
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["metadata_contains"] is None
    assert first["sticky_filters"] is None


# --- doc_type / solution sticky（資料引用ツール）-----------------------------------------


def test_single_query_passes_doc_type_and_solution() -> None:
    """明示 doc_type / solution が cls_doc_type / cls_solution の等価 sticky で渡る。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg).run(
        input=SearchInput(
            query="動画広告の提案資料",
            filter_doc_type="提案書",
            filter_solution="動画広告",
        ),
        ctx=SkillContext(),
    )
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["sticky_filters"] == {"cls_doc_type": "提案書", "cls_solution": "動画広告"}


def test_doc_type_solution_combine_with_budget_sticky() -> None:
    """budget + doc_type + solution は同一 sticky dict に併載される。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg).run(
        input=SearchInput(
            query="x",
            filter_budget="500万〜",
            filter_doc_type="報告書",
            filter_solution="SNS運用",
        ),
        ctx=SkillContext(),
    )
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["sticky_filters"] == {
        "cls_budget": "500万〜",
        "cls_doc_type": "報告書",
        "cls_solution": "SNS運用",
    }


def test_fail_open_keeps_doc_type_solution_sticky() -> None:
    """fail-open 再検索でも doc_type / solution sticky は保持される（落とさない）。"""
    pg = _pgvector(side_effect=[[], [_hit(1)]])
    _skill(pg).run(
        input=SearchInput(
            query="食品の提案資料",
            filter_industry="食品",
            filter_doc_type="提案書",
            filter_solution="動画広告",
        ),
        ctx=SkillContext(),
    )
    assert pg.search_similar_new_schema.call_count == 2
    reopen = pg.search_similar_new_schema.call_args_list[1].kwargs
    assert reopen["sticky_filters"] == {"cls_doc_type": "提案書", "cls_solution": "動画広告"}
    assert reopen["filter_industry"] is None  # 自動付与の業界は外れる


def test_query_planner_branch_wires_doc_type_solution() -> None:
    """query_planner 経路の全 _pool_search にも doc_type / solution sticky が渡る。"""
    plan = QueryPlan(
        paraphrases=["言い換えA"],
        hyde_answer="想定回答",
        industry=None,
        doc_type=None,
        client_names=[],
        is_aggregation=False,
    )
    pg = _pgvector(side_effect=[[_hit(1)], [_hit(2)], [_hit(3)]])
    _skill(pg, planner=_FakePlanner(plan)).run(
        input=SearchInput(query="元", filter_doc_type="提案書", filter_solution="SEO"),
        ctx=SkillContext(),
    )
    assert pg.search_similar_new_schema.call_count == 3
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs["sticky_filters"] == {
            "cls_doc_type": "提案書",
            "cls_solution": "SEO",
        }


def test_explicit_doc_type_overrides_planner_doc_type() -> None:
    """明示 filter_doc_type があれば plan.doc_type は metadata_filters に載らない（明示優先）。"""
    plan = QueryPlan(
        paraphrases=[],
        hyde_answer="",
        industry=None,
        doc_type="議事録",  # 自動抽出は議事録だが…
        client_names=[],
        is_aggregation=False,
    )
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg, planner=_FakePlanner(plan), use_knowledge_filters=True).run(
        input=SearchInput(query="x", filter_doc_type="提案書"),  # …明示は提案書
        ctx=SkillContext(),
    )
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    # 明示 doc_type は sticky に、自動 plan.doc_type は metadata_filters に載らない
    assert first["sticky_filters"] == {"cls_doc_type": "提案書"}
    assert first["metadata_filters"] is None


def test_explicit_doc_type_drops_auto_extracted_in_single_query() -> None:
    """単一クエリ経路: クエリ自動抽出 cls_doc_type は明示があれば外す（衝突回避・明示優先）。"""
    # クエリ「議事録」で自動抽出 cls_doc_type=議事録 だが、明示は提案書。
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg, use_knowledge_filters=True).run(
        input=SearchInput(query="議事録ください", filter_doc_type="提案書"),
        ctx=SkillContext(),
    )
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["sticky_filters"] == {"cls_doc_type": "提案書"}
    # 自動抽出の cls_doc_type は metadata_filters から外れている
    mf = first["metadata_filters"]
    assert mf is None or "cls_doc_type" not in mf


def test_no_doc_type_solution_passes_none() -> None:
    """doc_type / solution 未指定なら sticky_filters は None（後方互換）。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(pg).run(input=SearchInput(query="x"), ctx=SkillContext())
    first = pg.search_similar_new_schema.call_args_list[0].kwargs
    assert first["sticky_filters"] is None


# --- client_boost が明示 doc_type/solution sticky を運ぶ（major fix）-------------------


def _boost_call(pg: MagicMock) -> dict[str, object]:
    """boost のサブ検索（2 本目以降で __client__ を持つ呼び出し）を返す。

    boost は filter_client 未指定時だけ走るので、本検索（1 本目）は __client__ を持たない。
    したがって「1 本目以外で __client__ を持つ呼び出し」で一意に特定できる。
    以前は metadata_filters の client_name で特定していたが、それは
    「client_name 完全一致 AND」という誤った絞り込み自体を前提にした特定方法だった。
    """
    for call in pg.search_similar_new_schema.call_args_list[1:]:
        mc = call.kwargs.get("metadata_contains")
        if isinstance(mc, dict) and "__client__" in mc:
            return dict(call.kwargs)
    raise AssertionError("client_boost のサブ検索が見つからない")


def test_client_boost_carries_explicit_doc_type_solution_sticky() -> None:
    """boost が走るとき、明示 doc_type/solution の等価 sticky を boost サブ検索にも運ぶ。

    filter_client 未指定で boost が起動する経路（『電通の動画広告施策レポート』など query に
    client 名を置き doc_type/solution を明示するケース）で、boost が doc_type/solution を無視して
    別種別の資料を合流させないことを保証する（設計 §A/§E の不変条件）。
    """
    pg = _pgvector(return_value=[_hit(1)])
    skill = _skill(pg, use_client_boost=True)
    skill._match_client = MagicMock(return_value="電通")  # type: ignore[method-assign]
    skill.run(
        input=SearchInput(
            query="電通の動画広告施策レポート",
            filter_doc_type="報告書",
            filter_solution="動画広告",
        ),
        ctx=SkillContext(),
    )
    boost = _boost_call(pg)
    assert boost["sticky_filters"] == {"cls_doc_type": "報告書", "cls_solution": "動画広告"}
    # boost は自分で __client__ を注入する（client_name 完全一致 AND では
    # Drive の提案書しか無い取引先を拾えないため）。
    assert boost["metadata_contains"] == {"__client__": "電通"}


def test_client_boost_carries_budget_sticky() -> None:
    """boost が走るとき、明示 budget sticky も boost サブ検索へ運ぶ（既存 sticky も同様に保護）。"""
    pg = _pgvector(return_value=[_hit(1)])
    skill = _skill(pg, use_client_boost=True)
    skill._match_client = MagicMock(return_value="電通")  # type: ignore[method-assign]
    skill.run(
        input=SearchInput(query="電通の提案", filter_budget="500万〜"),
        ctx=SkillContext(),
    )
    boost = _boost_call(pg)
    assert boost["sticky_filters"] == {"cls_budget": "500万〜"}


def test_client_boost_sticky_none_when_no_explicit_filters() -> None:
    """明示フィルタ無しなら boost の sticky_filters / metadata_contains は None（後方互換）。"""
    pg = _pgvector(return_value=[_hit(1)])
    skill = _skill(pg, use_client_boost=True)
    skill._match_client = MagicMock(return_value="電通")  # type: ignore[method-assign]
    skill.run(input=SearchInput(query="電通の提案"), ctx=SkillContext())
    boost = _boost_call(pg)
    assert boost["sticky_filters"] is None
    assert boost["metadata_contains"] == {"__client__": "電通"}


# --- §D: NL client 抽出 → __client__ 昇格（query_planner 経路）------------------------


def _plan(client_names: list[str]) -> QueryPlan:
    return QueryPlan(
        paraphrases=[],  # 言い換え無し → 元クエリ 1 本のみ（_pool_search 呼び出し1回）
        hyde_answer="",
        industry=None,
        doc_type=None,
        client_names=client_names,
        is_aggregation=False,
    )


def test_plan_client_promoted_to_metadata_contains() -> None:
    """USE_QUERY_PLANNER 経路で plan.client_names 先頭を __client__ へ昇格（NL client）。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(
        pg,
        planner=_FakePlanner(_plan(["電通", "博報堂"])),
        use_knowledge_filters=True,
    ).run(input=SearchInput(query="電通への提案資料"), ctx=SkillContext())
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs["metadata_contains"] == {"__client__": "電通"}


def test_explicit_filter_client_not_overridden_by_plan() -> None:
    """明示 filter_client があれば plan.client_names では上書きしない（mc is None ガード）。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(
        pg,
        planner=_FakePlanner(_plan(["博報堂"])),  # plan は別 client を抽出するが…
        use_knowledge_filters=True,
    ).run(
        input=SearchInput(query="提案", filter_client="電通"),  # …明示が優先
        ctx=SkillContext(),
    )
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs["metadata_contains"] == {"__client__": "電通"}


def test_empty_plan_client_names_is_noop() -> None:
    """plan.client_names が空なら __client__ は乗らない（後方互換・no-op）。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(
        pg,
        planner=_FakePlanner(_plan([])),
        use_knowledge_filters=True,
    ).run(input=SearchInput(query="提案"), ctx=SkillContext())
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs["metadata_contains"] is None


def test_plan_client_not_promoted_when_knowledge_filters_off() -> None:
    """USE_KNOWLEDGE_FILTERS OFF なら plan.client_names は昇格しない（gate 一貫性）。"""
    pg = _pgvector(return_value=[_hit(1)])
    _skill(
        pg,
        planner=_FakePlanner(_plan(["電通"])),
        use_knowledge_filters=False,
    ).run(input=SearchInput(query="電通への提案"), ctx=SkillContext())
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs["metadata_contains"] is None


def test_plan_client_promotion_skips_client_boost() -> None:
    """plan 由来 client を昇格したら client_boost をスキップ（明示時と挙動を揃える）。"""
    pg = _pgvector(return_value=[_hit(1)])
    skill = _skill(
        pg,
        use_client_boost=True,
        planner=_FakePlanner(_plan(["電通"])),
        use_knowledge_filters=True,
    )
    skill._apply_client_boost = MagicMock()  # type: ignore[method-assign]
    skill.run(input=SearchInput(query="電通への提案"), ctx=SkillContext())
    skill._apply_client_boost.assert_not_called()


def test_boost_runs_when_plan_has_no_client() -> None:
    """plan に client が無ければ boost は従来どおり走る（昇格していないので skip しない）。"""
    pg = _pgvector(return_value=[_hit(1)])
    skill = _skill(
        pg,
        use_client_boost=True,
        planner=_FakePlanner(_plan([])),
        use_knowledge_filters=True,
    )
    skill._apply_client_boost = MagicMock(return_value=[_hit(1)])  # type: ignore[method-assign]
    skill.run(input=SearchInput(query="ふつうの検索"), ctx=SkillContext())
    skill._apply_client_boost.assert_called_once()
