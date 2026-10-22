"""r69 の業種・名指し比較・短文を、外部接続なしで固定する。"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import compact, fanout
from teamagent.skills.search.campaign_files import scope_by_industry
from teamagent.skills.search.comparison import named_comparison_clients
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.schema import SearchInput, SlackHitOut
from tests.skills.search.test_comparison import _campaign
from tests.skills.search.test_drive_only_case_list import _case
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


@pytest.fixture(autouse=True)
def drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("planner", [False, True])
def test_industry_argument_never_restricts_drive_sql(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, planner: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hit = replace(_case(1, cls_industry=None, title="調味料会社の提案"), content="調理動画を提案。")
    skill, pg = make([hit])
    skill._use_cohere_rerank = False
    if planner:
        skill._query_planner = MagicMock()
        skill._query_planner.plan.return_value = MagicMock(
            industry="食品",
            paraphrases=["食品の動画"],
            hyde_answer="",
            client_names=[],
            doc_type=None,
        )
    pg.search_similar_new_schema.side_effect = lambda **kw: (
        [hit] if kw.get("filter_industry") is None else []
    )
    out = skill.run(
        SearchInput(
            query="食品メーカーのショート動画の事例出して",
            filter_industry="食品",
            strict_industry=True,
        ),
        SkillContext(metadata=META),
    )
    assert out.found and [ref.drive_file_id for ref in out.displayed_refs] == ["FILE1"]
    assert all(
        c.kwargs.get("filter_industry") is None for c in pg.search_similar_new_schema.call_args_list
    )
    assert pg.list_case_studies.call_args.kwargs["limit"] == 30
    assert pg.list_case_studies.call_args.kwargs.get("soft_industry") is True


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_unknown_industry_is_last_and_known_mismatch_is_excluded(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hits = [
        replace(_case(1, cls_industry=None, title="分類なし"), content="動画を提案。"),
        _case(2, cls_industry="化粧品"),
        replace(_case(3, cls_industry=None, title="外食会社の提案"), content="調理を紹介。"),
        replace(_case(4, cls_industry=None, title="菓子会社の提案"), content="商品を紹介。"),
        _case(5),
        replace(_case(6, cls_industry=None, title="化粧品の施策"), content="美容を紹介。"),
    ]
    skill, _ = make(hits)
    out = skill.run(SearchInput(query="食品の事例出して"), SkillContext(metadata=META))
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["FILE5", "FILE3", "FILE4", "FILE1"]


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("missing_second", [False, True])
def test_named_comparison_collects_each_company_and_saves_links(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, fallback: bool, missing_second: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    item = SlackItem(
        SlackHitOut(channel="#shared", permalink="https://example.slack.com/archives/C1/p123"),
        "架空会社甲は調理実演、架空会社乙は商品紹介。",
    )
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok", (item,)))
    skill, pg = make([])
    skill._start_slack_lookup = MagicMock(return_value=MagicMock())
    skill._collect_slack_lookup = MagicMock(return_value=SlackLookup("ok", "ok", (item,)))
    pg.list_client_names.return_value = ["架空会社甲", "架空会社乙"]
    docs = [
        _case(1, title="架空会社甲 提案書", client_name="架空会社甲"),
        _case(2, title="架空会社乙 レポート", client_name="架空会社乙"),
    ]
    pg.find_documents_for_advertiser.side_effect = lambda conn, name, **kw: (
        [docs[0]] if name == "架空会社甲" else [] if missing_second else [docs[1]]
    )
    normal = "架空会社甲は調理実演、架空会社乙は商品紹介が中心です。"
    skill._summarize = MagicMock(
        side_effect=[("" if fallback else normal + "[S:slack0]", 0.0), (normal, 0.0)]
    )
    out = skill.run(
        SearchInput(query="架空会社甲と架空会社乙の事例を比べて", filter_client="架空会社甲"),
        SkillContext(metadata=META),
    )
    assert {c.args[1] for c in pg.find_documents_for_advertiser.call_args_list} == {
        "架空会社甲",
        "架空会社乙",
    }
    assert all(c.kwargs["drive_only"] for c in pg.find_documents_for_advertiser.call_args_list)
    expected = docs[:1] if missing_second else docs
    assert [ref.drive_file_id for ref in out.displayed_refs] == [
        f"FILE{h.chunk_id}" for h in expected
    ]
    assert "架空会社甲: <https://drive.google.com/file/d/FILE1/view|資料を見る>" in out.answer
    assert (
        "架空会社乙: <https://drive.google.com/file/d/FILE2/view|資料を見る>" in out.answer
    ) is (not missing_second)
    summary_hits = skill._summarize.call_args_list[0].args[1]
    assert {h.chunk_id for h in expected} <= {h.chunk_id for h in summary_hits}
    assert skill._summarize.call_args_list[0].kwargs["compact_drive_mode"] is True


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_comparison_prefers_code_facts_without_model_summary(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hits = [
        replace(
            _campaign(i, f"施策{i}", f"再生数: 平均 {i * 100}"),
            metadata={
                **_case(i).metadata,
                "campaign_aggregate": "true",
                "advertiser": f"架空会社{i}",
                "campaign": f"施策{i}",
            },
        )
        for i in (1, 2)
    ]
    skill, _ = make(hits)
    skill._summarize = MagicMock(return_value=("# 見出し\n根拠のない999倍です。", 0.0))
    out = skill.run(
        SearchInput(query="施策を比較して"),
        SkillContext(
            metadata={
                **META,
                "_search_direct_answer": True,
                "_search_direct_answer_kinds": ("comparison",),
            }
        ),
    )
    assert out.answer_format == "compact" and "200回" in out.answer
    assert out.direct_answer == out.answer and out.direct_answer_kind == "comparison"
    skill._summarize.assert_not_called()


def test_compact_prompt_requires_plain_cited_grounded_sentences() -> None:
    prompt = compact.prompt(drive_mode=True)
    assert "箇条書き" in prompt and "各文の末尾" in prompt
    assert "数字は引用した資料にある値だけ" in prompt
    assert "数字は引用した資料にある値だけ" not in compact.prompt()


def test_drive_off_keeps_industry_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "0")
    skill, pg = make([_case(1)])
    skill.run(
        SearchInput(query="食品の事例出して", filter_industry="食品"), SkillContext(metadata=META)
    )
    assert pg.search_similar_new_schema.call_args.kwargs["filter_industry"] == "食品"
    assert not pg.list_case_studies.call_args.kwargs.get("soft_industry")


def test_boost_and_drive_floor_also_drop_industry_sql() -> None:
    slack = _case(9, source_type="slack", source_uri="https://example.slack.com/archives/C1/p123")
    skill, pg = make([slack])
    skill._use_client_boost = True
    skill._use_cohere_rerank = True
    skill._drive_pool_floor = 2
    skill._apply_cohere_rerank = lambda **kw: kw["hits"]
    pg.list_client_names.return_value = ["架空会社甲"]
    skill.retrieve_hits(
        "架空会社甲の食品施策",
        SkillContext(metadata=META),
        filter_industry="食品",
        strict_industry=True,
    )
    calls = pg.search_similar_new_schema.call_args_list
    assert any((c.kwargs.get("metadata_contains") or {}).get("__client__") for c in calls)
    assert any(c.kwargs.get("filter_source_types") == ["gdrive"] for c in calls)
    assert all(c.kwargs.get("filter_industry") is None for c in calls)
    assert all(c.kwargs.get("strict_industry") is False for c in calls)


@pytest.mark.parametrize("term", ["外食", "飲料", "調味料", "菓子", "ラーメン"])
def test_food_neighbors_precede_unclassified_candidates(term: str) -> None:
    unknown = replace(_case(1, title="分類なし", cls_industry=None), content="動画を提案。")
    food = replace(
        _case(2, title="匿名案件", cls_industry=None, client_name=term + "会社"),
        content="動画を提案。",
    )
    assert scope_by_industry([unknown, food], "食品", soft=True) == [food, unknown]
    assert scope_by_industry([unknown, food], "食品") == []


def test_comparison_names_keep_word_boundaries_and_deduplicate_long_names() -> None:
    assert named_comparison_clients(
        "架空会社甲株式会社と架空会社乙とExampleBetaを比較して",
        ["架空会社甲", "架空会社甲株式会社", "架空会社乙", "Example"],
    ) == ["架空会社甲株式会社", "架空会社乙"]
    assert named_comparison_clients("架空会社甲の事例出して", ["架空会社甲"]) == []


def test_failed_named_company_does_not_skip_other_company() -> None:
    skill, pg = make([])
    doc = _case(2, title="架空会社乙 レポート", client_name="架空会社乙")
    pg.find_documents_for_advertiser.side_effect = [RuntimeError("unavailable"), [doc]]
    probe = {}
    fanout.collect_named_sources(
        skill,
        MagicMock(),
        None,
        SkillContext(metadata=META),
        probe,
        comparison_clients=["架空会社甲", "架空会社乙"],
    )
    assert probe["named"] == [doc] and probe["named_lane"].status == "error"


def test_shared_comparison_document_labels_both_companies_and_saves_one_ref() -> None:
    refs = []
    answer = compact.append_comparison_drive_links(
        "比較です。\n<https://drive.google.com/file/d/FILE1/view|資料を見る>",
        [_case(1, title="架空会社甲と架空会社乙の提案")],
        ["架空会社甲", "架空会社乙"],
        refs,
    )
    assert "架空会社甲: <https://drive.google.com/file/d/FILE1/view|資料を見る>" in answer
    assert "架空会社乙: <https://drive.google.com/file/d/FILE1/view|資料を見る>" in answer
    assert len(refs) == 1
