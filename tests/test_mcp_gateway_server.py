"""MCP 公開層（mcp_gateway.server）の単体テスト（外部I/O無し・DB/トークン不要）。

検証の主眼（P0 の de-risk 対象）:
1. tool 列挙が ToolSpec から生成され、入力スキーマに _user_context が付く。
2. user_email 無しの呼び出しは fail-closed で拒否（越権防止）。
3. user_email 有りの呼び出しで RLS 用コンテキストが SkillContext.metadata へ伝播する
   （＝ RLS が MCP 越しでも skill に届く＝境界内で行権限を効かせられる）。
4. 入力検証エラー・skill 例外は構造化エラーで返り、サーバを落とさない。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel

from teamagent.mcp_gateway.server import (
    USER_CONTEXT_KEY,
    dispatch_tool,
    list_tool_defs,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext


class _EchoInput(BaseModel):
    q: str


class _EchoOutput(BaseModel):
    echo: str
    saw_user_email: str | None
    saw_groups: list[str]


class _EchoSkill(BaseSkill[_EchoInput, _EchoOutput]):
    """RLS コンテキストの伝播を観測するためのフェイク skill（外部I/O無し）。"""

    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "テスト用エコー。ctx の RLS メタを観測する。"
    input_schema: ClassVar[type[BaseModel]] = _EchoInput
    output_schema: ClassVar[type[BaseModel]] = _EchoOutput

    def run(self, input: _EchoInput, ctx: SkillContext) -> _EchoOutput:
        return _EchoOutput(
            echo=input.q,
            saw_user_email=ctx.metadata.get("user_email"),
            saw_groups=list(ctx.metadata.get("user_groups") or []),
        )


class _BoomSkill(BaseSkill[_EchoInput, _EchoOutput]):
    name: ClassVar[str] = "boom"
    description: ClassVar[str] = "必ず例外を投げる skill（エラー隔離の検証用）。"
    input_schema: ClassVar[type[BaseModel]] = _EchoInput
    output_schema: ClassVar[type[BaseModel]] = _EchoOutput

    def run(self, input: _EchoInput, ctx: SkillContext) -> _EchoOutput:
        raise RuntimeError("kaboom")


_SPECS = [
    ToolSpec("echo", _EchoSkill.description, _EchoSkill),
    ToolSpec("boom", _BoomSkill.description, _BoomSkill),
]
_BY_NAME = {s.name: s for s in _SPECS}


def _parse(contents: list[Any]) -> dict[str, Any]:
    assert len(contents) == 1
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def test_list_tools_includes_user_context() -> None:
    tools = list_tool_defs(_SPECS)
    names = {t.name for t in tools}
    assert names == {"echo", "boom"}
    echo = next(t for t in tools if t.name == "echo")
    props = echo.inputSchema["properties"]
    assert "q" in props  # 元スキーマ保持
    assert USER_CONTEXT_KEY in props  # RLS コンテキスト口を付与
    assert "user_email" in props[USER_CONTEXT_KEY]["properties"]


async def test_fail_closed_without_user_email() -> None:
    out = _parse(await dispatch_tool(_BY_NAME, "echo", {"q": "hi"}, require_rls=True))
    assert "fail-closed" in out["error"]


async def test_rls_context_propagates_with_user_email() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {
                "q": "hi",
                USER_CONTEXT_KEY: {"user_email": "a@vectorinc.co.jp", "user_groups": ["g1"]},
            },
            require_rls=True,
        )
    )
    # RLS 用 user_email / groups が skill の ctx.metadata まで届いている
    assert out["echo"] == "hi"
    assert out["saw_user_email"] == "a@vectorinc.co.jp"
    assert out["saw_groups"] == ["g1"]


async def test_unknown_tool_is_structured_error() -> None:
    out = _parse(await dispatch_tool(_BY_NAME, "nope", {}, require_rls=False))
    assert "unknown tool" in out["error"]


async def test_invalid_input_is_structured_error() -> None:
    # q（必須）を欠く → 入力検証エラーを構造化で返す（require_rls=False で RLS 前提を外す）
    out = _parse(await dispatch_tool(_BY_NAME, "echo", {}, require_rls=False))
    assert "invalid input" in out["error"]


async def test_skill_exception_is_isolated() -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "boom",
            {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
            require_rls=True,
        )
    )
    assert "RuntimeError" in out["error"]
    assert "request_id" in out  # request_id 付きで追跡可能


async def test_require_rls_false_allows_no_email() -> None:
    # 非データ tool 等で RLS 不要のときは user_email 無しでも通す（将来の chitchat 等向け）
    out = _parse(await dispatch_tool(_BY_NAME, "echo", {"q": "hi"}, require_rls=False))
    assert out["echo"] == "hi"
    assert out["saw_user_email"] is None
