"""直接投稿の経路が Block Kit を渡すこと・描けない/弾かれたときは文字だけに戻ることのテスト。

経路は 3 つ（本番の形のデータ＝slack_prod_shape で組む）:
- A: 検索上位チェック 1 段目（``direct_summary.deliver``）
- B: 2 段目の追記（``surface_video_followup._complete``・使い回しの ``_post_reused_later``）
- C: 動画分析の完了（``server._complete_detached``＝dispatch_tool の切り離し経由）
確かめること:
(6) 描画の例外 → 今の文字だけの投稿（``slack_summary`` に slack_escape）に戻る（結果を消さない）
(7) 各経路が blocks と通知文を ``post_message`` に渡す
Slack が blocks を弾いた（``invalid_blocks``＝SlackApiError）→ 2 回目は文字だけで 1 回だけ送り直す。
タイムアウトは届いた可能性があるので送り直さない（二重投稿を避ける）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel
from slack_sdk.errors import SlackApiError

from teamagent.adapters.slack_client import SlackPostResult
from teamagent.mcp_gateway import detached_jobs, direct_summary, server, surface_video_followup
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search_surface_check import slack_render as surface_render
from teamagent.skills.search_surface_check.slack_render import (
    REUSED_NOTE,
    followup_message,
    surface_message,
)
from teamagent.skills.video_algorithm import slack_render as algo_render
from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput, VideoAlgorithmOutput
from teamagent.skills.video_algorithm.slack_render import completion_message
from tests.skills.search_surface_check import slack_prod_shape as shape

DM = "D0123456789"
USER_ID = "U0123456789"
DEST = detached_jobs.Destination(channel_id=DM, thread_ts=None)


class _Slack:
    """SlackClient.post_message の代わり。本番の失敗の形（SlackApiError・TimeoutError）を再現する。"""

    def __init__(self, *, reject_blocks: bool = False, timeout: bool = False) -> None:
        self.posts: list[dict[str, Any]] = []
        self.reject_blocks = reject_blocks
        self.timeout = timeout
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
            self.posts.append({"channel": channel, "text": text, "blocks": blocks})
        if self.timeout:
            raise TimeoutError("slack read timeout")
        if blocks is not None and self.reject_blocks:
            # slack_sdk は ok=false を SlackApiError で投げる（chat.postMessage の invalid_blocks）
            raise SlackApiError(
                "The request to the Slack API failed.", {"ok": False, "error": "invalid_blocks"}
            )
        return SlackPostResult(channel=channel, ts="1784424999.000100", ok=True)


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> _Slack:
    fake = _Slack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    return fake


def _use(monkeypatch: pytest.MonkeyPatch, fake: _Slack) -> _Slack:
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    return fake


def _as_text(markdown: str) -> str:
    """今の文字だけの投稿（直接投稿の既存経路の最後の処理）。"""
    return detached_jobs.slack_escape(markdown_bold_to_mrkdwn(markdown))


# ── A: 検索上位チェック 1 段目 ─────────────────────────────────────────


def test_a_deliver_passes_blocks_and_fallback_text(slack: _Slack) -> None:
    out = shape.a_output()
    status = direct_summary.deliver(
        out.model_dump(), DEST, request_id="r", skill_input=shape.a_input()
    )
    expected = surface_message(out, shape.a_input())
    assert status == "posted" and expected is not None
    assert len(slack.posts) == 1
    assert slack.posts[0]["blocks"] == expected.blocks
    assert slack.posts[0]["text"] == expected.text  # 通知文は描画側でエスケープ済み（二重にしない）
    assert f"<{shape.A_REPORT}|レポートを開く>" in slack.posts[0]["text"]


def test_a_render_failure_falls_back_to_the_text_post(
    monkeypatch: pytest.MonkeyPatch, slack: _Slack
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("render bug")

    monkeypatch.setattr(surface_render, "surface_message", boom)
    out = shape.a_output()
    status = direct_summary.deliver(
        out.model_dump(), DEST, request_id="r", skill_input=shape.a_input()
    )
    assert status == "posted"
    assert slack.posts == [{"channel": DM, "text": _as_text(out.slack_summary), "blocks": None}]
    assert f"<{shape.A_REPORT}>" in slack.posts[0]["text"]


def test_a_unlinkable_report_url_falls_back_to_the_text_post(slack: _Slack) -> None:
    out = shape.a_output()
    out.report_url = "https://connect.newstv.co.jp/r/全角"
    assert direct_summary.deliver(out.model_dump(), DEST, request_id="r") == "posted"
    assert slack.posts[0]["blocks"] is None


def test_a_rejected_blocks_are_resent_as_text_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _use(monkeypatch, _Slack(reject_blocks=True))
    out = shape.a_output()
    status = direct_summary.deliver(
        out.model_dump(), DEST, request_id="r", skill_input=shape.a_input()
    )
    assert status == "posted"
    assert [p["blocks"] is not None for p in fake.posts] == [True, False]
    assert fake.posts[1]["text"] == _as_text(out.slack_summary)


def test_a_timeout_with_blocks_is_not_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _use(monkeypatch, _Slack(timeout=True))
    out = shape.a_output()
    status = direct_summary.deliver(
        out.model_dump(), DEST, request_id="r", skill_input=shape.a_input()
    )
    assert status == "uncertain"
    assert len(fake.posts) == 1 and fake.posts[0]["blocks"] is not None


# ── B: 2 段目の追記 ──────────────────────────────────────────────────


def _complete(result: Any, error: BaseException | None = None) -> None:
    loop = asyncio.new_event_loop()
    try:
        surface_video_followup._complete(
            result,
            error,
            False,
            keyword=shape.KEYWORD,
            destination=DEST,
            request_id="r-video",
            started=time.perf_counter(),
            user_email=None,
            usage_user_id=None,
            fallback_user_id=USER_ID,
            record_usage=lambda **kw: None,
            loop=loop,
            cache_key=None,
        )
    finally:
        loop.close()


def test_b_completion_passes_blocks(slack: _Slack) -> None:
    result = shape.b_output()
    _complete(result)
    expected = followup_message(result)
    assert expected is not None
    assert slack.posts == [{"channel": DM, "text": expected.text, "blocks": expected.blocks}]


def test_b_failure_is_text_only(slack: _Slack) -> None:
    _complete(None, RuntimeError("MEDIA_ACQUIRE_JOB_FAILED: boom"))
    assert len(slack.posts) == 1 and slack.posts[0]["blocks"] is None
    assert "動画の取得・変換で一時的な不具合" in slack.posts[0]["text"]


def test_b_render_failure_falls_back_to_the_text_post(
    monkeypatch: pytest.MonkeyPatch, slack: _Slack
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("render bug")

    monkeypatch.setattr(surface_render, "followup_message", boom)
    result = shape.b_output()
    _complete(result)
    assert slack.posts == [{"channel": DM, "text": _as_text(result.slack_text), "blocks": None}]


async def test_b_reused_post_passes_reused_blocks(
    monkeypatch: pytest.MonkeyPatch, slack: _Slack
) -> None:
    monkeypatch.setattr(surface_video_followup, "REUSE_POST_DELAY_S", 0.0)
    result = shape.b_output()
    entry = surface_video_followup.CachedFollowup(
        slack_text=result.slack_text, report_url=result.report_url, stored_at=0.0, source=result
    )
    surface_video_followup._post_reused_later(
        entry, destination=DEST, request_id="r-video", fallback_user_id=USER_ID
    )
    deadline = time.monotonic() + 5
    while not slack.posts and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    expected = followup_message(result, reused=True)
    assert expected is not None
    assert slack.posts == [{"channel": DM, "text": expected.text, "blocks": expected.blocks}]
    assert slack.posts[0]["blocks"][1]["elements"][0]["text"] == REUSED_NOTE


async def test_b_reused_post_without_source_is_text_only(
    monkeypatch: pytest.MonkeyPatch, slack: _Slack
) -> None:
    monkeypatch.setattr(surface_video_followup, "REUSE_POST_DELAY_S", 0.0)
    entry = surface_video_followup.CachedFollowup(
        slack_text="**上位5本の動画の中身**\n_概算 $0.1000_", report_url=None, stored_at=0.0
    )
    surface_video_followup._post_reused_later(
        entry, destination=DEST, request_id="r-video", fallback_user_id=USER_ID
    )
    deadline = time.monotonic() + 5
    while not slack.posts and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert slack.posts[0]["blocks"] is None
    assert slack.posts[0]["text"].startswith(surface_video_followup.REUSED_PREFIX)


# ── C: 動画分析の完了（dispatch_tool の切り離し経由）──────────────────────


class _SlowAlgorithm(BaseSkill[VideoAlgorithmInput, VideoAlgorithmOutput]):
    """本物の出力（VideoAlgorithmOutput）を、切り離された後に返す video_algorithm の代役。"""

    name: ClassVar[str] = "video_algorithm"
    description: ClassVar[str] = "fake"
    input_schema: ClassVar[type[BaseModel]] = VideoAlgorithmInput
    output_schema: ClassVar[type[BaseModel]] = VideoAlgorithmOutput

    def __init__(self) -> None:
        self.release = threading.Event()

    def run(self, input: VideoAlgorithmInput, ctx: SkillContext) -> VideoAlgorithmOutput:
        assert self.release.wait(10)
        return shape.c_output()


def _detach_on(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from tests.mcp_gateway.test_video_algorithm_detach import _policy

    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    monkeypatch.setattr(detached_jobs, "REGISTRY", detached_jobs.DetachedJobRegistry())
    for name in ("ENABLE_PROGRESS_NOTIFY", "USE_PAYLOAD_OFFLOAD", "VIDEO_QUOTA_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "_record_usage", lambda **kw: records.append(kw))
    return records


async def _run_detached(skill: _SlowAlgorithm) -> dict[str, Any]:
    from tests.mcp_gateway.test_video_algorithm_detach import _call, _spec

    out = await _call(_spec(skill), {"query": shape.KEYWORD})
    skill.release.set()
    return out


async def test_c_completion_passes_blocks(monkeypatch: pytest.MonkeyPatch, slack: _Slack) -> None:
    usage = _detach_on(monkeypatch)
    out = await _run_detached(_SlowAlgorithm())
    assert out["status"] == "running"
    deadline = time.monotonic() + 5
    while not (slack.posts and usage) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    expected = completion_message(shape.c_output())
    assert expected is not None
    assert slack.posts == [{"channel": DM, "text": expected.text, "blocks": expected.blocks}]


async def test_c_render_failure_falls_back_to_the_text_post(
    monkeypatch: pytest.MonkeyPatch, slack: _Slack
) -> None:
    usage = _detach_on(monkeypatch)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("render bug")

    monkeypatch.setattr(algo_render, "completion_message", boom)
    await _run_detached(_SlowAlgorithm())
    deadline = time.monotonic() + 5
    while not (slack.posts and usage) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    text = detached_jobs.slack_escape(
        detached_jobs.completion_text(shape.c_output(), shape.KEYWORD)
    )
    assert slack.posts == [{"channel": DM, "text": text, "blocks": None}]


def test_completion_message_is_none_for_non_video_algorithm_outputs() -> None:
    class _Other(BaseModel):
        slack_summary: str = "x"

    assert detached_jobs.completion_message(_Other(), request_id="r") is None
    assert detached_jobs.completion_message(shape.c_output(), request_id="r") is not None
