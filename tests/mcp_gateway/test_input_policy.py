"""MCP境界は実際のPydantic失敗を使い、条件外はバイト一致で固定する。"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.mcp_gateway import server, surface_video_followup
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills._shared import input_policy as policy
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search_surface_check.confirm import confirm_required
from teamagent.skills.search_surface_check.schema import SearchSurfaceCheckInput
from teamagent.skills.x_research.schema import XVoiceSearchInput


class _Output(BaseModel):
    slack_summary: str
    status: str = "done"


class _Voice(BaseSkill[XVoiceSearchInput, _Output]):
    name: ClassVar[str] = "x_voice_search"
    description: ClassVar[str] = "検証用"
    input_schema: ClassVar[type[BaseModel]] = XVoiceSearchInput
    output_schema: ClassVar[type[BaseModel]] = _Output

    def run(self, input: XVoiceSearchInput, ctx: SkillContext) -> _Output:
        if input.product_name == "費用上限":
            raise RuntimeError(
                "VIDEO_QUOTA_PARTIAL_AVAILABLE: 今月の残りは2本です。2本で進めますか？"
            )
        if input.product_name == "認証切れ":
            raise PermissionError("再連携が必要です")
        return _Output(slack_summary="、".join(input.queries))


class _Surface(BaseSkill[SearchSurfaceCheckInput, _Output]):
    name: ClassVar[str] = "search_surface_check"
    description: ClassVar[str] = "検証用"
    input_schema: ClassVar[type[BaseModel]] = SearchSurfaceCheckInput
    output_schema: ClassVar[type[BaseModel]] = _Output

    def run(self, input: SearchSurfaceCheckInput, ctx: SkillContext) -> _Output:
        return _Output(
            slack_summary=f"分析{input.max_videos}本",
            status=("needs_input" if confirm_required(input, ctx.metadata) else "done"),
        )


SPECS = {cls.name: ToolSpec(cls.name, cls.description, cls) for cls in (_Voice, _Surface)}


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    metadata = {
        "user_email": "a@example.com",
        "identity_verified": True,
        "channel_id": "D123",
        "thread_ts": "1",
    }
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "1")
    monkeypatch.setenv("INPUT_POLICY_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("USAGE_EVENTS_DISABLE", "1")
    monkeypatch.setenv("USE_SURFACE_VIDEO_FOLLOWUP", "1")
    monkeypatch.setenv("SURFACE_VIDEO_ONE_SHOT", "1")
    monkeypatch.setenv("SURFACE_VIDEO_FOLLOWUP_ALLOWED_EMAILS", "*")
    monkeypatch.setattr(policy, "CACHE", policy.PolicyCache())

    async def verify(*args: Any, **kwargs: Any) -> tuple[None, None]:
        return None, None

    async def resolve(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], None]:
        return dict(metadata), None

    monkeypatch.setattr(server, "_verify_caller", verify)
    monkeypatch.setattr(server, "_resolve_metadata", resolve)
    monkeypatch.setattr(surface_video_followup, "maybe_schedule", lambda **kwargs: None)
    return metadata


async def call(name: str, args: dict[str, Any]) -> str:
    return (await server.dispatch_tool(SPECS, name, args))[0].text


@pytest.mark.parametrize("condition", ["off", "outside", "unknown", "empty"])
async def test_condition_outside_validation_bytes(
    monkeypatch: pytest.MonkeyPatch, setup: dict[str, Any], condition: str
) -> None:
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "0")
    expected = await call("x_voice_search", {})
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "1")
    if condition == "off":
        monkeypatch.setenv("USE_INPUT_POLICY_V1", "0")
    elif condition == "outside":
        monkeypatch.setenv("INPUT_POLICY_ALLOWED_EMAILS", "b@example.com")
    elif condition == "unknown":
        setup["identity_verified"] = False
    else:
        monkeypatch.setenv("INPUT_POLICY_ALLOWED_EMAILS", "")
    assert await call("x_voice_search", {}) == expected
    assert "ValidationError" in expected


async def test_real_validation_missing_once_then_partial(setup: dict[str, Any]) -> None:
    first = json.loads(await call("x_voice_search", {}))["error"]
    second = json.loads(await call("x_voice_search", {}))["error"]
    assert "1. 対象の商材" in first
    assert "2." in first
    assert "ValidationError" not in first
    assert "骨子：" in second
    setup["thread_ts"] = "2"
    assert json.loads(await call("x_voice_search", {}))["error"] == first


async def test_voice_default_and_decline() -> None:
    out = json.loads(await call("x_voice_search", {"product_name": "白湯"}))
    assert out["slack_summary"].startswith("白湯\n今回は検索語は")
    out = json.loads(await call("x_voice_search", {"product_name": "白湯", "queries": ["不要"]}))
    assert "検索は行わず" in out["error"]
    again = json.loads(await call("x_voice_search", {"product_name": "白湯"}))
    assert again == out


async def test_surface_optional_confirmation_skipped_and_default_is_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = json.loads(await call("search_surface_check", {"keywords": ["美容"]}))
    assert out["status"] == "done"
    assert out["slack_summary"] == "分析5本\n今回は分析本数は5にしました、競合比較を省きました。"
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "0")
    old = json.loads(await call("search_surface_check", {"keywords": ["美容"]}))
    assert old["status"] == "needs_input"


async def test_bad_values_keep_original_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    args = {"product_name": "白湯", "queries": ["白湯"], "results_per_query": 100}
    enabled = await call("x_voice_search", args)
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "0")
    assert await call("x_voice_search", args) == enabled


def test_tool_defs_unchanged_even_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    before = [t.model_dump_json() for t in server.list_tool_defs(list(SPECS.values()))]
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "0")
    assert [t.model_dump_json() for t in server.list_tool_defs(list(SPECS.values()))] == before


@pytest.mark.parametrize("product", ["費用上限", "認証切れ"])
async def test_auth_and_expensive_confirmation_keep_original_error(
    monkeypatch: pytest.MonkeyPatch, product: str
) -> None:
    args = {"product_name": product, "queries": [product]}
    enabled = json.loads(await call("x_voice_search", args))["error"]
    repeated = json.loads(await call("x_voice_search", args))["error"]
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "0")
    assert json.loads(await call("x_voice_search", args))["error"] == enabled == repeated


@pytest.mark.parametrize("direct_status", ["posted", "failed"])
async def test_surface_acquire_instructions_reach_model_without_posting_or_policy_note(
    monkeypatch: pytest.MonkeyPatch,
    direct_status: str,
) -> None:
    from teamagent.mcp_gateway import direct_summary
    from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill

    monkeypatch.setenv("SEARCH_SURFACE_ALLOWED_EMAILS", "a@example.com")
    posted: list[dict[str, Any]] = []
    scheduled: list[Any] = []
    monkeypatch.setattr(direct_summary, "decide", lambda *a, **kw: (object(), "ok"))

    def deliver(data: dict[str, Any], *args: Any, **kwargs: Any) -> str:
        posted.append(data)
        return direct_status

    monkeypatch.setattr(direct_summary, "deliver", deliver)
    monkeypatch.setattr(surface_video_followup, "maybe_schedule", lambda **kw: scheduled.append(kw))
    cls = SearchSurfaceCheckSkill
    specs = {cls.name: ToolSpec(cls.name, cls.description, cls)}
    result = await server.dispatch_tool(
        specs,
        cls.name,
        {"keywords": ["ハンバーグ", "冷凍ハンバーグ", "夜ご飯"]},
    )
    out = json.loads(result[0].text)
    assert posted == []
    assert scheduled == []
    assert out["slack_summary"] == "検索上位チェックには、先に検索結果の取得が必要です。"
    assert (
        "tiktok_acquire(keywords=['ハンバーグ', '冷凍ハンバーグ', '夜ご飯'], videos_per_kw=0)"
        in out["model_instructions"]
    )
    assert "acquire_job_id" in out["model_instructions"]
