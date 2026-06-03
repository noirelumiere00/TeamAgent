"""忠実性スコアラー（⑤）の決定的テスト（課金0）。

引用抽出・捏造引用検出・無引用の扱いを純関数で検証する。
"""

from __future__ import annotations

from teamagent.orchestrator.faithfulness import (
    extract_chunk_ids_from_tool_json,
    extract_cited_chunk_ids,
    score_faithfulness,
)


def test_extract_cited_from_answer() -> None:
    a = "勝ち筋は[chunk_id: 123]。料金は chunk_id：456 を参照。chunk_id=789 も。"
    assert extract_cited_chunk_ids(a) == [123, 456, 789]


def test_extract_from_tool_json() -> None:
    j = '{"hits":[{"chunk_id": 123,"content":"x"},{"chunk_id":456}]}'
    assert extract_chunk_ids_from_tool_json(j) == [123, 456]


def test_score_clean() -> None:
    s = score_faithfulness("根拠[chunk_id: 1][chunk_id: 2]", [1, 2, 3])
    assert s.is_clean
    assert s.fabricated == ()
    assert s.valid == (1, 2)
    assert s.citation_validity == 1.0
    assert s.has_citations


def test_score_fabricated() -> None:
    s = score_faithfulness("根拠[chunk_id: 1] と [chunk_id: 999]", [1, 2])
    assert not s.is_clean
    assert s.fabricated == (999,)  # available に無い引用＝捏造の痕跡
    assert s.valid == (1,)
    assert s.citation_validity == 0.5


def test_score_no_citations() -> None:
    s = score_faithfulness("一般論で根拠を引用していない回答", [1, 2])
    assert not s.has_citations
    assert s.is_clean  # 捏造引用は無い
    assert s.citation_validity == 1.0  # 引用なしは validity で減点しない（has_citations で別途判定）


def test_dedup_cited() -> None:
    s = score_faithfulness("[chunk_id: 1][chunk_id: 1][chunk_id: 2]", [1, 2])
    assert s.cited == (1, 2)  # 重複除去・順序保持
