"""mcp 全体の同時実行の上限（M7・MCP_REQUEST_GATE）。本物の MCP SDK の client ⇄ server で通す。

固定すること:
- 既定 OFF は素通り（今と同じ）
- 上限を超えたら形の決まった「混雑」応答（無言で詰まらせない）。待ち上限・待ち時間の両方
- 例外で終わった呼び出しもスロットを返す（次の呼び出しが通る）
- 本人メモの経路は上限の外（埋まっていても通る）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp import types
from mcp.types import TextContent

from teamagent.mcp_gateway import capacity
from teamagent.mcp_gateway import server as mcp_server
from teamagent.mcp_gateway.server import build_server
from teamagent.orchestrator.tools import ToolSpec
from tests.mcp_gateway.test_personal_memory_gate import _EchoSkill


@pytest.fixture
def slow_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"running": 0, "peak": 0, "release": asyncio.Event(), "fail": False}

    async def fake(by_name: Any, name: str, arguments: dict[str, Any], **_: Any) -> Any:
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        try:
            await state["release"].wait()
            if state["fail"]:
                raise RuntimeError("boom")
            return [TextContent(type="text", text=json.dumps({"ok": name}))]
        finally:
            state["running"] -= 1

    async def fake_pm(name: str, arguments: dict[str, Any], **_: Any) -> Any:
        return [TextContent(type="text", text=json.dumps({"pm": name}))]

    monkeypatch.setattr(mcp_server, "dispatch_tool", fake)
    monkeypatch.setattr(mcp_server, "dispatch_personal_memory_tool", fake_pm)
    return state


def _server() -> Any:
    return build_server([ToolSpec("echo", "echo", _EchoSkill)], require_rls=False)


async def _call(server: Any, name: str) -> Any:
    """server に登録された call_tool のハンドラを直接呼ぶ（同じ server＝同じ上限を共有）。

    本番の plugin は 1 呼び出し 1 セッションで並行に来る。SDK のメモリ接続は 1 セッション内の
    要求を順に処理するため、並行の再現にはハンドラを直接並べて呼ぶ。
    """
    request = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments={})
    )
    result = await server.request_handlers[types.CallToolRequest](request)
    return result.root


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)  # type: ignore[no-any-return]


async def test_gate_off_is_a_pass_through(
    monkeypatch: pytest.MonkeyPatch, slow_dispatch: dict[str, Any]
) -> None:
    monkeypatch.delenv(capacity.ENABLED_ENV, raising=False)
    server = _server()
    tasks = [asyncio.create_task(_call(server, "echo")) for _ in range(5)]
    while slow_dispatch["running"] < 5:
        await asyncio.sleep(0.01)
    slow_dispatch["release"].set()
    results = await asyncio.gather(*tasks)
    assert [_payload(r) for r in results] == [{"ok": "echo"}] * 5
    assert slow_dispatch["peak"] == 5  # 上限なし


async def _wait_running(state: dict[str, Any], n: int) -> None:
    for _ in range(500):
        if state["running"] >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"running never reached {n}")


async def test_over_the_limit_gets_a_structured_overload(
    monkeypatch: pytest.MonkeyPatch, slow_dispatch: dict[str, Any]
) -> None:
    """同時 1・待ち 1: 1 本目は実行・2 本目は待つ・3 本目は即「混雑」。待った 2 本目は後で通る。"""
    monkeypatch.setenv(capacity.ENABLED_ENV, "1")
    monkeypatch.setenv(capacity.CONCURRENCY_ENV, "1")
    monkeypatch.setenv(capacity.QUEUE_ENV, "1")
    server = _server()
    first = asyncio.create_task(_call(server, "echo"))
    await _wait_running(slow_dispatch, 1)
    second = asyncio.create_task(_call(server, "echo"))
    await asyncio.sleep(0.05)
    rejected = await _call(server, "echo")
    slow_dispatch["release"].set()
    done = [await first, await second]
    assert _payload(rejected) == {"error": capacity.OVERLOAD_TEXT, "code": capacity.OVERLOAD_CODE}
    assert [_payload(r) for r in done] == [{"ok": "echo"}] * 2
    assert slow_dispatch["peak"] == 1


async def test_queue_zero_does_not_reject_everyone(
    monkeypatch: pytest.MonkeyPatch, slow_dispatch: dict[str, Any]
) -> None:
    monkeypatch.setenv(capacity.ENABLED_ENV, "1")
    monkeypatch.setenv(capacity.QUEUE_ENV, "0")
    slow_dispatch["release"].set()
    assert _payload(await _call(_server(), "echo")) == {"ok": "echo"}


async def test_memory_path_is_outside_the_gate(
    monkeypatch: pytest.MonkeyPatch, slow_dispatch: dict[str, Any]
) -> None:
    monkeypatch.setenv(capacity.ENABLED_ENV, "1")
    monkeypatch.setenv(capacity.CONCURRENCY_ENV, "1")
    monkeypatch.setenv(capacity.QUEUE_ENV, "1")
    monkeypatch.setenv("USE_PERSONAL_MEMORY", "1")
    server = _server()
    first = asyncio.create_task(_call(server, "echo"))
    await _wait_running(slow_dispatch, 1)
    second = asyncio.create_task(_call(server, "echo"))
    await asyncio.sleep(0.05)
    memo = await _call(server, "personal_memory_context")  # 実行 1・待ち 1 で埋まっていても通る
    slow_dispatch["release"].set()
    await first
    await second
    assert _payload(memo) == {"pm": "personal_memory_context"}


async def test_waiting_too_long_is_an_overload(
    monkeypatch: pytest.MonkeyPatch, slow_dispatch: dict[str, Any]
) -> None:
    monkeypatch.setenv(capacity.ENABLED_ENV, "1")
    monkeypatch.setenv(capacity.CONCURRENCY_ENV, "1")
    monkeypatch.setenv(capacity.QUEUE_ENV, "4")
    monkeypatch.setenv(capacity.TIMEOUT_ENV, "1")
    server = _server()
    first = asyncio.create_task(_call(server, "echo"))
    await _wait_running(slow_dispatch, 1)
    waited = await _call(server, "echo")
    slow_dispatch["release"].set()
    await first
    assert _payload(waited)["code"] == capacity.OVERLOAD_CODE


async def test_a_failed_call_returns_its_slot(
    monkeypatch: pytest.MonkeyPatch, slow_dispatch: dict[str, Any]
) -> None:
    monkeypatch.setenv(capacity.ENABLED_ENV, "1")
    monkeypatch.setenv(capacity.CONCURRENCY_ENV, "1")
    monkeypatch.setenv(capacity.QUEUE_ENV, "1")
    slow_dispatch["fail"] = True
    slow_dispatch["release"].set()
    server = _server()
    failed = await _call(server, "echo")
    slow_dispatch["fail"] = False
    ok = await _call(server, "echo")
    assert failed.isError
    assert _payload(ok) == {"ok": "echo"}
