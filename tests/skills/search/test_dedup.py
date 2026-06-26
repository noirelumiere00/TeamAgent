"""検索結果の「資料の被り」対策（dedup.py）の単体テスト。

- collapse_near_duplicates: テンプレ文の near-dup 畳み込み・しきい値境界・決定性
- cap_per_document: 同一資料の上限・複数 doc 混在・document_id 欠落の source_uri フォールバック
- エッジ（空入力・1 件）
"""

from __future__ import annotations

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.dedup import (
    cap_per_document,
    collapse_near_duplicates,
)


def _hit(
    chunk_id: int,
    content: str,
    score: float,
    *,
    document_id: object | None = None,
    source_uri: str | None = None,
) -> SearchHit:
    meta: dict[str, object] = {}
    if document_id is not None:
        meta["document_id"] = document_id
    if source_uri is not None:
        meta["source_uri"] = source_uri
    return SearchHit(chunk_id=chunk_id, content=content, score=score, metadata=meta)


# テンプレページ（表紙・会社紹介）を模した near-identical な文字列
_TEMPLATE_A = "株式会社ベクトル 会社紹介 私たちは100年企業を目指すPR会社です。"
_TEMPLATE_B = (
    "株式会社ベクトル 会社紹介  私たちは100年企業を目指す、PR会社です！"  # 記号/空白差のみ
)
_BODY = "ユニー様向け第2回提案：縦型動画でZ世代の指名検索を増やす施策。KPIは保存率。"


# --- collapse_near_duplicates -------------------------------------------------


def test_collapse_keeps_template_once_and_body_survives() -> None:
    hits = [
        _hit(1, _TEMPLATE_A, 0.80),
        _hit(2, _TEMPLATE_B, 0.70),  # テンプレ near-dup → 落ちる
        _hit(3, _BODY, 0.60),  # 別本文 → 残る
    ]
    result = collapse_near_duplicates(hits, jaccard_threshold=0.9)
    contents = [h.content for h in result]
    assert _TEMPLATE_A in contents  # score 最大のテンプレ代表が残る
    assert _TEMPLATE_B not in contents
    assert _BODY in contents
    assert len(result) == 2


def test_collapse_keeps_highest_score_representative() -> None:
    # 低 score 側を先に並べても、残るのは score 最大の hit
    hits = [
        _hit(2, _TEMPLATE_B, 0.30),
        _hit(1, _TEMPLATE_A, 0.95),
    ]
    result = collapse_near_duplicates(hits, jaccard_threshold=0.9)
    assert len(result) == 1
    assert result[0].chunk_id == 1
    assert result[0].score == 0.95


def test_collapse_is_order_independent() -> None:
    # 順序を入れ替えても同一の chunk_id 集合・同一代表になる（決定性）
    base = [
        _hit(1, _TEMPLATE_A, 0.80),
        _hit(2, _TEMPLATE_B, 0.70),
        _hit(3, _BODY, 0.60),
    ]
    forward = collapse_near_duplicates(base, jaccard_threshold=0.9)
    reversed_in = collapse_near_duplicates(list(reversed(base)), jaccard_threshold=0.9)
    assert [h.chunk_id for h in forward] == [h.chunk_id for h in reversed_in]


def test_collapse_threshold_boundary_high_keeps_distinct() -> None:
    # 別本文同士はしきい値 0.9 では畳まれない（共通語があっても Jaccard < 0.9）
    hits = [
        _hit(1, "縦型動画でZ世代の指名検索を増やす施策", 0.80),
        _hit(2, "店頭POPで来店客の購買単価を上げる施策", 0.70),
    ]
    result = collapse_near_duplicates(hits, jaccard_threshold=0.9)
    assert len(result) == 2


def test_collapse_threshold_low_collapses_more() -> None:
    # しきい値を下げると、緩い類似でも畳まれる（境界挙動の確認）
    hits = [
        _hit(1, "縦型動画でZ世代の指名検索を増やす施策です", 0.80),
        _hit(2, "縦型動画でZ世代の指名検索を増やす取り組み", 0.70),
    ]
    strict = collapse_near_duplicates(hits, jaccard_threshold=0.95)
    loose = collapse_near_duplicates(hits, jaccard_threshold=0.5)
    assert len(strict) == 2
    assert len(loose) == 1


def test_collapse_empty_and_single() -> None:
    assert collapse_near_duplicates([]) == []
    one = [_hit(1, _BODY, 0.5)]
    out = collapse_near_duplicates(one)
    assert [h.chunk_id for h in out] == [1]


def test_collapse_does_not_mutate_input() -> None:
    hits = [_hit(1, _TEMPLATE_A, 0.8), _hit(2, _TEMPLATE_B, 0.7)]
    snapshot = list(hits)
    collapse_near_duplicates(hits)
    assert hits == snapshot


# --- cap_per_document ---------------------------------------------------------


def test_cap_limits_same_document_to_n() -> None:
    hits = [
        _hit(1, "p1", 0.90, document_id=42),
        _hit(2, "p2", 0.80, document_id=42),
        _hit(3, "p3", 0.70, document_id=42),  # 3 本目 → 落ちる（cap=2）
    ]
    result = cap_per_document(hits, max_per_doc=2)
    assert [h.chunk_id for h in result] == [1, 2]


def test_cap_drops_lowest_score_when_over_cap() -> None:
    # score 順で上から N。低 score 側が落ちる（入力順は問わない）
    hits = [
        _hit(3, "p3", 0.10, document_id=7),
        _hit(1, "p1", 0.90, document_id=7),
        _hit(2, "p2", 0.50, document_id=7),
    ]
    result = cap_per_document(hits, max_per_doc=2)
    kept_ids = {h.chunk_id for h in result}
    assert kept_ids == {1, 2}  # 0.90 と 0.50 が残り 0.10 が落ちる


def test_cap_preserves_input_order_of_kept() -> None:
    # 落とさない hit の相対順序は入力順のまま
    hits = [
        _hit(1, "p1", 0.90, document_id=7),
        _hit(2, "p2", 0.50, document_id=7),
        _hit(3, "p3", 0.10, document_id=7),
    ]
    result = cap_per_document(hits, max_per_doc=2)
    assert [h.chunk_id for h in result] == [1, 2]


def test_cap_multiple_docs_each_under_limit() -> None:
    hits = [
        _hit(1, "a1", 0.90, document_id=1),
        _hit(2, "b1", 0.85, document_id=2),
        _hit(3, "a2", 0.80, document_id=1),
        _hit(4, "b2", 0.75, document_id=2),
        _hit(5, "a3", 0.70, document_id=1),  # doc1 の 3 本目 → 落ちる
    ]
    result = cap_per_document(hits, max_per_doc=2)
    doc1 = [h for h in result if h.metadata["document_id"] == 1]
    doc2 = [h for h in result if h.metadata["document_id"] == 2]
    assert len(doc1) == 2
    assert len(doc2) == 2
    assert 5 not in [h.chunk_id for h in result]


def test_cap_falls_back_to_source_uri_when_no_document_id() -> None:
    hits = [
        _hit(1, "p1", 0.90, source_uri="gdrive://X"),
        _hit(2, "p2", 0.80, source_uri="gdrive://X"),
        _hit(3, "p3", 0.70, source_uri="gdrive://X"),  # 3 本目 → 落ちる
        _hit(4, "q1", 0.60, source_uri="gdrive://Y"),
    ]
    result = cap_per_document(hits, max_per_doc=2)
    x = [h for h in result if h.metadata.get("source_uri") == "gdrive://X"]
    assert len(x) == 2
    assert 4 in [h.chunk_id for h in result]  # 別 source_uri は残る


def test_cap_keeps_hits_without_any_identifier() -> None:
    # document_id も source_uri も無い hit は cap 対象外＝常に残る
    hits = [
        _hit(1, "p1", 0.90),
        _hit(2, "p2", 0.80),
        _hit(3, "p3", 0.70),
    ]
    result = cap_per_document(hits, max_per_doc=2)
    assert [h.chunk_id for h in result] == [1, 2, 3]


def test_cap_zero_or_negative_disables() -> None:
    hits = [
        _hit(1, "p1", 0.9, document_id=1),
        _hit(2, "p2", 0.8, document_id=1),
        _hit(3, "p3", 0.7, document_id=1),
    ]
    assert len(cap_per_document(hits, max_per_doc=0)) == 3
    assert len(cap_per_document(hits, max_per_doc=-1)) == 3


def test_cap_empty_and_single() -> None:
    assert cap_per_document([], max_per_doc=2) == []
    one = [_hit(1, "x", 0.5, document_id=1)]
    assert [h.chunk_id for h in cap_per_document(one, max_per_doc=2)] == [1]


def test_cap_does_not_mutate_input() -> None:
    hits = [
        _hit(1, "p1", 0.9, document_id=1),
        _hit(2, "p2", 0.8, document_id=1),
        _hit(3, "p3", 0.7, document_id=1),
    ]
    snapshot = list(hits)
    cap_per_document(hits, max_per_doc=2)
    assert hits == snapshot


# --- 組み合わせ（near-dup 畳み込み → per-doc cap）の順序 -----------------------


def test_pipeline_collapse_then_cap() -> None:
    # 同一テンプレが 2 資料に跨って 2 本 + doc1 の本文 2 本。
    # collapse でテンプレ 1 本化 → cap で doc1 を 2 本までに。
    hits = [
        _hit(1, _TEMPLATE_A, 0.95, document_id=1),
        _hit(2, _TEMPLATE_B, 0.90, document_id=2),  # テンプレ near-dup → collapse で落ちる
        _hit(3, _BODY, 0.85, document_id=1),
        _hit(4, "ユニー様向け第2回提案の補足：撮影体制と納期。", 0.80, document_id=1),
    ]
    collapsed = collapse_near_duplicates(hits, jaccard_threshold=0.9)
    capped = cap_per_document(collapsed, max_per_doc=2)
    # collapse 後: テンプレ(chunk1) + body(chunk3) + 補足(chunk4) = 3 本（全部 doc1）
    # cap 後: doc1 は上位 2 本（score 0.95, 0.85）
    assert {h.chunk_id for h in capped} == {1, 3}
