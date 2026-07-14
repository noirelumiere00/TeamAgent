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
    sort_by_client_match,
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


# ── B6: sort_by_client_match（cls_project/client_name 一致を前出し・絞らない） ──


def _chit(
    chunk_id: int,
    score: float,
    *,
    cls_project: str | None = None,
    client_name: str | None = None,
) -> SearchHit:
    meta: dict[str, object] = {}
    if cls_project is not None:
        meta["cls_project"] = cls_project
    if client_name is not None:
        meta["client_name"] = client_name
    return SearchHit(chunk_id=chunk_id, content="x", score=score, metadata=meta)


def test_client_match_promotes_cls_project_over_higher_score_intro() -> None:
    """汎用イントロ（高 score・client メタ無し）より実案件（cls_project 一致）を前出し。"""
    hits = [
        _chit(1, 0.95),  # 汎用イントロ（最高 score・client なし）
        _chit(2, 0.40, cls_project="出光興産"),  # 実案件（score 低いが一致）
    ]
    out = sort_by_client_match(hits, "出光興産")
    assert _ids(out) == [2, 1]  # 一致が先頭


def test_client_match_uses_client_name_too() -> None:
    """client_name（FB 由来）での一致も拾う。"""
    hits = [
        _chit(1, 0.9),
        _chit(2, 0.3, client_name="ユニー"),
    ]
    out = sort_by_client_match(hits, "ユニー")
    assert out[0].chunk_id == 2


def test_client_match_bidirectional_substring() -> None:
    """クエリ語が案件名に含まれる / 案件名がクエリ語に含まれる の双方向一致。"""
    # cls_project='出光興産株式会社' に対しクエリ 'a=出光興産'（needle ⊂ s）
    hits_a = [_chit(1, 0.9), _chit(2, 0.3, cls_project="出光興産株式会社")]
    assert sort_by_client_match(hits_a, "出光興産")[0].chunk_id == 2
    # cls_project='ユニー' に対しクエリ 'ユニーの2回目提案'（s ⊂ needle）
    hits_b = [_chit(1, 0.9), _chit(2, 0.3, cls_project="ユニー")]
    assert sort_by_client_match(hits_b, "ユニーの2回目提案")[0].chunk_id == 2


def test_client_match_within_group_score_descending() -> None:
    """一致グループ内・非一致グループ内とも score 降順を保つ（安定ソート）。"""
    hits = [
        _chit(1, 0.5, cls_project="A社"),
        _chit(2, 0.9, cls_project="A社"),
        _chit(3, 0.8),  # 非一致
        _chit(4, 0.6),  # 非一致
    ]
    out = sort_by_client_match(hits, "A社")
    assert _ids(out) == [2, 1, 3, 4]


def test_client_match_no_match_is_noop_count_preserved() -> None:
    """一致 0 件なら相対順序を保ち件数も不変（取りこぼしてもランク後退なし）。"""
    hits = [_chit(1, 0.9), _chit(2, 0.5, cls_project="別会社")]
    out = sort_by_client_match(hits, "出光興産")
    assert _ids(out) == [1, 2]
    assert len(out) == 2


def test_client_match_empty_client_is_noop() -> None:
    hits = [_chit(1, 0.5, cls_project="A社"), _chit(2, 0.9)]
    assert _ids(sort_by_client_match(hits, "")) == [1, 2]
    assert _ids(sort_by_client_match(hits, "   ")) == [1, 2]


def test_client_match_empty_list() -> None:
    assert sort_by_client_match([], "A社") == []


# ── C: 親クライアントで子コラボが出る（title/content/cls_entities も一致対象） ──


def _chit_full(
    chunk_id: int,
    score: float,
    *,
    content: str = "x",
    cls_project: str | None = None,
    title: str | None = None,
    cls_entities: object = None,
) -> SearchHit:
    meta: dict[str, object] = {}
    if cls_project is not None:
        meta["cls_project"] = cls_project
    if title is not None:
        meta["title"] = title
    if cls_entities is not None:
        meta["cls_entities"] = cls_entities
    return SearchHit(chunk_id=chunk_id, content=content, score=score, metadata=meta)


def test_client_match_by_content_promotes_collab_doc() -> None:
    """本文に『サンマルクカフェ』が出る祇園辻利コラボ資料を、サンマルクカフェ検索で前出し。"""
    intro = _chit_full(1, 0.9, content="会社紹介の一般的な導入")  # 高score・無関係
    collab = _chit_full(
        2, 0.3, content="サンマルクカフェ×祇園辻利コラボのPR施策", cls_project="祇園辻利コラボ"
    )
    out = sort_by_client_match([intro, collab], "サンマルクカフェ")
    assert out[0].chunk_id == 2  # cls_project は祇園辻利でも、本文一致でブーストされる


def test_client_match_by_title() -> None:
    hits = [
        _chit_full(1, 0.9),
        _chit_full(2, 0.3, title="20260115_株式会社サンマルクカフェ_PR施策"),
    ]
    out = sort_by_client_match(hits, "サンマルクカフェ")
    assert out[0].chunk_id == 2


def test_client_match_by_cls_entities_list() -> None:
    hits = [
        _chit_full(1, 0.9),
        _chit_full(
            2, 0.3, cls_entities=["祇園辻利", "サンマルクカフェ", "株式会社サンマルクカフェ"]
        ),
    ]
    out = sort_by_client_match(hits, "サンマルクカフェ")
    assert out[0].chunk_id == 2


def test_client_match_by_cls_entities_csv() -> None:
    hits = [_chit_full(1, 0.9), _chit_full(2, 0.3, cls_entities="祇園辻利,サンマルクカフェ")]
    out = sort_by_client_match(hits, "サンマルクカフェ")
    assert out[0].chunk_id == 2


def test_client_match_content_requires_two_chars() -> None:
    # 1文字クエリは content 一致でノイズを出さない（メタ一致のみ）。
    from teamagent.skills.search.rerank import _hit_matches_client

    h = _chit_full(1, 0.5, content="Aを含む長文")
    assert _hit_matches_client(h, "A") is False
