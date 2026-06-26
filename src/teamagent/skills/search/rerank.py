"""予算近接 re-rank（純関数・副作用なし・ネット不要・env を読まない）。

検索 Skill の取得後（Cohere rerank / min_relevance 適用の後）に1段だけ挟む並べ替え。
``ORDER BY`` は不変（バンド距離を SQL 第1キーにすると LIMIT がベクトル近傍を先に切り
良質ヒットが落ちるため）。絞り込みはせず、母数を痩せさせない。

ソートキー（安定ソート・3段）:
1. バンド距離 昇順（同帯=0 / 隣=1 / 2つ隣=2 / 不明・未付与・NULL=末尾）
2. is_low_confidence 0/1 昇順（exclusion_rescue 救出 hit を同帯内で末尾へ）
3. -score 降順（同予算感の中で関連度が高い順）

env-gate（``SEARCH_BUDGET_SORT``）と呼び出し判定は呼び側（skill.py）の責務。
本モジュールは os.environ を一切読まない（テスト容易・恒等性が明示的）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from teamagent.ingest.classify import _BUDGETS  # 単一の真実源

if TYPE_CHECKING:
    from teamagent.adapters.pgvector_client import SearchHit

# 並べ替え可能な3バンドのみ rank を持つ（'不明' は _BUDGETS に含まれるが順序対象外）。
_BUDGET_RANK: dict[str, int] = {
    band: i for i, band in enumerate(b for b in _BUDGETS if b != "不明")
}
_UNKNOWN = 99  # '不明' / 未付与 / NULL は末尾固定（NULLS LAST 流儀）


def budget_rank(band: str | None) -> int:
    """予算バンドを順序整数に変換する（不明・未知は末尾）。"""
    if not band:
        return _UNKNOWN
    return _BUDGET_RANK.get(band, _UNKNOWN)


def sort_by_budget_proximity(hits: list[SearchHit], target_band: str) -> list[SearchHit]:
    """target_band に近い予算順 → 同帯内は低信頼を末尾 → さらに関連度降順で並べ替える。

    絞らず並べ替えのみ。基準が並べ替え対象外（'不明'/不正値）なら no-op（恒等）。
    """
    tr = _BUDGET_RANK.get(target_band)
    if tr is None:
        return hits

    def key(h: SearchHit) -> tuple[int, int, float]:
        r = budget_rank(h.metadata.get("cls_budget"))
        band_dist = abs(r - tr) if r != _UNKNOWN else _UNKNOWN
        low_conf = 1 if h.metadata.get("is_low_confidence") else 0
        return (band_dist, low_conf, -float(h.score))

    return sorted(hits, key=key)  # Python sorted は安定ソート
