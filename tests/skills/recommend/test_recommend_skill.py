"""RecommendSkill の単体テスト (SearchSkill.retrieve_hits をモック・DB 非依存)。

検証: SearchHit のメタ振り分け (提案書/議事録/営業FB) / 各バケット上限3 / 空結果 /
RLS 用 user_email の retrieve_hits 受け渡し。Bedrock は呼ばない (近傍提示のみ)。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.recommend.schema import RecommendInput
from teamagent.skills.recommend.skill import RecommendSkill


def _hit(
    chunk_id: int,
    score: float,
    *,
    doc_type: str | None = None,
    is_sales_fb: bool = False,
    title: str | None = None,
    source_uri: str | None = None,
    client_name: str | None = None,
    cls_project: str | None = None,
) -> SearchHit:
    meta: dict[str, object] = {}
    if doc_type is not None:
        meta["cls_doc_type"] = doc_type
    if is_sales_fb:
        meta["is_sales_fb"] = True
    if title is not None:
        meta["title"] = title
    if source_uri is not None:
        meta["source_uri"] = source_uri
    if client_name is not None:
        meta["client_name"] = client_name
    if cls_project is not None:
        meta["cls_project"] = cls_project
    return SearchHit(chunk_id=chunk_id, content=f"chunk {chunk_id}", score=score, metadata=meta)


def _make_skill(hits: list[SearchHit]) -> tuple[RecommendSkill, MagicMock]:
    search = MagicMock()
    search.retrieve_hits.return_value = hits
    return RecommendSkill(search=search), search


def test_buckets_by_doc_type_and_sales_fb() -> None:
    hits = [
        _hit(1, 0.9, doc_type="提案書", title="アース製薬 提案", client_name="アース製薬"),
        _hit(2, 0.8, doc_type="議事録", title="花王 定例議事録"),
        _hit(3, 0.7, is_sales_fb=True, client_name="東芝", source_uri="https://drive/abc"),
        _hit(4, 0.6, doc_type="報告書"),  # どのバケットにも入らない
    ]
    skill, _ = _make_skill(hits)

    out = skill.run(RecommendInput(brief="日用品メーカー向けTikTok提案"), SkillContext())

    assert [i.title for i in out.similar_proposals] == ["アース製薬 提案"]
    assert [i.title for i in out.similar_minutes] == ["花王 定例議事録"]
    assert [i.client_name for i in out.similar_sales_fb] == ["東芝"]
    assert out.similar_sales_fb[0].source_uri == "https://drive/abc"
    assert out.total_count == 3  # 報告書は除外
    # スコアと cls_project が item に乗る
    assert out.similar_proposals[0].score == 0.9
    assert out.similar_proposals[0].client_name == "アース製薬"


def test_sales_fb_takes_priority_over_doc_type() -> None:
    """is_sales_fb が立つ hit は cls_doc_type='提案書' でも営業FBバケットに入る。"""
    hits = [_hit(1, 0.9, doc_type="提案書", is_sales_fb=True, client_name="資生堂")]
    skill, _ = _make_skill(hits)

    out = skill.run(RecommendInput(brief="コスメ"), SkillContext())

    assert out.similar_proposals == []
    assert len(out.similar_sales_fb) == 1
    assert out.similar_sales_fb[0].client_name == "資生堂"


def test_each_bucket_capped_at_three() -> None:
    # スコアを意図的に非単調にして「retrieve_hits の並びをそのまま尊重し再ソートしない」を固定。
    # （p0=0.5, p1=0.9, p2=0.7 ... なので score 降順に再ソートすれば順序が変わる＝回帰検知できる）
    scores = [0.5, 0.9, 0.7, 0.3, 0.6]
    hits = [_hit(i, scores[i], doc_type="提案書", title=f"p{i}") for i in range(5)]
    skill, _ = _make_skill(hits)

    out = skill.run(RecommendInput(brief="x"), SkillContext())

    # 5 件中 先頭3件だけ・入力順を保持（score で再ソートしていれば [p1, p2, p0] になる）
    assert [i.title for i in out.similar_proposals] == ["p0", "p1", "p2"]
    assert [i.score for i in out.similar_proposals] == [0.5, 0.9, 0.7]
    assert out.total_count == 3


def test_empty_hits_returns_empty_buckets() -> None:
    skill, search = _make_skill([])

    out = skill.run(RecommendInput(brief="前例のない新規業態"), SkillContext())

    assert out.similar_proposals == []
    assert out.similar_minutes == []
    assert out.similar_sales_fb == []
    assert out.total_count == 0
    search.retrieve_hits.assert_called_once()


def test_passes_top_k_and_industry_to_retrieve() -> None:
    skill, search = _make_skill([])

    skill.run(RecommendInput(brief="化粧品提案", industry="化粧品", top_k=20), SkillContext())

    args, kwargs = search.retrieve_hits.call_args
    assert args[0] == "化粧品提案"  # query は位置引数
    assert kwargs["top_k"] == 20
    assert kwargs["filter_industry"] == "化粧品"


def test_user_email_is_passed_through_via_ctx_for_rls() -> None:
    """RLS 用 user_email / user_groups は ctx をそのまま retrieve_hits に渡すことで既存配線に届く。"""
    skill, search = _make_skill([])
    ctx = SkillContext(
        metadata={
            "user_email": "sales@vectorinc.co.jp",
            "user_groups": ["sales", "team-a"],
        }
    )

    skill.run(RecommendInput(brief="案件A"), ctx)

    # ctx (= 第2位置引数) に user_email / user_groups が乗ったまま retrieve_hits に渡る
    # （RLS の GUC は _retrieve 側が app.user_email / groups として接続にセットする既存配線）
    passed_ctx = search.retrieve_hits.call_args.args[1]
    assert passed_ctx.metadata["user_email"] == "sales@vectorinc.co.jp"
    assert passed_ctx.metadata["user_groups"] == ["sales", "team-a"]
