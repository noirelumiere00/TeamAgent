"""MCP 公開層（mcp_gateway.server）の単体テスト（外部I/O無し・DB/トークン不要）。

検証の主眼（P0 の de-risk 対象）:
1. tool 列挙が ToolSpec から生成され、入力スキーマに _user_context が付く。
2. user_email 無しの呼び出しは fail-closed で拒否（越権防止）。
3. user_email 有りの呼び出しで RLS 用コンテキストが SkillContext.metadata へ伝播する
   （＝ RLS が MCP 越しでも skill に届く＝境界内で行権限を効かせられる）。
4. 入力検証エラー・skill 例外は構造化エラーで返り、サーバを落とさない。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway import server as mcp_server
from teamagent.mcp_gateway.server import (
    SEARCH_TOOL_NAME,
    USER_CONTEXT_KEY,
    _envflag,
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
    assert "slack_team_id" in props[USER_CONTEXT_KEY]["properties"]
    assert "caller_claim" in props[USER_CONTEXT_KEY]["properties"]


async def test_fail_closed_without_user_email() -> None:
    out = _parse(await dispatch_tool(_BY_NAME, "echo", {"q": "hi"}, require_rls=True))
    assert "RLS required" in out["error"]


@pytest.mark.parametrize("invalid_context", [[], "", False, 0])
async def test_user_context_must_be_an_object_even_when_falsy(
    invalid_context: object,
) -> None:
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: invalid_context},
            require_rls=True,
        )
    )
    assert "_user_context must be an object" in out["error"]


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


# --- search 応答への Web UI リンク注入（ゲート層のみ・SearchSkill 不変） ---------------


class _FakeSearchOutput(BaseModel):
    answer: str


class _FakeSearchSkill(BaseSkill[_EchoInput, _FakeSearchOutput]):
    """name="search" のフェイク skill（Web UI リンク注入の検証用・外部I/O無し）。"""

    name: ClassVar[str] = SEARCH_TOOL_NAME
    description: ClassVar[str] = "テスト用フェイク検索。"
    input_schema: ClassVar[type[BaseModel]] = _EchoInput
    output_schema: ClassVar[type[BaseModel]] = _FakeSearchOutput

    def run(self, input: _EchoInput, ctx: SkillContext) -> _FakeSearchOutput:
        return _FakeSearchOutput(answer=f"hits for {input.q}")


_SEARCH_BY_NAME = {SEARCH_TOOL_NAME: ToolSpec(SEARCH_TOOL_NAME, "fake", _FakeSearchSkill)}


# --- 本番 MCP 経路の usage_events best-effort 記録 ------------------------------


class _UsageSearchInput(BaseModel):
    query: str
    mail_body: str | None = None


class _UsageSearchOutput(BaseModel):
    answer: str
    total_cost_usd: float


class _UsageSearchSkill(BaseSkill[_UsageSearchInput, _UsageSearchOutput]):
    """query と cost の usage 配線だけを観測する外部 I/O 無しの search。"""

    name: ClassVar[str] = SEARCH_TOOL_NAME
    description: ClassVar[str] = "usage 記録テスト用フェイク検索。"
    input_schema: ClassVar[type[BaseModel]] = _UsageSearchInput
    output_schema: ClassVar[type[BaseModel]] = _UsageSearchOutput

    def run(self, input: _UsageSearchInput, ctx: SkillContext) -> _UsageSearchOutput:
        return _UsageSearchOutput(answer=f"hits for {input.query}", total_cost_usd=0.125)


class _UsageSearchBoomSkill(BaseSkill[_UsageSearchInput, _UsageSearchOutput]):
    name: ClassVar[str] = SEARCH_TOOL_NAME
    description: ClassVar[str] = "usage エラー記録テスト用フェイク検索。"
    input_schema: ClassVar[type[BaseModel]] = _UsageSearchInput
    output_schema: ClassVar[type[BaseModel]] = _UsageSearchOutput

    def run(self, input: _UsageSearchInput, ctx: SkillContext) -> _UsageSearchOutput:
        raise RuntimeError("usage search kaboom")


_USAGE_SEARCH_BY_NAME = {SEARCH_TOOL_NAME: ToolSpec(SEARCH_TOOL_NAME, "fake", _UsageSearchSkill)}
_USAGE_SEARCH_BOOM_BY_NAME = {
    SEARCH_TOOL_NAME: ToolSpec(SEARCH_TOOL_NAME, "fake", _UsageSearchBoomSkill)
}


class _FakeUsageRecorder:
    def __init__(self, *, raises: bool = False) -> None:
        self.events: list[Any] = []
        self._raises = raises

    async def record(self, event: Any) -> None:
        self.events.append(event)
        if self._raises:
            # UsageRecorder.record 自体が想定外に失敗する本番の failure mode を再現する。
            raise RuntimeError("usage db down")


def _install_usage_recorder(monkeypatch: pytest.MonkeyPatch, recorder: _FakeUsageRecorder) -> None:
    monkeypatch.delenv("USAGE_EVENTS_DISABLE", raising=False)
    monkeypatch.setattr(mcp_server, "_usage_record_tasks", set())
    monkeypatch.setattr(mcp_server, "_usage_recorder", lambda: recorder)


async def _drain_usage_tasks() -> None:
    tasks = tuple(mcp_server._usage_record_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # task の done callback（set からの discard と例外回収）まで進める。
    await asyncio.sleep(0)


async def test_dispatch_records_search_query_and_trusted_slack_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _FakeUsageRecorder()
    _install_usage_recorder(monkeypatch, recorder)

    async def resolver(slack_user_id: str) -> ResolvedIdentity | None:
        assert slack_user_id == TEST_SLACK_USER_ID
        return ResolvedIdentity(
            slack_user_id=slack_user_id,
            email="member@vectorinc.co.jp",
        )

    out = _parse(
        await dispatch_tool(
            _USAGE_SEARCH_BY_NAME,
            SEARCH_TOOL_NAME,
            sign_arguments(
                SEARCH_TOOL_NAME,
                {"query": "競合の最新事例", "mail_body": "保存してはいけない本文"},
            ),
            identity_resolver=resolver,
            caller_claim_verifier=make_verifier(),
        )
    )
    assert out["answer"] == "hits for 競合の最新事例"
    await _drain_usage_tasks()

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.skill == SEARCH_TOOL_NAME
    assert event.user_email == "member@vectorinc.co.jp"
    assert event.user_id == TEST_SLACK_USER_ID  # 署名検証済み claim の ID
    assert event.query_text == "競合の最新事例"
    assert event.query_chars == len("競合の最新事例")
    assert event.cost_usd == 0.125
    assert event.status == "ok"
    assert event.via == "mcp"
    assert not hasattr(event, "mail_body")
    assert not mcp_server._usage_record_tasks


async def test_dispatch_without_query_records_none_and_ignores_legacy_slack_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _FakeUsageRecorder()
    _install_usage_recorder(monkeypatch, recorder)
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {
                "q": "hi",
                USER_CONTEXT_KEY: {
                    "user_email": "a@b.co",
                    "slack_user_id": "U_UNVERIFIED",
                },
            },
        )
    )
    assert out["echo"] == "hi"
    await _drain_usage_tasks()

    event = recorder.events[0]
    assert event.query_text is None
    assert event.query_chars is None
    assert event.user_id is None  # LEGACY の未検証申告値は保存しない


async def test_usage_recorder_exception_does_not_change_dispatch_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _FakeUsageRecorder(raises=True)
    _install_usage_recorder(monkeypatch, recorder)
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
        )
    )
    # record coroutine は await されず、失敗する前に tool 応答がそのまま返る。
    assert out["echo"] == "hi"
    await _drain_usage_tasks()
    assert len(recorder.events) == 1
    assert not mcp_server._usage_record_tasks


async def test_usage_events_disable_skips_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _FakeUsageRecorder()
    _install_usage_recorder(monkeypatch, recorder)
    monkeypatch.setenv("USAGE_EVENTS_DISABLE", "1")
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
        )
    )
    assert out["echo"] == "hi"
    await asyncio.sleep(0)
    assert recorder.events == []
    assert not mcp_server._usage_record_tasks


async def test_dispatch_error_records_error_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _FakeUsageRecorder()
    _install_usage_recorder(monkeypatch, recorder)
    out = _parse(
        await dispatch_tool(
            _USAGE_SEARCH_BOOM_BY_NAME,
            SEARCH_TOOL_NAME,
            {
                "query": "失敗する質問",
                USER_CONTEXT_KEY: {"user_email": "a@b.co"},
            },
        )
    )
    assert "RuntimeError" in out["error"]
    await _drain_usage_tasks()

    event = recorder.events[0]
    assert event.status == "error"
    assert event.error_code == "RuntimeError"
    assert event.query_text == "失敗する質問"
    assert event.query_chars == len("失敗する質問")
    assert isinstance(event.latency_ms, int)
    assert event.latency_ms >= 0


def test_usage_recorder_init_failure_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.pgvector_client import PgVectorClient

    calls = 0

    def fail_from_env(cls: type[PgVectorClient]) -> PgVectorClient:
        nonlocal calls
        calls += 1
        raise RuntimeError("DATABASE_URL missing")

    monkeypatch.setattr(PgVectorClient, "from_env", classmethod(fail_from_env))
    monkeypatch.setattr(
        mcp_server,
        "_usage_recorder_singleton",
        mcp_server._USAGE_RECORDER_UNSET,
    )
    assert mcp_server._usage_recorder() is None
    assert mcp_server._usage_recorder() is None
    assert calls == 1


async def test_search_includes_web_links_when_base_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.co.jp")
    out = _parse(
        await dispatch_tool(
            _SEARCH_BY_NAME,
            SEARCH_TOOL_NAME,
            {"q": "見積もり", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
            require_rls=True,
        )
    )
    assert out["answer"] == "hits for 見積もり"  # 元応答は保持
    assert out["web_url"] == "https://connect.example.co.jp/search"
    assert out["graph_url"] == "https://connect.example.co.jp/search/graph"


async def test_search_web_links_trailing_slash_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 末尾スラッシュ付きの base でも二重スラッシュにならない。
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.co.jp/")
    out = _parse(
        await dispatch_tool(
            _SEARCH_BY_NAME,
            SEARCH_TOOL_NAME,
            {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
            require_rls=True,
        )
    )
    assert out["web_url"] == "https://connect.example.co.jp/search"
    assert out["graph_url"] == "https://connect.example.co.jp/search/graph"


async def test_search_omits_web_links_when_base_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONNECT_BASE_URL", raising=False)
    out = _parse(
        await dispatch_tool(
            _SEARCH_BY_NAME,
            SEARCH_TOOL_NAME,
            {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
            require_rls=True,
        )
    )
    # 壊れた相対リンクを出さない＝キー自体を省く（後方互換）。
    assert "web_url" not in out
    assert "graph_url" not in out
    assert out["answer"] == "hits for x"


async def test_non_search_tool_never_gets_web_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # search 以外の tool には CONNECT_BASE_URL 設定済みでもリンクを足さない。
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.co.jp")
    out = _parse(
        await dispatch_tool(
            _BY_NAME,
            "echo",
            {"q": "hi", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
            require_rls=True,
        )
    )
    assert "web_url" not in out
    assert "graph_url" not in out


# --- L4: _envflag は前後空白を strip してから判定する（末尾改行/スペース付きでも ON） ---


@pytest.mark.parametrize(
    "raw",
    ["1 ", " 1", "true ", " true ", "yes\n", "\tTRUE\t", " True "],
)
def test_envflag_on_with_surrounding_whitespace(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    # task-def の env に紛れた末尾空白/改行付きの "1" 等でも True 判定（取りこぼし防止）。
    monkeypatch.setenv("TA_ENVFLAG_TEST", raw)
    assert _envflag("TA_ENVFLAG_TEST") is True


@pytest.mark.parametrize("raw", ["0 ", " false ", "", "  ", "no\n", "off "])
def test_envflag_off_values_after_strip(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    # 偽値・空白のみは strip 後も False のまま（誤って ON にならない）。
    monkeypatch.setenv("TA_ENVFLAG_TEST", raw)
    assert _envflag("TA_ENVFLAG_TEST") is False


def test_envflag_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TA_ENVFLAG_TEST", raising=False)
    assert _envflag("TA_ENVFLAG_TEST") is False
    assert _envflag("TA_ENVFLAG_TEST", default="1") is True


# --- Aico Vault ディープリンク注入（v0.3 Task6・USE_AILAVAULT_DEEPLINKS 既定OFF） ---------


class _FakeSearchHitsOutput(BaseModel):
    answer: str
    hits: list[dict[str, Any]] = []


class _FakeSearchHitsSkill(BaseSkill[_EchoInput, _FakeSearchHitsOutput]):
    """client_name 付き/無しの hit を返すフェイク search（ディープリンク注入の検証用）。"""

    name: ClassVar[str] = SEARCH_TOOL_NAME
    description: ClassVar[str] = "テスト用フェイク検索（hits あり）。"
    input_schema: ClassVar[type[BaseModel]] = _EchoInput
    output_schema: ClassVar[type[BaseModel]] = _FakeSearchHitsOutput

    def run(self, input: _EchoInput, ctx: SkillContext) -> _FakeSearchHitsOutput:
        return _FakeSearchHitsOutput(
            answer=f"hits for {input.q}",
            hits=[
                {"chunk_id": 1, "client_name": "株式会社ベクトル"},
                {"chunk_id": 2, "client_name": None},
            ],
        )


_SEARCH_HITS_BY_NAME = {SEARCH_TOOL_NAME: ToolSpec(SEARCH_TOOL_NAME, "fake", _FakeSearchHitsSkill)}


async def test_ailavault_links_injected_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.parse import quote

    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.co.jp")
    monkeypatch.setenv("USE_AILAVAULT_DEEPLINKS", "1")
    out = _parse(
        await dispatch_tool(
            _SEARCH_HITS_BY_NAME,
            SEARCH_TOOL_NAME,
            {"q": "ベクトル", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
            require_rls=True,
        )
    )
    assert out["app_url"] == "https://connect.example.co.jp/app"
    expected = "https://connect.example.co.jp/app#client:" + quote("株式会社ベクトル", safe="")
    assert out["hits"][0]["app_client_url"] == expected
    # client_name の無い hit にはキー自体を足さない。
    assert "app_client_url" not in out["hits"][1]
    # 既存キーは従来どおり（後方互換）。
    assert out["web_url"] == "https://connect.example.co.jp/search"


async def test_ailavault_links_absent_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 既定 OFF（§10 E1-2）: CONNECT_BASE_URL があっても app 系キーは一切足さない。
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.co.jp")
    monkeypatch.delenv("USE_AILAVAULT_DEEPLINKS", raising=False)
    out = _parse(
        await dispatch_tool(
            _SEARCH_HITS_BY_NAME,
            SEARCH_TOOL_NAME,
            {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
            require_rls=True,
        )
    )
    assert "app_url" not in out
    assert all("app_client_url" not in h for h in out["hits"])
    assert out["web_url"] == "https://connect.example.co.jp/search"  # 既存注入は不変


async def test_ailavault_links_absent_when_base_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # flag ON でも CONNECT_BASE_URL 未設定なら何も足さない（壊れたリンクを出さない）。
    monkeypatch.delenv("CONNECT_BASE_URL", raising=False)
    monkeypatch.setenv("USE_AILAVAULT_DEEPLINKS", "1")
    out = _parse(
        await dispatch_tool(
            _SEARCH_HITS_BY_NAME,
            SEARCH_TOOL_NAME,
            {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co"}},
            require_rls=True,
        )
    )
    assert "app_url" not in out
    assert all("app_client_url" not in h for h in out["hits"])
    assert "web_url" not in out


# --- 進捗表示（v0.3.1 Task7）が dispatch_tool の返り値を変えないことの統合検証 --------


class _ProgFakeSlack:
    """progress_notify 用の SlackClient ダブル。post/delete を記録。"""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self.deleted: list[str] = []

    async def post_message(
        self, *, channel: str, text: str, request_id: str, thread_ts: str | None = None
    ) -> Any:
        self.posted.append(channel)

        class _R:
            ok = True
            ts = "9.9"

        return _R()

    async def open_dm(self, user_id: str, request_id: str) -> str | None:
        return "D1"

    async def delete_message(self, channel: str, ts: str, request_id: str) -> bool:
        self.deleted.append(ts)
        return True


class _SearchBoomSkill(BaseSkill[_EchoInput, _FakeSearchOutput]):
    name: ClassVar[str] = SEARCH_TOOL_NAME
    description: ClassVar[str] = "必ず例外を投げる検索（進捗の finally 削除の検証用）。"
    input_schema: ClassVar[type[BaseModel]] = _EchoInput
    output_schema: ClassVar[type[BaseModel]] = _FakeSearchOutput

    def run(self, input: _EchoInput, ctx: SkillContext) -> _FakeSearchOutput:
        raise RuntimeError("search kaboom")


def _install_prog(monkeypatch: pytest.MonkeyPatch, fake: _ProgFakeSlack) -> None:
    from teamagent.mcp_gateway import progress_notify as pn

    monkeypatch.setattr(pn.SlackClient, "from_env", classmethod(lambda c: fake))


async def test_dispatch_progress_on_success_unchanged_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """進捗 ON でも search 応答は不変・進捗は post→delete される。"""
    monkeypatch.setenv("ENABLE_PROGRESS_NOTIFY", "true")
    fake = _ProgFakeSlack()
    _install_prog(monkeypatch, fake)
    out = _parse(
        await dispatch_tool(
            _SEARCH_BY_NAME,
            SEARCH_TOOL_NAME,
            {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co", "channel_id": "C1"}},
            require_rls=True,
        )
    )
    assert out["answer"] == "hits for x"  # 返り値は進捗 ON でも不変
    assert fake.posted == ["C1"] and fake.deleted == ["9.9"]  # post → delete


async def test_dispatch_progress_on_error_still_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ツールが例外でも finally で進捗が削除され、構造化エラーが返る。"""
    monkeypatch.setenv("ENABLE_PROGRESS_NOTIFY", "true")
    fake = _ProgFakeSlack()
    _install_prog(monkeypatch, fake)
    boom_by_name = {SEARCH_TOOL_NAME: ToolSpec(SEARCH_TOOL_NAME, "fake", _SearchBoomSkill)}
    out = _parse(
        await dispatch_tool(
            boom_by_name,
            SEARCH_TOOL_NAME,
            {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co", "channel_id": "C1"}},
            require_rls=True,
        )
    )
    assert "error" in out  # 構造化エラーで返る（従来どおり）
    assert fake.deleted == ["9.9"]  # 例外時も進捗は削除される


async def test_dispatch_progress_off_no_slack_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定 OFF では Slack を一切叩かない（regression ゼロ）。"""
    monkeypatch.delenv("ENABLE_PROGRESS_NOTIFY", raising=False)
    fake = _ProgFakeSlack()
    _install_prog(monkeypatch, fake)
    out = _parse(
        await dispatch_tool(
            _SEARCH_BY_NAME,
            SEARCH_TOOL_NAME,
            {"q": "x", USER_CONTEXT_KEY: {"user_email": "a@b.co", "channel_id": "C1"}},
            require_rls=True,
        )
    )
    assert out["answer"] == "hits for x"
    assert fake.posted == [] and fake.deleted == []  # OFF なので何も送らない
