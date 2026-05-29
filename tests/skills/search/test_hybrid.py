"""skills/search/hybrid.py の純粋ロジック単体テスト。

extract_terms (MeCab なしの語彙抽出) と reciprocal_rank_fusion (RRF) を固定する。
DB は使わない。
"""

from __future__ import annotations

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.hybrid import extract_terms, reciprocal_rank_fusion


def _hit(chunk_id: int, score: float = 0.0) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, content=f"c{chunk_id}", score=score, metadata={})


# -----------------------------------------------------------
# extract_terms
# -----------------------------------------------------------
def test_extract_terms_picks_proper_nouns_drops_particles() -> None:
    # 「の」「について」等のひらがな助詞は落ち、固有名詞/内容語が残る
    terms = extract_terms("日本ガイシのケイパ提案について教えて")
    assert "日本ガイシ" in terms
    assert "提案" in terms
    # 助詞のみのひらがなは含まれない
    assert "の" not in terms
    assert "について" not in terms


def test_extract_terms_katakana_and_alnum() -> None:
    terms = extract_terms("マンダムのROI改善")
    assert "マンダム" in terms
    assert "ROI" in terms


def test_extract_terms_dedupes_preserving_order() -> None:
    terms = extract_terms("提案 提案 ヒアリング")
    assert terms == ["提案", "ヒアリング"]


def test_extract_terms_respects_max_terms() -> None:
    terms = extract_terms(
        "東芝 マツダ キリン 電通 サントリー 花王 資生堂 トヨタ ホンダ", max_terms=3
    )
    assert len(terms) == 3


def test_extract_terms_empty_for_hiragana_only() -> None:
    # ひらがな主体の助詞だけならヒットなし → 呼び出し側は語彙検索スキップ
    assert extract_terms("これはどうですか") == []


# -----------------------------------------------------------
# reciprocal_rank_fusion
# -----------------------------------------------------------
def test_rrf_single_ranking_preserves_order() -> None:
    ranking = [_hit(1), _hit(2), _hit(3)]
    fused = reciprocal_rank_fusion([ranking])
    assert [h.chunk_id for h in fused] == [1, 2, 3]


def test_rrf_overlap_boosts_shared_doc() -> None:
    # doc 5 は両方の ranking で上位 → 融合後 1 位になる
    vector = [_hit(1), _hit(5), _hit(2)]
    lexical = [_hit(5), _hit(9), _hit(8)]
    fused = reciprocal_rank_fusion([vector, lexical], k=60)
    assert fused[0].chunk_id == 5


def test_rrf_proper_noun_only_in_lexical_surfaces() -> None:
    # dense が外した doc 42 を lexical が 1 位で拾う → 融合で上位に入る
    vector = [_hit(1), _hit(2), _hit(3), _hit(4)]
    lexical = [_hit(42), _hit(1)]
    fused = reciprocal_rank_fusion([vector, lexical])
    ids = [h.chunk_id for h in fused]
    # doc1 は両方 → 最上位、doc42 は lexical 1 位の貢献で 4 より上に来る
    assert ids[0] == 1
    assert ids.index(42) < ids.index(4)


def test_rrf_score_is_sum_of_reciprocal_ranks() -> None:
    fused = reciprocal_rank_fusion([[_hit(7)], [_hit(7)]], k=60)
    # doc7 は両ランキングで rank1 → 2 * 1/(60+1)
    assert fused[0].chunk_id == 7
    assert abs(fused[0].score - 2.0 / 61.0) < 1e-9


def test_rrf_respects_limit() -> None:
    ranking = [_hit(i) for i in range(1, 11)]
    fused = reciprocal_rank_fusion([ranking], limit=3)
    assert len(fused) == 3


def test_rrf_dedupes_by_chunk_id() -> None:
    fused = reciprocal_rank_fusion([[_hit(1), _hit(1)]])
    assert len(fused) == 1
