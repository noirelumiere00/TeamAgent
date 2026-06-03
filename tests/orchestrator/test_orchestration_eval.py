"""オーケストレーション評価ロジックの決定的テスト（Phase 4・課金0）。

`score_case` / `summarize` は純関数なので実 Bedrock 不要で全分岐を検証できる。
ゴールドセット自体の健全性（id 一意・期待が空でない等）も点検する。
"""

from __future__ import annotations

from teamagent.orchestrator.eval import (
    GOLD_CASES,
    GoldCase,
    score_case,
    summarize,
)

_CASE = GoldCase(
    id="c",
    goal="g",
    expect_all=("search", "proposal_draft"),
    expect_any=("clientkarte", "search"),
    forbid=("mail_constraints",),
    max_turns=5,
)


def test_pass_when_all_expectations_met() -> None:
    s = score_case(_CASE, ["search", "proposal_draft", "search"], num_turns=4)
    assert s.passed
    assert s.reasons == ()
    assert s.tools_called == ("search", "proposal_draft")  # 重複除去・順序保持


def test_fail_missing_required() -> None:
    s = score_case(_CASE, ["search"], num_turns=3)  # proposal_draft 欠落
    assert not s.passed
    assert s.missing_required == ("proposal_draft",)
    assert any("未実行" in r for r in s.reasons)


def test_fail_missing_any() -> None:
    case = GoldCase(id="x", goal="g", expect_any=("clientkarte",), max_turns=5)
    s = score_case(case, ["search"], num_turns=2)
    assert not s.passed
    assert s.missing_any is True


def test_fail_forbidden_called() -> None:
    s = score_case(_CASE, ["search", "proposal_draft", "mail_constraints"], num_turns=4)
    assert not s.passed
    assert s.forbidden_called == ("mail_constraints",)


def test_fail_over_turns() -> None:
    s = score_case(_CASE, ["search", "proposal_draft"], num_turns=6)  # max_turns=5
    assert not s.passed
    assert s.over_turns is True


def test_summarize() -> None:
    scores = [
        score_case(_CASE, ["search", "proposal_draft"], num_turns=3),  # pass
        score_case(_CASE, ["search"], num_turns=3),  # fail (missing proposal_draft)
    ]
    summary = summarize(scores)
    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["failed_ids"] == ["c"]


def test_summarize_empty() -> None:
    summary = summarize([])
    assert summary["total"] == 0
    assert summary["pass_rate"] == 0.0


# ── ゴールドセットの健全性 ──────────────────────────────────────────────────


def test_gold_set_is_well_formed() -> None:
    assert len(GOLD_CASES) >= 10
    ids = [c.id for c in GOLD_CASES]
    assert len(ids) == len(set(ids)), "ゴールドケース id が重複"
    for c in GOLD_CASES:
        assert c.goal.strip(), f"{c.id}: goal が空"
        # 各ケースは最低 1 つの期待（expect_all / expect_any / forbid）を持つ
        assert c.expect_all or c.expect_any or c.forbid, f"{c.id}: 期待が未定義"
        assert c.max_turns >= 1


def test_mail_cases_are_flagged() -> None:
    """mail_constraints を期待するケースは USE_MAIL_TOOLS を needs_flags に持つ。"""
    for c in GOLD_CASES:
        uses_mail = "mail_constraints" in (*c.expect_all, *c.expect_any)
        if uses_mail:
            assert "USE_MAIL_TOOLS" in c.needs_flags, f"{c.id}: USE_MAIL_TOOLS 未タグ"
