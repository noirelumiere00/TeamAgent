"""r70: 業種引数・30候補・構造化3事例を、外部接続なしで再現する。"""

from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import drive_first, fanout
from teamagent.skills.search.campaign_files import scope_by_industry
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.fanout_sources import SourceResult
from teamagent.skills.search.schema import SearchInput, SlackHitOut
from tests.skills.search.test_drive_only_case_list import EMPTY, POST, QUERY, _case
from tests.skills.search.test_fanout import META, make


@pytest.fixture(autouse=True)
def drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    expanded = replace(_case(90, source_type="slack", source_uri=POST), content="食品の動画")
    monkeypatch.setattr(
        fanout,
        "run_expansion",
        lambda *a, **kw: ExpansionResult(
            terms=("食品の動画",),
            industry="食品",
            hits=(expanded,),
            status="ok",
            elapsed_ms=3,
            cost_usd=0.0,
        ),
    )
    items = tuple(
        SlackItem(SlackHitOut(channel="#shared", permalink=POST), "食品の動画") for _ in range(4)
    )
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok", items))


def test_unknown_metadata_survives_incidental_other_industry_words() -> None:
    unknown = [
        replace(
            _case(i, cls_industry=value, title="匿名案件"), content="店舗のシステムで動画を紹介。"
        )
        for i, value in enumerate((None, "食品メーカー・加工", "未分類"), 1)
    ]
    known = _case(4, cls_industry="食料品")
    other = _case(5, cls_industry="金融")
    assert scope_by_industry([*unknown, other, known], "食品", soft=True) == [known, *unknown]
    assert scope_by_industry([*unknown, other, known], "食品") == [known]


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("main_score", [0.01, 0.9])
def test_structured_cases_survive_threshold_and_bind_only_matching_drive_files(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, visible: bool, main_score: float
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    # 30候補のうち29件は閾値未満。残る施策も業種不明で、本文に別業種の一般語がある。
    hits = [
        replace(
            _case(i, cls_industry=None, source_type="gsheets", source_uri="/app", title="匿名案件"),
            content="店舗のシステムで動画を紹介。",
            score=main_score if i == 1 else 0.01,
        )
        for i in range(1, 31)
    ]
    hits[0].metadata.update(
        campaign_aggregate="true",
        advertiser="架空会社甲",
        campaign="春の実演",
        title="架空会社甲 春の実演",
    )
    skill, pg = make(hits)
    pg.list_client_names.return_value = []
    pg.get_industry_for_client.return_value = None
    skill._apply_cohere_rerank = lambda **kw: kw["hits"]
    skill._campaign_files = True
    skill._summarize = MagicMock(return_value=("Slackで補う", 0.0))
    rows: list[dict[str, Any]] = [
        dict(
            document_id="case-1",
            title="架空会社甲 春の実演",
            case_client="架空会社甲",
            case_source="campaign_db",
            case_industry="食品",
            case_product="春の実演",
            source_uri="https://docs.google.com/spreadsheets/d/LEDGER/edit",
        ),
        dict(
            document_id="case-2",
            title="架空会社乙 実施事例",
            case_client="架空会社乙",
            case_source="deck",
            case_industry="食品",
            source_uri="/app/case-2",
            drive_file_id="DECK" if visible else None,
        ),
        dict(
            document_id="case-3",
            title="架空会社丙 資料なし",
            case_client="架空会社丙",
            case_source="sheet",
            case_industry="食品",
            source_uri="https://docs.google.com/spreadsheets/d/LEDGER/edit",
        ),
    ]
    pg.list_case_studies.return_value = rows
    # 12行あるだけでは正本ではない。同じ広告主かつ同じ施策の1行だけを結び付ける。
    pg.find_drive_files_for_advertisers.return_value = [
        dict(
            advertiser="架空会社甲",
            title="春の実演 レポート" if visible and i == 0 else "別案件資料",
            source_uri=f"gdrive://REPORT{i}",
            cls_industry=None,
            updated_at="2026-09-18",
        )
        for i in range(12)
    ]
    pg.list_authorized_document_references.return_value = (
        [dict(source_type="gdrive", external_id="REPORT0", title="春の実演 レポート")]
        if visible
        else []
    )
    retrieved: dict[str, int] = {}
    retrieve = skill._retrieve

    def trace(*args: Any, **kwargs: Any) -> Any:
        result = retrieve(*args, **kwargs)
        probe = kwargs["fanout_probe"]
        retrieved.update(
            candidates=len(probe["candidates"]), hits=len(result), cases=len(probe["case"].items)
        )
        return result

    skill._retrieve = trace
    out = skill.run(
        SearchInput(query=QUERY, top_k=5, filter_industry="食品"), SkillContext(metadata=META)
    )
    assert retrieved == {"candidates": 30, "hits": int(main_score >= 0.4), "cases": 3}
    assert pg.list_case_studies.call_args.kwargs["stage"] == 4
    assert all(
        c.kwargs.get("filter_industry") is None for c in pg.search_similar_new_schema.call_args_list
    )
    if visible:
        assert out.found
        assert {ref.drive_file_id for ref in out.displayed_refs} == {"DECK", "REPORT0"}
        assert len(out.displayed_refs) == out.answer.count("|資料を見る>") == 2
        assert "架空会社甲 春の実演" in out.answer and "架空会社乙 実施事例" in out.answer
        assert "架空会社丙" not in out.answer and "REPORT1/" not in out.answer
    else:
        assert not out.found and out.answer == EMPTY and out.displayed_refs == []
    assert "slack.com" not in out.answer and "spreadsheets" not in out.answer
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("source", ["sheet", "campaign_db", "deck"])
@pytest.mark.parametrize("file_id", ["DECK", "bad/id", "https://example.test/file", ""])
def test_only_ingested_deck_id_can_supply_its_own_reference(source: str, file_id: str) -> None:
    hit = _case(1, source_type="other", source_uri=None, case_source=source, drive_file_id=file_id)
    # 対象者向けのannotateを通さない既存カード経路は変えない。
    assert drive_first.canonical_reference(hit.metadata).kind != "drive"
    skill, _ = make([])
    groups, _, _ = drive_first.annotate(skill, [[hit]], (), SkillContext(metadata=META))
    ref = groups[0][0].metadata[drive_first.REFERENCE_KEY]
    assert (ref.kind == "drive") is (source == "deck" and file_id == "DECK")


def test_deck_source_uri_wins_and_sheet_uri_is_never_promoted() -> None:
    skill, _ = make([])
    hits = [
        _case(1, source_type="other", source_uri=uri, case_source="deck", drive_file_id="DECK")
        for uri in ("gdrive://ORIGINAL", "https://docs.google.com/spreadsheets/d/LEDGER/edit")
    ]
    groups, _, _ = drive_first.annotate(skill, [hits], (), SkillContext(metadata=META))
    original, sheet = (hit.metadata[drive_first.REFERENCE_KEY] for hit in groups[0])
    assert original.drive_file_id == "ORIGINAL"
    assert sheet.kind == "sheet" and sheet.drive_file_id is None


@pytest.mark.parametrize(
    "query,initial_failure",
    [("春の実演の事例出して", False), ("事例を出して", False), ("事例を出して", True)],
)
def test_campaign_link_survives_product_scope_and_late_case_lookup(
    monkeypatch: pytest.MonkeyPatch, query: str, initial_failure: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    # 後段の補助検索は主検索が見つかった場合の回収予算で走らせる。
    main = _case(10, source_type="gsheets", source_uri="/app", title="実施メモ")
    skill, pg = make([main] if query == "事例を出して" else [])
    if initial_failure:
        lookup = fanout.case_lookup_by_industry

        def fail_initial(*args: Any, **kwargs: Any) -> Any:
            if kwargs["industry"] is None:
                return SourceResult(
                    source="case", status="failed", reason="db_error", incomplete=True
                )
            return lookup(*args, **kwargs)

        monkeypatch.setattr(fanout, "case_lookup_by_industry", fail_initial)
    pg.list_client_names.return_value = []
    pg.list_case_studies.return_value = [
        dict(
            document_id="case-1",
            title="架空会社甲 春の実演",
            case_client="架空会社甲",
            case_source="campaign_db",
            case_industry="食品",
            case_product="春の実演",
            source_uri="https://docs.google.com/spreadsheets/d/LEDGER/edit",
        )
    ]
    pg.find_drive_files_for_advertisers.return_value = [
        dict(
            advertiser="架空会社甲",
            title="春の実演 レポート",
            source_uri="gdrive://REPORT",
            cls_industry="食品",
        )
    ]
    skill._campaign_files = True
    out = skill.run(SearchInput(query=query), SkillContext(metadata=META))
    assert out.found and [ref.drive_file_id for ref in out.displayed_refs] == ["REPORT"]
    pg.find_drive_files_for_advertisers.assert_called_once()
