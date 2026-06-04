"""適応ループの決定トレース①〜⑥とガードレールをオフラインで検証.

ユーザー要望シナリオ:
  履歴確認 → 認知が滑ってたのでCVへ方針転換 → 施策案 → MailでNG → 別案へ差替
  → Driveから裏付け事例 → 統合して提案
"""

from __future__ import annotations

import pytest

from teamagent.orchestrator import OrchestratorError, run_agent
from teamagent.orchestrator.decider import Decision, Observation, ToolCall
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import SkillContext

from .fixtures import (
    ExpensiveDecider,
    HighCostSkill,
    RunawayDecider,
    ScenarioDecider,
    scenario_tools,
)


def test_adaptive_trace_pivots_to_cv_and_swaps_on_mail_ng() -> None:
    ctx = SkillContext(request_id="req-test-001", user_id="U_TEST")
    result = run_agent(
        goal="クライアントXに次の施策を提案して",
        tools=scenario_tools(),
        decider=ScenarioDecider(),
        ctx=ctx,
        max_steps=8,
        cost_cap_usd=1.0,
    )

    # 最終回答まで到達
    assert result.stopped_reason == "final"

    # ①〜⑤ のツール呼び出し順（②の方針転換と④'の差し替えを含む）
    assert result.tool_trace == [
        "get_client_history",
        "draft_measure",  # ② 認知→CV ピボット後の最初の案
        "check_mail_constraints",  # ④ Mail制約チェック
        "draft_measure",  # ④' NGだったので別案へ差し替え
        "search_past_cases",  # ⑤ Driveから裏付け
    ]

    # 適応の証拠
    assert result.steps[2].output["ng"] is True  # Mailが手法をNG判定
    assert "NG回避" in result.steps[3].input["brief"]  # 2案目はNG回避指示で差し替え
    assert (
        result.steps[3].output["approach"] != result.steps[1].output["approach"]
    )  # 手法が変わった

    # 最終回答に「認知の失敗→CV提案」「裏付け」が反映
    assert "CV" in result.answer
    assert "認知" in result.answer
    assert "成功事例" in result.answer

    # コストは各ツール出力から集計されている
    assert result.total_cost_usd > 0.0


def test_max_steps_guardrail_stops_runaway_loop() -> None:
    ctx = SkillContext(request_id="req-test-002")
    result = run_agent(
        goal="x",
        tools=scenario_tools(),
        decider=RunawayDecider(),
        ctx=ctx,
        max_steps=3,
        cost_cap_usd=999.0,
    )
    assert result.stopped_reason == "max_steps"
    assert len(result.steps) == 3


def test_cost_cap_guardrail_stops_expensive_loop() -> None:
    ctx = SkillContext(request_id="req-test-003")
    tools = [ToolSpec("expensive_tool", HighCostSkill.description, HighCostSkill)]
    result = run_agent(
        goal="x",
        tools=tools,
        decider=ExpensiveDecider(),
        ctx=ctx,
        max_steps=10,
        cost_cap_usd=0.5,
    )
    assert result.stopped_reason == "cost_cap"
    assert result.total_cost_usd >= 1.0
    assert len(result.steps) == 1


class _BadDecider:
    """未登録ツールを指す decider（エラー処理検証用）。"""

    def decide(self, goal: str, tools: list[ToolSpec], history: list[Observation]) -> Decision:
        return ToolCall("does_not_exist", {})


def test_unknown_tool_raises() -> None:
    ctx = SkillContext(request_id="req-test-004")
    with pytest.raises(OrchestratorError):
        run_agent(
            goal="x",
            tools=scenario_tools(),
            decider=_BadDecider(),
            ctx=ctx,
        )
