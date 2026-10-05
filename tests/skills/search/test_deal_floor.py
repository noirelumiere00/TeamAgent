"""案件決定のリコール床（_apply_deal_floor）・意図判定・固有名の抽出（10-05）。

**再現する本番の失敗**: 「ADK経由で受注したショート動画施策を知りたい」に、#proj-01 の
案件決定投稿（本文に「代理店：ADK」・topic=案件決定・client_name 等の分類は無いことがある）が
候補に入らず、利用者が「#proj-01案件決定-同行依頼 も見て」と添える必要があった。投稿は定型文で
長く、意味の近さでは提案書・営業 FB に負ける。

フェイクは test_campaign_floor の ``_SqlLikePg``（引数を SQL と同じ意味で適用し、本物の
アダプタで SearchHit を作る）に、``search_topic_by_terms``（topic 一致 ∧ 本文/題名の ILIKE）を
足したもの。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.knowledge_query import deal_query_terms, is_deal_intent
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_campaign_floor import (
    _build,
    _Chunk,
    _Doc,
    _like,
    _pool_has,
    _proposal_pdf,
    _SqlLikePg,
)

Q_ADK = "ADK経由で受注したショート動画施策を知りたい"
ADK_MARK = "代理店：ADK"


class _DealPg(_SqlLikePg):
    def __init__(self, docs: list[_Doc]) -> None:
        super().__init__(docs)
        self.term_calls: list[dict[str, Any]] = []

    def search_topic_by_terms(
        self,
        conn: Any,
        embedding: list[float],
        *,
        topic: str,
        terms: list[str],
        limit: int = 5,
        embedding_col: str = "embedding",
        request_id: str | None = None,
    ) -> list[SearchHit]:
        self.term_calls.append({"topic": topic, "terms": list(terms)})
        picked = [
            (d, c)
            for d in self._docs
            if d.metadata.get("topic") == topic
            for c in d.chunks
            if any(_like(t, c.content) or _like(t, d.title) for t in terms)
        ]
        picked.sort(key=lambda p: (-p[1].score, p[1].chunk_id))
        return [
            SearchHit(
                chunk_id=c.chunk_id,
                content=c.content,
                score=c.score,
                metadata={**d.metadata, "source_uri": d.source_uri, "title": d.title},
            )
            for d, c in picked[:limit]
        ]


def _deal_post(doc_id: str, *, agency: str, client: str, score: float, chunk_id: int) -> _Doc:
    """#proj-01 の新規案件投稿（ingest の slack と同じ形・分類キーは無い）。"""
    return _Doc(
        document_id=doc_id,
        source_type="slack",
        source_uri=f"https://slack.com/archives/C08MH3MG02F/p{chunk_id}",
        title="#proj-01案件決定-同行依頼",
        metadata={"topic": "案件決定", "channel_name": "#proj-01案件決定-同行依頼"},
        chunks=[
            _Chunk(
                chunk_id=chunk_id,
                content=(
                    ":tada:*新規案件*:tada: 新規案件が決定しました！\n"
                    f"クライアント名／商材名：{client}\n代理店：{agency}\n予算：300万円"
                ),
                score=score,
            )
        ],
    )


def _corpus() -> list[_Doc]:
    return [
        # 「ショート動画施策」に意味が近い提案書が 40 チャンク（0.90〜）で枠を埋める
        _proposal_pdf(doc_id="pdf", client="サラヤ", n=40, top=0.90, step=0.001, base_id=100),
        # 案件決定の投稿。ADK は意味の近さが低い（0.40）＝dense の上位 5 には入らない
        *[
            _deal_post(
                f"deal{i}", agency="電通", client=f"社{i}", score=0.60 - i * 0.01, chunk_id=700 + i
            )
            for i in range(8)
        ],
        _deal_post("adk", agency="ADK", client="ヤクルト", score=0.40, chunk_id=800),
    ]


def test_intent_and_terms() -> None:
    assert is_deal_intent(Q_ADK)
    assert is_deal_intent("今月決まった案件を教えて")
    assert not is_deal_intent("サラヤのショート動画の提案書")
    assert deal_query_terms(Q_ADK) == ["ADK"]
    assert deal_query_terms("ＡＤＫ経由のTikTok PR案件") == ["ADK"]  # 全角も・一般語は除く
    assert deal_query_terms("サイバーエージェント経由で受注") == ["サイバーエージェント"]


def test_adk_deal_reaches_rerank_without_naming_the_channel() -> None:
    """赤（床 0）→ 緑（床 5）。床ありでは ADK の投稿がプールに入る。"""
    inp = SearchInput(query=Q_ADK, top_k=5)
    red_pg = _DealPg(_corpus())
    skill, pools = _build(red_pg)
    skill._deal_pool_floor = 0
    skill.run(input=inp, ctx=SkillContext())
    assert pools and not _pool_has(pools, ADK_MARK), "床なしで届く＝失敗を再現できていない"

    pg = _DealPg(_corpus())
    skill, pools = _build(pg)
    skill.run(input=inp, ctx=SkillContext())
    assert _pool_has(pools, ADK_MARK)
    assert pg.term_calls == [{"topic": "案件決定", "terms": ["ADK"]}]


def test_no_extra_queries_for_other_questions() -> None:
    pg = _DealPg(_corpus())
    skill, _ = _build(pg)
    before = len(pg.calls)
    skill.run(input=SearchInput(query="サラヤのショート動画の提案書", top_k=5), ctx=SkillContext())
    assert pg.term_calls == []
    assert not any(
        (c["sticky_filters"] or {}).get("topic") == "案件決定" for c in pg.calls[before:]
    )


def test_floor_failure_keeps_the_main_search() -> None:
    pg = _DealPg(_corpus())

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db down")

    pg.search_topic_by_terms = boom  # type: ignore[method-assign]
    skill, _pools = _build(pg)
    out = skill.run(input=SearchInput(query=Q_ADK, top_k=5), ctx=SkillContext())
    assert out.hits  # 本体は返る


def test_adapter_query_escapes_terms_and_maps_metadata() -> None:
    """本物の search_topic_by_terms: LIKE のメタ文字を潰し、文書の metadata を載せる。"""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchall.return_value = [
        {
            "chunk_id": 1,
            "content": "代理店：ADK",
            "score": 0.4,
            "page_num": None,
            "source_uri": "https://slack.com/x",
            "source_type": "slack",
            "title": "#proj-01",
            "doc_metadata": {"topic": "案件決定", "channel_name": "#proj-01案件決定-同行依頼"},
            "updated_at": "2026-08-17",
        }
    ]
    conn = MagicMock()
    conn.cursor.return_value = cur
    pg = PgVectorClient(dsn="postgresql://stub")
    hits = pg.search_topic_by_terms(conn, [0.1], topic="案件決定", terms=["A%D_K"], limit=3)
    sql, params = cur.execute.call_args[0]
    assert "ILIKE ANY" in sql and "d.metadata->>'topic' = %s" in sql
    assert params[1] == "案件決定" and params[2] == ["%A\\%D\\_K%"]
    assert hits[0].metadata["channel_name"] == "#proj-01案件決定-同行依頼"
    assert hits[0].metadata["source_uri"] == "https://slack.com/x"
    # 列名は許可リストだけ（SQL に直接埋めるため）
    assert (
        pg.search_topic_by_terms(conn, [0.1], topic="案件決定", terms=["x"], embedding_col="e;--")
        == []
    )
