"""明示業種の事例は、意味検索・言い換えが外れても本文の根拠になる。"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_fanout import META, make


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "0")
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "build_summary_client", MagicMock)
    monkeypatch.setattr(fanout, "build_expansion_planner", lambda: MagicMock(bedrock=MagicMock()))
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "timeout", 1, 0.0)
    )


def context() -> SkillContext:
    return SkillContext(
        metadata={
            **META,
            "_search_direct_answer": True,
            "_search_direct_answer_kinds": ("not_found",),
        }
    )


@pytest.mark.parametrize("alias_incomplete", [False, True])
def test_explicit_food_case_fallback_reaches_summary_and_sources(
    monkeypatch: pytest.MonkeyPatch, alias_incomplete: bool
) -> None:
    skill, pg = make([])
    row = {
        "title": "みなと食品様：ハンバーグ",
        "source_uri": "https://docs.google.com/presentation/d/FoodCase/edit",
        "case_client": "みなと食品",
        "case_brand": "ハンバーグ",
        "case_product": "ハンバーグ",
        "case_effect": "調理アレンジを紹介したショート動画施策",
        "case_source": "deck",
        "case_external_use": "ng",
        "case_industry": "食品",
    }
    pg.list_case_studies.side_effect = lambda *a, **kw: (
        [row] if kw.get("industry") == "食品" else []
    )
    if alias_incomplete:
        skill._alias_expansion = True
        monkeypatch.setattr(skill, "_retry_aliases", lambda *a: ([], ["食品 事例"], True))
    summary = MagicMock(return_value=("みなと食品では調理アレンジを紹介しています。", 0.0))
    monkeypatch.setattr(skill, "_summarize", summary)

    out = skill.run(SearchInput(query="食品メーカーのショート動画の事例ってある？"), context())

    assert out.found
    assert "見つかりませんでした" not in out.answer
    assert row["source_uri"] in out.answer
    assert "近隣" not in out.answer
    summary.assert_called_once()
    roots = summary.call_args.args[1]
    assert roots and row["case_effect"] in roots[0].content
    assert roots[0].metadata["case_industry"] == "食品"
    assert any(hit.url == row["source_uri"] for hit in out.hits)
    assert any(
        call.kwargs.get("industry") == "食品" for call in pg.list_case_studies.call_args_list
    )


def test_failed_explicit_industry_lookup_never_claims_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill, pg = make([])
    pg.list_case_studies.side_effect = TimeoutError("case lookup deadline")
    out = skill.run(SearchInput(query="食品メーカーのショート動画の事例ってある？"), context())
    assert "確認できませんでした" in out.answer
    assert "見つかりませんでした" not in out.answer
    assert out.direct_answer is None
    assert any(source["lane"] == "case" and source["status"] == "error" for source in out.sources)


def test_industry_fallback_does_not_promote_unrelated_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill, pg = make([])
    pg.list_case_studies.return_value = [
        {
            "title": "別業種の事例",
            "source_uri": "https://example.test/unrelated",
            "case_industry": "化粧品",
            "case_source": "deck",
        }
    ]
    summary: Any = MagicMock(side_effect=AssertionError("unrelated case must not be summarized"))
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(SearchInput(query="食品メーカーの事例"), context())
    assert not out.found
    assert "unrelated" not in out.model_dump_json()
    summary.assert_not_called()


def test_named_case_lookup_is_a_compact_answer_source_without_vector_hits(monkeypatch):
    skill, pg = make([])
    pg.list_case_studies.return_value = [
        {
            "title": "みなと食品 ハンバーグ事例",
            "case_client": "みなと食品",
            "case_industry": "食品",
            "case_source": "deck",
            "source_uri": "https://example.test/named-case",
            "case_effect": "調理アレンジの投稿を配信しました。",
        }
    ]
    monkeypatch.setattr(
        skill, "_summarize", lambda *a, **kw: ("調理アレンジの投稿を配信しました。[S:-1]", 0.0)
    )
    ctx = context()
    ctx.metadata["_search_compact_answer"] = True
    out = skill.run(SearchInput(query="みなと食品の事例ある？", filter_client="みなと食品"), ctx)
    assert "調理アレンジの投稿を配信しました。" in out.answer
    assert "https://example.test/named-case" in out.answer
    assert "確認できません" not in out.answer
