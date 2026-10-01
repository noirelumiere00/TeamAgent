"""本人のメールを読む道具は本人 DM でだけ動く（10-01 監査候補②③・gateway の出力面ガード）。

チャンネルに第三者が置いた指示文でモデルが mail_* を呼ばされても、受信メールの要約・件名・
相手がスレッドへ出ない（スレッドの記録にも残らない）。判定は署名済み caller claim の
channel_id と、resolver が確定した身元だけで行う（モデルが渡す値は使わない）。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway.server import (
    DM_ONLY_MESSAGE,
    DM_ONLY_TOOLS,
    dispatch_tool,
)
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from tests.caller_claim_testkit import TEST_SLACK_USER_ID, make_verifier, sign_arguments

_MEMBER = ResolvedIdentity(slack_user_id=TEST_SLACK_USER_ID, email="member@vectorinc.co.jp")


class _In(BaseModel):
    query: str = ""


class _Out(BaseModel):
    ran: str


def _mail_skill(tool: str, calls: list[str]) -> type[BaseSkill[_In, _Out]]:
    class _MailSkill(BaseSkill[_In, _Out]):
        """本物の mail_* と同じく、呼ばれたら本人の受信箱を読む（ここでは記録だけ）。"""

        name: ClassVar[str] = tool
        description: ClassVar[str] = "テスト用のメール道具。"
        input_schema: ClassVar[type[BaseModel]] = _In
        output_schema: ClassVar[type[BaseModel]] = _Out

        def run(self, input: _In, ctx: SkillContext) -> _Out:
            calls.append(tool)
            return _Out(ran=tool)

    return _MailSkill


def _resolver() -> IdentityResolver:
    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        return _MEMBER if slack_user_id == TEST_SLACK_USER_ID else None

    return resolve


async def _call(tool: str, channel_id: str, calls: list[str]) -> dict[str, Any]:
    skill = _mail_skill(tool, calls)
    by_name = {tool: ToolSpec(tool, skill.description, skill)}
    contents = await dispatch_tool(
        by_name,
        tool,
        sign_arguments(tool, {"query": "最近のメール"}, channel_id=channel_id),
        identity_resolver=_resolver(),
        caller_claim_verifier=make_verifier(),
        allowed_domains=frozenset({"vectorinc.co.jp"}),
        require_rls=True,
    )
    assert len(contents) == 1
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def test_the_list_covers_every_tool_that_reads_the_inbox() -> None:
    """足し忘れ防止。本人の受信箱・ダイジェストを読む道具が全部入っている。"""
    assert {
        "mail_summary",
        "mail_followup",
        "mail_reply",
        "mail_to_internal_context",
        "morning_digest",
    } <= DM_ONLY_TOOLS


@pytest.mark.parametrize("tool", sorted(DM_ONLY_TOOLS))
@pytest.mark.parametrize("channel_id", ["C0PUBLIC01", "G0PRIVATE1"])
async def test_mail_tools_refuse_outside_dm_without_touching_gmail(
    tool: str, channel_id: str
) -> None:
    """チャンネル・グループでは定型文だけ返し、skill（＝Gmail）を 1 度も呼ばない。

    変異: dispatch_tool の DM 判定を外すと skill が呼ばれて赤。
    """
    calls: list[str] = []
    out = await _call(tool, channel_id, calls)
    assert out == {"error": "dm_only", "message": DM_ONLY_MESSAGE}
    assert calls == []


@pytest.mark.parametrize("tool", sorted(DM_ONLY_TOOLS))
async def test_mail_tools_work_in_the_users_dm(tool: str) -> None:
    calls: list[str] = []
    out = await _call(tool, "D0SELFDM01", calls)
    assert out.get("ran") == tool
    assert calls == [tool]


async def test_other_tools_are_not_affected_in_channels() -> None:
    """DM 限定はメール系だけ。資料検索などはチャンネルでも今までどおり動く。"""
    calls: list[str] = []
    out = await _call("search", "C0PUBLIC01", calls)
    assert out.get("ran") == "search" and calls == ["search"]
