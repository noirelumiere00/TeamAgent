"""mcp_tool_usage の内訳（gateway受信→skill開始→skill完了）と二段返しの印のテスト。

Slack 体感 19 秒 と mcp 実測 8.3 秒 の差を詰めるには、まず「ゲート層で何ミリ秒
溶けているか」が要る。latency_ms（skill 実行窓）の定義は変えない＝既存台帳の連続性を守る。
"""

from __future__ import annotations

import json
import time
from typing import Any, ClassVar

from pydantic import BaseModel
from structlog.testing import capture_logs

from teamagent.mcp_gateway.server import SEARCH_TOOL_NAME, USER_CONTEXT_KEY, dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search.two_stage import TWO_STAGE_CTX_KEY


class _In(BaseModel):
    query: str


class _Out(BaseModel):
    answer: str
    saw_two_stage_mark: bool
    total_cost_usd: float = 0.0


class _SlowSkill(BaseSkill[_In, _Out]):
    """計測可能な最小 skill（外部 I/O 無し）。"""

    name: ClassVar[str] = SEARCH_TOOL_NAME
    description: ClassVar[str] = "テスト用フェイク検索。"
    input_schema: ClassVar[type[BaseModel]] = _In
    output_schema: ClassVar[type[BaseModel]] = _Out

    def run(self, input: _In, ctx: SkillContext) -> _Out:
        time.sleep(0.02)
        return _Out(
            answer=f"hits for {input.query}",
            saw_two_stage_mark=bool(ctx.metadata.get(TWO_STAGE_CTX_KEY)),
        )


class _OtherSkill(_SlowSkill):
    name: ClassVar[str] = "knowledge_deliver"


_BY_NAME = {
    SEARCH_TOOL_NAME: ToolSpec(SEARCH_TOOL_NAME, "fake", _SlowSkill),
    "knowledge_deliver": ToolSpec("knowledge_deliver", "fake", _OtherSkill),
}


def _parse(contents: list[Any]) -> dict[str, Any]:
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _usage(logs: list[dict[str, Any]]) -> dict[str, Any]:
    events = [e for e in logs if e.get("event") == "mcp_tool_usage"]
    assert len(events) == 1
    return events[0]


async def test_mcp_tool_usage_has_gateway_and_total_breakdown() -> None:
    with capture_logs() as logs:
        await dispatch_tool(
            _BY_NAME,
            SEARCH_TOOL_NAME,
            {"query": "x", USER_CONTEXT_KEY: {"user_email": "a@vectorinc.co.jp"}},
            require_rls=True,
        )

    usage = _usage(logs)
    assert usage["tool"] == SEARCH_TOOL_NAME
    # latency_ms は従来どおり skill 実行窓（定義不変）
    assert usage["latency_ms"] >= 20
    # 追加: 受信→skill 開始 / 受信→skill 完了
    assert usage["gateway_ms"] >= 0
    assert usage["total_ms"] == usage["gateway_ms"] + usage["latency_ms"]
    # 本文・クエリ原文は載せない（G8）
    assert "x" not in {k: v for k, v in usage.items() if k != "tool"}.values()


async def test_two_stage_mark_only_on_search_tool() -> None:
    """二段返しの印は search tool にだけ付く（knowledge_deliver 等には付かない）。"""
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            SEARCH_TOOL_NAME,
            {"query": "x", USER_CONTEXT_KEY: {"user_email": "a@vectorinc.co.jp"}},
            require_rls=True,
        )
    )
    assert out["saw_two_stage_mark"] is True

    other = _parse(
        await dispatch_tool(
            _BY_NAME,
            "knowledge_deliver",
            {"query": "x", USER_CONTEXT_KEY: {"user_email": "a@vectorinc.co.jp"}},
            require_rls=True,
        )
    )
    assert other["saw_two_stage_mark"] is False
