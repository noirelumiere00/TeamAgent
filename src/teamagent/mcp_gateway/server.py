"""TeamAgent を spec-MCP (stdio) サーバとして公開する薄いラッパ（自律外殻 ⟷ ドメイン能力の境界）。

OpenClaw 等の MCP クライアント（＝自律オーケストレーションの外殻）が、TeamAgent のドメイン能力を
tool として叩くための境界。RLS 行権限・per-user OAuth・fail-closed・反ハルシは本サーバ
（＝境界の内側 Python）で死守し、外殻はここを越えて RDS/Secrets/Google に直接触れない。

セキュリティ不変条件:
- 各 tool は呼び出し元の ``_user_context.user_email`` を要求し、無ければ fail-closed（越権防止）。
  RLS は ``SkillContext.metadata`` 経由で adapter 層（SET ROLE + GUC）が強制する。
- ⚠️ P0 段階は外殻から渡る user_email を受けて「RLS が MCP 越しでも漏れない」ことを検証する。
  user_email の“信頼できる解決”（Slack token から MCP 側で確定し詐称を排除）は WS-C で差し替える。
- 重操作（シート書込/メール下書き/PPTX 確定）の HITL propose→confirm 化は WS-D で別 tool 化する。

3層分離: 本モジュールは runtime 寄りの境界層。adapter は直叩きせず、既存 ToolSpec/Skill 経由で呼ぶ。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import SkillContext

logger = structlog.get_logger(__name__)

# 呼び出し元（外殻）が RLS 用コンテキストを渡す予約キー。skill 入力とは分離する。
USER_CONTEXT_KEY = "_user_context"


def _augment_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """入力スキーマに RLS 用の ``_user_context`` を足す（外殻が user_email を渡す口）。"""
    out = dict(schema)
    props = dict(out.get("properties") or {})
    props[USER_CONTEXT_KEY] = {
        "type": "object",
        "description": (
            "RLS 用の呼び出し元コンテキスト。営業データは user_email でスコープされる"
            "（未指定は fail-closed で拒否）。"
        ),
        "properties": {
            "user_email": {"type": "string"},
            "user_groups": {"type": "array", "items": {"type": "string"}},
            "user_role": {"type": "string"},
        },
    }
    out["properties"] = props
    return out


def list_tool_defs(specs: list[ToolSpec]) -> list[Tool]:
    """ToolSpec 群を MCP の Tool 定義へ（入力スキーマに _user_context を付与）。"""
    return [
        Tool(
            name=s.name,
            description=s.description,
            inputSchema=_augment_schema(s.json_schema()),
        )
        for s in specs
    ]


def _extract_user_context(arguments: dict[str, Any]) -> dict[str, Any]:
    """引数から RLS 用 user_context を取り出し正規化する。"""
    raw = arguments.get(USER_CONTEXT_KEY) or {}
    return {
        "user_email": raw.get("user_email"),
        "user_groups": list(raw.get("user_groups") or []),
        "user_role": raw.get("user_role"),
    }


def _err(message: str, **extra: Any) -> list[TextContent]:
    """構造化エラーを TextContent で返す（サーバ/外殻ループを落とさない）。"""
    payload: dict[str, Any] = {"error": message, **extra}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def dispatch_tool(
    by_name: dict[str, ToolSpec],
    name: str,
    arguments: dict[str, Any],
    *,
    require_rls: bool = True,
) -> list[TextContent]:
    """1 tool 呼び出しを実行する（fail-closed → 入力検証 → 同期 skill を thread 実行）。

    例外は握って構造化エラーで返す（MCP サーバも外殻のループも落とさない）。
    """
    spec = by_name.get(name)
    if spec is None:
        return _err(f"unknown tool: {name}")

    uctx = _extract_user_context(arguments)
    # fail-closed: RLS 必須なのに user_email 無し → 越権防止（境界での一次防壁）。
    if require_rls and not uctx["user_email"]:
        logger.warning("mcp_rls_fail_closed", tool=name)
        return _err("RLS required: _user_context.user_email is missing (fail-closed)")

    skill_args = {k: v for k, v in arguments.items() if k != USER_CONTEXT_KEY}
    try:
        skill_input = spec.input_schema(**skill_args)
    except Exception as e:  # 入力検証エラーは構造化で返す
        return _err(f"invalid input: {type(e).__name__}: {e}")

    ctx = SkillContext(user_id=uctx["user_email"], metadata=uctx)
    try:
        # 同期 skill.run（DB I/O 等でブロックする）を thread に逃がしイベントループを塞がない。
        output = await asyncio.to_thread(spec.instantiate().run, skill_input, ctx)
    except Exception as e:
        logger.warning(
            "mcp_tool_error", tool=name, error=type(e).__name__, request_id=ctx.request_id
        )
        return _err(f"{type(e).__name__}: {e}", request_id=ctx.request_id)

    data = output.model_dump() if hasattr(output, "model_dump") else {"result": str(output)}
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))]


def build_server(specs: list[ToolSpec] | None = None, *, require_rls: bool = True) -> Server:
    """TeamAgent MCP サーバを構築する（specs 省略時は本番ツールを遅延構築）。"""
    if specs is None:
        from teamagent.orchestrator.factory import build_production_tools

        specs = build_production_tools()
    by_name = {s.name: s for s in specs}
    server: Server = Server("teamagent")

    @server.list_tools()
    async def _list() -> list[Tool]:
        return list_tool_defs(specs)

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        return await dispatch_tool(by_name, name, arguments, require_rls=require_rls)

    return server


async def _amain() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """stdio で MCP サーバを起動する CLI エントリポイント。"""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
