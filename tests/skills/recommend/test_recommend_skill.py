"""他社の類似提案書・原本リンク・回答カードを検証する（外部接続なし）。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.file_record_lookup import SavedFileRecord
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.recommend.schema import RecommendInput
from teamagent.skills.recommend.skill import RecommendSkill
from teamagent.skills.search import drive_first
from teamagent.skills.search.client_match import alias_pairs
from teamagent.skills.search.query_planner import QueryPlan
from teamagent.skills.search.skill import SearchSkill


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
    search._resolve_file_urls.return_value = {}
    return RecommendSkill(search=search), search


def test_only_proposals_are_recommended() -> None:
    hits = [
        _proposal(1, "比較食品A"),
        _hit(2, 0.8, doc_type="議事録", title="比較食品B 定例議事録"),
        _hit(3, 0.7, is_sales_fb=True, client_name="比較食品C"),
        _hit(4, 0.6, doc_type="報告書"),
    ]
    skill, _ = _make_skill(hits)

    out = skill.run(RecommendInput(brief="食品メーカー向け動画提案"), SkillContext())

    assert [i.client_name for i in out.similar_proposals] == ["比較食品A"]
    assert out.similar_minutes == out.similar_sales_fb == []
    assert out.total_count == 1


def test_sales_fb_with_proposal_classification_is_not_a_proposal() -> None:
    hits = [_hit(1, 0.9, doc_type="提案書", is_sales_fb=True, client_name="比較食品A")]
    skill, _ = _make_skill(hits)

    out = skill.run(RecommendInput(brief="食品"), SkillContext())

    assert out.similar_proposals == []
    assert out.similar_sales_fb == []


def test_proposals_capped_at_three_in_retrieval_order() -> None:
    # スコアを意図的に非単調にして「retrieve_hits の並びをそのまま尊重し再ソートしない」を固定。
    # （p0=0.5, p1=0.9, p2=0.7 ... なので score 降順に再ソートすれば順序が変わる＝回帰検知できる）
    scores = [0.5, 0.9, 0.7, 0.3, 0.6]
    hits = [replace(_proposal(i, f"比較食品{i}"), score=scores[i]) for i in range(5)]
    skill, _ = _make_skill(hits)

    out = skill.run(RecommendInput(brief="x"), SkillContext())

    # 5 件中 先頭3件だけ・入力順を保持（score で再ソートしていれば [p1, p2, p0] になる）
    assert [i.client_name for i in out.similar_proposals] == ["比較食品0", "比較食品1", "比較食品2"]
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


def _proposal(n: int, client: str, *, uri: str | None = None) -> SearchHit:
    hit = _hit(
        n,
        0.66,
        doc_type="提案書",
        title=f"{client}_冷凍食品の動画提案_2026年4月.pdf",
        client_name=client,
        source_uri=uri or f"gdrive://proposal-{n}",
    )
    hit.metadata.update(source_type="gdrive", cls_industry="食品", cls_solution="動画広告")
    return hit


def test_similar_proposals_prioritize_other_clients_and_expand_the_query() -> None:
    source = [_proposal(n, "基準食品") for n in range(1, 4)]
    source[0] = replace(source[0], content="基準食品の冷凍調理品を紹介する動画施策")
    others = [_proposal(4, "比較食品A"), _proposal(5, "比較食品B"), _proposal(6, "比較食品C")]
    skill, search = _make_skill(source)
    search.retrieve_hits.side_effect = [source, [*source, *others]]
    search._resolve_file_urls.return_value = {}

    ctx = SkillContext(metadata={"user_email": "reader@example.com", "user_groups": ["sales"]})
    out = skill.run(RecommendInput(brief="基準食品の提案書に似た過去の提案書を探して"), ctx)

    assert [i.client_name for i in out.similar_proposals] == ["比較食品A", "比較食品B", "比較食品C"]
    query = search.retrieve_hits.call_args_list[1].args[0]
    assert "基準食品" not in query
    assert "食品" in query and "動画広告" in query and "冷凍食品" in query
    assert "冷凍調理品" in query
    assert search.retrieve_hits.call_args_list[1].kwargs["prefer_same_client"] is False
    assert all(call.args[1] is ctx for call in search.retrieve_hits.call_args_list)
    assert len(out.same_client_materials) == 1
    assert out.similar_minutes == out.similar_sales_fb == []


def test_recommend_output_has_case_cards_and_never_serializes_scores() -> None:
    skill, search = _make_skill([_proposal(1, "比較食品A")])
    search._resolve_file_urls.return_value = {}

    out = skill.run(RecommendInput(brief="食品の動画広告に似た提案書"), SkillContext())

    assert "score" not in out.model_dump_json()
    assert "0.66" not in out.model_dump_json()
    assert "スコア" not in out.answer
    assert "*比較食品A_冷凍食品の動画提案_2026年4月.pdf*（2026年4月）" in out.answer
    assert "似ている点" in out.answer and "食品" in out.answer and "動画広告" in out.answer
    assert "• <https://drive.google.com/file/d/proposal-1/view|資料を見る>" in out.answer
    assert len(out.answer.splitlines()) == 3


def test_unresolved_links_and_slack_threads_are_not_recommendations() -> None:
    missing = _hit(1, 0.9, doc_type="提案書", title="原本不明")
    slack = _proposal(2, "比較食品B", uri="slack://C123-1234567890.123456")
    slack.metadata.update(source_type="slack")
    skill, search = _make_skill([missing, slack, _proposal(3, "比較食品C")])
    search._resolve_file_urls.return_value = {}

    out = skill.run(RecommendInput(brief="食品の動画広告の提案書"), SkillContext())

    assert [i.client_name for i in out.similar_proposals] == ["比較食品C"]
    assert "slack://" not in out.model_dump_json()


def test_same_company_and_same_original_do_not_fill_the_list() -> None:
    first = _proposal(1, "比較食品A")
    duplicate = replace(first, chunk_id=2)
    same_company = _proposal(3, "株式会社比較食品A")
    skill, _ = _make_skill([first, duplicate, same_company, _proposal(4, "比較食品B")])

    out = skill.run(RecommendInput(brief="食品の動画広告の提案書"), SkillContext())

    assert [i.client_name for i in out.similar_proposals] == ["比較食品A", "比較食品B"]
    assert out.total_count == 2


def test_same_client_only_is_one_supplementary_line() -> None:
    skill, _ = _make_skill([_proposal(n, "基準食品") for n in range(1, 4)])

    out = skill.run(RecommendInput(brief="基準食品の提案書に似た過去の提案書"), SkillContext())

    assert out.similar_proposals == []
    assert out.total_count == 0
    assert len(out.same_client_materials) == 1
    assert len(out.answer.split("同じ会社の過去資料：")[1].splitlines()) == 1
    assert out.answer.count("資料を見る") == 1


def test_expanded_query_removes_company_without_legal_prefix() -> None:
    source = _proposal(1, "株式会社基準食品")
    source.metadata["title"] = "基準食品_冷凍食品_動画提案.pdf"
    skill, search = _make_skill([source])

    skill.run(RecommendInput(brief="株式会社基準食品の提案書に似た提案書"), SkillContext())

    assert "基準食品" not in search.retrieve_hits.call_args_list[1].args[0]


def test_brand_alias_stays_in_same_company_supplement() -> None:
    company, brand = alias_pairs()[0]
    source = _proposal(1, company)
    source.metadata["title"] = f"{brand}_動画提案.pdf"
    skill, search = _make_skill([source])
    search.retrieve_hits.side_effect = [[source], [source, _proposal(2, "比較食品A")]]

    out = skill.run(RecommendInput(brief=f"{brand}の提案書に似た過去の提案書"), SkillContext())

    assert [i.client_name for i in out.similar_proposals] == ["比較食品A"]
    assert len(out.same_client_materials) == 1
    expanded = search.retrieve_hits.call_args_list[1].args[0]
    assert company not in expanded and brand not in expanded


def test_sheet_file_name_resolves_to_drive_original() -> None:
    hit = _proposal(1, "比較食品A", uri="https://docs.google.com/spreadsheets/d/ledger/edit")
    hit.metadata.update(source_type="gsheets")
    skill, search = _make_skill([hit])
    search._resolve_file_urls.return_value = {
        hit.metadata["title"]: "https://drive.google.com/file/d/resolved-original/view"
    }

    out = skill.run(RecommendInput(brief="食品の動画広告の提案書"), SkillContext())

    search._resolve_file_urls.assert_called_once()
    assert (
        out.similar_proposals[0].source_uri
        == "https://drive.google.com/file/d/resolved-original/view"
    )
    assert "ledger" not in out.model_dump_json()


def test_slack_uses_saved_original_and_its_date(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = _proposal(1, "比較食品A", uri="slack://C123/1234567890.123456")
    hit.metadata.update(
        source_type="slack", title="比較食品Aの提案スレッド", file_names=["提案書.pdf"]
    )
    record = SavedFileRecord(
        "42",
        "1",
        hit.metadata["source_uri"],
        "saved-original",
        "https://drive.google.com/file/d/saved-original/view",
        "比較食品A_提案書_2025年8月.pdf",
    )
    saved = MagicMock(return_value=[record])
    monkeypatch.setattr(drive_first, "resolve_saved_files", saved)
    skill, search = _make_skill([hit])
    ctx = SkillContext(metadata={"user_email": "reader@example.com", "user_groups": ["sales"]})

    out = skill.run(RecommendInput(brief="食品の動画広告の提案書"), ctx)

    saved.assert_called_once_with(search, [hit], ctx)
    assert search.retrieve_hits.call_args.args[1] is ctx
    assert out.similar_proposals[0].source_uri == record.drive_url
    assert "*比較食品A_提案書_2025年8月.pdf*（2025年8月）" in out.answer
    assert "スレッド" not in out.answer


def test_slack_attachment_never_uses_a_same_name_file(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = _proposal(1, "比較食品A", uri="slack://C123/1234567890.123456")
    hit.metadata.update(source_type="slack", file_names=["提案書.pdf"])
    monkeypatch.setattr(drive_first, "resolve_saved_files", lambda *_: [None])
    skill, search = _make_skill([hit])
    search._resolve_file_urls.return_value = {
        "提案書.pdf": "https://drive.google.com/file/d/wrong-original/view"
    }

    out = skill.run(RecommendInput(brief="食品の動画広告の提案書"), SkillContext())

    assert out.similar_proposals == []
    assert "wrong-original" not in out.model_dump_json()


@pytest.mark.parametrize("planned", [False, True])
def test_comparison_retrieval_does_not_prefer_a_company_in_source_text(
    monkeypatch: pytest.MonkeyPatch, planned: bool
) -> None:
    hits = [_proposal(1, "比較食品A"), _proposal(2, "基準食品")]
    planner = MagicMock()
    planner.plan.return_value = QueryPlan(
        paraphrases=[], hyde_answer="", doc_type="提案書", client_names=["基準食品"]
    )
    embedder = MagicMock()
    embedder.embed.return_value = [0.1]
    skill = SearchSkill(
        bedrock=MagicMock(),
        pgvector=MagicMock(),
        embedder=embedder,
        use_new_schema=True,
        use_client_boost=True,
        use_knowledge_filters=True,
        query_planner=planner if planned else None,
    )
    pool = MagicMock(return_value=hits)
    boost = MagicMock(return_value=hits)
    match = MagicMock(return_value="基準食品")
    monkeypatch.setattr(skill, "_pool_search", pool)
    monkeypatch.setattr(skill, "_apply_client_boost", boost)
    monkeypatch.setattr(skill, "_match_client", match)
    monkeypatch.setattr(skill, "_client_match_sort", True)

    actual = skill.retrieve_hits(
        "基準食品の冷凍食品の動画提案書", SkillContext(), prefer_same_client=False
    )

    assert [h.chunk_id for h in actual] == [1, 2]
    assert all(call.kwargs["metadata_contains"] is None for call in pool.call_args_list)
    boost.assert_not_called()
    match.assert_not_called()
    assert skill._use_client_boost and skill._client_match_sort
