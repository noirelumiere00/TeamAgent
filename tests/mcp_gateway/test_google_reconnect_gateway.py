"""署名済みの会話で、Skill から MCP まで共通案内を保つ。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from teamagent.adapters.oauth_token_store import InMemoryTokenStore
from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway.server import dispatch_tool
from teamagent.orchestrator.tools import tool_from_skill
from teamagent.skills.mail_summary.skill import MailSummarySkill
from tests.caller_claim_testkit import TEST_SLACK_USER_ID, make_verifier, sign_arguments
from tests.skills._shared.test_google_connection import EMAIL, MESSAGE, FailingGoogle, failure
from tests.skills._shared.test_google_connection import setup as setup


@pytest.mark.parametrize("kind", ["missing", "invalid_grant", "401", "insufficientPermissions"])
async def test_gateway_auth_failure_and_repeat(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setattr("teamagent.mcp_gateway.server._record_usage", lambda **kwargs: None)
    skill = (
        MailSummarySkill(token_store=InMemoryTokenStore())
        if kind == "missing"
        else MailSummarySkill(gmail=cast(Any, FailingGoogle(failure(kind))))
    )
    spec = tool_from_skill(MailSummarySkill, factory=lambda: skill)

    async def resolve(uid: str) -> ResolvedIdentity:
        return ResolvedIdentity(slack_user_id=TEST_SLACK_USER_ID, email=EMAIL)

    async def call() -> dict[str, Any]:
        content = await dispatch_tool(
            {spec.name: spec},
            spec.name,
            sign_arguments(
                spec.name,
                {"client_name": "花王"},
                channel_id="D0123456789",
                thread_ts="1784424000.000001",
            ),
            identity_resolver=resolve,
            caller_claim_verifier=make_verifier(),
            allowed_domains=frozenset({"example.com"}),
        )
        result = json.loads(content[0].text)
        assert isinstance(result, dict)
        return result

    assert await call() == {
        "error": "reauth_needed",
        "message": MESSAGE,
        "guidance_suppressed": False,
    }
    assert await call() == {"error": "reauth_needed", "message": "", "guidance_suppressed": True}


async def test_restored_drive_reference_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from teamagent.skills._shared.google_connection import record_google_failure
    from teamagent.skills.search.schema import SearchOutput
    from teamagent.skills.search.skill import SearchSkill

    monkeypatch.setattr("teamagent.mcp_gateway.server._record_usage", lambda **kwargs: None)

    def restore(skill: Any, query: str, context: Any, caller: Any) -> SearchOutput:
        record_google_failure(failure("401"), context)
        return SearchOutput(answer="資料を再確認できませんでした", total_cost_usd=0)

    monkeypatch.setattr("teamagent.mcp_gateway.document_followup.restore", restore)
    remember = MagicMock()
    monkeypatch.setattr("teamagent.mcp_gateway.document_followup.remember", remember)
    skill = SearchSkill(bedrock=MagicMock(), pgvector=MagicMock())
    spec = tool_from_skill(SearchSkill, factory=lambda: skill)

    async def resolve(uid: str) -> ResolvedIdentity:
        return ResolvedIdentity(slack_user_id=TEST_SLACK_USER_ID, email=EMAIL)

    content = await dispatch_tool(
        {spec.name: spec},
        spec.name,
        sign_arguments(
            spec.name,
            {"query": "その資料を見せて"},
            channel_id="D0123456789",
            thread_ts="1784424000.000001",
        ),
        identity_resolver=resolve,
        caller_claim_verifier=make_verifier(),
        allowed_domains=frozenset({"example.com"}),
    )
    assert json.loads(content[0].text) == {
        "error": "reauth_needed",
        "message": MESSAGE,
        "guidance_suppressed": False,
    }
    remember.assert_not_called()
