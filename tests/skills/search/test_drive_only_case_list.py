"""事例一覧は Drive 資料のあるカードだけ。実サービスへの接続なし。"""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import DocumentReference, SearchInput, SlackHitOut
from tests.skills.search.test_fanout import META, make

QUERY = "食品メーカーのショート動画の事例出して"
POST = "https://vector-workspcae.slack.com/archives/CFOOD/p1234567890123456"
EMPTY = "Drive に資料のある事例は見つかりませんでした。"


def _case(index: int, **meta: Any) -> SearchHit:
    return SearchHit(
        index,
        "食品のショート動画施策を実施。再生数を確認した。",
        0.9,
        {
            "document_id": f"doc-{index}",
            "title": f"食品会社{index}",
            "source_type": "gdrive",
            "source_uri": f"gdrive://FILE{index}",
            "cls_industry": "食品",
            "updated_at": "2026-09-18",
            **meta,
        },
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    yield


def _run(skill: Any, *, compact: bool = False, query: str = QUERY) -> Any:
    return skill.run(
        SearchInput(query=query, top_k=1),
        SkillContext(
            metadata={
                **META,
                "_search_compact_answer": compact,
                "_search_direct_answer": compact,
                "_search_direct_answer_kinds": ("list_fast",),
            }
        ),
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("rerank", [False, True])
def test_collects_five_after_filtering_and_top_k(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, compact: bool, rerank: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    excluded = [
        _case(91, source_type="slack", source_uri=POST, is_sales_fb=True),
        _case(92, is_sales_fb=True, case_source="deck"),  # Drive 付き FB も事例ではない。
        _case(93, source_type="slack", source_uri=POST),
        _case(
            94,
            source_type="gsheets",
            source_uri="https://docs.google.com/spreadsheets/d/SHEET/edit",
        ),
        _case(95, topic="案件決定"),
    ]
    hits = [*excluded, *[_case(i) for i in range(1, 7)]]
    skill, pg = make(hits)
    skill._use_cohere_rerank = rerank
    skill._apply_cohere_rerank = lambda **kw: kw["hits"]
    skill._summarize = MagicMock(return_value=("営業フィードバック " + POST, 0.0))
    pg.search_similar_new_schema.side_effect = lambda **kw: hits[: kw["limit"]]
    out = _run(skill, compact=compact)
    assert out.answer.count("|資料を見る>") == 5
    assert out.answer.startswith("*食品会社1*（2026年9月）")
    assert "• 食品のショート動画施策を実施。\n• 再生数を確認した。" in out.answer
    assert "slack.com" not in out.answer and "spreadsheets" not in out.answer
    assert "営業フィードバック" not in out.answer and "食品会社9" not in out.answer
    assert "食品会社6" not in out.answer and "/app" not in out.answer
    assert out.answer_format == "list" and out.suggested_next is None
    if compact:
        assert out.direct_answer == out.answer and out.direct_answer_kind == "list_fast"
    assert [ref.drive_file_id for ref in out.displayed_refs] == [f"FILE{i}" for i in range(1, 6)]
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("only_slack", [False, True])
def test_zero_drive_cases_never_falls_back_to_slack(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, only_slack: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hits = [_case(90, source_type="slack", source_uri=POST)] if only_slack else []
    skill, _ = make(hits)
    skill._summarize = MagicMock(return_value=("Slack の投稿 " + POST, 0.0))
    out = _run(skill)
    assert out.answer == EMPTY
    assert out.answer_format == "list" and not out.found and out.displayed_refs == []
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize(
    "query,subject", [(QUERY, "食品"), ("ベビー用品の事例出して", "ベビー用品")]
)
def test_structured_lookup_searches_past_five_sheet_only_rows(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, query: str, subject: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, pg = make([])
    rows = [
        {
            "document_id": f"case-{i}",
            "title": f"{subject}事例{i}",
            "case_client": f"会社{i}",
            "case_industry": subject,
            "case_source": "sheet" if i < 6 else "deck",
            "source_uri": "https://docs.google.com/spreadsheets/d/SHEET/edit"
            if i < 6
            else f"gdrive://CASE{i}",
            "case_effect": f"{subject}の動画を配信。結果を計測した。",
        }
        for i in range(1, 12)
    ]
    pg.list_case_studies.side_effect = lambda *a, **kw: rows[: kw["limit"]]
    out = _run(skill, query=query)
    assert out.answer.count("|資料を見る>") == 5
    assert [ref.drive_file_id for ref in out.displayed_refs] == [f"CASE{i}" for i in range(6, 11)]


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_sales_feedback_question_keeps_feedback(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make([_case(90, source_type="slack", source_uri=POST, is_sales_fb=True)])
    skill._summarize = MagicMock(
        return_value=("食品会社の営業フィードバック: 温度感B。[S:90]", 0.0)
    )
    monkeypatch.setattr(fanout, "build_summary_client", MagicMock)
    out = _run(skill, query="食品メーカーの営業の温度感を教えて")
    assert "温度感B" in out.answer
    assert out.answer_format == "compact"
    assert out.displayed_refs == [] and f"<{POST}|会話の記録>" in out.answer
    skill._summarize.assert_called_once()


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("outside_allowlist", [False, True])
def test_ineligible_matches_legacy_answer(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, outside_allowlist: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, pg = make([_case(90, source_type="slack", source_uri=POST)])
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "0")
    baseline = _run(skill)
    if outside_allowlist:
        monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
        monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "other@example.test")
    actual = _run(skill)
    assert actual.model_dump() == baseline.model_dump()
    assert all(call.kwargs["limit"] == 30 for call in pg.search_similar_new_schema.call_args_list)


def test_live_sales_feedback_with_drive_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    skill, pg = make([_case(1)])
    ref = DocumentReference(
        kind="drive", drive_file_id="FB", url="https://drive.google.com/file/d/FB/view"
    )
    item = SlackItem(
        SlackHitOut(
            channel="#proj-ショート動画_営業フィードバック情報", permalink=POST, reference=ref
        ),
        "食品メーカーへの提案・温度感B https://drive.google.com/file/d/FB/view",
    )
    pg.list_authorized_document_references.return_value = [
        {"source_type": "gdrive", "external_id": "FB", "title": "営業メモ"}
    ]
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok", (item,)))
    out = _run(skill)
    assert "温度感" not in out.answer and "/d/FB/" not in out.answer
    assert out.answer.count("|資料を見る>") == 1


def test_relevance_threshold_still_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    skill, _ = make([_case(1), replace(_case(2), score=0.01)])
    skill._apply_cohere_rerank = lambda **kw: kw["hits"]
    out = _run(skill)
    assert out.answer.count("|資料を見る>") == 1
    assert "食品会社2" not in out.answer
