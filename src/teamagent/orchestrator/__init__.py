"""マルチSkill 自律オーケストレーター（PoC / C案 前段）.

タイプB＝適応ループ。「調べた結果を見て次の手と結論を自分で変える」を実現する。
詳細設計: docs/poc/agent_orchestrator_poc_design.md
"""

from __future__ import annotations

from .decider import Decision, FinalAnswer, LLMDecider, Observation, ToolCall
from .loop import AgentResult, OrchestratorError, run_agent
from .tools import SkillFactory, ToolSpec, tool_from_skill

__all__ = [
    "AgentResult",
    "Decision",
    "FinalAnswer",
    "LLMDecider",
    "Observation",
    "OrchestratorError",
    "SkillFactory",
    "ToolCall",
    "ToolSpec",
    "run_agent",
    "tool_from_skill",
]
