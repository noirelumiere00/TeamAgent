"""動画系の施策フィルタで、分類の割れた資料を落とさない（2026-10-14 本番 DM で確認）。

本番で起きたこと: 「伊藤ハムのショート動画の施策実績を教えて」から cls_solution=動画広告 が
自動で付いた。金庫では伊藤ハムの提案書 PDF・事例 PDF・営業 FB が cls_solution=ショート動画
だったため全部落ち、分類の無い施策実績 2 件だけが残った（金庫全体では「動画広告」1,112 件・
「ショート動画」1,024 件に割れている）。

固定すること:
- 動画系の語では cls_solution を自動で付けない → ショート動画の分類の資料も候補に入る
- DM のモデルが明示した動画系の filter_solution（MCP の印あり）も使わない
- /app の画面で利用者が選んだ値（MCP の印なし）は今までどおり厳密に使う
- 動画系以外の明示（SNS運用 等）は今までどおり厳密に使う

フェイクは test_campaign_floor の _SqlLikePg（WHERE は search_similar_new_schema の引数どおりに絞る）。
"""

from __future__ import annotations

from teamagent.skills.base import SkillContext
from teamagent.skills.search.composite import MCP_SURFACE_CTX_KEY
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_campaign_floor import _build, _Chunk, _Doc, _SqlLikePg

QUERY = "伊藤ハムのショート動画の施策実績を教えて"
PDF_MARK = "伊藤ハム 提案書 ショート動画 アレンジ提案 PDF_BODY"


def _corpus() -> list[_Doc]:
    return [
        _Doc(
            document_id="itoham-proposal",
            source_type="gdrive",
            source_uri="https://drive.google.com/file/d/itohamprop/view",
            title="提案_伊藤ハム米久HD様【ご提案書】.pdf",
            metadata={
                "cls_solution": "ショート動画",
                "cls_doc_type": "提案書",
                "cls_project": "伊藤ハム米久HD",
            },
            chunks=[_Chunk(chunk_id=700, content=PDF_MARK, score=0.84)],
        ),
        _Doc(
            document_id="other-video-ad",
            source_type="gdrive",
            source_uri="https://drive.google.com/file/d/otherad/view",
            title="他社 動画広告 ご提案",
            metadata={"cls_solution": "動画広告", "cls_doc_type": "提案書", "cls_project": "他社"},
            chunks=[_Chunk(chunk_id=701, content="他社 動画広告 提案", score=0.60)],
        ),
        _Doc(
            document_id="sns-doc",
            source_type="gdrive",
            source_uri="https://drive.google.com/file/d/sns/view",
            title="伊藤ハム SNS運用 報告",
            metadata={
                "cls_solution": "SNS運用",
                "cls_doc_type": "報告書",
                "cls_project": "伊藤ハム",
            },
            chunks=[_Chunk(chunk_id=702, content="伊藤ハム SNS運用 報告 SNS_BODY", score=0.70)],
        ),
    ]


def _in_pool(pools: list[list[str]], mark: str) -> bool:
    return any(mark in doc for pool in pools for doc in pool)


def test_short_video_classified_proposal_reaches_rerank() -> None:
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    skill.run(input=SearchInput(query=QUERY, top_k=5), ctx=SkillContext())
    assert "cls_solution" not in (pg.calls[0]["metadata_filters"] or {})
    assert _in_pool(pools, PDF_MARK), (
        "ショート動画の分類の提案書が候補に入っていない（10-14 の失敗）"
    )


def test_model_chosen_video_filter_is_dropped_on_dm_surface() -> None:
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    skill.run(
        input=SearchInput(query=QUERY, top_k=5, filter_solution="動画広告"),
        ctx=SkillContext(metadata={MCP_SURFACE_CTX_KEY: True}),
    )
    assert "cls_solution" not in (pg.calls[0]["sticky_filters"] or {})
    assert _in_pool(pools, PDF_MARK)


def test_app_screen_choice_stays_strict() -> None:
    """/app で利用者が選んだ値は MCP の印が無いので今までどおり厳密に使う。"""
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    skill.run(
        input=SearchInput(query=QUERY, top_k=5, filter_solution="動画広告"),
        ctx=SkillContext(),
    )
    assert pg.calls[0]["sticky_filters"] == {"cls_solution": "動画広告"}
    assert not _in_pool(pools, PDF_MARK)


def test_non_video_explicit_filter_stays_strict_on_dm_surface() -> None:
    pg = _SqlLikePg(_corpus())
    skill, pools = _build(pg)
    skill.run(
        input=SearchInput(query="伊藤ハムの報告", top_k=5, filter_solution="SNS運用"),
        ctx=SkillContext(metadata={MCP_SURFACE_CTX_KEY: True}),
    )
    assert pg.calls[0]["sticky_filters"] == {"cls_solution": "SNS運用"}
    assert not _in_pool(pools, PDF_MARK)
