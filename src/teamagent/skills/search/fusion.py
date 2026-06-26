"""Reciprocal Rank Fusion（RRF）の純関数モジュール。"""

from __future__ import annotations

from dataclasses import replace

from teamagent.adapters.pgvector_client import SearchHit


def reciprocal_rank_fusion(ranked_lists: list[list[SearchHit]], *, k: int = 60) -> list[SearchHit]:
    """複数の順位付きリストを RRF で融合する。

    各リスト内の 1-based 順位 rank に `1/(k+rank)` を与え chunk_id 単位で合算し、
    その融合スコアの降順で**並び順**を決める（どの hit が上位プールに入るかを決定）。

    ただし戻り値の各 SearchHit の ``score`` は融合スコアでは**なく**、元の検索スコア
    （cosine 等の relevance スケール）の最大値を保持する。これは下流の min_relevance
    ゲート（Rerank が無効なときは元スコアに対して閾値判定する）が、~0.02 スケールの
    RRF スコアで全件を誤って 0 件化しないようにするため。RRF はあくまで順位付け専用、
    relevance の絶対値は元スコアという二層構造にする（単一クエリ経路と同じスケール）。
    Rerank 有効時は後段で score が 0-1 の relevance に上書きされるため影響しない。

    chunk_id で dedup し、最大の元スコアを持つ hit の content/metadata を採用する。
    空入力なら空リストを返す。
    """
    fused_scores: dict[int, float] = {}
    best_hit: dict[int, SearchHit] = {}
    best_score: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked, start=1):
            cid = hit.chunk_id
            fused_scores[cid] = fused_scores.get(cid, 0.0) + 1.0 / (k + rank)
            if cid not in best_score or hit.score > best_score[cid]:
                best_score[cid] = hit.score
                best_hit[cid] = hit
    ordered = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
    return [replace(best_hit[cid], score=best_score[cid]) for cid, _rrf in ordered]
