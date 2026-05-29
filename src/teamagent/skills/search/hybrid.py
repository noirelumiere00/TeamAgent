"""BM25 ハイブリッド検索の純粋ロジック (DB 非依存・単体テスト可能)。

Sprint 5。dense (pgvector cosine) と語彙検索 (pg_bigm バイグラム索引上の
idf 重み付き term-containment) を Reciprocal Rank Fusion で順位融合する。

設計判断 (2026-05-29 実機検証):
- pg_bigm の bigm_similarity (対称 Dice) は短クエリ×長文書で固有名詞を沈める
  ため語彙ランカーに不適。代わりに「クエリ内の rare な内容語を含むか」を
  idf 重みで合算する term-containment スコアを使う。これで dense が外す
  固有名詞 (「日本ガイシ」「マンダム」等) が語彙側 top に浮上する。
- 真の BM25 (tf 正規化) ではないが、RRF は順位しか使わないため融合品質は
  保たれる。MeCab (textsearch_ja) は RDS 非対応のためそもそも採れない。
"""

from __future__ import annotations

import re

from teamagent.adapters.pgvector_client import SearchHit

# クエリから「内容語っぽい」候補語を抽出する (MeCab なしのヒューリスティック)。
# - 英数字連続 >=2
# - カタカナ連続 >=2 (ー 含む)
# - 漢字連続 >=2 (+ 後続カタカナ、複合語「日本ガイシ」を 1 語で拾う)
# - カタカナ + 漢字連続 (「ヒアリング表」等)
# ひらがな主体の助詞 (の/を/に/は/について 等) は対象外 = ノイズ除去。
_TERM_RE = re.compile(
    r"[A-Za-z0-9]{2,}"  # 英数字
    r"|[ァ-ヶー]{2,}"  # カタカナ単独 (複合語より先に試し「ケイパ提案」を分割)
    r"|[一-龯々]{2,}[ァ-ヶー]*"  # 漢字 (+後続カタカナ)。「日本ガイシ」を 1 語で拾う
    r"|[ァ-ヶー]*[一-龯々]{2,}"  # カタカナ+漢字
)


def extract_terms(query: str, *, max_terms: int = 8) -> list[str]:
    """クエリから語彙検索用の候補語を抽出する (重複除去・登場順保持)。

    返り値が空なら呼び出し側は語彙検索をスキップする (dense のみにフォールバック)。
    """
    seen: list[str] = []
    for m in _TERM_RE.findall(query):
        if m not in seen:
            seen.append(m)
        if len(seen) >= max_terms:
            break
    return seen


def reciprocal_rank_fusion(
    rankings: list[list[SearchHit]],
    *,
    k: int = 60,
    limit: int | None = None,
) -> list[SearchHit]:
    """複数のランキングを RRF で融合する。

    RRF score(d) = Σ_i 1 / (k + rank_i(d))   (rank は 1 始まり)

    同一 chunk は chunk_id で名寄せし、最初に出現した SearchHit を代表として
    採用 (content/metadata は両ランキングで同一)。score は RRF スコアで上書きする。
    k=60 は Cormack+ 2009 / pgvector 公式推奨の既定値。

    Args:
        rankings: 各検索手法のランキング (それぞれ順位付き SearchHit リスト)。
        k: RRF 定数。大きいほど上位の優位が緩む。
        limit: 返す最大件数 (None で全件)。

    Returns:
        RRF スコア降順の SearchHit リスト。
    """
    scores: dict[int, float] = {}
    rep: dict[int, SearchHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            rep.setdefault(hit.chunk_id, hit)

    fused = [
        SearchHit(
            chunk_id=cid,
            content=rep[cid].content,
            score=score,
            metadata=rep[cid].metadata,
        )
        for cid, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return fused[:limit] if limit is not None else fused
