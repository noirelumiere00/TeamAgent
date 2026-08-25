"""MCP 境界の「連携」決定論分岐と `_user_context` 必須化のテスト。

本番実測（2026-08）で確定した 2 つの欠陥を塞いだことを固定する:

1. 外側 LLM が `oauth_connect` を選ばず `search` へ落ちる → 境界側で `oauth_connect` へ寄せる。
2. 入力 0 個の `oauth_connect` を LLM が `{}` で呼び、ingress plugin が**無言 block** する
   → スキーマで `_user_context` を required にして必ず載せさせる。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway.server import (
    OAUTH_CONNECT_TOOL_NAME,
    USER_CONTEXT_KEY,
    dispatch_tool,
    list_tool_defs,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from tests.caller_claim_testkit import (
    TEST_SLACK_USER_ID,
    make_verifier,
    sign_arguments,
)


class _SearchInput(BaseModel):
    query: str


class _SearchOutput(BaseModel):
    ran: str
    query: str


class _SearchSkill(BaseSkill[_SearchInput, _SearchOutput]):
    """`search` の代役（資料検索へ落ちたことを観測するためのフェイク）。"""

    name: ClassVar[str] = "search"
    description: ClassVar[str] = "テスト用の資料検索。"
    input_schema: ClassVar[type[BaseModel]] = _SearchInput
    output_schema: ClassVar[type[BaseModel]] = _SearchOutput

    def run(self, input: _SearchInput, ctx: SkillContext) -> _SearchOutput:
        return _SearchOutput(ran="search", query=input.query)


class _NoInput(BaseModel):
    """本番 `oauth_connect` と同じく入力パラメータを 1 つも持たない。"""


class _ConnectOutput(BaseModel):
    ran: str
    email: str


class _ConnectSkill(BaseSkill[_NoInput, _ConnectOutput]):
    name: ClassVar[str] = OAUTH_CONNECT_TOOL_NAME
    description: ClassVar[str] = "テスト用の連携リンク発行。"
    input_schema: ClassVar[type[BaseModel]] = _NoInput
    output_schema: ClassVar[type[BaseModel]] = _ConnectOutput

    def run(self, input: _NoInput, ctx: SkillContext) -> _ConnectOutput:
        return _ConnectOutput(ran=OAUTH_CONNECT_TOOL_NAME, email=str(ctx.metadata["user_email"]))


_SEARCH_SPEC = ToolSpec("search", _SearchSkill.description, _SearchSkill)
_CONNECT_SPEC = ToolSpec(OAUTH_CONNECT_TOOL_NAME, _ConnectSkill.description, _ConnectSkill)
_BY_NAME = {"search": _SEARCH_SPEC, OAUTH_CONNECT_TOOL_NAME: _CONNECT_SPEC}
_WITHOUT_CONNECT = {"search": _SEARCH_SPEC}

_MEMBER = ResolvedIdentity(slack_user_id=TEST_SLACK_USER_ID, email="member@vectorinc.co.jp")


def _parse(contents: list[Any]) -> dict[str, Any]:
    assert len(contents) == 1
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _resolver() -> IdentityResolver:
    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        return _MEMBER if slack_user_id == TEST_SLACK_USER_ID else None

    return resolve


async def _dispatch(
    tool: str,
    business_arguments: dict[str, Any],
    *,
    by_name: dict[str, ToolSpec] | None = None,
) -> dict[str, Any]:
    return _parse(
        await dispatch_tool(
            by_name if by_name is not None else _BY_NAME,
            tool,
            sign_arguments(tool, business_arguments),
            identity_resolver=_resolver(),
            caller_claim_verifier=make_verifier(),
            allowed_domains=frozenset({"vectorinc.co.jp"}),
            require_rls=True,
        )
    )


# ── ① 入力 0 個のツールでも _user_context を required にする ────────────────────


def test_zero_input_tool_still_requires_user_context() -> None:
    """`{}` 呼び出し（＝ingress plugin の無言 block）をスキーマ側で塞ぐ。"""
    schema = next(
        t for t in list_tool_defs([_CONNECT_SPEC]) if t.name == OAUTH_CONNECT_TOOL_NAME
    ).inputSchema
    assert USER_CONTEXT_KEY in schema["properties"]
    assert USER_CONTEXT_KEY in schema["required"]


def test_existing_required_fields_are_preserved() -> None:
    schema = next(t for t in list_tool_defs([_SEARCH_SPEC]) if t.name == "search").inputSchema
    assert set(schema["required"]) == {"query", USER_CONTEXT_KEY}


# ── ② 「連携」依頼は oauth_connect へ寄せる ──────────────────────────────────


@pytest.mark.parametrize("text", ["連携", "連携して", "Google連携したい", "connect"])
async def test_connect_request_is_redirected_to_oauth_connect(text: str) -> None:
    out = await _dispatch("search", {"query": text})
    assert out["ran"] == OAUTH_CONNECT_TOOL_NAME, out
    assert out["email"] == "member@vectorinc.co.jp"


@pytest.mark.parametrize(
    "text",
    [
        "〇〇社との連携について提案書を作って",
        "他社との連携事例を検索して",
        "花王の最近の提案資料",
    ],
)
async def test_non_connect_request_is_not_redirected(text: str) -> None:
    out = await _dispatch("search", {"query": text})
    assert out["ran"] == "search", out
    assert out["query"] == text


async def test_no_redirect_when_connect_tool_is_not_exposed() -> None:
    """`USE_OAUTH_CONNECT_TOOL` 未設定の環境では寄せずに通常ディスパッチへ落とす。"""
    out = await _dispatch("search", {"query": "連携"}, by_name=_WITHOUT_CONNECT)
    assert out["ran"] == "search", out


async def test_direct_oauth_connect_call_still_works() -> None:
    out = await _dispatch(OAUTH_CONNECT_TOOL_NAME, {})
    assert out["ran"] == OAUTH_CONNECT_TOOL_NAME, out


# ── ③ 観測性: 誰が・連携語を検出したか だけをログへ（本文は出さない）──────────


async def test_redirect_emits_structured_log_without_the_message_body() -> None:
    text = "Google連携したい"
    with capture_logs() as logs:
        await _dispatch("search", {"query": text})
    events = [entry for entry in logs if entry.get("event") == "mcp_connect_intent"]
    assert len(events) == 1, logs
    event = events[0]
    assert event["connect_keyword"] is True
    assert event["redirected"] is True
    assert event["tool_requested"] == "search"
    assert event["tool_dispatched"] == OAUTH_CONNECT_TOOL_NAME
    assert event["slack_user_id"] == TEST_SLACK_USER_ID
    assert event["connect_field"] == "query"
    # G7: 本文・顧客名はログに出さない。
    assert text not in json.dumps(event, ensure_ascii=False)


async def test_direct_call_is_logged_as_not_redirected() -> None:
    with capture_logs() as logs:
        await _dispatch(OAUTH_CONNECT_TOOL_NAME, {})
    event = next(entry for entry in logs if entry.get("event") == "mcp_connect_intent")
    assert event["redirected"] is False
    assert event["tool_dispatched"] == OAUTH_CONNECT_TOOL_NAME
    assert event["slack_user_id"] == TEST_SLACK_USER_ID


async def test_ordinary_request_does_not_emit_connect_log() -> None:
    """常時ログにしない（連携語ゼロの通常依頼でノイズを出さない）。"""
    with capture_logs() as logs:
        await _dispatch("search", {"query": "花王の最近の提案資料"})
    assert [entry for entry in logs if entry.get("event") == "mcp_connect_intent"] == []
