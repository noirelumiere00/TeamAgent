from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.answer_mode import classify_answer_mode, is_case_list, strip_advice
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_answer_modes import _hit
from tests.skills.search.test_fanout import META, make


@pytest.mark.parametrize(
    "query",
    [
        "ベビー用品の事例出して",
        "食品の実績見せて",
        "案件ある？",
        "施策ちょうだい",
        "ベビー用品の事例",
    ],
)
def test_case_request_is_list(query):
    assert classify_answer_mode(query) == "list"


@pytest.mark.parametrize("query", ["施策実績", "事例", "実績", "案件", "施策"])
def test_bare_case_noun_does_not_request_a_list(query):
    assert classify_answer_mode(query) == "fact"
    assert not is_case_list(query)


@pytest.mark.parametrize("query", ["ベビー用品の事例", "伊藤ハムの実績", "食品メーカーの施策実績"])
def test_targeted_case_noun_requests_a_list(query):
    assert classify_answer_mode(query) == "list"
    assert is_case_list(query)


@pytest.mark.parametrize("query", ["JAL の過去提案で何が効いた？", "JAL の過去提案で効いたのは？"])
def test_what_worked_requests_analysis_without_advice(query):
    assert classify_answer_mode(query) == "insight"
    answer = "実施案が採用されています。\n\n💡 推奨アクション\n次回も同じ条件で試す。"
    assert strip_advice(answer, query) == "実施案が採用されています。"


def test_baby_list_merges_five_cases_without_recommendations(monkeypatch):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    expanded = tuple(_hit(i, f"ベビー用品 案件{i}") for i in range(2, 8))
    monkeypatch.setattr(
        fanout,
        "run_expansion",
        lambda *a, **kw: ExpansionResult((), "日用品", expanded, "ok", 1, 0.0),
    )
    skill, pg = make([_hit(1, "ベビー用品 案件1")])
    summary = MagicMock(
        return_value=(
            "事例1件。\n\n💡 推奨アクション\n紹介を増やす。\n\n共感が効くパターンです。",
            0.0,
        )
    )
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(
        SearchInput(query="ベビー用品の事例出して"),
        SkillContext(metadata={**META, "_search_compact_answer": True}),
    )
    assert len(out.answer.splitlines()) == 5
    assert all("—" in line and "https://" in line for line in out.answer.splitlines())
    assert "推奨" not in out.answer and "パターン" not in out.answer
    summary.assert_not_called()
    assert out.answer_format == "list"
    assert out.suggested_next is None
    assert any(
        c.kwargs.get("stage") == 5
        and c.kwargs.get("product") == "ベビー用品"
        and c.kwargs.get("industry") is None
        for c in pg.list_case_studies.call_args_list
    )


def test_fact_summary_drops_model_advice():
    from teamagent.skills.search.skill import SearchSkill
    from tests.skills.search.test_answer_modes import _fake_bedrock, _FakeEmbedder, _pgvector

    b = _fake_bedrock()
    b.converse.return_value = replace(
        b.converse.return_value,
        text="撮影日は資料に記載があります。\n\n💡 推奨アクション\n営業に確認しましょう。\n\n共感が効くパターンです。",
    )
    skill = SearchSkill(
        bedrock=b, pgvector=_pgvector([]), embedder=_FakeEmbedder(), use_new_schema=True
    )
    answer, _ = skill._summarize("撮影日はいつ？", [_hit(1, "資料")], "test")
    assert answer == "撮影日は資料に記載があります。"


def test_nonfanout_case_list_does_not_start_a_second_answer(monkeypatch):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    skill, _ = make([_hit(1, "ベビー用品 案件1")])
    monkeypatch.setattr(skill, "_should_defer_answer", lambda *args: True)
    followup = MagicMock(return_value="後ほどお届けします。")
    monkeypatch.setattr(skill, "_start_followup_answer", followup)
    out = skill.run(
        SearchInput(query="ベビー用品の事例出して"),
        SkillContext(metadata={**META, "_search_compact_answer": True}),
    )
    followup.assert_not_called()
    assert out.answer_format == "list"
    assert len(out.answer.splitlines()) == 1
    assert "—" in out.answer and "https://" in out.answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_case_without_a_verified_link_does_not_revive_inventory(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    monkeypatch.setattr(
        fanout,
        "run_expansion",
        lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0),
    )
    hit = replace(_hit(1, "ベビー用品"), metadata={"title": "ベビー用品"})
    skill, _ = make([hit])
    out = skill.run(
        SearchInput(query="ベビー用品の事例出して"),
        SkillContext(metadata={**META, "_search_compact_answer": True}),
    )
    assert "資料リンク" in out.answer
    assert len(out.answer.splitlines()) == 1
    assert "データベースの資料" not in out.answer


def test_separate_advice_body_is_removed_but_sources_remain():
    from teamagent.skills.search.answer_mode import strip_advice

    text = "撮影日は確認済みです。\n\n💡 推奨アクション\n\n営業に確認しましょう。\n\n出典: https://example.test/source"
    assert (
        strip_advice(text, "撮影日はいつ？")
        == "撮影日は確認済みです。\n\n出典: https://example.test/source"
    )
    assert strip_advice(text, "次の打ち手を考えて") == text
