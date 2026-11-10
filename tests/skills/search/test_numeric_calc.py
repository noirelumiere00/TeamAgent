"""計算は架空の資料だけを使い、実サービスへ接続しない。"""

import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout, numeric
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import SearchHitOut, SearchInput
from tests.skills.search.test_fanout import META, _converse, make
from tests.skills.search.test_search_dates_exposure import _build, _hit, _pgvector


def source(text: str, chunk_id: int = 1, page: int = 4) -> SearchHitOut:
    return SearchHitOut(
        chunk_id=chunk_id,
        content=text,
        score=0.9,
        title="架空社_再配信提案書.pdf",
        page_num=page,
        url=f"https://example.test/proposal/{chunk_id}",
    )


def calculate(text: str, plan: dict, query: str = "再配信の単価") -> str:
    model = MagicMock()
    model.converse.return_value = _converse(json.dumps(plan))
    answer, _, _ = numeric.answer(query, [source(text)], model, "numeric-test")
    return answer


DIVIDE = {"operation": "divide", "operands": ["n1", "n2"]}


def test_numeric_price_has_conclusion_formula_and_each_source() -> None:
    answer = calculate("再配信費用300万円、保証視聴回数100万回", DIVIDE)
    assert answer.splitlines()[:2] == [
        "1再生あたり約3円です。",
        "計算式: 300万円 ÷ 100万回 = 3円/1回",
    ]
    for value in ("300万円", "100万回"):
        assert (
            f"出典（{value}）: 架空社_再配信提案書.pdf p.4 https://example.test/proposal/1"
        ) in answer


@pytest.mark.parametrize("rate", ["CPV: 4円", "単価: 4円", "単価4円/回", "単価は4円", "CPVは4円"])
def test_numeric_stated_price_takes_priority_and_discrepancy_keeps_both(rate: str) -> None:
    answer = calculate(
        f"再配信費用300万円、保証視聴回数100万回、{rate}", {**DIVIDE, "stated": "n3"}
    )
    assert answer.startswith("資料記載の単価は1再生あたり4円です。")
    assert "計算値は1再生あたり約3円" in answer
    assert "記載値と異なります" in answer
    assert "計算式:" in answer and "出典（4円" in answer


def test_numeric_stated_price_without_operands_is_answered() -> None:
    answer = calculate("再配信 CPV: 3円", {"operation": "stated", "stated": "n1"})
    assert answer.startswith("資料記載の単価は1再生あたり3円です。")
    assert "出典（3円）" in answer
    assert "計算式:" not in answer


def test_numeric_missing_count_never_invents_it() -> None:
    answer = calculate("再配信費用300万円", {"operation": "missing", "missing": ["count"]})
    assert "保証視聴回数" in answer and "計算できません" in answer
    assert not any(char.isdigit() for char in answer)
    assert len(answer.splitlines()) == 1


def test_numeric_matching_stated_price_still_keeps_formula() -> None:
    answer = calculate("再配信費用300万円、保証100万回、単価3円", {**DIVIDE, "stated": "n3"})
    assert answer.startswith("資料記載の単価は1再生あたり3円です。")
    assert "計算式:" in answer and "異なります" not in answer


@pytest.mark.parametrize(
    ("cost", "views", "expected"),
    [
        ("300万円", "100万回", "3"),
        ("3000千円", "100万回", "3"),
        ("３０千円", "２万回", "1.5"),
        ("1,000円", "300回", "3.3333"),
    ],
)
def test_numeric_unit_conversion(cost: str, views: str, expected: str) -> None:
    answer = calculate(f"費用{cost}、保証{views}", DIVIDE)
    assert answer.startswith(f"1再生あたり約{expected}円です。")


@pytest.mark.parametrize(
    ("query", "text", "plan", "expected"),
    [
        (
            "費用の合計",
            "制作費50万円、配信費300千円",
            {"operation": "sum", "operands": ["n1", "n2"]},
            "合計は800,000円",
        ),
        (
            "費用の差",
            "案A300万円、案B200万円",
            {"operation": "difference", "operands": ["n1", "n2"]},
            "は1,000,000円",
        ),
        ("CPM", "費用300千円、表示100万imp", DIVIDE, "1,000表示あたり約300円"),
        ("CPM", "費用300千円、表示回数100万回", DIVIDE, "1,000表示あたり約300円"),
        ("1件あたりの費用", "費用30万円、件数100件", DIVIDE, "1件あたり約3,000円"),
        ("1日あたりの費用", "費用30万円、期間10日", DIVIDE, "1日あたり約30,000円"),
    ],
)
def test_numeric_operations(query: str, text: str, plan: dict, expected: str) -> None:
    answer = calculate(text, plan, query)
    assert expected in answer and "計算式:" in answer and "出典" in answer
    if query == "CPM":
        assert "× 1,000" in answer


@pytest.mark.parametrize(
    ("text", "plan", "query"),
    [
        ("費用300万円、保証0回", DIVIDE, "CPV"),
        ("費用300万円、保証100万回", {**DIVIDE, "result": 999}, "単価"),
        (
            "費用300万円、保証100万回",
            {"operation": "divide", "operands": ["n1", "unknown"]},
            "単価",
        ),
        ("費用300万円、保証100万回", {"operation": "sum", "operands": ["n1", "n2"]}, "合計"),
        ("費用300万円、保証100万回", DIVIDE, "CPM"),
        ("費用300万円、保証100万回", {"operation": "sum", "operands": ["n1", "n1"]}, "合計"),
    ],
)
def test_numeric_invalid_plans_fail_closed(text: str, plan: dict, query: str) -> None:
    answer = calculate(text, plan, query)
    assert "計算できません" in answer and "計算式:" not in answer
    assert not any(char.isdigit() for char in answer)


def test_numeric_distinct_pages_are_cited_for_each_operand() -> None:
    model = MagicMock()
    model.converse.return_value = _converse(json.dumps(DIVIDE))
    answer, _, _ = numeric.answer(
        "CPV", [source("費用300万円", 1, 4), source("保証100万回", 2, 8)], model, "numeric"
    )
    assert "p.4 https://example.test/proposal/1" in answer
    assert "p.8 https://example.test/proposal/2" in answer


@pytest.mark.parametrize("text", ["100〜300万円", "300万円以下", "費用不明"])
def test_numeric_uncertain_values_are_not_used(text: str) -> None:
    assert numeric.quantities([source(text)]) == {}


def test_numeric_low_confidence_or_uncitable_sources_are_not_used() -> None:
    hit = source("費用300万円")
    assert not numeric.quantities([hit.model_copy(update={"is_low_confidence": True})])
    assert not numeric.quantities([hit.model_copy(update={"url": None})])
    assert numeric.quantities([hit])["n1"].value == Decimal(3_000_000)


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("compact_enabled", [False, True])
def test_numeric_search_uses_retrieval_then_code_without_summary(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, compact_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "0")
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    hit = replace(
        _hit(1, "架空社_提案書.pdf", {"page_num": 6}),
        content="架空社 再配信費用300万円、保証視聴回数100万回",
    )
    skill, _ = make([hit])
    skill._bedrock.converse.return_value = _converse(json.dumps(DIVIDE))
    skill._summarize = MagicMock(side_effect=AssertionError("モデルの要約・計算は禁止"))
    meta = {**META, "_search_compact_answer": compact_enabled}
    out = skill.run(SearchInput(query="再配信の単価を教えて"), SkillContext(metadata=meta))
    assert out.answer.startswith("1再生あたり約3円です。")
    assert "計算式: 300万円 ÷ 100万回" in out.answer
    assert "架空社_提案書.pdf p.6 https://drive.google.com/file/d/FILE1/view" in out.answer
    assert out.suggested_next is None
    skill._summarize.assert_not_called()
    skill._bedrock.converse.assert_called_once()
    message = json.loads(
        skill._bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    )
    assert message["quantities"][0]["raw"] == "300万円"


def test_numeric_retrieval_only_request_stays_retrieval_only() -> None:
    hit = replace(_hit(1, "架空社.pdf"), content="再配信費用300万円、保証100万回")
    skill = _build(_pgvector([hit]))
    out = skill.run(SearchInput(query="単価", include_answer=False), SkillContext())
    assert out.answer == ""
    skill._bedrock.converse.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "提案書を探して",
        "資料の心当たりは？",
        "資料が見当たりません",
        "案件の状況は？",
        "単価表を送って",
        "CPVが載っている提案書を探して",
        "当たり前のことを教えて",
        "行き当たりばったりな企画",
        "計算書のテンプレどこ？",
    ],
)
def test_numeric_intent_does_not_capture_normal_queries(query: str) -> None:
    assert not numeric.is_calculation(query)


def test_numeric_noncalculation_search_keeps_normal_summary() -> None:
    skill = _build(_pgvector([_hit(1, "架空社.pdf")]))
    skill._bedrock.converse.return_value = _converse("従来の要約です。")
    out = skill.run(SearchInput(query="提案書を探して"), SkillContext())
    assert out.answer.startswith("従来の要約です。")
    assert (
        "quantities"
        not in skill._bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    )
