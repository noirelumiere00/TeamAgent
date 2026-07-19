"""End-to-end adversarial tests for Slack-event caller authorization."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel

from teamagent.adapters.slack_client import SlackClient
from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway.caller_claim import (
    CALLER_CLAIM_REPLAY_TABLE_ENV,
    CallerClaimError,
    CallerClaimVerifier,
    DynamoDbCallerClaimReplayStore,
)
from teamagent.mcp_gateway.server import USER_CONTEXT_KEY, dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from tests.caller_claim_testkit import (
    TEST_CALLER_CLAIM_SECRET,
    TEST_NOW,
    TEST_SLACK_CHANNEL_ID,
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
    make_verifier,
    sign_arguments,
)

ROOT = Path(__file__).resolve().parents[1]
CALLER_PLUGIN = ROOT / "infra/openclaw/caller-identity-plugin/dist/index.js"


class _Input(BaseModel):
    q: str


class _Output(BaseModel):
    email: str
    verified: bool


class _IdentitySkill(BaseSkill[_Input, _Output]):
    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "Caller identity test skill."
    input_schema: ClassVar[type[BaseModel]] = _Input
    output_schema: ClassVar[type[BaseModel]] = _Output

    def run(self, input: _Input, ctx: SkillContext) -> _Output:
        assert input.q
        return _Output(
            email=str(ctx.metadata["user_email"]),
            verified=bool(ctx.metadata["identity_verified"]),
        )


_BY_NAME = {"echo": ToolSpec("echo", _IdentitySkill.description, _IdentitySkill)}
_MEMBER = ResolvedIdentity(
    slack_user_id=TEST_SLACK_USER_ID,
    email="member@vectorinc.co.jp",
)


def _parse(contents: list[Any]) -> dict[str, Any]:
    assert len(contents) == 1
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


def _resolver(
    identity: ResolvedIdentity | None = _MEMBER,
    *,
    raises: bool = False,
) -> IdentityResolver:
    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        if raises:
            raise RuntimeError("resolver unavailable")
        if identity is None or slack_user_id != identity.slack_user_id:
            return None
        return identity

    return resolve


async def _dispatch(
    arguments: dict[str, Any],
    *,
    verifier: CallerClaimVerifier | None = None,
    resolver: IdentityResolver | None = None,
    company_shared: bool = True,
) -> dict[str, Any]:
    return _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            arguments,
            identity_resolver=resolver or _resolver(),
            company_shared_groups=(frozenset({"vectorinc.co.jp"}) if company_shared else None),
            caller_claim_verifier=verifier or make_verifier(),
            allowed_domains=frozenset({"vectorinc.co.jp"}),
            require_rls=True,
        )
    )


def _node_plugin_contract() -> dict[str, Any]:
    script = f"""
import {{createCallerIdentityPlugin}} from {json.dumps(CALLER_PLUGIN.as_uri())};
const hooks = {{}};
const plugin = createCallerIdentityPlugin({{
  env: {{
    TEAMAGENT_CALLER_CLAIM_SECRET: process.env.TEST_CLAIM_SECRET,
    SLACK_TEAM_ID: process.env.TEST_TEAM_ID,
  }},
  now: () => {TEST_NOW * 1000},
  randomBytesFn: () => Buffer.alloc(16, 7),
}});
plugin.register({{
  on: (name, callback) => {{ hooks[name] = callback; }},
  logger: {{warn: () => {{}}}},
}});
const trustedEvent = {{
  messageId: "1784424000.000001",
  metadata: {{guildId: process.env.TEST_TEAM_ID, to: process.env.TEST_CHANNEL_ID}},
}};
const trustedContext = {{
  channelId: "slack",
  sessionKey: "agent:main:slack:channel:test",
  senderId: process.env.TEST_USER_ID,
  conversationId: process.env.TEST_CHANNEL_ID,
}};
hooks.message_received(trustedEvent, trustedContext);
const runContext = {{
  runId: "11111111-1111-4111-8111-111111111111",
  sessionKey: trustedContext.sessionKey,
  messageProvider: "slack",
  senderId: process.env.TEST_USER_ID,
  channel: process.env.TEST_CHANNEL_ID,
  channelId: process.env.TEST_CHANNEL_ID,
}};
hooks.before_model_resolve({{prompt: "cross-language"}}, runContext);
const toolContext = {{
  ...runContext,
  toolName: "teamagent__echo",
  toolCallId: "toolu_valid_0123456789",
}};
const valid = hooks.before_tool_call(
  {{
    toolName: "teamagent__echo",
    runId: runContext.runId,
    toolCallId: toolContext.toolCallId,
    params: {{
      q: "cross-language",
      _user_context: {{slack_user_id: process.env.TEST_USER_ID}},
    }},
  }},
  toolContext,
);
const mismatch = hooks.before_tool_call(
  {{
    toolName: "teamagent__echo",
    runId: runContext.runId,
    toolCallId: "toolu_mismatch_0123456789",
    params: {{
      q: "mismatch",
      _user_context: {{slack_user_id: "U9999999999"}},
    }},
  }},
  {{...toolContext, toolCallId: "toolu_mismatch_0123456789"}},
);
const foreignContext = {{
  ...trustedContext,
  sessionKey: "agent:main:slack:channel:foreign",
}};
hooks.message_received(
  {{
    ...trustedEvent,
    metadata: {{...trustedEvent.metadata, guildId: "T9999999999"}},
  }},
  foreignContext,
);
const foreignRunContext = {{
  ...runContext,
  runId: "22222222-2222-4222-8222-222222222222",
  sessionKey: foreignContext.sessionKey,
}};
hooks.before_model_resolve({{prompt: "foreign"}}, foreignRunContext);
const foreignTeam = hooks.before_tool_call(
  {{
    toolName: "teamagent__echo",
    runId: foreignRunContext.runId,
    toolCallId: "toolu_foreign_0123456789",
    params: {{
      q: "foreign",
      _user_context: {{slack_user_id: process.env.TEST_USER_ID}},
    }},
  }},
  {{
    ...foreignRunContext,
    toolName: "teamagent__echo",
    toolCallId: "toolu_foreign_0123456789",
  }},
);
const replay = hooks.before_tool_call(
  {{
    toolName: "teamagent__echo",
    runId: runContext.runId,
    toolCallId: toolContext.toolCallId,
    params: {{
      q: "cross-language",
      _user_context: {{slack_user_id: process.env.TEST_USER_ID}},
    }},
  }},
  toolContext,
);
const nativeMessage = hooks.before_tool_call(
  {{toolName: "message", params: {{action: "send"}}}},
  {{toolName: "message"}},
);
process.stdout.write(JSON.stringify({{
  valid,
  mismatch,
  foreignTeam,
  replay,
  nativeMessage,
}}));
"""
    env = {
        **os.environ,
        "TEST_CLAIM_SECRET": TEST_CALLER_CLAIM_SECRET,
        "TEST_TEAM_ID": TEST_SLACK_TEAM_ID,
        "TEST_USER_ID": TEST_SLACK_USER_ID,
        "TEST_CHANNEL_ID": TEST_SLACK_CHANNEL_ID,
    }
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )
    return cast(dict[str, Any], json.loads(result.stdout))


async def test_openclaw_node_claim_is_accepted_by_python_mcp() -> None:
    contract = _node_plugin_contract()
    assert contract["mismatch"]["block"] is True
    assert "does not match" in contract["mismatch"]["blockReason"]
    assert contract["foreignTeam"]["block"] is True
    assert "missing or stale" in contract["foreignTeam"]["blockReason"]
    assert contract["replay"]["block"] is True
    assert "replay" in contract["replay"]["blockReason"]
    assert contract["nativeMessage"]["block"] is True
    assert "native" in contract["nativeMessage"]["blockReason"]
    out = await _dispatch(contract["valid"]["params"])
    assert out == {"email": "member@vectorinc.co.jp", "verified": True}


def test_same_session_cross_user_race_binds_each_exact_run_and_invocation() -> None:
    """A later user's event in one session must never become the first run's caller."""

    script = f"""
import {{createCallerIdentityPlugin}} from {json.dumps(CALLER_PLUGIN.as_uri())};
const hooks = {{}};
let nowMs = {TEST_NOW * 1000};
createCallerIdentityPlugin({{
  env: {{
    TEAMAGENT_CALLER_CLAIM_SECRET: process.env.TEST_CLAIM_SECRET,
    SLACK_TEAM_ID: process.env.TEST_TEAM_ID,
  }},
  now: () => nowMs,
  randomBytesFn: () => Buffer.alloc(16, 11),
}}).register({{
  on: (name, callback) => {{ hooks[name] = callback; }},
  logger: {{warn: () => {{}}}},
}});
const sessionKey = "agent:main:slack:channel:shared-race";
const channelId = process.env.TEST_CHANNEL_ID;
function ingress(userId, messageId, runId) {{
  const event = {{
    channel: "slack",
    content: `message-${{messageId}}`,
    isGroup: true,
    messageId,
    runId,
    senderId: userId,
    metadata: {{
      guildId: process.env.TEST_TEAM_ID,
      to: channelId,
      senderId: userId,
      messageId,
    }},
  }};
  const context = {{
    channelId: "slack",
    sessionKey,
    runId,
    senderId: userId,
    conversationId: channelId,
    messageId,
  }};
  hooks.inbound_claim(event, context);
  hooks.message_received(event, context);
}}
function bind(runId, userId) {{
  hooks.before_model_resolve({{prompt: `prompt-${{userId}}`}}, {{
    runId,
    sessionKey,
    messageProvider: "slack",
    senderId: userId,
    channel: channelId,
    channelId,
  }});
}}
function call(runId, userId, toolCallId) {{
  return hooks.before_tool_call(
    {{
      toolName: "teamagent__echo",
      runId,
      toolCallId,
      params: {{
        q: userId,
        _user_context: {{slack_user_id: userId}},
      }},
    }},
    {{
      toolName: "teamagent__echo",
      runId,
      toolCallId,
      sessionKey,
      channelId,
    }},
  );
}}
const userA = "U0123456789";
const userB = "U9999999999";
const runA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const runB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
ingress(userA, "1784424000.000001", runA);
ingress(userB, "1784424000.000002", runB);
bind(runA, userA);
bind(runB, userB);
const conflictingRun = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
ingress(userA, "1784424000.000003", conflictingRun);
ingress(userB, "1784424000.000004", conflictingRun);
bind(conflictingRun, userB);
const signedA = call(runA, userA, "toolu_race_a_0123456789");
const signedB = call(runB, userB, "toolu_race_b_0123456789");
const conflictingIngress = call(
  conflictingRun,
  userB,
  "toolu_conflicting_ingress_0123456789",
);
function payload(result) {{
  const token = result.params._user_context.caller_claim;
  return JSON.parse(Buffer.from(token.split(".")[0], "base64url").toString("utf8"));
}}
const replay = call(runA, userA, "toolu_race_a_0123456789");
const mismatchedRun = hooks.before_tool_call(
  {{
    toolName: "teamagent__echo",
    runId: runA,
    toolCallId: "toolu_wrong_run_0123456789",
    params: {{q: "wrong", _user_context: {{slack_user_id: userA}}}},
  }},
  {{
    toolName: "teamagent__echo",
    runId: runB,
    toolCallId: "toolu_wrong_run_0123456789",
    sessionKey,
    channelId,
  }},
);
const mismatchedToolCall = hooks.before_tool_call(
  {{
    toolName: "teamagent__echo",
    runId: runA,
    toolCallId: "toolu_event_0123456789",
    params: {{q: "wrong", _user_context: {{slack_user_id: userA}}}},
  }},
  {{
    toolName: "teamagent__echo",
    runId: runA,
    toolCallId: "toolu_context_0123456789",
    sessionKey,
    channelId,
  }},
);
const unbound = call(
  "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  userA,
  "toolu_unbound_0123456789",
);
nowMs += 10 * 60 * 1000 + 1;
const stale = call(runB, userB, "toolu_stale_0123456789");
process.stdout.write(JSON.stringify({{
  payloadA: payload(signedA),
  payloadB: payload(signedB),
  conflictingIngress,
  replay,
  mismatchedRun,
  mismatchedToolCall,
  unbound,
  stale,
}}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "TEST_CLAIM_SECRET": TEST_CALLER_CLAIM_SECRET,
            "TEST_TEAM_ID": TEST_SLACK_TEAM_ID,
            "TEST_CHANNEL_ID": TEST_SLACK_CHANNEL_ID,
        },
    )
    result = json.loads(completed.stdout)

    assert result["payloadA"]["sub"] == "U0123456789"
    assert result["payloadA"]["message"] == "1784424000.000001"
    assert result["payloadA"]["run_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert result["payloadA"]["tool_call_id"] == "toolu_race_a_0123456789"
    assert result["payloadB"]["sub"] == "U9999999999"
    assert result["payloadB"]["message"] == "1784424000.000002"
    assert result["payloadB"]["run_id"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    assert result["payloadB"]["tool_call_id"] == "toolu_race_b_0123456789"
    assert result["conflictingIngress"]["block"] is True
    assert "missing or stale" in result["conflictingIngress"]["blockReason"]
    assert result["replay"]["block"] is True
    assert "replay" in result["replay"]["blockReason"]
    assert result["mismatchedRun"]["block"] is True
    assert "run binding" in result["mismatchedRun"]["blockReason"]
    assert result["mismatchedToolCall"]["block"] is True
    assert "tool invocation binding" in result["mismatchedToolCall"]["blockReason"]
    assert result["unbound"]["block"] is True
    assert "missing or stale" in result["unbound"]["blockReason"]
    assert result["stale"]["block"] is True
    assert "stale" in result["stale"]["blockReason"]


@pytest.mark.parametrize(
    "case",
    [
        "caller_mismatch",
        "expired",
        "wrong_audience",
        "tamper",
        "wrong_team",
        "old_version",
    ],
)
async def test_claim_adversarial_cases_fail_closed(case: str) -> None:
    if case == "expired":
        arguments = sign_arguments(
            "echo",
            {"q": "expired"},
            now=TEST_NOW - 120,
            expires_at=TEST_NOW - 60,
        )
    elif case == "wrong_audience":
        arguments = sign_arguments(
            "echo",
            {"q": "wrong-audience"},
            audience="attacker-service",
        )
    elif case == "wrong_team":
        arguments = sign_arguments(
            "echo",
            {"q": "wrong-team"},
            team_id="T9999999999",
        )
    elif case == "old_version":
        arguments = sign_arguments(
            "echo",
            {"q": "old-version"},
            payload_overrides={"v": 1},
        )
    else:
        arguments = sign_arguments("echo", {"q": case})
        if case == "caller_mismatch":
            arguments[USER_CONTEXT_KEY]["slack_user_id"] = "U9999999999"
        elif case == "tamper":
            arguments["q"] = "tampered-after-signing"
    out = await _dispatch(arguments)
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


async def test_claim_replay_is_rejected_after_one_success() -> None:
    verifier = make_verifier()
    arguments = sign_arguments(
        "echo",
        {"q": "one-use"},
        nonce_seed="replay-contract",
    )
    first = await _dispatch(arguments, verifier=verifier)
    second = await _dispatch(arguments, verifier=verifier)
    assert first["verified"] is True
    assert second["code"] == "CALLER_IDENTITY_REJECTED"


async def test_claim_replay_is_rejected_across_mcp_verifier_instances() -> None:
    """Conditional PutItem is shared by rolling old/new MCP tasks."""

    class _ConditionalCheckError(Exception):
        def __init__(self) -> None:
            super().__init__("already consumed")
            self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}

    class _FakeDynamoDb:
        def __init__(self) -> None:
            self.seen: set[str] = set()
            self.items: list[dict[str, Any]] = []

        def put_item(self, **kwargs: Any) -> None:
            assert kwargs["TableName"] == "caller-claim-nonces"
            assert kwargs["ConditionExpression"] == "attribute_not_exists(#nonce)"
            assert kwargs["ExpressionAttributeNames"] == {"#nonce": "nonce"}
            item = kwargs["Item"]
            nonce = str(item["nonce"]["S"])
            assert item["expires_at"] == {"N": str(TEST_NOW + 60)}
            if nonce in self.seen:
                raise _ConditionalCheckError
            self.seen.add(nonce)
            self.items.append(item)

    dynamodb = _FakeDynamoDb()
    replay_store = DynamoDbCallerClaimReplayStore(
        table_name="caller-claim-nonces",
        client=dynamodb,
    )
    first_verifier = CallerClaimVerifier(
        secret=TEST_CALLER_CLAIM_SECRET,
        expected_team_id=TEST_SLACK_TEAM_ID,
        clock=lambda: TEST_NOW,
        replay_store=replay_store,
    )
    second_verifier = CallerClaimVerifier(
        secret=TEST_CALLER_CLAIM_SECRET,
        expected_team_id=TEST_SLACK_TEAM_ID,
        clock=lambda: TEST_NOW,
        replay_store=replay_store,
    )
    arguments = sign_arguments(
        "echo",
        {"q": "cross-task-one-use"},
        nonce_seed="cross-task-one-use",
    )

    first = await _dispatch(arguments, verifier=first_verifier)
    second = await _dispatch(arguments, verifier=second_verifier)

    assert first["verified"] is True
    assert second["code"] == "CALLER_IDENTITY_REJECTED"
    assert len(dynamodb.items) == 1


async def test_replay_store_failure_rejects_before_identity_resolution() -> None:
    class _UnavailableDynamoDb:
        def put_item(self, **_kwargs: Any) -> None:
            raise RuntimeError("simulated DynamoDB outage")

    resolver_called = False

    async def resolver(_slack_user_id: str) -> ResolvedIdentity | None:
        nonlocal resolver_called
        resolver_called = True
        return _MEMBER

    verifier = CallerClaimVerifier(
        secret=TEST_CALLER_CLAIM_SECRET,
        expected_team_id=TEST_SLACK_TEAM_ID,
        clock=lambda: TEST_NOW,
        replay_store=DynamoDbCallerClaimReplayStore(
            table_name="caller-claim-nonces",
            client=_UnavailableDynamoDb(),
        ),
    )
    out = await _dispatch(
        sign_arguments("echo", {"q": "replay-store-outage"}),
        verifier=verifier,
        resolver=resolver,
    )

    assert out["code"] == "CALLER_IDENTITY_REJECTED"
    assert resolver_called is False


async def test_common_mcp_bearer_cannot_forge_caller_claim() -> None:
    arguments = sign_arguments(
        "echo",
        {"q": "forged"},
        secret="attacker-knows-only-the-common-mcp-bearer",
    )
    out = await _dispatch(arguments)
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


@pytest.mark.parametrize("member_flag", ["is_restricted", "is_ultra_restricted", "is_stranger"])
async def test_guest_and_stranger_resolver_results_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    member_flag: str,
) -> None:
    monkeypatch.setenv("SLACK_TEAM_ID", TEST_SLACK_TEAM_ID)

    class _FakeSlack:
        async def users_info(self, *, user: str) -> dict[str, Any]:
            assert user == TEST_SLACK_USER_ID
            return {
                "user": {
                    "id": user,
                    "team_id": TEST_SLACK_TEAM_ID,
                    member_flag: True,
                    "profile": {"email": "guest@vectorinc.co.jp"},
                }
            }

    client = SlackClient(bot_token="test", client=_FakeSlack())  # type: ignore[arg-type]
    out = await _dispatch(
        sign_arguments("echo", {"q": member_flag}),
        resolver=client.resolve_identity,
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


async def test_resolver_failure_fails_closed_for_company_shared() -> None:
    out = await _dispatch(
        sign_arguments("echo", {"q": "resolver-failure"}),
        resolver=_resolver(raises=True),
    )
    assert out["code"] == "CALLER_IDENTITY_REJECTED"


def test_production_claim_contract_requires_separate_secret_and_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAMAGENT_CALLER_CLAIM_SECRET", raising=False)
    monkeypatch.delenv("TEAMAGENT_MCP_BEARER", raising=False)
    monkeypatch.delenv("SLACK_TEAM_ID", raising=False)
    monkeypatch.delenv(CALLER_CLAIM_REPLAY_TABLE_ENV, raising=False)
    with pytest.raises(CallerClaimError):
        CallerClaimVerifier.from_env()

    monkeypatch.setenv("TEAMAGENT_CALLER_CLAIM_SECRET", TEST_CALLER_CLAIM_SECRET)
    monkeypatch.setenv(CALLER_CLAIM_REPLAY_TABLE_ENV, "caller-claim-nonces")
    monkeypatch.setenv("SLACK_TEAM_ID", "T_BAD")
    with pytest.raises(CallerClaimError):
        CallerClaimVerifier.from_env()

    monkeypatch.setenv("SLACK_TEAM_ID", TEST_SLACK_TEAM_ID)
    monkeypatch.setenv(CALLER_CLAIM_REPLAY_TABLE_ENV, "")
    with pytest.raises(CallerClaimError):
        CallerClaimVerifier.from_env()

    monkeypatch.setenv(CALLER_CLAIM_REPLAY_TABLE_ENV, "caller-claim-nonces")
    monkeypatch.setenv("TEAMAGENT_MCP_BEARER", TEST_CALLER_CLAIM_SECRET)
    with pytest.raises(CallerClaimError, match="must differ"):
        CallerClaimVerifier.from_env()
