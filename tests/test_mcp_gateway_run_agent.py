"""Batch C2: L2 オーケストレーター(run_agent) MCP tool の gating / dispatch を検証する。

実 SDK(Bedrock + Node CLI)は呼ばず、run_sdk_agent を monkeypatch して
gating(USE_AGENT_ORCHESTRATOR)・RLS fail-closed・入力検証・正常系の payload を固定する。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.mcp_gateway import server as srv
from teamagent.mcp_gateway.server import (
    RUN_AGENT_TOOL_NAME,
    USER_CONTEXT_KEY,
    dispatch_run_agent,
    list_all_tool_defs,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext


class _EchoInput(BaseModel):
    q: str


class _EchoOutput(BaseModel):
    echo: str


class _EchoSkill(BaseSkill[_EchoInput, _EchoOutput]):
    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "テスト用エコー。"
    input_schema: ClassVar[type[BaseModel]] = _EchoInput
    output_schema: ClassVar[type[BaseModel]] = _EchoOutput

    def run(self, input: _EchoInput, ctx: SkillContext) -> _EchoOutput:
        return _EchoOutput(echo=input.q)


_SPECS = [ToolSpec("echo", _EchoSkill.description, _EchoSkill)]


def _parse(contents: list[Any]) -> dict[str, Any]:
    assert len(contents) == 1
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _fake_sdk(answer: str = "最終提案です", calls: list[str] | None = None) -> Any:
    """run_sdk_agent の置換: 呼び出し kwargs を記録し SdkAgentResult 風オブジェクトを返す。"""
    recorder: dict[str, Any] = {}

    async def _run(**kwargs: Any) -> Any:
        recorder.update(kwargs)
        return SimpleNamespace(
            answer=answer,
            stopped_reason="final",
            is_error=False,
            num_turns=3,
            tool_calls=calls if calls is not None else ["search", "proposal_draft"],
            session_total_cost_usd=0.012,
        )

    _run.recorder = recorder  # type: ignore[attr-defined]
    return _run


# -----------------------------------------------------------
# gating: list に出るのは有効時だけ
# -----------------------------------------------------------
def test_run_agent_hidden_by_default() -> None:
    defs = list_all_tool_defs(_SPECS, enable_orchestrator=False)
    names = {t.name for t in defs}
    assert names == {"echo"}
    assert RUN_AGENT_TOOL_NAME not in names


def test_run_agent_exposed_when_enabled() -> None:
    defs = list_all_tool_defs(_SPECS, enable_orchestrator=True)
    names = {t.name for t in defs}
    assert names == {"echo", RUN_AGENT_TOOL_NAME}
    run_agent = next(t for t in defs if t.name == RUN_AGENT_TOOL_NAME)
    props = run_agent.inputSchema["properties"]
    assert "goal" in props
    assert USER_CONTEXT_KEY in props  # RLS の口が付く


# -----------------------------------------------------------
# dispatch: 正常系 / RLS fail-closed / 入力検証
# -----------------------------------------------------------
async def test_dispatch_run_agent_calls_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_sdk(answer="勝ち筋まとめ")
    monkeypatch.setattr("teamagent.orchestrator.sdk_runner.run_sdk_agent", fake)
    out = _parse(
        await dispatch_run_agent(
            _SPECS,
            {"goal": "過去事例を調べて提案して", USER_CONTEXT_KEY: {"user_email": "a@x.jp"}},
            require_rls=True,
        )
    )
    assert out["answer"] == "勝ち筋まとめ"
    assert out["stopped_reason"] == "final"
    assert out["tool_calls"] == ["search", "proposal_draft"]
    # L1 specs がそのまま SDK に渡る（run_agent 自身は含まれない＝再帰しない）
    assert fake.recorder["specs"] is _SPECS
    assert fake.recorder["goal"] == "過去事例を調べて提案して"
    # user_email があるので RLS 強制
    assert fake.recorder["require_rls"] is True
    assert fake.recorder["ctx_metadata"]["user_email"] == "a@x.jp"


async def test_dispatch_run_agent_rls_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    async def _should_not_run(**kwargs: Any) -> Any:
        called["n"] += 1
        raise AssertionError("run_sdk_agent must not be called when fail-closed")

    monkeypatch.setattr("teamagent.orchestrator.sdk_runner.run_sdk_agent", _should_not_run)
    out = _parse(
        await dispatch_run_agent(
            _SPECS,
            {"goal": "x"},  # user_email 無し・resolver 無し（LEGACY）→ fail-closed
            require_rls=True,
        )
    )
    assert "fail-closed" in out["error"]
    assert called["n"] == 0


async def test_dispatch_run_agent_rejects_empty_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teamagent.orchestrator.sdk_runner.run_sdk_agent", _fake_sdk())
    out = _parse(
        await dispatch_run_agent(
            _SPECS,
            {"goal": "   ", USER_CONTEXT_KEY: {"user_email": "a@x.jp"}},
            require_rls=True,
        )
    )
    assert "invalid input" in out["error"]


async def test_dispatch_run_agent_company_shared_no_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """会社共有モードでは user_email 無しでも実行（groups で認可・require_rls=False を SDK へ）。"""
    fake = _fake_sdk()
    monkeypatch.setattr("teamagent.orchestrator.sdk_runner.run_sdk_agent", fake)
    out = _parse(
        await dispatch_run_agent(
            _SPECS,
            {"goal": "全社ナレッジから調べて", USER_CONTEXT_KEY: {"slack_user_id": "U1"}},
            require_rls=True,
            company_shared_groups=frozenset({"vectorinc.co.jp"}),
        )
    )
    assert out["stopped_reason"] == "final"
    # 会社共有は email 不在 → SDK へは require_rls=False（groups で認可済）
    assert fake.recorder["require_rls"] is False
    assert "vectorinc.co.jp" in fake.recorder["ctx_metadata"].get("user_groups", [])


def test_build_server_dark_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """USE_AGENT_ORCHESTRATOR 未設定なら build_server は run_agent を出さない（後方互換）。"""
    monkeypatch.delenv("USE_AGENT_ORCHESTRATOR", raising=False)
    # _envflag を読むのは build_server。specs を渡して factory 起動を避ける。
    server = srv.build_server(_SPECS, require_rls=False)
    assert server is not None  # 構築自体が落ちない
    assert srv._envflag("USE_AGENT_ORCHESTRATOR") is False
