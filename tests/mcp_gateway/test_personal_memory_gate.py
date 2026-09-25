"""本人メモ 3 ツールの MCP 境界（M5 の不変条件 I1〜I10）。

実際の build_server と SDK の CallTool ハンドラを通し、署名は caller claim の testkit で作る。
service（DB・Hermes）は呼び出しを記録するだけのフェイクに差し替える。
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from mcp import types
from pydantic import BaseModel

import teamagent.mcp_gateway.server as server_mod
from teamagent.identity import IdentityResolver, ResolvedIdentity
from teamagent.mcp_gateway.caller_claim import InMemoryCallerClaimReplayStore
from teamagent.mcp_gateway.personal_memory import PERSONAL_MEMORY_TOOL_NAMES, gate, schemas
from teamagent.mcp_gateway.personal_memory import service as pm_service
from teamagent.mcp_gateway.server import build_server, dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills._shared.connect_intent import FREE_TEXT_FIELDS
from teamagent.skills.base import BaseSkill, SkillContext
from tests.caller_claim_testkit import (
    TEST_CALLER_CLAIM_SECRET,
    TEST_SLACK_TEAM_ID,
    TEST_SLACK_USER_ID,
    sign_arguments,
)

DM = "D0123456789"
EMAIL = "member@vectorinc.co.jp"
MARKER = "ZQX-MARKER"
KIND = {"personal_memory_observe": "obs", "personal_memory_context": "ctx"}
KIND["personal_memory_command"] = "cmd"
ARGS: dict[str, dict[str, Any]] = {
    "personal_memory_observe": {"utterance": "資料は短めが好き", "has_attachment": False},
    "personal_memory_context": {},
    "personal_memory_command": {"action": "list"},
}


# --- フェイク -------------------------------------------------------------------------------


class _Input(BaseModel):
    q: str = ""


class _Output(BaseModel):
    ok: bool


class _EchoSkill(BaseSkill[_Input, _Output]):
    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "echo"
    input_schema: ClassVar[type[BaseModel]] = _Input
    output_schema: ClassVar[type[BaseModel]] = _Output

    def run(self, input: _Input, ctx: SkillContext) -> _Output:
        return _Output(ok=True)


class _ConnectSkill(_EchoSkill):
    name: ClassVar[str] = "oauth_connect"
    calls: ClassVar[int] = 0

    def run(self, input: _Input, ctx: SkillContext) -> _Output:
        type(self).calls += 1
        return _Output(ok=True)


class _CountingReplayStore(InMemoryCallerClaimReplayStore):
    def __init__(self) -> None:
        super().__init__()
        self.consumed = 0

    async def consume(self, nonce: str, *, expires_at: int, now: int) -> None:
        self.consumed += 1
        await super().consume(nonce, expires_at=expires_at, now=now)


def _resolver(email: str = EMAIL) -> IdentityResolver:
    async def resolve(slack_user_id: str) -> ResolvedIdentity | None:
        if slack_user_id != TEST_SLACK_USER_ID:
            return None
        return ResolvedIdentity(slack_user_id=slack_user_id, email=email)

    return resolve


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str, Any]] = []
        self.raise_error: Exception | None = None

    async def __call__(
        self, tool: str, principal: Any, message_id: str, payload: Any
    ) -> dict[str, Any]:
        self.calls.append((tool, principal, message_id, payload))
        if self.raise_error is not None:
            raise self.raise_error
        return {"status": "buffered"} if tool.endswith("observe") else {"ok": True}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(pm_service, "handle_personal_memory", rec)
    return rec


@pytest.fixture(autouse=True)
def _pm_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("USE_PERSONAL_MEMORY", "1")
    monkeypatch.setenv(gate.ALLOWED_EMAILS_ENV, EMAIL)
    monkeypatch.delenv("USE_AGENT_ORCHESTRATOR", raising=False)
    _ConnectSkill.calls = 0
    yield


@pytest.fixture
def replay() -> _CountingReplayStore:
    return _CountingReplayStore()


def _server(
    replay: _CountingReplayStore,
    *,
    specs: list[ToolSpec] | None = None,
    resolver: IdentityResolver | None = None,
    legacy: bool = False,
) -> Any:
    specs = specs if specs is not None else [ToolSpec("echo", "echo", _EchoSkill)]
    if legacy:
        return build_server(specs, require_rls=False)
    from teamagent.mcp_gateway.caller_claim import CallerClaimVerifier
    from tests.caller_claim_testkit import TEST_NOW

    verifier = CallerClaimVerifier(
        secret=TEST_CALLER_CLAIM_SECRET,
        expected_team_id=TEST_SLACK_TEAM_ID,
        clock=lambda: TEST_NOW,
        replay_store=replay,
    )
    return build_server(
        specs,
        identity_resolver=resolver or _resolver(),
        caller_claim_verifier=verifier,
    )


def _invocation(tool: str) -> str:
    return f"aico-pm-{KIND[tool]}-{secrets.token_hex(16)}"


def _signed(
    tool: str,
    business: dict[str, Any] | None = None,
    *,
    channel_id: str = DM,
    tool_call_id: str | None = None,
    run_id: str | None = None,
    thread_ts: str | None = None,
    declared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    call_id = tool_call_id if tool_call_id is not None else _invocation(tool)
    return sign_arguments(
        tool,
        ARGS[tool] if business is None else business,
        channel_id=channel_id,
        tool_call_id=call_id,
        run_id=run_id if run_id is not None else call_id,
        thread_ts=thread_ts,
        declared_context=declared,
    )


async def _call(server: Any, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """SDK の CallTool ハンドラを通す（未捕捉の例外は SDK が str(e) で返す経路）。"""
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await server.request_handlers[types.CallToolRequest](request)
    content = result.root.content
    assert len(content) == 1
    return content[0].text, bool(result.root.isError)


def _code(text: str) -> str | None:
    payload = json.loads(text)
    return payload.get("code") if payload.get("error") == "personal_memory_rejected" else None


# --- 純粋な門 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "ok"),
    [
        ("D0123456789", True),
        ("C0123456789", False),
        ("G0123456789", False),
        ("C08D3KQ7ABC", False),  # D を含む公開チャンネル
        (" D0123456789", False),  # strip しない
        ("D0123456789\n", False),
        ("D0123", False),
        (None, False),
    ],
)
def test_is_dm_channel(channel: Any, ok: bool) -> None:
    assert gate.is_dm_channel(channel) is ok


@pytest.mark.parametrize("tool", sorted(PERSONAL_MEMORY_TOOL_NAMES))
def test_reserved_invocation(tool: str) -> None:
    good = _invocation(tool)
    assert gate.reserved_invocation_ok(tool, good, good)
    other = next(k for t, k in KIND.items() if t != tool)
    wrong_kind = f"aico-pm-{other}-{'a' * 32}"
    bad = [
        "toolu_0123456789abcdef",
        wrong_kind,
        f"aico-pm-{KIND[tool]}-{'a' * 31}",
        f"aico-pm-{KIND[tool]}-{'a' * 32}x",
        f"x-aico-pm-{KIND[tool]}-{'a' * 32}",
        f"aico-pm-{KIND[tool]}-{'A' * 32}",
    ]
    for call_id in bad:
        assert not gate.reserved_invocation_ok(tool, call_id, call_id), call_id
    assert not gate.reserved_invocation_ok(tool, good, "11111111-1111-4111-8111-111111111111")
    assert not gate.reserved_invocation_ok("search", good, good)


@pytest.mark.parametrize("raw", [None, "", " ", ",", " , "])
def test_allowlist_empty_denies_everyone(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
    if raw is None:
        monkeypatch.delenv(gate.ALLOWED_EMAILS_ENV, raising=False)
    else:
        monkeypatch.setenv(gate.ALLOWED_EMAILS_ENV, raw)
    assert gate.allowed_emails_from_env() == frozenset()
    assert not gate.is_allowed(EMAIL)
    assert not gate.is_allowed("")


def test_allowlist_is_case_insensitive_and_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gate.ALLOWED_EMAILS_ENV, " Member@VectorInc.co.jp , other@vectorinc.co.jp")
    assert gate.is_allowed("member@vectorinc.co.jp")
    assert gate.is_allowed("MEMBER@vectorinc.co.jp ")
    assert not gate.is_allowed("member@vectorinc.co.jp.evil")
    assert not gate.is_allowed("x-member@vectorinc.co.jp")


def test_schema_field_names_disjoint_from_free_text_fields() -> None:
    forbidden = {"query"} | set(FREE_TEXT_FIELDS)
    for name, model in schemas.INPUT_MODELS.items():
        assert model.model_config.get("extra") == "forbid", name
        assert not set(model.model_fields) & forbidden, name
    fields = set().union(*(m.model_fields for m in schemas.INPUT_MODELS.values()))
    assert fields == {"utterance", "has_attachment", "action", "item_no"}
    assert set(schemas.INPUT_MODELS) == PERSONAL_MEMORY_TOOL_NAMES


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "forget"},
        {"action": "list", "item_no": 1},
        {"action": "forget", "item_no": 0},
        {"action": "forget", "item_no": 81},
        {"action": "forget", "item_no": "3"},
        {"action": "forget", "item_no": True},
        {"action": "delete_everything"},
    ],
)
def test_command_input_rejects(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        schemas.CommandInput.model_validate(payload)


def test_command_input_accepts_forget_with_item_no() -> None:
    assert schemas.CommandInput.model_validate({"action": "forget", "item_no": 3}).item_no == 3


# --- I1: list_tools に出さない -------------------------------------------------------------


@pytest.mark.parametrize("flag", ["1", "0"])
async def test_list_tools_never_includes_personal_memory(
    monkeypatch: pytest.MonkeyPatch, replay: _CountingReplayStore, flag: str
) -> None:
    monkeypatch.setenv("USE_PERSONAL_MEMORY", flag)
    server = _server(replay)
    result = await server.request_handlers[types.ListToolsRequest](None)
    names = {tool.name for tool in result.root.tools}
    assert "echo" in names
    assert not {n for n in names if n.startswith("personal_memory")}


def test_toolspec_named_personal_memory_is_refused(replay: _CountingReplayStore) -> None:
    specs = [ToolSpec("personal_memory_observe", "x", _EchoSkill)]
    with pytest.raises(RuntimeError):
        _server(replay, specs=specs)


# --- I2: フラグ off は未登録ツールと区別できない ----------------------------------------------


@pytest.mark.parametrize("tool", sorted(PERSONAL_MEMORY_TOOL_NAMES))
async def test_flag_off_is_indistinguishable_from_unknown_tool(
    monkeypatch: pytest.MonkeyPatch,
    replay: _CountingReplayStore,
    recorder: _Recorder,
    tool: str,
) -> None:
    monkeypatch.setenv("USE_PERSONAL_MEMORY", "0")
    server = _server(replay)
    text, _ = await _call(server, tool, _signed(tool))
    unknown = await dispatch_tool({}, tool, {})
    assert text == unknown[0].text
    assert replay.consumed == 0  # claim の nonce を消費しない
    assert recorder.calls == []


# --- I3/I4: LEGACY は拒否・claim 必須 -------------------------------------------------------


async def test_legacy_mode_rejected(replay: _CountingReplayStore, recorder: _Recorder) -> None:
    server = _server(replay, legacy=True)
    args = dict(ARGS["personal_memory_observe"])
    args["_user_context"] = {"user_email": EMAIL, "channel_id": DM}
    text, _ = await _call(server, "personal_memory_observe", args)
    assert _code(text) == "PM_UNAVAILABLE"
    assert recorder.calls == []


async def test_missing_or_wrong_tool_claim_rejected(
    replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    server = _server(replay)
    unsigned = {"utterance": "x", "_user_context": {"slack_user_id": TEST_SLACK_USER_ID}}
    text, _ = await _call(server, "personal_memory_observe", unsigned)
    assert _code(text) == "PM_CALLER_REJECTED"
    # observe 用に署名した claim を command に使い回せない（tool 名に束縛）
    signed = _signed("personal_memory_observe")
    text, _ = await _call(server, "personal_memory_context", signed)
    assert _code(text) == "PM_CALLER_REJECTED"
    assert recorder.calls == []


async def test_verifier_returning_none_is_rejected(
    replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    # 本物の検証器は claim を返すか例外を投げるが、None が返っても素通りさせない（多重防御）
    class _NoneVerifier:
        async def verify(self, *, tool: str, arguments: dict[str, Any]) -> None:
            return None

    server = build_server(
        [ToolSpec("echo", "echo", _EchoSkill)],
        identity_resolver=_resolver(),
        caller_claim_verifier=_NoneVerifier(),  # type: ignore[arg-type]
    )
    text, _ = await _call(server, "personal_memory_context", _signed("personal_memory_context"))
    assert _code(text) == "PM_CALLER_REQUIRED"
    assert recorder.calls == []


async def test_user_context_must_be_object(
    replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    server = _server(replay)
    text, _ = await _call(server, "personal_memory_context", {"_user_context": "x"})
    assert _code(text) == "PM_INVALID_INPUT"
    assert recorder.calls == []


# --- I5: DM だけ -----------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["C0123456789", "G0123456789", "C08D3KQ7ABC"])
async def test_non_dm_channel_rejected(
    replay: _CountingReplayStore, recorder: _Recorder, channel: str
) -> None:
    server = _server(replay)
    text, _ = await _call(
        server, "personal_memory_observe", _signed("personal_memory_observe", channel_id=channel)
    )
    assert _code(text) == "PM_NOT_DM"
    assert recorder.calls == []


async def test_dm_passes(replay: _CountingReplayStore, recorder: _Recorder) -> None:
    server = _server(replay)
    text, is_error = await _call(
        server, "personal_memory_observe", _signed("personal_memory_observe")
    )
    assert json.loads(text) == {"status": "buffered"}
    assert not is_error
    tool, principal, message_id, payload = recorder.calls[0]
    assert tool == "personal_memory_observe"
    assert principal.key == f"{TEST_SLACK_TEAM_ID}:{TEST_SLACK_USER_ID}"
    assert principal.user_email == EMAIL
    assert message_id == "1784424000.000001"
    assert payload.utterance == "資料は短めが好き"


async def test_thread_rejected(replay: _CountingReplayStore, recorder: _Recorder) -> None:
    server = _server(replay)
    args = _signed("personal_memory_observe", thread_ts="1784424000.000001")
    text, _ = await _call(server, "personal_memory_observe", args)
    assert _code(text) == "PM_THREAD_REJECTED"
    assert recorder.calls == []


# --- I6: 予約 tool_call_id ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_call_id", "run_id"),
    [
        ("toolu_0123456789abcdef", None),
        (f"aico-pm-ctx-{'b' * 32}", None),  # observe に ctx の ID
        (f"aico-pm-obs-{'b' * 31}", None),
        (f"aico-pm-obs-{'b' * 32}x", None),  # claim の書式は満たすが fullmatch に違反
        (f"aico-pm-obs-{'b' * 32}", "11111111-1111-4111-8111-111111111111"),
    ],
)
async def test_reserved_invocation_required(
    replay: _CountingReplayStore,
    recorder: _Recorder,
    tool_call_id: str,
    run_id: str | None,
) -> None:
    server = _server(replay)
    args = _signed("personal_memory_observe", tool_call_id=tool_call_id, run_id=run_id)
    text, _ = await _call(server, "personal_memory_observe", args)
    assert _code(text) == "PM_INVOCATION_REJECTED"
    assert recorder.calls == []


# --- I7/I8: allowlist とフラグ --------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "other@vectorinc.co.jp"])
async def test_allowlist_rejects(
    monkeypatch: pytest.MonkeyPatch, replay: _CountingReplayStore, recorder: _Recorder, raw: str
) -> None:
    monkeypatch.setenv(gate.ALLOWED_EMAILS_ENV, raw)
    server = _server(replay)
    text, _ = await _call(server, "personal_memory_context", _signed("personal_memory_context"))
    assert _code(text) == "PM_NOT_ALLOWED"
    assert recorder.calls == []


async def test_allowlist_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    monkeypatch.setenv(gate.ALLOWED_EMAILS_ENV, "MEMBER@vectorinc.co.jp")
    server = _server(replay)
    text, _ = await _call(server, "personal_memory_context", _signed("personal_memory_context"))
    assert _code(text) is None
    assert len(recorder.calls) == 1


async def test_declared_email_ignored(
    monkeypatch: pytest.MonkeyPatch, replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    # resolver は別人（対象外）の email を返す。申告値に allowlist の人を書いても通さない
    server = _server(replay, resolver=_resolver("someone@vectorinc.co.jp"))
    args = _signed("personal_memory_context", declared={"user_email": EMAIL})
    text, _ = await _call(server, "personal_memory_context", args)
    assert _code(text) == "PM_NOT_ALLOWED"
    assert recorder.calls == []


async def test_flag_and_allowlist_both_required(
    monkeypatch: pytest.MonkeyPatch, replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    monkeypatch.setenv(gate.ALLOWED_EMAILS_ENV, "")
    server = _server(replay)
    text, _ = await _call(server, "personal_memory_context", _signed("personal_memory_context"))
    assert _code(text) == "PM_NOT_ALLOWED"
    monkeypatch.setenv(gate.ALLOWED_EMAILS_ENV, EMAIL)
    monkeypatch.setenv("USE_PERSONAL_MEMORY", "false")
    server = _server(replay)
    text, _ = await _call(server, "personal_memory_context", _signed("personal_memory_context"))
    assert json.loads(text)["error"] == "unknown tool: personal_memory_context"
    assert recorder.calls == []


async def test_identity_resolution_failure_rejected(
    replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    async def none_resolver(slack_user_id: str) -> ResolvedIdentity | None:
        return None

    server = _server(replay, resolver=none_resolver)
    text, _ = await _call(server, "personal_memory_context", _signed("personal_memory_context"))
    assert _code(text) == "PM_IDENTITY_REJECTED"
    assert recorder.calls == []


# --- I9/I10: 入力検証と例外は固定コードだけ ---------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "business"),
    [
        ("personal_memory_observe", {"utterance": MARKER + "あ" * 800}),
        ("personal_memory_observe", {"utterance": "x", "query": MARKER}),
        ("personal_memory_observe", {"utterance": 12345, "has_attachment": False}),
        ("personal_memory_observe", {"utterance": "添付の有無を渡し忘れた"}),
        ("personal_memory_command", {"action": MARKER}),
        ("personal_memory_command", {"action": "forget", "item_no": MARKER}),
        ("personal_memory_context", {"text": MARKER}),
    ],
)
async def test_invalid_input_returns_fixed_code_without_marker(
    replay: _CountingReplayStore,
    recorder: _Recorder,
    tool: str,
    business: dict[str, Any],
) -> None:
    server = _server(replay)
    text, _ = await _call(server, tool, _signed(tool, business))
    assert _code(text) == "PM_INVALID_INPUT"
    assert MARKER not in text
    assert "input_value" not in text
    assert recorder.calls == []


async def test_internal_error_returns_fixed_code_without_marker(
    replay: _CountingReplayStore, recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    recorder.raise_error = ValueError(MARKER)
    server = _server(replay)
    caplog.set_level(logging.DEBUG)
    text, _ = await _call(server, "personal_memory_observe", _signed("personal_memory_observe"))
    assert _code(text) == "PM_INTERNAL"
    assert MARKER not in text
    assert MARKER not in caplog.text


# --- I3: dispatch_tool の後処理を通らない ----------------------------------------------------


async def test_connect_keyword_is_not_redirected(
    replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    specs = [
        ToolSpec("echo", "echo", _EchoSkill),
        ToolSpec("oauth_connect", "connect", _ConnectSkill),
    ]
    server = _server(replay, specs=specs)
    args = _signed(
        "personal_memory_observe",
        {"utterance": "Googleカレンダーを連携して", "has_attachment": False},
    )
    text, _ = await _call(server, "personal_memory_observe", args)
    assert json.loads(text) == {"status": "buffered"}
    assert _ConnectSkill.calls == 0
    assert len(recorder.calls) == 1


async def test_usage_events_not_recorded(
    monkeypatch: pytest.MonkeyPatch, replay: _CountingReplayStore, recorder: _Recorder
) -> None:
    recorded: list[Any] = []
    monkeypatch.setattr(server_mod, "_record_usage", lambda *a, **k: recorded.append((a, k)))
    server = _server(replay)
    for tool in sorted(PERSONAL_MEMORY_TOOL_NAMES):
        await _call(server, tool, _signed(tool))
    assert len(recorder.calls) == 3
    assert recorded == []
    # 対照: 通常のツールは記録される（spy が効いていることの確認）
    await _call(server, "echo", sign_arguments("echo", {"q": "x"}))
    assert len(recorded) == 1


# --- SDK の WARNING ------------------------------------------------------------------------


async def test_sdk_not_listed_warning_suppressed_only_for_personal_memory(
    replay: _CountingReplayStore, recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    server = _server(replay)
    caplog.set_level(logging.WARNING, logger="mcp.server.lowlevel.server")
    await _call(server, "personal_memory_context", _signed("personal_memory_context"))
    assert "not listed" not in caplog.text
    await _call(server, "no_such_tool", {})
    assert "Tool 'no_such_tool' not listed" in caplog.text
