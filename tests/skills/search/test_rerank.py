"""予算近接 re-rank（sort_by_budget_proximity）の純関数単体テスト。

設計 v2 §3/§4 の検証項目:
- 同帯=0 / 隣帯=1 / 不明・未付与=末尾
- 不正基準（'不明' / 未知バンド）は no-op（恒等・絞らない）
- 同帯内で低信頼（is_low_confidence）を末尾へ
- 同帯・同信頼は関連度（score）降順でタイブレーク
- 並べ替えのみ（件数は不変）
"""

from __future__ import annotations

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.rerank import (
    budget_rank,
    sort_by_budget_proximity,
)


def _hit(chunk_id: int, score: float, budget: str | None, low_conf: bool = False) -> SearchHit:
    meta: dict[str, object] = {}
    if budget is not None:
        meta["cls_budget"] = budget
    if low_conf:
        meta["is_low_confidence"] = True
    return SearchHit(chunk_id=chunk_id, content="x", score=score, metadata=meta)


def _ids(hits: list[SearchHit]) -> list[int]:
    return [h.chunk_id for h in hits]


def test_budget_rank_values() -> None:
    assert budget_rank("〜100万") == 0
    assert budget_rank("100〜500万") == 1
    assert budget_rank("500万〜") == 2
    assert budget_rank("不明") == 99
    assert budget_rank(None) == 99
    assert budget_rank("謎の値") == 99


def test_same_band_first_then_neighbor_then_far() -> None:
    """target=100〜500万 → 同帯(0) → 隣帯(1) → 2つ隣(2) の順。"""
    hits = [
        _hit(1, 0.5, "500万〜"),  # dist 1
        _hit(2, 0.5, "〜100万"),  # dist 1
        _hit(3, 0.5, "100〜500万"),  # dist 0
    ]
    out = sort_by_budget_proximity(hits, "100〜500万")
    assert out[0].chunk_id == 3  # 同帯が先頭


def test_unknown_and_missing_go_last() -> None:
    """'不明' / cls_budget 未付与 は末尾へ。"""
    hits = [
        _hit(1, 0.9, "不明"),
        _hit(2, 0.8, None),
        _hit(3, 0.5, "〜100万"),
    ]
    out = sort_by_budget_proximity(hits, "〜100万")
    assert out[0].chunk_id == 3
    assert set(_ids(out[1:])) == {1, 2}  # 不明/未付与が末尾2件


def test_invalid_target_is_noop() -> None:
    """基準が '不明' / 未知バンドなら恒等（並べ替えない）。"""
    hits = [_hit(1, 0.3, "500万〜"), _hit(2, 0.9, "〜100万")]
    assert _ids(sort_by_budget_proximity(hits, "不明")) == [1, 2]
    assert _ids(sort_by_budget_proximity(hits, "謎")) == [1, 2]


def test_low_confidence_sinks_within_band() -> None:
    """同帯内では低信頼を末尾へ（高信頼が score 低くても先）。"""
    hits = [
        _hit(1, 0.99, "〜100万", low_conf=True),  # 同帯・低信頼
        _hit(2, 0.40, "〜100万", low_conf=False),  # 同帯・高信頼
    ]
    out = sort_by_budget_proximity(hits, "〜100万")
    assert _ids(out) == [2, 1]  # 高信頼が先


def test_score_tiebreak_descending() -> None:
    """同帯・同信頼は関連度降順。"""
    hits = [
        _hit(1, 0.40, "100〜500万"),
        _hit(2, 0.95, "100〜500万"),
        _hit(3, 0.70, "100〜500万"),
    ]
    out = sort_by_budget_proximity(hits, "100〜500万")
    assert _ids(out) == [2, 3, 1]


def test_count_preserved() -> None:
    """並べ替えのみで件数は不変（絞らない）。"""
    hits = [_hit(i, 0.5, "不明") for i in range(5)]
    out = sort_by_budget_proximity(hits, "500万〜")
    assert len(out) == 5


def test_empty_list() -> None:
    assert sort_by_budget_proximity([], "〜100万") == []
