"""訪問先と名前の似た会社を混同せず、資料は確認済みの保存版を示す。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search import compact, drive_first, expand, fanout, visit_cases
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.schema import DocumentReference, SearchInput
from tests.skills.search.test_fanout import META, _converse, make
from tests.skills.search.test_fanout import setup as setup

POST = "https://example.slack.com/archives/C1/p1792727460000000"
DRIVE = "https://drive.google.com/file/d/SAVED/view"


def _vault_hit(chunk_id: int = 1, **meta: Any) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        content="商品の実演動画を配信。\n利用シーンを紹介。",
        score=0.9,
        metadata={"title": f"匿名提案{chunk_id}.pdf", "source_type": "gdrive", **meta},
    )


@pytest.fixture(autouse=True)
def drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(drive_first, "resolve_saved_files", lambda s, hits, c: [None] * len(hits))
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))


def test_normal_fallback_preserves_authorized_slack_attachment_reference() -> None:
    hit = _vault_hit(1, source_type="slack", source_uri=POST, file_names=["0824_提案資料.pdf"])
    ref = drive_first.canonical_reference({**hit.metadata, "drive_url": DRIVE})
    hit.metadata[drive_first.REFERENCE_KEY] = ref
    refs: list[DocumentReference] = []
    answer = compact.append_used_drive_links("紹介投稿があります。[S:1]", [hit], refs)
    assert answer == f"紹介投稿があります。\n\n<{DRIVE}|資料を見る>"
    assert refs == [ref]


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("industry", ["食品", None])
def test_unknown_visit_company_uses_category_cases_only(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, industry: str | None
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    planner = MagicMock()
    planner.plan.return_value = SimpleNamespace(industry=industry, cost_usd=0.0)
    monkeypatch.setattr(expand, "build_expansion_planner", lambda: planner)
    hits = [
        _vault_hit(1, title="星空 食品提案", cls_project="株式会社星空", cls_industry="金融"),
        *[
            _vault_hit(
                i, title=f"匿名食品案件{i}", cls_project=f"匿名食品会社{i}", cls_industry="食品"
            )
            for i in range(2, 6)
        ],
    ]
    for hit in hits:
        hit.metadata.update(source_type="gdrive", source_uri=f"gdrive://DOC{hit.chunk_id}")
    skill, pg = make(hits)
    pg.list_client_names.return_value = ["株式会社星空"]
    skill._apply_cohere_rerank = lambda **kw: kw["hits"]
    skill._summarize = MagicMock(return_value=("正式社名を確認してみてください。", 0.0))
    out = skill.run(
        SearchInput(
            query="乳製品系商品の星空堂って会社に行くんだけど、知っておくべき事例とかってあるかな？"
        ),
        SkillContext(metadata=META),
    )
    assert out.answer.startswith("星空堂との過去案件は見つかりませんでした。")
    assert "星空 食品提案" not in out.answer
    assert "確認して" not in out.answer and out.suggested_next is None
    assert out.query_client == "星空堂"
    if industry:
        assert out.answer.count("|資料を見る>") == 3
        assert "近い事例" in out.answer
        assert len(out.displayed_refs) == 3
        assert out.answer.count("• 商品の実演動画を配信。") == 3
        assert out.answer.count("• 利用シーンを紹介。") == 3
        assert pg.list_case_studies.call_args.kwargs.get("industry") == "食品"
    else:
        assert "近い事例は見つかりませんでした" in out.answer
        assert not out.displayed_refs
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("company", ["株式会社星空堂", "星空堂"])
def test_exact_visit_company_keeps_its_own_cases(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, company: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hit = _vault_hit(1, title="匿名訪問先の提案", cls_project=company, source_uri="gdrive://OWN")
    skill, pg = make([hit])
    pg.list_client_names.return_value = [company, "星空"]
    out = skill.run(
        SearchInput(query="食品系商品の星空堂って会社に行く。知っておくべき事例ある？"),
        SkillContext(metadata=META),
    )
    assert "星空堂との過去案件" in out.answer and "見つかりませんでした" not in out.answer
    assert "匿名訪問先の提案" in out.answer
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["OWN"]


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("authorized", [False, True])
def test_saved_attachment_survives_normal_fallback_without_index_match(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, authorized: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hit = _vault_hit(1, source_type="slack", source_uri=POST, file_names=["0824_提案資料.pdf"])
    saved = SimpleNamespace(
        drive_url=DRIVE, title="0824_提案資料.pdf", record_number="42", branch_number="1"
    )
    monkeypatch.setattr(
        drive_first,
        "resolve_saved_files",
        lambda s, hits, c: [saved if authorized else None for _ in hits],
    )
    skill, pg = make([hit])
    pg.list_authorized_document_references.return_value = []
    normal = f"紹介投稿があります。[S:1]\n資料: [0824_提案資料]({POST})"
    skill._bedrock.converse.side_effect = [_converse(""), _converse(normal)]
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = skill.run(SearchInput(query="提案の進め方は？"), SkillContext(metadata=META))
    assert "[0824_提案資料](" not in out.answer
    assert "|0824_提案資料>" not in out.answer
    assert skill._bedrock.converse.call_count == 2
    if authorized:
        assert f"<{DRIVE}|資料を見る>" in out.answer
        assert [ref.drive_file_id for ref in out.displayed_refs] == ["SAVED"]
    else:
        assert DRIVE not in out.answer and not out.displayed_refs
        assert f"<{POST}|会話の記録>" in out.answer


def test_only_exact_registered_aliases_count_as_same_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visit_cases, "alias_pairs", lambda: (("星空堂", "銀河工房"),))
    assert visit_cases.same_company(_vault_hit(1, cls_project="株式会社銀河工房"), "星空堂")
    assert not visit_cases.same_company(_vault_hit(1, cls_project="星空"), "星空堂")
    # 10-23 の実例と同じ形: 先頭が同じだけの別会社（「◯◯カン」と「◯◯ラボ」）を同じ会社にしない。
    assert not visit_cases.same_company(_vault_hit(1, cls_project="株式会社星空ラボ"), "星空カン")
    assert not visit_cases.same_company(_vault_hit(1, cls_project="銀河工房"), "星空堂東日本")
    assert not visit_cases.same_company(_vault_hit(1, cls_entities=["星空堂"]), "星空堂")
    assert not visit_cases.same_company(_vault_hit(), "株式会社")


@pytest.mark.parametrize("label", ["0824_提案資料", "資料を見る", "proposal.pdf"])
@pytest.mark.parametrize("markdown", [False, True])
def test_final_guard_unlinks_material_titles_but_keeps_conversation(
    label: str, markdown: bool
) -> None:
    link = f"[{label}]({POST})" if markdown else f"<{POST}|{label}>"
    text = compact.strip_linkless_invitation(
        f"資料: {link}\n<{POST}|会話の記録>\n正式社名を確認してみてください。\n"
        f"商材名を確認すると見つかるかもしれません。\n<{DRIVE}|資料を見る>"
    )
    assert f"資料: {label}" in text
    assert f"<{POST}|会話の記録>" in text
    assert f"<{DRIVE}|資料を見る>" in text
    assert "確認して" not in text and "確認すると" not in text


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_visit_path_is_limited_to_the_authorized_drive_first_user(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "0")
    skill, _ = make([])
    query = SearchInput(query="食品系商品の星空堂って会社に行く。知っておくべき事例ある？")
    baseline = skill.run(query, SkillContext(metadata=META))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "outside@example.test")
    skill, _ = make([])
    actual = skill.run(query, SkillContext(metadata=META))
    assert baseline.model_dump_json() == actual.model_dump_json()


def test_incomplete_lookup_does_not_claim_the_company_has_no_cases() -> None:
    text = visit_cases.answer(visit_cases.Visit("星空堂", "食品"), [], "食品", [], complete=False)
    assert "検索が完了せず確認できませんでした" in text
    assert "過去案件は見つかりませんでした" not in text
