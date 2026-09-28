"""検索上位チェックの 2 段目と、MCP 返却の絞り込みのテスト（server.dispatch_tool 経由）。

本物の SearchSurfaceCheckSkill と本物の VideoAlgorithmSkill（Gemini・取得だけ偽物）を、本物の
dispatch_tool・登録簿・post_to_origin に通す。Slack は偽物。確かめること:
- フラグ OFF / allowlist 外 / DM 以外 / LEGACY は 1 段目が今と同じで、2 段目を登録しない
- 対象なら 1 段目の文面（レポート行の前）に予告が付き、2 段目が検証済みの DM へ追記される
- 再デプロイで中断通知（1 通だけ）・二重依頼・混雑（動画分析の切り離しと枠を共有）
- MCP 返却は slack_summary・report_url・warnings・total_cost_usd だけ。usage は費用を正しく読む
- 他のツールの返却は変わらない
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.adapters.slack_client import SlackPostResult
from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway import detached_jobs, server, surface_video_followup
from teamagent.mcp_gateway.caller_claim import VerifiedCallerClaim
from teamagent.mcp_gateway.server import USER_CONTEXT_KEY, dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search_surface_check.schema import SearchSurfaceCheckInput
from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
from teamagent.skills.search_surface_check.summary import followup_notice_line
from teamagent.skills.video_algorithm import thumbnails
from tests.skills.search_surface_check.fixtures import KEYWORD, NOW, s3_rows
from tests.skills.search_surface_check.video_fakes import (
    FakeDownloader,
    FakeGemini,
    VideoBedrock,
    make_engine,
)

ME = "s-komata@vectorinc.co.jp"
OTHER = "someone@vectorinc.co.jp"
USER_ID = "U0123456789"
TEAM_ID = "T0123456789"
DM = "D0123456789"
CHANNEL = "C0123456789"
TOOL = "search_surface_check"
RELAY_KEYS = {"slack_summary", "report_url", "warnings", "total_cost_usd"}
ARGS = {"keywords": [KEYWORD], "platforms": ["tiktok"], "acquire_job_id": "tk_0123456789ab"}


def _claim(
    *, channel: str = DM, thread_ts: str | None = None, message_id: str = "1784424000.000001"
) -> VerifiedCallerClaim:
    return VerifiedCallerClaim(
        slack_user_id=USER_ID,
        slack_team_id=TEAM_ID,
        channel_id=channel,
        thread_ts=thread_ts,
        message_id=message_id,
        session_sha256="0" * 64,
        run_id="11111111-1111-4111-8111-111111111111",
        tool_call_id="toolu_0123456789abcdef",
        nonce="test-nonce",
        issued_at=1,
        expires_at=2,
    )


class _Verifier:
    def __init__(self, claim: VerifiedCallerClaim) -> None:
        self.claim = claim

    async def verify(self, *, tool: str, arguments: dict[str, Any]) -> VerifiedCallerClaim:
        return self.claim


def _resolver_for(email: str) -> Callable[[str], Any]:
    async def _resolve(slack_user_id: str) -> ResolvedIdentity:
        return ResolvedIdentity(slack_user_id=slack_user_id, email=email)

    return _resolve


class _FakeSlack:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    async def open_dm(self, user_id: str, request_id: str) -> str | None:
        return "D0FALLBACK01"

    async def post_message(
        self,
        channel: str,
        text: str,
        request_id: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackPostResult:
        with self.lock:
            self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return SlackPostResult(channel=channel, ts="1784424999.000100", ok=True)


class _Source:
    def posts(self, n: int | None = None) -> list[dict[str, Any]]:
        rows = s3_rows()
        return rows[:n] if n else rows


class _Publisher:
    def __init__(self) -> None:
        self.count = 0
        self.lock = threading.Lock()

    def __call__(self, path: str, *, request_id: str, query: str) -> str:
        with self.lock:
            self.count += 1
            return f"https://s3.example/surface-{self.count}"


def _skill(
    *, downloader: FakeDownloader | None = None, engine: Any | None = None
) -> tuple[SearchSurfaceCheckSkill, FakeGemini, FakeDownloader]:
    gem = FakeGemini()
    dl = downloader or FakeDownloader()
    skill = SearchSurfaceCheckSkill(
        bedrock=VideoBedrock(),
        publisher=_Publisher(),
        tiktok_source_factory=lambda job_id, audit_hash: _Source(),
        clock=lambda: NOW,
        video_engine=engine or make_engine(gem, dl),
    )
    return skill, gem, dl


def _baseline_summary() -> str:
    """同じ偽物で 1 段目だけを直接回した文面（今の挙動）。"""
    skill, _g, _d = _skill()
    ctx = SkillContext(request_id="r", metadata={"user_email": ME})
    return skill.run(SearchSurfaceCheckInput(**ARGS), ctx).slack_summary


def _spec(skill: BaseSkill[Any, Any], name: str = TOOL) -> dict[str, ToolSpec]:
    return {name: ToolSpec(name, "x", type(skill), factory=lambda: skill)}


async def _call(
    by_name: dict[str, ToolSpec],
    *,
    claim: VerifiedCallerClaim | None = None,
    email: str = ME,
    tool: str = TOOL,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contents = await dispatch_tool(
        by_name,
        tool,
        {**(args or ARGS), USER_CONTEXT_KEY: {}},
        identity_resolver=_resolver_for(email),
        caller_claim_verifier=_Verifier(claim or _claim()),  # type: ignore[arg-type]
    )
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


async def _eventually(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met in time")


def _enable(monkeypatch: pytest.MonkeyPatch, *, emails: frozenset[str] = frozenset({ME})) -> None:
    monkeypatch.setattr(
        surface_video_followup,
        "load_policy",
        lambda: surface_video_followup.FollowupPolicy(enabled=True, allowed_emails=emails),
    )


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> _FakeSlack:
    fake = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    return fake


@pytest.fixture(autouse=True)
def usage(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    for name in (
        surface_video_followup.ENABLED_ENV,
        surface_video_followup.ALLOWED_EMAILS_ENV,
        surface_video_followup.MAX_VIDEOS_ENV,
        detached_jobs.MAX_BACKGROUND_ENV,
        "ENABLE_PROGRESS_NOTIFY",
        "USE_PAYLOAD_OFFLOAD",
        "VIDEO_QUOTA_ENABLED",
        "SEARCH_SURFACE_ALLOWED_EMAILS",
        "USE_TIKTOK_APIFY_FALLBACK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")
    monkeypatch.setattr(thumbnails, "fetch_cover", lambda *a, **k: None)
    monkeypatch.setattr(detached_jobs, "REGISTRY", detached_jobs.DetachedJobRegistry())
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "_record_usage", lambda **kw: records.append(kw))
    return records


# ── 対象外は 1 段目が今と同じ ──────────────────────────────────────────


async def test_flag_off_keeps_first_stage_and_registers_nothing(slack: _FakeSlack) -> None:
    skill, gem, dl = _skill()
    out = await _call(_spec(skill))
    assert out["slack_summary"] == _baseline_summary()
    assert "動画の中身" not in out["slack_summary"]
    assert detached_jobs.REGISTRY.active_count() == 0
    await asyncio.sleep(0.05)
    assert gem.calls == [] and dl.urls == [] and slack.posts == []


@pytest.mark.parametrize(
    ("email", "claim"),
    [
        (OTHER, _claim()),  # allowlist 外
        (ME, _claim(channel=CHANNEL, thread_ts="1784424000.000009")),  # チャンネルのスレッド
    ],
)
async def test_not_eligible_keeps_first_stage(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, email: str, claim: VerifiedCallerClaim
) -> None:
    _enable(monkeypatch)
    skill, gem, _d = _skill()
    out = await _call(_spec(skill), claim=claim, email=email)
    assert out["slack_summary"] == _baseline_summary()
    assert detached_jobs.REGISTRY.active_count() == 0
    assert gem.calls == []


async def test_empty_allowlist_applies_to_nobody(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    _enable(monkeypatch, emails=frozenset())
    skill, _g, _d = _skill()
    out = await _call(_spec(skill))
    assert out["slack_summary"] == _baseline_summary()
    assert detached_jobs.REGISTRY.active_count() == 0


async def test_legacy_caller_keeps_first_stage(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """resolver 無し（LEGACY）＝宛先を信用できないので 2 段目はしない。"""
    _enable(monkeypatch)
    skill, gem, _d = _skill()
    contents = await dispatch_tool(
        _spec(skill), TOOL, {**ARGS, USER_CONTEXT_KEY: {"user_email": ME, "channel_id": DM}}
    )
    out = json.loads(contents[0].text)
    assert out["slack_summary"] == _baseline_summary()
    assert detached_jobs.REGISTRY.active_count() == 0
    assert gem.calls == []


async def test_disabled_flag_wins_over_allowlist(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    monkeypatch.setattr(
        surface_video_followup,
        "load_policy",
        lambda: surface_video_followup.FollowupPolicy(
            enabled=False, allowed_emails=frozenset({ME})
        ),
    )
    skill, gem, _d = _skill()
    out = await _call(_spec(skill))
    assert out["slack_summary"] == _baseline_summary()
    assert detached_jobs.REGISTRY.active_count() == 0 and gem.calls == []


@pytest.mark.parametrize(
    ("caller", "metadata", "reason"),
    [
        (None, {"identity_verified": True, "user_email": ME}, "legacy"),
        (_claim(), {"identity_verified": False, "user_email": ME}, "unverified"),
        (_claim(), {"identity_verified": True, "user_email": OTHER}, "not_allowed"),
        (
            _claim(channel=CHANNEL, message_id="m-1"),
            {"identity_verified": True, "user_email": ME},
            "no_destination",
        ),
        (
            _claim(channel=CHANNEL, thread_ts="1784424000.000009"),
            {"identity_verified": True, "user_email": ME},
            "not_dm",
        ),
        (_claim(), {"identity_verified": True, "user_email": ME.upper()}, "ok"),
    ],
)
def test_decide_reasons(caller: Any, metadata: dict[str, Any], reason: str) -> None:
    policy = surface_video_followup.FollowupPolicy(enabled=True, allowed_emails=frozenset({ME}))
    destination, got = surface_video_followup.decide(
        policy, verified_caller=caller, metadata=metadata
    )
    assert got == reason
    assert (destination is not None) is (reason == "ok")


def test_policy_from_env_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = surface_video_followup.FollowupPolicy.from_env()
    assert (policy.enabled, policy.allowed_emails, policy.max_videos) == (False, frozenset(), 5)
    monkeypatch.setenv(surface_video_followup.ENABLED_ENV, "1")
    monkeypatch.setenv(surface_video_followup.ALLOWED_EMAILS_ENV, " S-Komata@vectorinc.co.jp ,")
    monkeypatch.setenv(surface_video_followup.MAX_VIDEOS_ENV, "99")
    policy = surface_video_followup.FollowupPolicy.from_env()
    assert policy.enabled and policy.allowed_emails == frozenset({ME})
    assert policy.max_videos == 10


# ── 対象: 予告 → 裏で分析 → 追記 ────────────────────────────────────────


async def test_eligible_dm_gets_notice_then_followup_post(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    _enable(monkeypatch)
    gate = threading.Event()
    skill, gem, dl = _skill(downloader=FakeDownloader(gate=gate))
    out = await _call(_spec(skill), claim=_claim(channel=DM, thread_ts="1784424000.000009"))

    # 1 段目: 予告はレポート行の直前。それ以外は今と同じ。
    notice = followup_notice_line(5)
    lines = out["slack_summary"].splitlines()
    assert lines[lines.index(notice) + 1].startswith("レポート（全")
    assert out["slack_summary"].replace(notice + "\n", "") == _baseline_summary()
    assert detached_jobs.REGISTRY.active_count() == 1
    assert slack.posts == []

    gate.set()
    await _eventually(lambda: len(slack.posts) == 1 and len(usage) == 2)
    post = slack.posts[0]
    assert (post["channel"], post["thread_ts"]) == (DM, "1784424000.000009")
    assert post["text"].startswith(f"*上位5本の動画の中身*「{KEYWORD}」TikTok")
    assert "**" not in post["text"]
    assert "https://s3.example/surface-2" in post["text"]
    top5 = [r["url"] for r in s3_rows()[:5]]
    assert sorted(dl.urls) == sorted(top5)
    assert gem.ranks == [1, 2, 3, 4, 5]
    followup_usage = next(u for u in usage if u["skill"] == surface_video_followup.USAGE_SKILL)
    assert followup_usage["status"] == "ok" and followup_usage["cost_usd"] > 0.05
    assert followup_usage["user_id"] == USER_ID and followup_usage["user_email"] == ME
    assert followup_usage["request_id"].endswith("-video")
    await _eventually(lambda: detached_jobs.REGISTRY.active_count() == 0)


async def test_followup_failure_posts_a_plain_message(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    _enable(monkeypatch)

    class _BrokenEngine:
        @staticmethod
        def reserve_video_quota(ctx: SkillContext, count: int) -> int:
            return count

        def analyze_videos(self, metas: list[Any], **kw: Any) -> list[Any]:
            raise RuntimeError("MEDIA_ACQUIRE_JOB_FAILED: boom")

    skill, _g, _d = _skill(engine=_BrokenEngine())
    await _call(_spec(skill))
    await _eventually(lambda: len(slack.posts) == 1)
    text = slack.posts[0]["text"]
    assert text.startswith(f"「{KEYWORD}」上位の動画の中身: 動画の取得・変換で一時的な不具合")
    assert "MEDIA_" not in text
    await _eventually(lambda: any(u.get("status") == "error" for u in usage))


async def test_redeploy_sends_one_interrupt_notice(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    _enable(monkeypatch)
    gate = threading.Event()
    dl = FakeDownloader(gate=gate)
    skill, _g, _d = _skill(downloader=dl)
    await _call(_spec(skill))
    await asyncio.to_thread(dl.started.wait, 5)

    assert await detached_jobs.notify_interrupted() == 1
    assert len(slack.posts) == 1
    assert slack.posts[0]["text"] == surface_video_followup.interrupted_text(KEYWORD)
    assert "システム更新で中断されました" in slack.posts[0]["text"]

    gate.set()  # 中断を知らせた後に完了しても 2 通目は出さない
    await _eventually(lambda: any(u["skill"] == surface_video_followup.USAGE_SKILL for u in usage))
    await asyncio.sleep(0.05)
    assert len(slack.posts) == 1


async def test_duplicate_request_does_not_start_a_second_job(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    _enable(monkeypatch)
    gate = threading.Event()
    dl = FakeDownloader(gate=gate)
    skill, gem, _d = _skill(downloader=dl)
    await _call(_spec(skill))
    second = await _call(_spec(skill))
    assert "前のご依頼の分を分析中です。終わったらこの会話に追記します" in second["slack_summary"]
    assert followup_notice_line(5) not in second["slack_summary"]
    assert detached_jobs.REGISTRY.active_count() == 1
    gate.set()
    await _eventually(lambda: len(slack.posts) == 1)
    await asyncio.sleep(0.05)
    assert len(slack.posts) == 1 and len(gem.calls) == 5


async def test_capacity_is_shared_with_video_algorithm_detach(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """動画分析の切り離しと同じ枠（上限＋待ち行列）。満杯なら 2 段目は始めず、そう書く。"""
    _enable(monkeypatch)
    monkeypatch.setattr(
        detached_jobs,
        "load_policy",
        lambda: detached_jobs.DetachPolicy(max_background=1, max_queued=0),
    )
    # 動画分析の切り離しジョブが 1 本走っている状態を作る。
    hold = threading.Event()
    job, state = detached_jobs.REGISTRY.start(
        key="other",
        max_background=1,
        max_queued=0,
        tool="video_algorithm",
        query="別の依頼",
        request_id="r-other",
        destination=detached_jobs.Destination(channel_id=DM, thread_ts=None),
        target=lambda: hold.wait(5),
        on_detached_done=lambda *a: None,
    )
    assert state == "started" and job is not None
    skill, gem, _d = _skill()
    out = await _call(_spec(skill))
    assert surface_video_followup.busy_line() in out["slack_summary"]
    assert gem.calls == []
    hold.set()


async def test_queued_notice_when_all_slots_are_taken(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        detached_jobs, "load_policy", lambda: detached_jobs.DetachPolicy(max_background=1)
    )
    hold = threading.Event()
    detached_jobs.REGISTRY.start(
        key="other",
        max_background=1,
        tool="video_algorithm",
        query="別の依頼",
        request_id="r-other",
        destination=detached_jobs.Destination(channel_id=DM, thread_ts=None),
        target=lambda: hold.wait(5),
        on_detached_done=lambda *a: None,
    )
    skill, _g, _d = _skill()
    out = await _call(_spec(skill))
    assert "順番待ちのあと始めます" in out["slack_summary"]
    hold.set()
    await _eventually(lambda: len(slack.posts) == 1)


# ── MCP の返却 ───────────────────────────────────────────────────────


async def test_mcp_returns_only_the_slack_text_fields_and_usage_reads_cost(
    slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    skill, _g, _d = _skill()
    out = await _call(_spec(skill))
    assert set(out) == RELAY_KEYS
    assert "surfaces" not in out and "keywords" not in out
    assert out["report_url"] == "https://s3.example/surface-1"
    assert out["total_cost_usd"] > 0
    assert usage[0]["skill"] == TOOL
    assert usage[0]["cost_usd"] == out["total_cost_usd"]


class _PlainOut(BaseModel):
    slack_summary: str = "ok"
    total_cost_usd: float = 0.5
    rows: list[int] = [1, 2, 3]


class _PlainSkill(BaseSkill[SearchSurfaceCheckInput, _PlainOut]):
    name: ClassVar[str] = "plain_tool"
    description: ClassVar[str] = "x"
    input_schema: ClassVar[type[BaseModel]] = SearchSurfaceCheckInput
    output_schema: ClassVar[type[BaseModel]] = _PlainOut

    def run(self, input: SearchSurfaceCheckInput, ctx: SkillContext) -> _PlainOut:
        return _PlainOut()


async def test_other_tools_return_everything(
    slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    out = await _call(_spec(_PlainSkill(), "plain_tool"), tool="plain_tool")
    assert out == {"slack_summary": "ok", "total_cost_usd": 0.5, "rows": [1, 2, 3]}
    assert usage[0]["cost_usd"] == 0.5
