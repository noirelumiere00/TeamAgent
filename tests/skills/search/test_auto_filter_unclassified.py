"""自動推定の分類フィルタで、分類の付いていない文書を消さない（2026-10-02）。

本番で起きたこと: 「ADK経由で受注したショート動画施策を知りたい」から
cls_phase=受注 / cls_solution=動画広告 が自動で付き、厳密一致の AND になった。
10-01 に取り込んだ #proj-01案件決定-同行依頼 の 65 件は Bedrock の費用上限で分類が失敗して
cls_* を 1 つも持たず、一律に除外された（検索に一度も出なかった）。

固定すること:
- 既定 ON: 未分類の文書は「不明」として自動フィルタを通る
- 別の値が付いた文書（cls_phase=提案 等）は従来どおり外れる
- SEARCH_AUTO_FILTERS_ALLOW_UNCLASSIFIED=false で従来の厳密一致に戻る（＝本番の失敗の再現）
- ユーザー明示のフィルタ（sticky）は厳密のまま

フェイクは test_campaign_floor の _SqlLikePg（本物の SQL の SELECT 句を解析し、WHERE は
search_similar_new_schema の引数どおりに絞る）を使う。
"""

from __future__ import annotations

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_campaign_floor import _build, _Chunk, _Doc, _SqlLikePg

QUERY = "ADK経由で受注したショート動画施策を知りたい"
ADK_POST = "ADK九州経由 ローソン ショート動画 受注 同行依頼"


def _corpus() -> list[_Doc]:
    docs = [
        # 分類済み・条件に合う（受注・動画広告）が ADK とは無関係の資料が多数。
        _Doc(
            document_id=f"won-{i}",
            source_type="gsheets",
            source_uri=f"https://docs.google.com/spreadsheets/d/won{i}",
            title=f"受注案件 {i}",
            metadata={"cls_phase": "受注", "cls_solution": "動画広告", "cls_project": f"他社{i}"},
            chunks=[_Chunk(chunk_id=100 + i, content=f"他社{i} ショート動画 受注", score=0.70)],
        )
        for i in range(8)
    ]
    # 未分類の Slack 投稿（#proj-01 の取り込み分と同じく cls_* を 1 つも持たない）。
    docs.append(
        _Doc(
            document_id="proj01-post",
            source_type="slack",
            source_uri="slack://C08MH3MG02F/1790000000.000100",
            title="#proj-01案件決定-同行依頼 1790000000.000100",
            metadata={"channel_id": "C08MH3MG02F"},
            chunks=[_Chunk(chunk_id=900, content=ADK_POST, score=0.86)],
        )
    )
    # 分類済みで別の値（提案段階）の ADK 資料。自動フィルタで外れるべき。
    docs.append(
        _Doc(
            document_id="adk-proposal",
            source_type="gdrive",
            source_uri="https://docs.google.com/presentation/d/adkprop",
            title="ADK様 ご提案",
            metadata={"cls_phase": "提案", "cls_solution": "動画広告", "cls_project": "ADK"},
            chunks=[_Chunk(chunk_id=901, content="ADK 提案 ショート動画", score=0.88)],
        )
    )
    return docs


def _hit_ids(out) -> set[int]:  # type: ignore[no-untyped-def]
    return {h.chunk_id for h in out.hits}


def test_unclassified_post_survives_auto_filters_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEARCH_AUTO_FILTERS_ALLOW_UNCLASSIFIED", raising=False)
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    out = skill.run(input=SearchInput(query=QUERY, top_k=5), ctx=SkillContext())

    assert pg.calls[0]["metadata_filters"] == {"cls_phase": "受注", "cls_solution": "動画広告"}
    assert pg.calls[0]["metadata_filters_allow_missing"] is True
    assert any(ADK_POST in doc for pool in pools for doc in pool), (
        "未分類の投稿が候補に入っていない"
    )
    assert 900 in _hit_ids(out)
    assert 901 not in _hit_ids(out)  # 提案段階と分類済みの資料は従来どおり外れる


def test_env_false_restores_strict_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """従来の挙動＝本番の失敗の再現: 未分類の投稿は候補にも入らない。"""
    monkeypatch.setenv("SEARCH_AUTO_FILTERS_ALLOW_UNCLASSIFIED", "false")
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    out = skill.run(input=SearchInput(query=QUERY, top_k=5), ctx=SkillContext())

    assert pg.calls[0]["metadata_filters_allow_missing"] is False
    assert not any(ADK_POST in doc for pool in pools for doc in pool)
    assert 900 not in _hit_ids(out)


def test_explicit_filters_stay_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """ユーザーが明示した資料種別（sticky）は soft 化しない＝未分類は外れたまま。"""
    monkeypatch.delenv("SEARCH_AUTO_FILTERS_ALLOW_UNCLASSIFIED", raising=False)
    pg = _SqlLikePg(_corpus())
    skill, _ = _build(pg)
    out = skill.run(
        input=SearchInput(query=QUERY, top_k=5, filter_solution="動画広告"),
        ctx=SkillContext(),
    )
    assert pg.calls[0]["sticky_filters"] == {"cls_solution": "動画広告"}
    assert 900 not in _hit_ids(out)
