"""Phase 0 堅牢化のオフライン検証（実Bedrock不要）.

検証対象（sdk_runner）:
- classify_result: ResultMessage subtype/is_error → stopped_reason
- _make_handler のガード: RLSコンテキスト注入 / 同一入力の繰返し拒否 /
  入力不正・例外・タイムアウトを raise せず is_error の構造化結果で返す
ハンドラは async なので、同期テスト内で asyncio.run() で駆動する（pytest-asyncio 不要）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.orchestrator.loop import OrchestratorError
from teamagent.orchestrator.sdk_runner import (
    _make_handler,
    classify_result,
    run_sdk_agent,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext


def test_classify_result() -> None:
    assert classify_result(subtype="success", is_error=False) == "final"
    assert classify_result(subtype="error_max_budget_usd", is_error=True) == "error_max_budget_usd"
    assert classify_result(subtype="error_max_turns", is_error=False) == "error_max_turns"
    assert classify_result(subtype="", is_error=True) == "error"


# --- fixture skills ---
class _In(BaseModel):
    x: str = "v"


class _Out(BaseModel):
    ok: bool = True
    total_cost_usd: float = 0.0


class _CaptureSkill(BaseSkill[_In, _Out]):
    name = "capture"
    description = "ctx を記録するだけ"
    input_schema = _In
    output_schema = _Out
    captured: ClassVar[list[SkillContext]] = []

    def run(self, input: _In, ctx: SkillContext) -> _Out:
        type(self).captured.append(ctx)
        return _Out()


class _BoomSkill(BaseSkill[_In, _Out]):
    name = "boom"
    description = "例外を投げる"
    input_schema = _In
    output_schema = _Out

    def run(self, input: _In, ctx: SkillContext) -> _Out:
        raise RuntimeError("boom")


class _SlowSkill(BaseSkill[_In, _Out]):
    name = "slow"
    description = "遅い"
    input_schema = _In
    output_schema = _Out

    def run(self, input: _In, ctx: SkillContext) -> _Out:
        time.sleep(0.5)
        return _Out()


class _StrictIn(BaseModel):
    n: int


class _StrictSkill(BaseSkill[_StrictIn, _Out]):
    name = "strict"
    description = "int 必須"
    input_schema = _StrictIn
    output_schema = _Out

    def run(self, input: _StrictIn, ctx: SkillContext) -> _Out:
        return _Out()


def _handler(skill_cls: type[BaseSkill[Any, Any]], **kw: Any) -> Any:
    spec = ToolSpec(skill_cls.name, skill_cls.description, skill_cls)
    opts: dict[str, Any] = {
        "request_id": "req-t",
        "user_id": None,
        "ctx_metadata": {},
        "call_counts": {},
        "tool_timeout_s": 5.0,
    }
    opts.update(kw)
    return _make_handler(spec, **opts)


def test_rls_context_injected() -> None:
    _CaptureSkill.captured.clear()
    h = _handler(
        _CaptureSkill,
        user_id="U1",
        ctx_metadata={"user_email": "a@b.com", "user_role": "member"},
    )
    out = asyncio.run(h({"x": "v"}))
    assert "is_error" not in out
    ctx = _CaptureSkill.captured[-1]
    assert ctx.request_id == "req-t"
    assert ctx.user_id == "U1"
    assert ctx.metadata["user_email"] == "a@b.com"
    assert ctx.metadata["user_role"] == "member"


def test_repeated_same_input_blocked() -> None:
    h = _handler(_CaptureSkill)  # max_same_call=2（既定）
    r1 = asyncio.run(h({"x": "same"}))
    r2 = asyncio.run(h({"x": "same"}))
    r3 = asyncio.run(h({"x": "same"}))
    assert "is_error" not in r1
    assert "is_error" not in r2
    assert r3.get("is_error") is True  # 3回目(>2)で機械的に拒否


def test_different_input_not_blocked() -> None:
    h = _handler(_CaptureSkill)
    r1 = asyncio.run(h({"x": "a"}))
    r2 = asyncio.run(h({"x": "b"}))
    assert "is_error" not in r1
    assert "is_error" not in r2


def test_exception_returns_structured_error() -> None:
    h = _handler(_BoomSkill)
    out = asyncio.run(h({"x": "v"}))  # raise しないこと
    assert out.get("is_error") is True


def test_timeout_returns_structured_error() -> None:
    h = _handler(_SlowSkill, tool_timeout_s=0.05)
    out = asyncio.run(h({"x": "v"}))
    assert out.get("is_error") is True


def test_invalid_input_returns_structured_error() -> None:
    h = _handler(_StrictSkill)
    out = asyncio.run(h({"n": "not-an-int"}))
    assert out.get("is_error") is True


def test_require_rls_fail_closed() -> None:
    # require_rls=True かつ user_email 無し → Bedrock を呼ぶ前に OrchestratorError（越権防止）.
    with pytest.raises(OrchestratorError):
        asyncio.run(
            run_sdk_agent(
                goal="x",
                request_id="r",
                specs=[],
                model="m",
                system_prompt="s",
                require_rls=True,
                ctx_metadata={},
            )
        )
