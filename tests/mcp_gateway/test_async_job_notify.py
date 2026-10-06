"""P4: 終わったら届く・催促すると本当の状態が分かる。外部接続はフェイクのみ。"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.mcp_gateway import detached_jobs, server
from teamagent.skills._shared import long_jobs
from teamagent.skills._shared.long_jobs import _OWNER_KEY, ORIGIN_KEY, Origin
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
from teamagent.skills.proposal_builder.skill import ProposalBuilderStatusSkill
from teamagent.skills.tiktok_acquire.schema import TikTokAcquireStatusInput
from teamagent.skills.tiktok_acquire.skill import TikTokAcquireStatusSkill


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    store = ProposalJobStore(table_name="", memory={})
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    monkeypatch.setattr(long_jobs, "ProposalJobStore", lambda: store)
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "1")
    posted: list[dict[str, Any]] = []

    def post(text: str, destination: Any, **kwargs: Any) -> bool:
        posted.append({"text": text, "destination": destination, **kwargs})
        return True

    monkeypatch.setattr(detached_jobs, "post_to_origin", post)
    notify._active.clear()
    yield posted
    notify._active.clear()


def target() -> Origin:
    return Origin("D12345678", "111.222", "U12345678")


def ctx(user: str = "U12345678", channel: str = "D12345678") -> SkillContext:
    return SkillContext(metadata={_OWNER_KEY: user, "channel_id": channel})


def test_completion_has_results_in_the_same_single_notice(notices: list[dict[str, Any]]) -> None:
    output = SimpleNamespace(
        status="done",
        message="完了しました。",
        counts={"posts": 2},
        videos=[],
        posts_json_url="https://example.test/result",
        error_code=None,
        job_id="tk_secret",
    )
    text = server._format_tiktok_completion(output)
    for origin in [target(), target()]:
        notify.publish_notice(text, origin=origin, job_id="tk_secret", request_id="req-1")
    assert len(notices) == 1
    assert "https://example.test/result" in notices[0]["text"]
    assert "tk_secret" not in notices[0]["text"]
    assert notices[0]["destination"] == detached_jobs.Destination("D12345678", "111.222")


@pytest.mark.parametrize(
    "error", ["ratelimited", "missing_scope", "not_in_channel", "TimeoutError"]
)
def test_failure_is_one_line_without_forwarding_codes(
    notices: list[dict[str, Any]],
    error: str,
) -> None:
    output = SimpleNamespace(status="failed", message=error * 10000, error_code=error)
    text = server._format_tiktok_completion(output)
    notify.publish_notice(text, origin=target(), job_id="tk_failure", request_id="req-2")
    assert len(notices) == 1
    assert len(text.splitlines()) == 1 and len(text) < 150
    assert error not in text


def test_long_running_job_gets_notice_then_saved_result(
    notices: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(notify, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(notify, "_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(notify, "_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(notify, "_MAX_WATCH_SECONDS", 200)
    monkeypatch.setattr(
        notify,
        "_wait_until_next_poll",
        lambda deadline, interval: clock.__setitem__(0, clock[0] + 30),
    )
    notify._run_completion_notice(
        job_id="slow",
        origin=target(),
        request_id="req-3",
        poll=lambda: (
            ("done", "保存済みの結果 https://example.test/report")
            if clock[0] >= 120
            else ("running", "実行中")
        ),
    )
    assert len(notices) == 2
    assert "まだ完了していません" in notices[0]["text"]
    assert "保存済みの結果" in notices[1]["text"]
    assert all("job_id" not in item["text"] for item in notices)


def test_poll_failure_does_not_invent_running_or_completion(
    notices: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(notify, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(notify, "_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(notify, "_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(notify, "_MAX_WATCH_SECONDS", 90)
    monkeypatch.setattr(
        notify,
        "_wait_until_next_poll",
        lambda deadline, interval: clock.__setitem__(0, clock[0] + 30),
    )
    notify._run_completion_notice(
        job_id="unknown",
        origin=target(),
        request_id="req-4",
        poll=lambda: (_ for _ in ()).throw(TimeoutError()),
    )
    assert "状態を確認できません" in notices[0]["text"]
    assert "実行中" not in notices[0]["text"]


def test_flag_off_neither_posts_nor_defers(
    notices: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "0")
    notify.publish_notice("完了", origin=target(), job_id="off", request_id="off")
    assert notices == []
    assert long_jobs.origin(SkillContext(metadata={ORIGIN_KEY: target()})) is None


def test_still_question_resolves_only_own_latest_job(notices: list[dict[str, Any]]) -> None:
    long_jobs.remember_latest(ctx(), "tiktok_acquire", "tk_first")
    long_jobs.remember_latest(ctx(), "tiktok_acquire", "tk_latest")
    seen: list[str] = []

    class Store:
        def get_status(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
            seen.append(job_id)
            return {"status": "running"}

    skill = TikTokAcquireStatusSkill(store=Store())
    result = skill.run(TikTokAcquireStatusInput(), ctx())
    assert result.job_id == "tk_latest" and result.status == "running"
    assert "実行中" in result.message
    other = skill.run(TikTokAcquireStatusInput(), ctx("Uother"))
    assert other.status == "unknown" and seen == ["tk_latest"]
    assert long_jobs.latest_job(ctx(channel="Cother"), "tiktok_acquire") == ""


def test_proposal_still_question_returns_stored_failed_state(notices: list[dict[str, Any]]) -> None:
    store = ProposalJobStore(table_name="", memory={})
    store.create_job("pb_latest", {})
    store.mark_failed("pb_latest", "PROPOSAL_BUILD_FAILED")
    long_jobs.remember_latest(ctx(), "proposal_builder_submit", "pb_latest")
    result = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(), ctx())
    assert result.job_id == "pb_latest" and result.status == "failed"


def test_unsigned_claim_cannot_supply_destination() -> None:
    assert detached_jobs.destination_from_claim(None) is None
    assert (
        long_jobs.origin(SkillContext(metadata={ORIGIN_KEY: {"channel_id": "Dattacker"}})) is None
    )


@pytest.mark.parametrize("fault", [TimeoutError, RuntimeError])
def test_failed_result_save_never_delivers_pending_files(
    notices: list[dict[str, Any]],
    tmp_path: Any,
    fault: type[Exception],
) -> None:
    path = tmp_path / "report.pptx"
    path.write_bytes(b"generated result")
    uploads: list[str] = []

    class Slack:
        async def upload_file(self, *args: Any, **kwargs: Any) -> bool:
            uploads.append(args[1])
            return True

    origin = target()
    origin.defer(Slack(), str(path), "資料", "完了", "request")
    notify.publish_notice(
        "結果の保存を確認できませんでした。",
        origin=origin,
        job_id=f"write-{fault.__name__}",
        request_id="write",
        completed=False,
    )
    assert uploads == [] and not origin.pending
    assert "完了" not in notices[0]["text"]


def test_retry_is_exactly_once_and_flag_can_disable_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.retry import retry_long_job_once

    calls: list[int] = []

    def action() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError()
        return 42

    assert retry_long_job_once(action) == 42 and len(calls) == 2
    calls.clear()
    monkeypatch.setenv("USE_LONG_JOB_RETRY", "0")
    with pytest.raises(TimeoutError):
        retry_long_job_once(action)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [RuntimeError("apify run failed"), ValueError("bad input"), PermissionError("budget")],
)
def test_retry_skips_non_transient_failures_so_paid_calls_are_not_doubled(
    error: Exception,
) -> None:
    """取得 API・生成・予算の失敗はやり直さない（同じ結果で課金だけ 2 倍になる）。"""
    from teamagent.adapters.retry import retry_long_job_once

    calls: list[int] = []

    def action() -> int:
        calls.append(1)
        raise error

    with pytest.raises(type(error)):
        retry_long_job_once(action)
    assert len(calls) == 1


def test_automatic_video_detach_uses_verified_claim_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "1")
    monkeypatch.delenv(detached_jobs.ENABLED_ENV, raising=False)
    monkeypatch.delenv(detached_jobs.ALLOWED_EMAILS_ENV, raising=False)
    monkeypatch.delenv(detached_jobs.DM_ONLY_ENV, raising=False)
    policy = detached_jobs.load_policy()
    assert policy.enabled and policy.allowed_emails == frozenset({"*"})
    assert not policy.dm_only
    assert (
        detached_jobs.decide(policy, tool="video_algorithm", verified_caller=None, metadata={})[0]
        is None
    )
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "0")
    assert not detached_jobs.load_policy().enabled


@pytest.mark.asyncio
async def test_shutdown_notifies_the_monitor_interruption_once(
    notices: list[dict[str, Any]],
) -> None:
    origin = target()
    notify._active["key"] = ("tiktok_acquire", "tk_interrupt", origin, "req-interrupt")
    assert await notify.notify_interrupted(budget_s=1) == 1
    await notify.notify_interrupted(budget_s=1)
    assert len(notices) == 1
    assert origin.cancelled
    assert "確認を再開" in notices[0]["text"]  # 更新後は台帳から見張りを再開する
    assert "作業が中断" not in notices[0]["text"]


@pytest.mark.asyncio
async def test_video_shutdown_and_recovery_share_one_terminal_notice(
    notices: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import teamagent.adapters.proposal_job_store as module

    store = notify.ProposalJobStore()
    monkeypatch.setattr(module, "ProposalJobStore", lambda: store)
    store.create_job("va_shutdown", {"kind": "video_algorithm"})
    store.mark_running("va_shutdown")
    registry = detached_jobs.DetachedJobRegistry()
    job = detached_jobs.DetachedJob(
        registry=registry,
        key="verified",
        tool="video_algorithm",
        query="動画分析",
        request_id="shutdown",
        destination=detached_jobs.Destination("D12345678", "111.222"),
        target=lambda: None,
        on_detached_done=lambda job: None,
    )
    jobs = [job]

    def interrupt() -> list[detached_jobs.DetachedJob]:
        result, jobs[:] = jobs[:], []
        return result

    monkeypatch.setattr(registry, "interrupt_all", interrupt)
    assert await detached_jobs.notify_interrupted(registry=registry, budget_s=1) == 1
    assert await detached_jobs.notify_interrupted(registry=registry, budget_s=1) == 0
    assert store.get_job("va_shutdown")["status"] == "failed"
    notify.publish_notice(
        "システム更新で動画分析が中断されました。",
        origin=target(),
        request_id="recovered",
        job_id="va_shutdown",
        completed=False,
    )
    assert len(notices) == 1
    assert "中断" in notices[0]["text"]


def test_empty_done_payload_cannot_send_premature_completion(
    notices: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import teamagent.skills.tiktok_acquire.skill as module
    from teamagent.skills.tiktok_acquire.schema import TikTokAcquireStatusOutput

    class Status:
        def run(self, input: Any, context: SkillContext) -> TikTokAcquireStatusOutput:
            return TikTokAcquireStatusOutput(job_id=input.job_id, status="done")

    monkeypatch.setattr(module, "TikTokAcquireStatusSkill", Status)
    state, text = server._build_async_job_poll("tiktok_acquire", "tk_empty", ctx())()
    assert state == "unknown" and "完了" not in text
    assert notices == []


def test_large_metadata_does_not_hide_the_result_link() -> None:
    output = SimpleNamespace(
        status="done",
        message="untrusted text" * 10000,
        counts={"caption" * 10000: "value" * 10000},
        videos=[],
        posts_json_url="https://example.test/result",
        error_code=None,
    )
    text = server._format_tiktok_completion(output)
    assert len(text) < 500 and "https://example.test/result" in text


@pytest.mark.asyncio
async def test_dispatch_uses_signed_destination_and_discards_spoofed_context(
    notices: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import ClassVar

    from pydantic import BaseModel

    from teamagent.identity import ResolvedIdentity
    from teamagent.orchestrator.tools import ToolSpec
    from teamagent.skills.base import BaseSkill

    class Input(BaseModel):
        pass

    class Output(BaseModel):
        job_id: str = "pb_signed"
        status: str = "queued"

    class Submit(BaseSkill[Input, Output]):
        name: ClassVar[str] = "proposal_builder_submit"
        description: ClassVar[str] = "fake"
        input_schema: ClassVar[type[BaseModel]] = Input
        output_schema: ClassVar[type[BaseModel]] = Output

        def run(self, input: Input, ctx: SkillContext) -> Output:
            return Output()

    class Verifier:
        async def verify(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                slack_user_id="U12345678",
                slack_team_id="T12345678",
                channel_id="D12345678",
                thread_ts="123.456",
                message_id="123.456",
            )

    async def resolve(user: str) -> ResolvedIdentity:
        return ResolvedIdentity(slack_user_id=user, email="caller@example.com")

    scheduled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify, "schedule_completion_notice", lambda **kwargs: scheduled.append(kwargs)
    )
    monkeypatch.setattr(server, "_record_usage", lambda **kwargs: None)
    monkeypatch.setenv("ENABLE_PROGRESS_NOTIFY", "0")
    spec = ToolSpec(Submit.name, Submit.description, Submit)
    await server.dispatch_tool(
        {spec.name: spec},
        spec.name,
        {
            server.USER_CONTEXT_KEY: {
                "channel_id": "Cattacker",
                "slack_user_id": "Uattacker",
                _OWNER_KEY: "Uattacker",
            }
        },
        identity_resolver=resolve,
        caller_claim_verifier=Verifier(),
        allowed_domains=frozenset({"example.com"}),
    )
    assert len(scheduled) == 1
    dest = scheduled[0]["origin"]
    assert (dest.channel_id, dest.thread_ts, dest.user_id) == ("D12345678", "123.456", "U12345678")


def test_restart_recovers_pending_notice_from_existing_store(
    notices: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[SkillContext] = []

    def build(tool: str, job_id: str, context: SkillContext) -> Any:
        captured.append(context)
        return lambda: ("done", "保存済み結果 https://example.test/result")

    class Thread:
        def __init__(self, *, target: Any, **kwargs: Any) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(server, "_build_async_job_poll", build)
    monkeypatch.setattr(notify.threading, "Thread", Thread)
    monkeypatch.setattr(notify, "_INITIAL_DELAY_SECONDS", 0)
    entry = {
        "key": "tiktok_acquire:tk_recover:D12345678:123.456",
        "tool": "tiktok_acquire",
        "job_id": "tk_recover",
        "channel_id": "D12345678",
        "thread_ts": "123.456",
        "user_id": "U12345678",
        "request_id": "req-recover",
        "owner": "verified-owner",
        "principal": "a" * 64,
        "created_at": notify.time.time() - 600,
        "updated_at": 0,
    }
    notify._change_outbox(lambda entries: {entry["key"]: entry})
    assert notify.recover_pending_notices() == 1
    assert len(notices) == 1 and "保存済み結果" in notices[0]["text"]
    assert captured[0].metadata["_long_job_principal_hash"] == "a" * 64
    assert notices[0]["destination"].thread_ts == "123.456"
    assert notify.recover_pending_notices() == 0


def test_restart_does_not_take_over_a_fresh_monitor(
    notices: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify.time, "time", lambda: 1000)
    notify._change_outbox(lambda entries: {"fresh": {"updated_at": 999}})
    assert notify.recover_pending_notices() == 0 and notices == []


def _outbox() -> dict[str, Any]:
    _, entries = notify._read_outbox(notify.ProposalJobStore())
    return entries


def _inline_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    class Thread:
        def __init__(self, *, target: Any, **kwargs: Any) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(notify.threading, "Thread", Thread)
    monkeypatch.setattr(notify, "_INITIAL_DELAY_SECONDS", 0)


def test_outbox_write_failure_does_not_fail_the_job_request(
    notices: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """台帳に書けなくても受付は落とさず、このプロセスの見張りで結果は届く。"""
    _inline_threads(monkeypatch)

    def broken(_change: Any) -> None:
        raise RuntimeError("notification outbox contention")

    monkeypatch.setattr(notify, "_change_outbox", broken)
    notify.schedule_completion_notice(
        tool="tiktok_acquire",
        job_id="tk_1",
        origin=target(),
        request_id="req-1",
        poll=lambda: ("done", "結果 https://example.test/r"),
        ctx=ctx(),
    )
    assert len(notices) == 1 and "結果" in notices[0]["text"]


def test_finished_watch_is_removed_from_outbox_but_shutdown_keeps_it(
    notices: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """完了・24 時間の見守り切れでは行を消す。更新での停止（cancelled）だけは残して再開に回す。"""
    _inline_threads(monkeypatch)
    notify.schedule_completion_notice(
        tool="tiktok_acquire",
        job_id="tk_done",
        origin=target(),
        request_id="req-done",
        poll=lambda: ("done", "結果"),
        ctx=ctx(),
    )
    assert _outbox() == {}

    clock = [0.0]
    monkeypatch.setattr(notify, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(notify, "_MAX_WATCH_SECONDS", 60)

    def advance(_deadline: float, interval: float) -> None:
        clock[0] += interval

    monkeypatch.setattr(notify, "_wait_until_next_poll", advance)
    notify.schedule_completion_notice(
        tool="tiktok_acquire",
        job_id="tk_slow",
        origin=target(),
        request_id="req-slow",
        poll=lambda: ("running", ""),
        ctx=ctx(),
    )
    assert _outbox() == {}

    cancelled = target()
    cancelled.cancelled = True
    notify.schedule_completion_notice(
        tool="tiktok_acquire",
        job_id="tk_cut",
        origin=cancelled,
        request_id="req-cut",
        poll=lambda: ("running", ""),
        ctx=ctx(),
    )
    assert list(_outbox()) == ["tiktok_acquire:tk_cut:D12345678:111.222"]


def test_expired_outbox_rows_are_dropped_and_not_resumed(
    notices: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """見守りの上限を過ぎた行は再開せず台帳から消す（溜まって満杯にならない）。"""
    built: list[str] = []
    monkeypatch.setattr(
        server, "_build_async_job_poll", lambda tool, job_id, context: built.append(job_id)
    )
    old = notify.time.time() - notify._MAX_WATCH_SECONDS - 3 * 60 * 60
    notify._change_outbox(
        lambda entries: {"stale": {"tool": "tiktok_acquire", "created_at": old, "updated_at": old}}
    )
    assert notify.recover_pending_notices() == 0
    assert built == [] and notices == [] and _outbox() == {}


def test_recovery_keeps_the_original_created_at(
    notices: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """再開のたびに期限が延びない（最初の受付時刻を引き継ぐ）。"""
    monkeypatch.setattr(server, "_build_async_job_poll", lambda *a: lambda: ("running", ""))

    class Thread:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def start(self) -> None:  # 見張りは走らせない（台帳の行だけを見る）
            pass

    monkeypatch.setattr(notify.threading, "Thread", Thread)
    created = notify.time.time() - 3600
    key = "tiktok_acquire:tk_r:D12345678:123.456"
    notify._change_outbox(
        lambda entries: {
            key: {
                "key": key,
                "tool": "tiktok_acquire",
                "job_id": "tk_r",
                "channel_id": "D12345678",
                "thread_ts": "123.456",
                "user_id": "U12345678",
                "request_id": "req-r",
                "owner": "verified-owner",
                "principal": "a" * 64,
                "created_at": created,
                "updated_at": 0,
            }
        }
    )
    assert notify.recover_pending_notices() == 1
    assert _outbox()[key]["created_at"] == pytest.approx(created)


def test_video_still_question_uses_owner_and_keeps_older_owned_jobs_visible(
    notices: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import teamagent.skills.video_algorithm.skill as module
    from teamagent.skills.video_algorithm.schema import VideoAlgorithmStatusInput

    store = ProposalJobStore(table_name="", memory={})
    monkeypatch.setattr(module, "ProposalJobStore", lambda: store, raising=False)
    # status の遅延 import と同じ adapter factory を差し替える。
    import teamagent.adapters.proposal_job_store as adapter

    monkeypatch.setattr(adapter, "ProposalJobStore", lambda: store)
    owner = long_jobs.owner_key(ctx(), "video_algorithm")
    for job_id in ["va_first", "va_latest"]:
        store.create_job(job_id, {"owner": owner})
        store.mark_running(job_id)
        long_jobs.remember_latest(ctx(), "video_algorithm", job_id)
    skill = module.VideoAlgorithmStatusSkill()
    assert skill.run(VideoAlgorithmStatusInput(), ctx()).job_id == "va_latest"
    older = skill.run(VideoAlgorithmStatusInput(job_id="va_first"), ctx())
    assert older.status == "running"
    denied = skill.run(VideoAlgorithmStatusInput(job_id="va_first"), ctx("Uother"))
    assert denied.status == "unknown" and "実行中" not in denied.message


def test_still_question_for_two_keywords_does_not_claim_both_finished(
    notices: list[dict[str, Any]],
) -> None:
    long_jobs.remember_latest(ctx(), "tiktok_acquire", "tk_done|tk_running")

    class Store:
        def get_status(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
            return (
                {"status": "done", "posts_json_url": "https://example.test/result"}
                if job_id == "tk_done"
                else {"status": "running"}
            )

    result = TikTokAcquireStatusSkill(store=Store()).run(TikTokAcquireStatusInput(), ctx())
    assert result.status == "running" and "完了 1/2件" in result.message
    assert len(result.job_results) == 2
    assert [job["status"] for job in result.job_results] == ["done", "running"]


def test_latest_omiyage_restart_failure_does_not_claim_analysis_finished(
    notices: list[dict[str, Any]],
) -> None:
    from datetime import UTC, datetime

    from teamagent.skills.omiyage_report.schema import OmiyageReportStatusInput
    from teamagent.skills.omiyage_report.skill import OmiyageReportStatusSkill

    store = ProposalJobStore(
        table_name="", memory={}, clock=lambda: datetime(2000, 1, 1, tzinfo=UTC)
    )
    job_id = "omy_" + "a" * 32
    store.create_job(job_id, {"kind": "omiyage_report"})
    store.mark_running(job_id)
    long_jobs.remember_latest(ctx(), "omiyage_report_submit", job_id)
    result = OmiyageReportStatusSkill(store=store).run(OmiyageReportStatusInput(), ctx())
    assert result.status == "failed" and result.error_code == "MCP_RESTARTED"
    assert "システム更新" in result.message
    assert "動画分析は終わりました" not in result.message
