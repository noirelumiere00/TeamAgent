"""LLMDecider — 「次にどのツールを呼ぶ / もう答える」を返す抽象.

この抽象を差し替えることで、同じ適応ループを以下で動かせる:
- MockDecider: スクリプト化（オフライン・決定的）。ループ機構と適応分岐の検証用。
- (将来) BedrockToolDecider: bedrock_client に converse_with_tools() を足して実装（方式A）。
- Python Anthropic Bedrock clientによるbounded tool loop（方式B）。

方式A/B の比較は docs/poc/agent_orchestrator_poc_design.md を参照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .tools import ToolSpec


@dataclass(frozen=True)
class ToolCall:
    """LLM の決定: このツールをこの入力で呼べ。"""

    tool: str
    input: dict[str, Any]


@dataclass(frozen=True)
class FinalAnswer:
    """LLM の決定: 十分な情報が揃ったので最終回答を返す。"""

    text: str


Decision = ToolCall | FinalAnswer


@dataclass(frozen=True)
class Observation:
    """1 ツール実行の観測結果（ループが history として decider に渡す）。"""

    tool: str
    input: dict[str, Any]
    output: dict[str, Any]
    cost_usd: float = 0.0


@runtime_checkable
class LLMDecider(Protocol):
    """goal と これまでの観測から、次の一手を決める。"""

    def decide(self, goal: str, tools: list[ToolSpec], history: list[Observation]) -> Decision: ...


__all__ = ["Decision", "FinalAnswer", "LLMDecider", "Observation", "ToolCall"]
