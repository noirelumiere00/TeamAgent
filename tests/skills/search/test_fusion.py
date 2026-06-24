"""reciprocal_rank_fusion の単体テスト。

RRF は**並び順**だけを決め、各 hit の ``score`` は元の検索スコア（cosine 等の
relevance スケール）の最大値を保持する二層構造になっている点を検証する。
"""

from __future__ import annotations

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.fusion import reciprocal_rank_fusion


def _hit(chunk_id: int, score: float = 0.0) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id, content=f"c{chunk_id}", score=score, metadata={"id": chunk_id}
    )


def test_empty_input_returns_empty() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_single_list_preserves_order() -> None:
    result = reciprocal_rank_fusion([[_hit(1, 0.9), _hit(2, 0.8), _hit(3, 0.7)]], k=60)
    assert [h.chunk_id for h in result] == [1, 2, 3]
    # score は RRF スコア(1/61)ではなく元の relevance スコアを保持する
    assert result[0].score == 0.9


def test_dedup_keeps_highest_score_payload() -> None:
    # 同一 chunk_id が複数リストに出たら、元スコアが最大の hit の payload を採用する
    a = SearchHit(chunk_id=7, content="first", score=0.9, metadata={"src": "A"})
    b = SearchHit(chunk_id=7, content="second", score=0.1, metadata={"src": "B"})
    result = reciprocal_rank_fusion([[b], [a]])
    assert len(result) == 1
    assert result[0].content == "first"  # score 0.9 の a を採用（出現順ではない）
    assert result[0].metadata == {"src": "A"}
    # score は元スコアの最大値（融合スコアではない）
    assert result[0].score == 0.9


def test_fusion_orders_by_combined_rank() -> None:
    # list1: 1, 2, 3 / list2: 3, 1, 2 → RRF で 1 と 3 が同点で先頭、2 が最下位
    list1 = [_hit(1), _hit(2), _hit(3)]
    list2 = [_hit(3), _hit(1), _hit(2)]
    result = reciprocal_rank_fusion([list1, list2], k=60)
    assert result[-1].chunk_id == 2
    assert {result[0].chunk_id, result[1].chunk_id} == {1, 3}


def test_score_is_original_not_rrf() -> None:
    # 元 score が高くても RRF スコア(~0.016)では上書きされない＝relevance スケール維持
    result = reciprocal_rank_fusion([[_hit(1, score=0.99)]], k=60)
    assert result[0].score == 0.99


def test_score_is_max_original_across_lists() -> None:
    # 別リストで異なる元スコアを持つ同一 chunk → 最大値を採用
    low = SearchHit(chunk_id=5, content="x", score=0.3, metadata={})
    high = SearchHit(chunk_id=5, content="x", score=0.8, metadata={})
    result = reciprocal_rank_fusion([[low], [high]])
    assert len(result) == 1
    assert result[0].score == 0.8
