"""本物の MCP SDK（client ⇄ server のメモリ接続）で、一覧に出さずに呼べることを固定する。

SDK の版で挙動が変わった（一覧に無いツールを拒否するようになった等）ときに、ここで気付く。
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway.personal_memory import gate
from teamagent.mcp_gateway.personal_memory import service as pm_service
from teamagent.mcp_gateway.server import build_server
from teamagent.orchestrator.tools import ToolSpec
from tests.caller_claim_testkit import TEST_SLACK_USER_ID, make_verifier, sign_arguments
from tests.mcp_gateway.test_personal_memory_gate import _EchoSkill

EMAIL = "member@vectorinc.co.jp"
MARKER = "ZQX-MARKER"


async def _resolve(slack_user_id: str) -> ResolvedIdentity | None:
    if slack_user_id != TEST_SLACK_USER_ID:
        return None
    return ResolvedIdentity(slack_user_id=slack_user_id, email=EMAIL)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, Any]]]:
    monkeypatch.setenv(gate.ALLOWED_EMAILS_ENV, EMAIL)
    calls: list[tuple[str, Any]] = []

    async def fake(tool: str, principal: Any, message_id: str, payload: Any) -> dict[str, Any]:
        calls.append((tool, payload))
        return {"status": "buffered"}

    monkeypatch.setattr(pm_service, "handle_personal_memory", fake)
    yield calls


def _args(tool: str = "personal_memory_observe", utterance: str = "資料は短めが好き") -> Any:
    call_id = f"aico-pm-obs-{secrets.token_hex(16)}"
    return sign_arguments(
        tool,
        {"utterance": utterance},
        channel_id="D0123456789",
        tool_call_id=call_id,
        run_id=call_id,
    )


def _server() -> Any:
    return build_server(
        [ToolSpec("echo", "echo", _EchoSkill)],
        identity_resolver=_resolve,
        caller_claim_verifier=make_verifier(),
    )


async def test_hidden_but_callable_through_sdk(
    monkeypatch: pytest.MonkeyPatch,
    _env: list[tuple[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("USE_PERSONAL_MEMORY", "1")
    caplog.set_level(logging.DEBUG)
    async with create_connected_server_and_client_session(_server()) as client:
        listed = await client.list_tools()
        assert "personal_memory_observe" not in {t.name for t in listed.tools}
        result = await client.call_tool("personal_memory_observe", _args(utterance=MARKER))
    assert not result.isError
    assert json.loads(result.content[0].text) == {"status": "buffered"}
    assert _env[0][0] == "personal_memory_observe"
    assert MARKER not in caplog.text  # SDK の WARNING やログに発話が出ない
    # サーバ側 SDK の WARNING は本人メモの 3 ツールだけ落とす（クライアント側の DEBUG は対象外。
    # 本番のクライアントは OpenClaw の plugin で、Python SDK ではない）
    assert "not listed, no validation will be performed" not in caplog.text


async def test_flag_off_is_unknown_tool_through_sdk(
    monkeypatch: pytest.MonkeyPatch, _env: list[tuple[str, Any]]
) -> None:
    monkeypatch.setenv("USE_PERSONAL_MEMORY", "0")
    async with create_connected_server_and_client_session(_server()) as client:
        result = await client.call_tool("personal_memory_observe", _args())
    assert json.loads(result.content[0].text) == {"error": "unknown tool: personal_memory_observe"}
    assert _env == []


async def test_internal_error_not_leaked_through_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_PERSONAL_MEMORY", "1")

    async def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError(MARKER)

    monkeypatch.setattr(pm_service, "handle_personal_memory", boom)
    async with create_connected_server_and_client_session(_server()) as client:
        result = await client.call_tool("personal_memory_observe", _args())
    text = result.content[0].text
    assert MARKER not in text
    assert json.loads(text)["code"] == "PM_INTERNAL"
