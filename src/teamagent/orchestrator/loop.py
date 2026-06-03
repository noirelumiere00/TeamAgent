"""適応型エージェントループ（plan → act → observe → replan）.

タイプB（自律ループ）の中核。decider が「結果を見て次の手を変える」ことで、
入力時に確定できない分岐（例: Mailで NG → 候補差し替え）を扱う。

ガードレール（CLAUDE.md 6-bis 準拠）:
- max_steps / cost_cap_usd でハードカット（暴走防止）
- 1 リクエスト = 1 request_id を全 Skill 呼び出しに伝播（SkillContext 経由）
- 各ステップで tool / cost / 累計を構造化ログ
"""

from __future__ import annotations

from dataclasses import dataclass, field

from teamagent.skills.base import SkillContext

from .decider import FinalAnswer, LLMDecider, Observation
from .tools import ToolSpec


class OrchestratorError(RuntimeError):
    """未知ツール指定など、ループ実行中の回復不能エラー。"""


@dataclass
class AgentResult:
    answer: str
    steps: list[Observation] = field(default_factory=list)
    total_cost_usd: float = 0.0
    stopped_reason: str = "final"  # final | max_steps | cost_cap

    @property
    def tool_trace(self) -> list[str]:
        """呼ばれたツール名の順序（テスト/監査用）。"""
        return [o.tool for o in self.steps]


def _find_tool(tools: list[ToolSpec], name: str) -> ToolSpec:
    for t in tools:
        if t.name == name:
            return t
    raise OrchestratorError(f"unknown tool: {name!r} (available: {[t.name for t in tools]})")


def run_agent(
    *,
    goal: str,
    tools: list[ToolSpec],
    decider: LLMDecider,
    ctx: SkillContext,
    max_steps: int = 8,
    cost_cap_usd: float = 0.5,
) -> AgentResult:
    """goal を満たすまで decider にツール選択させて回す適応ループ。

    Args:
        goal: ユーザー要望（例: 「クライアントXに次の施策を提案して」）
        tools: 利用可能なツール（= 既存 Skill のラップ）
        decider: 次の一手を決める LLM 抽象（mock / Bedrock / SDK 差し替え可能）
        ctx: request_id を持つ実行コンテキスト（全ツールへ伝播）
        max_steps: 最大反復回数（暴走防止）
        cost_cap_usd: 累計コスト上限（超過で打ち切り）
    """
    log = ctx.bind_logger("orchestrator")
    history: list[Observation] = []
    total_cost = 0.0

    log.info("agent_start", goal=goal, n_tools=len(tools), request_id=ctx.request_id)
    for step in range(1, max_steps + 1):
        decision = decider.decide(goal, tools, history)

        if isinstance(decision, FinalAnswer):
            log.info(
                "agent_final",
                step=step,
                total_cost_usd=round(total_cost, 6),
                request_id=ctx.request_id,
            )
            return AgentResult(
                answer=decision.text,
                steps=history,
                total_cost_usd=total_cost,
                stopped_reason="final",
            )

        # --- tool call ---
        spec = _find_tool(tools, decision.tool)
        skill = spec.instantiate()
        skill_input = spec.input_schema(**decision.input)
        output = skill.run(skill_input, ctx)

        cost = float(getattr(output, "total_cost_usd", 0.0) or 0.0)
        total_cost += cost
        history.append(
            Observation(
                tool=decision.tool,
                input=decision.input,
                output=output.model_dump(),
                cost_usd=cost,
            )
        )
        log.info(
            "agent_step",
            step=step,
            tool=decision.tool,
            cost_usd=round(cost, 6),
            total_cost_usd=round(total_cost, 6),
            request_id=ctx.request_id,
        )

        if total_cost > cost_cap_usd:
            log.warning(
                "agent_cost_cap",
                total_cost_usd=round(total_cost, 6),
                cap=cost_cap_usd,
                request_id=ctx.request_id,
            )
            return AgentResult(
                answer="(コスト上限に達したため打ち切りました)",
                steps=history,
                total_cost_usd=total_cost,
                stopped_reason="cost_cap",
            )

    log.warning("agent_max_steps", max_steps=max_steps, request_id=ctx.request_id)
    return AgentResult(
        answer="(最大ステップ数に達したため打ち切りました)",
        steps=history,
        total_cost_usd=total_cost,
        stopped_reason="max_steps",
    )


__all__ = ["AgentResult", "OrchestratorError", "run_agent"]
