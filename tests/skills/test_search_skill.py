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


def test_search_filter_industry_passed_as_metadata_filters(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """filter_industry は metadata_filters dict として adapter に渡る（SQL placeholder 化）。"""
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
    assert call_kwargs["metadata_filters"] == {"industry": "飲食"}
    assert call_kwargs["limit"] == 3
    assert call_kwargs["metadata_col"] == "metadata"
    assert call_kwargs["content_col"] == "content"
    # 旧 where API は廃止
    assert "where" not in call_kwargs


def test_search_filter_industry_no_injection(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """悪意ある filter_industry 文字列でも生 SQL に補間されず、placeholder に bind されること。

    Pydantic の max_length=100 を通せば従来の f-string 実装では
    ``metadata->>'industry' = ''; DROP TABLE chunks; --'`` のような SQL が生成され、
    将来 validation が緩和されると injection の余地が残る。本テストは
    値が dict として渡り、adapter 側で placeholder にバインドされるべきことを固定する。
    """
    payload = "'; DROP TABLE chunks; --"
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
        content_col="content",
        metadata_col="metadata",
    )
    skill.run(
        input=SearchInput(query="injection check", filter_industry=payload),
        ctx=SkillContext(),
    )

    call_kwargs: dict[str, Any] = fake_pgvector.search_similar.call_args.kwargs
    # 1. 値は dict のまま透過 — クォートや SQL 断片で wrap されていない
    assert call_kwargs["metadata_filters"] == {"industry": payload}
    # 2. もはや where 文字列に補間されることはない
    assert "where" not in call_kwargs


def test_search_passes_app_role_to_connection(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """既定で app_role='teamagent_app' が PgVectorClient.connection() に渡る（RLS bypass 防止）。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
    )
    skill.run(input=SearchInput(query="x"), ctx=SkillContext())

    conn_kwargs = fake_pgvector.connection.call_args.kwargs
    assert conn_kwargs["app_role"] == "teamagent_app"


def test_search_passes_user_email_from_ctx_metadata(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """ctx.metadata['user_email'] が connection() に user_email として伝播する。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
    )
    ctx = SkillContext(
        metadata={
            "user_email": "alice@vectorinc.co.jp",
            "user_groups": ["sales@vectorinc.co.jp"],
            "user_role": "member",
        }
    )
    skill.run(input=SearchInput(query="x"), ctx=ctx)

    conn_kwargs = fake_pgvector.connection.call_args.kwargs
    assert conn_kwargs["user_email"] == "alice@vectorinc.co.jp"
    assert conn_kwargs["user_groups"] == ["sales@vectorinc.co.jp"]
    assert conn_kwargs["user_role"] == "member"


def test_search_app_role_can_be_disabled(fake_bedrock: MagicMock, fake_pgvector: MagicMock) -> None:
    """app_role=None を渡すと SET ROLE しない（ローカル開発で teamagent_app 未作成時）。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        app_role=None,
    )
    skill.run(input=SearchInput(query="x"), ctx=SkillContext())

    conn_kwargs = fake_pgvector.connection.call_args.kwargs
    assert conn_kwargs["app_role"] is None


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
    assert call_kwargs["metadata_filters"] is None
    assert call_kwargs["content_col"] == "text"
    assert call_kwargs["metadata_col"] is None


# -----------------------------------------------------------
# use_new_schema=True パス（documents + chunks 新スキーマ）
# -----------------------------------------------------------


def test_search_new_schema_soft_industry_by_default(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """新スキーマで filter_industry を渡しても、既定 (strict_industry=False) で soft 検索。

    Router の auto-detect で industry が付いても、Slack docs (industry メタ無し) を
    全件除外しないようにする fail-safe。
    """
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        use_new_schema=True,
    )
    skill.run(
        input=SearchInput(query="INPEX案件", filter_industry="エネルギー"),
        ctx=SkillContext(),
    )
    call_kwargs = fake_pgvector.search_similar_new_schema.call_args.kwargs
    assert call_kwargs["filter_industry"] == "エネルギー"
    assert call_kwargs["strict_industry"] is False  # 既定 = soft


def test_search_new_schema_strict_industry_explicit(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """strict_industry=True 明示時は厳密一致モードで search_similar_new_schema に渡る。

    スラッシュコマンド `/teamagent_search 案件 industry=飲食` のように
    ユーザーが明示的に業界を指定した場合の経路。
    """
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        use_new_schema=True,
    )
    skill.run(
        input=SearchInput(query="飲食事例", filter_industry="飲食", strict_industry=True),
        ctx=SkillContext(),
    )
    call_kwargs = fake_pgvector.search_similar_new_schema.call_args.kwargs
    assert call_kwargs["filter_industry"] == "飲食"
    assert call_kwargs["strict_industry"] is True


@pytest.fixture
def fake_pgvector_new_schema() -> MagicMock:
    """新スキーマ用 pgvector モック。search_similar_new_schema() を返す。"""
    mock = MagicMock()
    cm_mock = MagicMock()
    cm_mock.__enter__ = MagicMock(return_value=MagicMock())
    cm_mock.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm_mock

    from teamagent.adapters.pgvector_client import SearchHit

    mock.search_similar_new_schema.return_value = [
        SearchHit(
            chunk_id=123456,
            content="営業FB: SNS広告運用で飲食 CPA 30%改善",
            score=0.92,
            metadata={
                "source_uri": "slack://C091ZSVTKF1/1748244936.050099",
                "source_type": "slack",
                "title": "#proj-ナレッジ共有 1748244936.050099",
                "channel_name": "#proj-ナレッジ共有",
            },
        ),
    ]
    return mock


def test_search_new_schema_calls_new_method(
    fake_bedrock: MagicMock, fake_pgvector_new_schema: MagicMock
) -> None:
    """use_new_schema=True のとき search_similar_new_schema() が呼ばれること。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_new_schema,
        embedder=FakeEmbedder(),
        use_new_schema=True,
    )
    out = skill.run(input=SearchInput(query="飲食の事例は？"), ctx=SkillContext())

    fake_pgvector_new_schema.search_similar_new_schema.assert_called_once()
    fake_pgvector_new_schema.search_similar.assert_not_called()
    assert len(out.hits) == 1


def test_search_new_schema_populates_source_fields(
    fake_bedrock: MagicMock, fake_pgvector_new_schema: MagicMock
) -> None:
    """新スキーマの SearchHit から source_uri / source_type / channel_name が SearchHitOut に反映される。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_new_schema,
        embedder=FakeEmbedder(),
        use_new_schema=True,
    )
    out = skill.run(input=SearchInput(query="飲食の事例は？"), ctx=SkillContext())

    hit = out.hits[0]
    assert hit.source_uri == "slack://C091ZSVTKF1/1748244936.050099"
    assert hit.source_type == "slack"
    assert hit.channel_name == "#proj-ナレッジ共有"
    assert hit.source == "#proj-ナレッジ共有"  # _build_source の戻り値


def test_search_new_schema_filter_industry_passed(
    fake_bedrock: MagicMock, fake_pgvector_new_schema: MagicMock
) -> None:
    """use_new_schema=True + filter_industry が search_similar_new_schema に渡ること。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_new_schema,
        embedder=FakeEmbedder(),
        use_new_schema=True,
    )
    skill.run(
        input=SearchInput(query="飲食事例", top_k=3, filter_industry="飲食"),
        ctx=SkillContext(),
    )

    call_kwargs: dict[str, Any] = (
        fake_pgvector_new_schema.search_similar_new_schema.call_args.kwargs
    )
    assert call_kwargs["filter_industry"] == "飲食"
    assert call_kwargs["limit"] == 3


def test_search_old_schema_not_affected_by_new_flag(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """use_new_schema=False（デフォルト）では旧 search_similar() が呼ばれること。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        use_new_schema=False,
    )
    skill.run(input=SearchInput(query="x"), ctx=SkillContext())

    fake_pgvector.search_similar.assert_called_once()
    fake_pgvector.search_similar_new_schema = MagicMock()  # 呼ばれていないことを確認
    fake_pgvector.search_similar_new_schema.assert_not_called()


# ==================================================================
# Day 8 (2026-05-28) Phase 2: FB Drive 自動マッチング
# ==================================================================
@pytest.fixture
def fake_pgvector_with_fb_hit() -> MagicMock:
    """営業 FB ヒット + Drive 関連資料を返す mock。"""
    mock = MagicMock()
    cm_mock = MagicMock()
    cm_mock.__enter__ = MagicMock(return_value=MagicMock())
    cm_mock.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm_mock

    # 主検索: Slack FB ヒット (is_sales_fb=True + client_name=日本ガイシ)
    mock.search_similar_new_schema.return_value = [
        SearchHit(
            chunk_id=100,
            content="*商談フェーズ* ケイパ *顧客名* 日本ガイシ ...",
            score=0.87,
            metadata={
                "source_uri": "slack://C0A1207GYHZ/1779188889.248589",
                "source_type": "slack",
                "title": "#proj-ショート動画_営業フィードバック情報 ts",
                "channel_name": "#proj-ショート動画_営業フィードバック情報",
                "is_sales_fb": True,
                "client_name": "日本ガイシ",
                "deal_phase": "ケイパ",
            },
        ),
    ]
    # Drive 関連資料 (Phase 2 で取得される想定)
    mock.search_drive_by_client_names.return_value = [
        SearchHit(
            chunk_id=200,
            content="日本ガイシ向け提案資料の冒頭抜粋...",
            score=1.0,
            metadata={
                "source_uri": "https://drive.google.com/file/d/abc",
                "source_type": "gdrive",
                "title": "日本ガイシ_リクルーティング提案_v2.pdf",
                "is_related_drive": True,
            },
        ),
    ]
    return mock


def test_phase2_fb_drive_match_triggers_secondary_query(
    fake_bedrock: MagicMock, fake_pgvector_with_fb_hit: MagicMock
) -> None:
    """use_fb_drive_match=True + Slack FB ヒット → search_drive_by_client_names が呼ばれる。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_with_fb_hit,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        use_fb_drive_match=True,
    )
    out = skill.run(input=SearchInput(query="日本ガイシのケイパ"), ctx=SkillContext())

    fake_pgvector_with_fb_hit.search_drive_by_client_names.assert_called_once()
    call_kwargs = fake_pgvector_with_fb_hit.search_drive_by_client_names.call_args.kwargs
    assert call_kwargs["client_names"] == ["日本ガイシ"]
    assert call_kwargs["limit"] == 3
    # 主 hit + 関連 Drive hit が両方 SearchOutput.hits に入る
    assert len(out.hits) == 2


def test_phase2_disabled_by_default_no_secondary_query(
    fake_bedrock: MagicMock, fake_pgvector_with_fb_hit: MagicMock
) -> None:
    """use_fb_drive_match=False (デフォルト) → search_drive_by_client_names は呼ばれない。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_with_fb_hit,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        # use_fb_drive_match 指定なし
    )
    out = skill.run(input=SearchInput(query="日本ガイシのケイパ"), ctx=SkillContext())

    fake_pgvector_with_fb_hit.search_drive_by_client_names.assert_not_called()
    assert len(out.hits) == 1  # 主 hit のみ、関連 Drive なし


def test_phase2_no_fb_hits_no_secondary_query(
    fake_bedrock: MagicMock, fake_pgvector_new_schema: MagicMock
) -> None:
    """主検索に FB がない場合は search_drive_by_client_names を呼ばない (副作用ゼロ)。"""
    # fake_pgvector_new_schema は is_sales_fb なしの hit を返す
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_new_schema,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        use_fb_drive_match=True,
    )
    fake_pgvector_new_schema.search_drive_by_client_names = MagicMock()
    skill.run(input=SearchInput(query="x"), ctx=SkillContext())

    fake_pgvector_new_schema.search_drive_by_client_names.assert_not_called()


def test_phase2_dedupes_client_names(
    fake_bedrock: MagicMock, fake_pgvector_with_fb_hit: MagicMock
) -> None:
    """複数 FB hit で同じ client_name は 1 度だけ Drive 検索される。"""
    # 同じ client_name の FB を 2 件返すよう書き換え
    fake_pgvector_with_fb_hit.search_similar_new_schema.return_value = [
        SearchHit(
            chunk_id=100,
            content="hit1",
            score=0.87,
            metadata={"is_sales_fb": True, "client_name": "日本ガイシ"},
        ),
        SearchHit(
            chunk_id=101,
            content="hit2",
            score=0.85,
            metadata={"is_sales_fb": True, "client_name": "日本ガイシ"},
        ),
    ]
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_with_fb_hit,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        use_fb_drive_match=True,
    )
    skill.run(input=SearchInput(query="日本ガイシ"), ctx=SkillContext())

    call_kwargs = fake_pgvector_with_fb_hit.search_drive_by_client_names.call_args.kwargs
    assert call_kwargs["client_names"] == ["日本ガイシ"]  # 重複除外


# ==================================================================
# Day 8 (2026-05-28) Sprint 4-A: Cohere Rerank v3.5
# ==================================================================
@pytest.fixture
def fake_pgvector_rerank_pool() -> MagicMock:
    """rerank pool 用に top-10 hits を返す pgvector mock。"""
    mock = MagicMock()
    cm_mock = MagicMock()
    cm_mock.__enter__ = MagicMock(return_value=MagicMock())
    cm_mock.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm_mock
    hits = [
        SearchHit(
            chunk_id=i,
            content=f"chunk_{i} content",
            score=0.9 - i * 0.01,  # dense score 降順 (0.90, 0.89, ..., 0.81)
            metadata={"source_type": "slack"},
        )
        for i in range(10)
    ]
    mock.search_similar_new_schema.return_value = hits
    return mock


def test_rerank_calls_bedrock_rerank_and_reorders(
    fake_bedrock: MagicMock, fake_pgvector_rerank_pool: MagicMock
) -> None:
    """USE_COHERE_RERANK=True で rerank が呼ばれ、index 順に並び替えること。"""
    from teamagent.adapters.bedrock_client import RerankResponse, RerankResult

    # Rerank が rank 6 を rank 1 に持ってくる結果を返す
    fake_bedrock.rerank.return_value = RerankResponse(
        results=[
            RerankResult(index=6, relevance_score=0.98),  # 元 dense rank 7 が 1 位に
            RerankResult(index=0, relevance_score=0.65),
            RerankResult(index=3, relevance_score=0.50),
        ],
        model_arn="arn:aws:bedrock:ap-northeast-1::foundation-model/cohere.rerank-v3-5:0",
        latency_ms=200,
        query_count=1,
    )
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_rerank_pool,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        use_cohere_rerank=True,
        rerank_pool_size=10,
    )
    out = skill.run(input=SearchInput(query="日本ガイシ", top_k=3), ctx=SkillContext())

    # pgvector は pool_size=10 で呼ばれた
    pg_kwargs = fake_pgvector_rerank_pool.search_similar_new_schema.call_args.kwargs
    assert pg_kwargs["limit"] == 10
    # Bedrock rerank が呼ばれた
    fake_bedrock.rerank.assert_called_once()
    rerank_kwargs = fake_bedrock.rerank.call_args.kwargs
    assert rerank_kwargs["query"] == "日本ガイシ"
    assert len(rerank_kwargs["documents"]) == 10
    assert rerank_kwargs["top_n"] == 3

    # 出力は rerank で並び替え後の top-3 (chunk_id 6 が先頭)
    assert len(out.hits) == 3
    assert out.hits[0].chunk_id == 6
    assert out.hits[0].score == 0.98  # rerank score で上書き
    assert out.hits[1].chunk_id == 0
    assert out.hits[2].chunk_id == 3


def test_rerank_disabled_by_default(
    fake_bedrock: MagicMock, fake_pgvector_rerank_pool: MagicMock
) -> None:
    """use_cohere_rerank=False (default) で rerank が呼ばれず、dense 結果がそのまま使われる。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_rerank_pool,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        # use_cohere_rerank 指定なし
    )
    out = skill.run(input=SearchInput(query="x", top_k=3), ctx=SkillContext())

    fake_bedrock.rerank.assert_not_called()
    # dense retrieval の top_k だけが返される (pool_size に膨らまない)
    pg_kwargs = fake_pgvector_rerank_pool.search_similar_new_schema.call_args.kwargs
    assert pg_kwargs["limit"] == 3
    assert len(out.hits) == 10  # mock は 10 件返すが、top_k=3 でフェッチ → mock が無視して 10 返す


def test_rerank_failure_falls_back_to_dense(
    fake_bedrock: MagicMock, fake_pgvector_rerank_pool: MagicMock
) -> None:
    """Rerank API が例外を投げても dense top_k で fail-safe に返ること。"""
    fake_bedrock.rerank.side_effect = RuntimeError("rerank API down")
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_rerank_pool,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        use_cohere_rerank=True,
        rerank_pool_size=10,
    )
    out = skill.run(input=SearchInput(query="x", top_k=3), ctx=SkillContext())

    # rerank 失敗時は dense 順上位 3 件 (chunk_id 0,1,2)
    assert len(out.hits) == 3
    assert [h.chunk_id for h in out.hits] == [0, 1, 2]


# ==================================================================
# Day 8 (2026-05-28) Sprint 4-B: prompt v2 (insight + actionable thinking)
# ==================================================================
def test_prompt_version_default_is_v1(fake_bedrock: MagicMock, fake_pgvector: MagicMock) -> None:
    """prompt_version 未指定なら v1 が load される (既存挙動互換)。"""
    from unittest.mock import patch

    with patch("teamagent.skills.search.skill.load_prompt") as mock_load:
        mock_load.return_value = "fake system v1"
        skill = SearchSkill(
            bedrock=fake_bedrock,
            pgvector=fake_pgvector,
            embedder=FakeEmbedder(),
            target_table="proposal_chunks",
        )
        skill.run(input=SearchInput(query="x"), ctx=SkillContext())

        mock_load.assert_called_with("search", "v1", "system")


def test_prompt_version_v2_is_used_when_specified(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """prompt_version='v2' を指定すると v2 prompt が load される。"""
    from unittest.mock import patch

    with patch("teamagent.skills.search.skill.load_prompt") as mock_load:
        mock_load.return_value = "fake system v2 (insight + actionable)"
        skill = SearchSkill(
            bedrock=fake_bedrock,
            pgvector=fake_pgvector,
            embedder=FakeEmbedder(),
            target_table="proposal_chunks",
            prompt_version="v2",
        )
        skill.run(input=SearchInput(query="x"), ctx=SkillContext())

        mock_load.assert_called_with("search", "v2", "system")


def test_prompt_v2_file_exists_and_has_insight_keywords() -> None:
    """src/teamagent/prompts/search/v2/system.md が存在し、insight 設計の主要キーワードを含むこと。

    回帰テスト: prompt v2 設計の根幹キーワードを偶発的に消さない。
    """
    from teamagent.prompts.loader import load_prompt

    content = load_prompt("search", "v2", "system")
    # 重要な役割転換キーワード
    assert "パターン" in content  # パターン抽出
    assert "推奨アクション" in content  # actionable
    assert "避けたい論点" in content  # negative pattern
    assert "刺さった" in content  # 成功パターン
    # 旧スタイルとの差別化
    assert "戦略" in content or "翻訳" in content  # insight role


# ==================================================================
# Day 8 (2026-05-28) Sprint 4-D: v2 compact (v2c) + max_tokens 制限
# ==================================================================
def test_prompt_v2c_file_exists_and_is_compact() -> None:
    """v2c prompt が v2 より大幅に短いこと (latency 短縮の根本)。"""
    from teamagent.prompts.loader import load_prompt

    v2 = load_prompt("search", "v2", "system")
    v2c = load_prompt("search", "v2c", "system")
    # v2c は v2 の compact 版、行数で 60% 以下 (latency 短縮の前提)
    assert len(v2c.splitlines()) < len(v2.splitlines()) * 0.6, (
        f"v2c should be < 60% of v2 length ({len(v2c.splitlines())} vs {len(v2.splitlines())})"
    )
    # ただし insight 設計の核は保持
    assert "パターン" in v2c
    assert "推奨アクション" in v2c
    assert "打ち返し" in v2c
    assert "戦略" in v2c or "翻訳" in v2c


def test_summary_max_tokens_passed_to_bedrock(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """summary_max_tokens が converse() に渡されること (latency 制御)。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
        summary_max_tokens=1200,
    )
    skill.run(input=SearchInput(query="x"), ctx=SkillContext())

    call_kwargs = fake_bedrock.converse.call_args.kwargs
    assert call_kwargs["max_tokens"] == 1200


def test_summary_max_tokens_default_unchanged(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """summary_max_tokens 未指定なら 4096 が converse に渡る (既存互換)。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
    )
    skill.run(input=SearchInput(query="x"), ctx=SkillContext())

    call_kwargs = fake_bedrock.converse.call_args.kwargs
    assert call_kwargs["max_tokens"] == 4096


# -----------------------------------------------------------
# min_relevance（反ハルシネーション閾値, Sprint 5）
# -----------------------------------------------------------
def test_min_relevance_drops_hits_below_threshold(
    fake_bedrock: MagicMock, fake_pgvector_new_schema: MagicMock
) -> None:
    """min_relevance を超えない hit は落とされ、空なら 0 件になる。

    fixture の hit は score=0.92。閾値 0.95 で全件落ち → hits 0 件 →
    Bot は「資料に記載がありません」相当を返す (expect_zero 救済の中核)。
    """
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_new_schema,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        min_relevance=0.95,
    )
    out = skill.run(input=SearchInput(query="東芝の半導体事業"), ctx=SkillContext())
    assert len(out.hits) == 0


def test_min_relevance_keeps_hits_above_threshold(
    fake_bedrock: MagicMock, fake_pgvector_new_schema: MagicMock
) -> None:
    """閾値以上の hit は残る（score 0.92 >= 0.4）。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_new_schema,
        embedder=FakeEmbedder(),
        use_new_schema=True,
        min_relevance=0.4,
    )
    out = skill.run(input=SearchInput(query="飲食の事例は？"), ctx=SkillContext())
    assert len(out.hits) == 1


def test_min_relevance_default_off_keeps_all(
    fake_bedrock: MagicMock, fake_pgvector_new_schema: MagicMock
) -> None:
    """既定 (min_relevance=0.0) では一切フィルタしない。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector_new_schema,
        embedder=FakeEmbedder(),
        use_new_schema=True,
    )
    out = skill.run(input=SearchInput(query="飲食の事例は？"), ctx=SkillContext())
    assert len(out.hits) == 1
