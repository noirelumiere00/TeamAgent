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
from teamagent.skills.search_surface_check.schema import SearchSurfaceCheckInput, VideoDigest
from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
from teamagent.skills.search_surface_check.slack_render import REUSED_NOTE
from teamagent.skills.search_surface_check.summary import (
    FOLLOWUP_QUOTA_EXHAUSTED_LINE,
    followup_notice_line,
    followup_reused_line,
)
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
RELAY_KEYS = {
    "slack_summary",
    "report_url",
    "warnings",
    "total_cost_usd",
    "keywords",
    "measured_epoch",
}
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
            self.posts.append(
                {"channel": channel, "text": text, "thread_ts": thread_ts, "blocks": blocks}
            )
        return SlackPostResult(channel=channel, ts="1784424999.000100", ok=True)


def _blocks_text(blocks: list[dict[str, Any]] | None) -> str:
    """Block Kit の中の文字（見出し・本文・欄・注記）を 1 つの文字列にする（照合用）。"""
    parts: list[str] = []
    for block in blocks or []:
        if "text" in block:
            parts.append(block["text"]["text"])
        parts += [f["text"] for f in block.get("fields", [])]
        parts += [e["text"] for e in block.get("elements", [])]
    return "\n".join(parts)


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
    monkeypatch.setattr(surface_video_followup, "CACHE", surface_video_followup.FollowupCache())
    monkeypatch.setattr(surface_video_followup, "REUSE_POST_DELAY_S", 0.0)
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
    assert "（動画分析の回数を最大 5 本使います）" in notice
    lines = out["slack_summary"].splitlines()
    assert lines[lines.index(notice) + 1].startswith("レポート（全")
    assert out["slack_summary"].replace(notice + "\n", "") == _baseline_summary()
    assert detached_jobs.REGISTRY.active_count() == 1
    assert slack.posts == []

    gate.set()
    await _eventually(lambda: len(slack.posts) == 1 and len(usage) == 2)
    post = slack.posts[0]
    assert (post["channel"], post["thread_ts"]) == (DM, "1784424000.000009")
    # 追記は Block Kit（見出し・集計の欄・1 本 1 行・文字リンク）。通知文に結論とレポートの URL。
    assert post["blocks"][0]["text"]["text"] == f"上位5本の動画の中身「{KEYWORD}」"
    assert post["text"].startswith(f"上位5本の動画の中身「{KEYWORD}」TikTok")
    assert "**" not in post["text"] and "**" not in _blocks_text(post["blocks"])
    assert "<https://s3.example/surface-2>" in post["text"]
    assert "<https://s3.example/surface-2|レポートを開く>" in _blocks_text(post["blocks"])
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
    assert "もう一度依頼すると、動画分析の回数を使い直します" in slack.posts[0]["text"]

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
    assert "surfaces" not in out
    assert out["keywords"] == [KEYWORD] and out["measured_epoch"] == NOW
    assert out["report_url"] == "https://s3.example/surface-1"
    # 投稿の URL は文面の上位の行に Markdown リンクで入っている（フラグに関係なく全員）
    for row in s3_rows()[:10]:
        assert f"[@{row['account_id']}]({row['url']})" in out["slack_summary"]
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


# ── 月間上限（1 段目の時点で残りを読む）─────────────────────────────────


def _peek(monkeypatch: pytest.MonkeyPatch, remaining: int | None) -> list[str]:
    from teamagent.adapters import quota_store

    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    asked: list[str] = []

    def _fake(self: Any, user_email: str, *, request_id: str) -> int | None:
        # DB を読むので event loop（main thread）の外で呼ばれていること
        assert threading.current_thread() is not threading.main_thread()
        asked.append(user_email)
        return remaining

    def _no_consume(self: Any, *a: Any, **k: Any) -> Any:
        raise AssertionError("1 段目の時点では消費しない")

    monkeypatch.setattr(quota_store.VideoQuotaStore, "peek_remaining", _fake)
    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _no_consume)
    return asked


async def test_zero_quota_says_so_instead_of_the_notice_and_registers_nothing(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    _enable(monkeypatch)
    asked = _peek(monkeypatch, 0)
    skill, gem, _d = _skill()
    out = await _call(_spec(skill))
    summary = out["slack_summary"]
    assert asked == [ME]
    assert FOLLOWUP_QUOTA_EXHAUSTED_LINE == (
        "今月の動画分析の上限に達しているため、動画の中身は分析しません（残り 0 本）"
    )
    lines = summary.splitlines()
    assert lines[lines.index(FOLLOWUP_QUOTA_EXHAUSTED_LINE) + 1].startswith("レポート（全")
    assert followup_notice_line(5) not in summary
    assert detached_jobs.REGISTRY.active_count() == 0
    await asyncio.sleep(0.05)
    assert gem.calls == [] and slack.posts == []  # 予告と食い違う「上限です」を後から送らない


async def test_quota_left_keeps_the_notice(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    from teamagent.adapters import quota_store
    from teamagent.adapters.quota_store import QuotaResult

    _enable(monkeypatch)
    asked = _peek(monkeypatch, None)  # 台帳を読めない＝止めない側に倒す（予約は 2 段目で）
    monkeypatch.setattr(
        quota_store.VideoQuotaStore,
        "try_consume",
        lambda self, email, count, *, request_id: QuotaResult(allowed=True, used=count, limit=50),
    )
    skill, gem, _d = _skill()
    out = await _call(_spec(skill))
    assert asked == [ME]
    assert followup_notice_line(5) in out["slack_summary"]
    await _eventually(lambda: len(slack.posts) == 1)
    assert slack.posts[0]["blocks"][0]["text"]["text"].startswith("上位5本の動画の中身")
    assert gem.ranks == [1, 2, 3, 4, 5]


def test_quota_on_without_email_registers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    skill, gem, _d = _skill()
    ctx = SkillContext(request_id="r", metadata={})
    output = skill.run(SearchSurfaceCheckInput(**ARGS), ctx)
    before = output.slack_summary
    reason = surface_video_followup.maybe_schedule(
        skill=skill,
        output=output,
        skill_input=SearchSurfaceCheckInput(**ARGS),
        ctx=ctx,
        verified_caller=_claim(),
        metadata={"identity_verified": True, "user_email": ME},
        usage_user_id=USER_ID,
        record_usage=lambda **kw: None,
        loop=asyncio.new_event_loop(),
    )
    assert reason == "no_identity"
    assert output.slack_summary == before
    assert detached_jobs.REGISTRY.active_count() == 0 and gem.calls == []


def test_quota_gate_states(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters import quota_store

    ctx = SkillContext(request_id="r", metadata={"user_email": ME})
    assert surface_video_followup.quota_gate(ctx) == ("off", None)
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    assert surface_video_followup.quota_gate(SkillContext(request_id="r", metadata={})) == (
        "no_identity",
        None,
    )
    for remaining, state in ((0, "exhausted"), (7, "available"), (None, "unknown")):
        monkeypatch.setattr(
            quota_store.VideoQuotaStore,
            "peek_remaining",
            lambda self, email, *, request_id, _r=remaining: _r,
        )
        assert surface_video_followup.quota_gate(ctx) == (state, remaining)


# ── 24 時間の使い回し ────────────────────────────────────────────────


async def _first_full_run(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> tuple[SearchSurfaceCheckSkill, FakeGemini]:
    _enable(monkeypatch)
    skill, gem, _d = _skill()
    await _call(_spec(skill))
    await _eventually(lambda: len(slack.posts) == 1)
    await _eventually(lambda: len(surface_video_followup.CACHE) == 1)
    return skill, gem


async def test_same_request_within_24h_reuses_the_result_without_quota_or_gemini(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    skill, gem = await _first_full_run(monkeypatch, slack)
    first_blocks = slack.posts[0]["blocks"]
    calls_before, usage_before = len(gem.calls), len(usage)

    from teamagent.adapters import quota_store

    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")

    def _never(*a: Any, **k: Any) -> Any:
        raise AssertionError("使い回しでは月間上限を読まない・使わない")

    monkeypatch.setattr(quota_store.VideoQuotaStore, "peek_remaining", _never)
    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _never)

    again = await _call(_spec(skill))
    assert followup_reused_line(5) in again["slack_summary"]
    assert followup_notice_line(5) not in again["slack_summary"]
    await _eventually(lambda: len(slack.posts) == 2)
    reused = slack.posts[1]["text"]
    assert reused.startswith("（24 時間以内の同じ分析の結果です）\n上位5本の動画の中身")
    assert "<https://s3.example/surface-2>" in reused
    blocks = slack.posts[1]["blocks"]
    # 使い回しの断り書き（先頭の注記）と概算（末尾の注記）以外は、前回の追記と同じ Block Kit
    # （章つきレポートの URL を含む）。
    assert blocks[1]["elements"][0]["text"] == REUSED_NOTE
    assert blocks[1]["elements"][1:] == first_blocks[1]["elements"]
    assert blocks[-1]["elements"][-1]["text"] == "概算 $0.0000（前回の分析を使い回しました）"
    assert blocks[:1] + blocks[2:-1] == first_blocks[:1] + first_blocks[2:-1]
    assert (slack.posts[1]["channel"], slack.posts[1]["thread_ts"]) == (DM, None)
    assert len(gem.calls) == calls_before  # Gemini を呼ばない
    assert detached_jobs.REGISTRY.active_count() == 0
    await asyncio.sleep(0.05)
    assert len(usage) == usage_before + 1  # 1 段目の記録だけ（2 段目の費用は無い）


async def test_reused_post_waits_for_the_first_stage_reply(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """使い回しはすぐ終わるので、Aico の 1 段目の返信より先に届かないよう少し置いて投稿する。"""
    skill, _gem = await _first_full_run(monkeypatch, slack)
    monkeypatch.setattr(surface_video_followup, "REUSE_POST_DELAY_S", 0.3)
    await _call(_spec(skill))
    await asyncio.sleep(0.1)
    assert len(slack.posts) == 1
    await _eventually(lambda: len(slack.posts) == 2)
    assert slack.posts[1]["text"].startswith(surface_video_followup.REUSED_PREFIX)


async def test_different_top_videos_or_person_are_analyzed_again(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    await _first_full_run(monkeypatch, slack)
    key = surface_video_followup.reuse_key
    urls = [r["url"] for r in s3_rows()[:5]]
    assert key(USER_ID, KEYWORD, urls) == key(USER_ID, f" {KEYWORD.upper()} ", urls[::-1])
    assert key(USER_ID, KEYWORD, urls) != key("U999", KEYWORD, urls)
    assert key(USER_ID, KEYWORD, urls) != key(USER_ID, KEYWORD, [*urls[:4], "https://x/1"])
    assert surface_video_followup.CACHE.get(key(USER_ID, KEYWORD, urls)) is not None
    assert surface_video_followup.CACHE.get(key(USER_ID, KEYWORD, urls[:4])) is None


def test_cache_expires_after_24h_and_keeps_only_the_newest() -> None:
    now = [1000.0]
    cache = surface_video_followup.FollowupCache(max_entries=2, clock=lambda: now[0])
    cache.put("a", slack_text="A", report_url="https://s3.example/a")
    now[0] += surface_video_followup.REUSE_TTL_S - 1
    assert cache.get("a") is not None
    now[0] += 1
    assert cache.get("a") is None  # 24 時間で消える
    for k in ("b", "c", "d"):
        cache.put(k, slack_text=k, report_url=None)
    assert cache.get("b") is None and cache.get("d") is not None and len(cache) == 2


def test_failed_or_partial_status_results_are_not_reused() -> None:
    from teamagent.skills.search_surface_check.schema import SurfaceVideoFollowupOutput

    cache = surface_video_followup.FollowupCache()

    def complete(result: Any, error: BaseException | None) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(surface_video_followup, "CACHE", cache)
            mp.setattr(detached_jobs, "post_to_origin", lambda *a, **k: True)
            surface_video_followup._complete(
                result,
                error,
                False,
                keyword=KEYWORD,
                destination=detached_jobs.Destination(channel_id=DM, thread_ts=None),
                request_id="r",
                started=0.0,
                user_email=ME,
                usage_user_id=USER_ID,
                fallback_user_id=USER_ID,
                record_usage=lambda **kw: None,
                loop=asyncio.new_event_loop(),
                cache_key="k",
            )

    def digest(**kw: Any) -> VideoDigest:
        base: dict[str, Any] = {
            "keyword": KEYWORD,
            "requested": 5,
            "reserved": 5,
            "watched": 5,
            "watched_ranks": [1, 2, 3, 4, 5],
        }
        base.update(kw)
        return VideoDigest(**base)

    def ok(d: VideoDigest | None, url: str | None = "https://s3.example/r") -> Any:
        return SurfaceVideoFollowupOutput(
            keyword=KEYWORD, status="ok", slack_text="y", digest=d, report_url=url
        )

    complete(SurfaceVideoFollowupOutput(keyword=KEYWORD, status="all_failed", slack_text="x"), None)
    complete(None, RuntimeError("boom"))
    # 一部だけの結果（一時的な失敗）を 24 時間固定しない
    complete(ok(None), None)  # 集計が無い
    complete(ok(digest(), url=None), None)  # レポートを出せなかった
    complete(ok(digest(reserved=3, watched=3, watched_ranks=[1, 2, 3])), None)  # 月間上限で一部
    complete(ok(digest(watched=4, watched_ranks=[1, 2, 3, 4], failed_ranks=[5])), None)
    complete(ok(digest(watched=4, watched_ranks=[1, 2, 3, 4], cover_only_ranks=[5])), None)
    assert cache.get("k") is None
    complete(ok(digest()), None)  # 全本を動画で分析でき、レポートもある
    assert cache.get("k") is not None


def test_reuse_post_delay_default_stays_long_enough() -> None:
    """テストの fixture は待ちを 0 に上書きするので、ソースの既定値（1 段目の返信より後に届く秒数）を
    読んで固定する。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(surface_video_followup))
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "REUSE_POST_DELAY_S" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert len(values) == 1 and values[0] >= 10


# ── 待ち行列の優先度（明示の動画分析を先に）───────────────────────────────


def test_explicit_video_algorithm_starts_before_the_automatic_followup() -> None:
    registry = detached_jobs.DetachedJobRegistry()
    order: list[str] = []
    hold = threading.Event()
    dest = detached_jobs.Destination(channel_id=DM, thread_ts=None)

    def start(key: str, target: Callable[[], Any], priority: int) -> None:
        job, state = registry.start(
            key=key,
            max_background=1,
            tool="t",
            query=key,
            request_id=key,
            destination=dest,
            target=target,
            on_detached_done=lambda *a: None,
            priority=priority,
        )
        assert state == "started" and job is not None

    start("running", lambda: hold.wait(5), detached_jobs.PRIORITY_EXPLICIT)
    start("auto-1", lambda: order.append("auto-1"), detached_jobs.PRIORITY_AUTO)
    start("auto-2", lambda: order.append("auto-2"), detached_jobs.PRIORITY_AUTO)
    start("explicit", lambda: order.append("explicit"), detached_jobs.PRIORITY_EXPLICIT)
    deadline = time.monotonic() + 5
    while registry.queued_count() < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    hold.set()
    deadline = time.monotonic() + 5
    while len(order) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert order == ["explicit", "auto-1", "auto-2"]


async def test_followup_is_queued_with_the_automatic_priority(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    seen: list[int] = []
    real_start = detached_jobs.DetachedJobRegistry.start

    def spy(self: Any, **kw: Any) -> Any:
        seen.append(kw.get("priority", detached_jobs.PRIORITY_EXPLICIT))
        return real_start(self, **kw)

    monkeypatch.setattr(detached_jobs.DetachedJobRegistry, "start", spy)
    _enable(monkeypatch)
    gate = threading.Event()
    skill, _g, _d = _skill(downloader=FakeDownloader(gate=gate))
    await _call(_spec(skill))
    assert seen == [detached_jobs.PRIORITY_AUTO]
    detached_jobs.REGISTRY.interrupt_all()
    gate.set()
