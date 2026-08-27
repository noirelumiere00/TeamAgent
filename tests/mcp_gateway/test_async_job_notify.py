"""非同期 job 完了通知のポーリング・Slack 配信契約。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.mcp_gateway import server
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusOutput
from teamagent.skills.tiktok_acquire.schema import TikTokAcquireStatusOutput


class _FakeSlack:
    def __init__(self, *, raise_on_post: bool = False) -> None:
        self.raise_on_post = raise_on_post
        self.opened: list[str] = []
        self.posted: list[dict[str, Any]] = []

    async def open_dm(self, user_id: str, request_id: str) -> str:
        self.opened.append(user_id)
        return "D123"

    async def post_message(
        self,
        *,
        channel: str,
        text: str,
        request_id: str,
        thread_ts: str | None = None,
    ) -> Any:
        if self.raise_on_post:
            raise RuntimeError("slack unavailable")
        self.posted.append(
            {
                "channel": channel,
                "text": text,
                "request_id": request_id,
                "thread_ts": thread_ts,
            }
        )
        return object()


class _ImmediateThread:
    created: ClassVar[list[_ImmediateThread]] = []

    def __init__(
        self,
        *,
        target: Callable[[], None],
        name: str,
        daemon: bool,
    ) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.created.append(self)

    def start(self) -> None:
        self.started = True
        self.target()


class _SubmitInput(BaseModel):
    request: str


class _SubmitOutput(BaseModel):
    job_id: str
    status: str
    message: str


class _SubmitSkill(BaseSkill[_SubmitInput, _SubmitOutput]):
    name: ClassVar[str] = "proposal_builder_submit"
    description: ClassVar[str] = "テスト用非同期submit"
    input_schema: ClassVar[type[BaseModel]] = _SubmitInput
    output_schema: ClassVar[type[BaseModel]] = _SubmitOutput

    def run(self, input: _SubmitInput, ctx: SkillContext) -> _SubmitOutput:
        return _SubmitOutput(job_id="pb_dispatch", status="queued", message=input.request)


@pytest.fixture
def immediate_notify(monkeypatch: pytest.MonkeyPatch) -> _FakeSlack:
    monkeypatch.setenv("USE_ASYNC_JOB_NOTIFY", "true")
    monkeypatch.setattr(notify.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(notify, "_INITIAL_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(notify, "_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(notify, "_TIMEOUT_SECONDS", 1.0)
    _ImmediateThread.created.clear()
    fake = _FakeSlack()
    monkeypatch.setattr(notify.SlackClient, "from_env", classmethod(lambda cls: fake))
    return fake


def test_done_posts_once_to_original_thread(immediate_notify: _FakeSlack) -> None:
    done = TikTokAcquireStatusOutput(
        job_id="ta_1",
        status="done",
        counts={"posts": 1},
        videos=[{"downloaded": True}],
        message="完了しました。",
    )
    statuses = iter(
        [
            ("running", "処理中"),
            (done.status, server._format_tiktok_completion(done)),
        ]
    )

    notify.schedule_completion_notice(
        tool="tiktok_acquire",
        job_id="ta_1",
        user_context={"channel_id": "C123", "thread_ts": "111.222"},
        request_id="req-1",
        poll=lambda: next(statuses),
    )

    assert len(immediate_notify.posted) == 1
    posted = immediate_notify.posted[0]
    assert posted["channel"] == "C123"
    assert posted["request_id"] == "req-1"
    assert posted["thread_ts"] == "111.222"
    assert "動画: 1/1本取得" in posted["text"]
    assert _ImmediateThread.created[0].daemon is True


def test_failed_posts_error_code(immediate_notify: _FakeSlack) -> None:
    failed = ProposalBuilderStatusOutput(
        job_id="pb_1",
        status="failed",
        error_code="PROPOSAL_BUILD_FAILED",
        message="提案書生成に失敗しました。",
    )
    notify.schedule_completion_notice(
        tool="proposal_builder_submit",
        job_id="pb_1",
        user_context={"slack_user_id": "U12345678"},
        request_id="req-2",
        poll=lambda: (failed.status, server._format_proposal_completion(failed)),
    )

    assert immediate_notify.opened == ["U12345678"]
    assert len(immediate_notify.posted) == 1
    assert "PROPOSAL_BUILD_FAILED" in immediate_notify.posted[0]["text"]


def test_timeout_posts_not_completed_message(
    immediate_notify: _FakeSlack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "_TIMEOUT_SECONDS", 0.0)

    notify.schedule_completion_notice(
        tool="tiktok_acquire",
        job_id="ta_slow",
        user_context={"channel_id": "C123"},
        request_id="req-3",
        poll=lambda: ("running", "処理中"),
    )

    assert len(immediate_notify.posted) == 1
    message = immediate_notify.posted[0]["text"]
    assert "まだ完了していません" in message
    assert "`tiktok_acquire_status`" in message
    assert "ta_slow" in message


def test_slack_exception_is_fail_open(
    immediate_notify: _FakeSlack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_slack = _FakeSlack(raise_on_post=True)
    monkeypatch.setattr(
        notify.SlackClient,
        "from_env",
        classmethod(lambda cls: failing_slack),
    )

    result = notify.schedule_completion_notice(
        tool="proposal_builder_submit",
        job_id="pb_2",
        user_context={"channel_id": "C123"},
        request_id="req-4",
        poll=lambda: ("done", "完了"),
    )

    assert result is None


@pytest.mark.asyncio
async def test_notify_exception_does_not_change_submit_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_ASYNC_JOB_NOTIFY", "true")
    monkeypatch.setattr(
        notify,
        "schedule_completion_notice",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("slack unavailable")),
    )
    spec = ToolSpec(_SubmitSkill.name, _SubmitSkill.description, _SubmitSkill)

    contents = await server.dispatch_tool(
        {spec.name: spec},
        spec.name,
        {
            "request": "accepted",
            server.USER_CONTEXT_KEY: {"channel_id": "C123"},
        },
        require_rls=False,
    )

    assert json.loads(contents[0].text) == {
        "job_id": "pb_dispatch",
        "status": "queued",
        "message": "accepted",
    }


def test_flag_off_does_not_start_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_ASYNC_JOB_NOTIFY", raising=False)

    # 呼び出し側の try/except が AssertionError まで握り潰すため、例外で検出せず
    # 「生成されたか」を記録して検証する（フラグ判定を外すとこのテストが赤になる）。
    created: list[dict[str, Any]] = []

    class _RecordingThread:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

        def start(self) -> None:
            created.append({"started": True})

    monkeypatch.setattr(notify.threading, "Thread", _RecordingThread)

    result = notify.schedule_completion_notice(
        tool="tiktok_acquire",
        job_id="ta_off",
        user_context={"channel_id": "C123"},
        request_id="req-5",
        poll=lambda: ("done", "完了"),
    )

    assert result is None
    assert created == [], "フラグ OFF ではスレッドを生成してはならない"


# ── 見張り時間（C6: 15 分 → 60 分）─────────────────────────────────────────────
# 実測の所要は proposal_builder / tiktok_acquire とも 40〜50 分帯。実時間で待てないので
# 仮想時計（_monotonic）と「待たずに時計だけ進める waiter」を差し込んで境界を検査する。

_MEASURED_JOB_SECONDS = 50 * 60.0
# fixture が短い値へ差し替える前の実値（このモジュールの import 時点＝未パッチ）。
_REAL_TIMEOUT_SECONDS = notify._TIMEOUT_SECONDS


class _VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.steps = 0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def virtual_time(monkeypatch: pytest.MonkeyPatch) -> _VirtualClock:
    """待たずに仮想時計だけを進める（poll 間隔ぶんずつ）。"""

    clock = _VirtualClock()
    monkeypatch.setattr(notify, "_monotonic", clock)

    def _advance(deadline: float, interval_seconds: float) -> None:
        clock.steps += 1
        # 打ち切りを外すと時計が deadline で止まったまま poll し続ける（無限スレッド）。
        # 実時間で気づけないので、ここで見切って赤にする。
        assert clock.steps < 10_000, "見張りが deadline で終わっていない（無限ループ）"
        clock.now = min(deadline, clock.now + interval_seconds)

    monkeypatch.setattr(notify, "_wait_until_next_poll", _advance)
    return clock


def test_watch_window_outlasts_the_measured_job_duration() -> None:
    """見張り時間は実測所要より長い（短いと毎回が誤報になる）。"""

    assert _REAL_TIMEOUT_SECONDS >= _MEASURED_JOB_SECONDS


def test_forty_five_minute_job_gets_the_completion_notice(
    immediate_notify: _FakeSlack,
    virtual_time: _VirtualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """45 分かかるジョブでも「まだ完了していません」ではなく完了通知が届く。"""

    monkeypatch.setattr(notify, "_INITIAL_DELAY_SECONDS", 30.0)
    monkeypatch.setattr(notify, "_POLL_INTERVAL_SECONDS", 30.0)
    monkeypatch.setattr(notify, "_TIMEOUT_SECONDS", _REAL_TIMEOUT_SECONDS)

    def _poll() -> tuple[str, str]:
        if virtual_time.now >= 45 * 60.0:
            return ("done", "提案書ができました。")
        return ("running", "処理中")

    notify.schedule_completion_notice(
        tool="proposal_builder_submit",
        job_id="pb_slow",
        user_context={"channel_id": "C123"},
        request_id="req-45min",
        poll=_poll,
    )

    assert len(immediate_notify.posted) == 1
    assert immediate_notify.posted[0]["text"] == "提案書ができました。"
    assert virtual_time.now == pytest.approx(45 * 60.0)


def test_stuck_job_still_gives_up_at_the_watch_window(
    immediate_notify: _FakeSlack,
    virtual_time: _VirtualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """終わらないジョブは見張り時間ちょうどで打ち切る（無限スレッドを作らない）。"""

    monkeypatch.setattr(notify, "_INITIAL_DELAY_SECONDS", 30.0)
    monkeypatch.setattr(notify, "_POLL_INTERVAL_SECONDS", 30.0)
    monkeypatch.setattr(notify, "_TIMEOUT_SECONDS", _REAL_TIMEOUT_SECONDS)

    notify.schedule_completion_notice(
        tool="tiktok_acquire",
        job_id="ta_stuck",
        user_context={"channel_id": "C123"},
        request_id="req-stuck",
        poll=lambda: ("running", "処理中"),
    )

    assert len(immediate_notify.posted) == 1
    assert "まだ完了していません" in immediate_notify.posted[0]["text"]
    assert virtual_time.now == pytest.approx(_REAL_TIMEOUT_SECONDS)
