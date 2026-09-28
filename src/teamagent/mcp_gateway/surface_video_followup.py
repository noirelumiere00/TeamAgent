"""検索上位チェック（search_surface_check）の 2 段目: 上位の動画の中身を裏で分析して追記する。

背景（2026-09-28 小俣さんの要望と裁定「2 段構えで自動」）: 検索上位チェックは約 2 分で結論を返す。
上位の動画は動画そのものを見ないと、フック・テロップ・構成・CTA というショート動画の評価軸で
語れない。そこで 1 段目は今どおり返し、**同じ上位 N 本**（検索し直さない）を video_algorithm の
分析エンジンで裏で分析して、終わったら（5〜9 分後）同じ会話に追記し、章を足したレポートを届ける。

仕組み（video_algorithm の切り離し＝detached_jobs.py の部品をそのまま使う）:
- gateway（``server.dispatch_tool``）が search_surface_check の skill.run が返った直後に
  ``maybe_schedule`` を呼ぶ。対象なら 2 段目のジョブを**同じ登録簿**（``detached_jobs.REGISTRY``）へ
  登録してすぐ切り離し、1 段目の slack_summary の末尾（レポート行の前）に予告を 1 行足す。
  対象外なら何もしない（1 段目は今と完全に同じ）。
- 宛先は署名検証済み claim から作る（``detached_jobs.destination_from_claim``）。DM だけ。
- ジョブは登録簿の thread で ``SearchSurfaceCheckSkill.run_video_followup`` を走らせ、完了したら
  ``post_to_origin``（検証済み宛先・2 回まで・DM 退避・エスケープ）で追記する。
- 同時実行の上限と順番待ちは video_algorithm の切り離しと**共有**する
  （VIDEO_ALGORITHM_MAX_BACKGROUND 既定 2・待ち 10 件）。どちらも mcp の同じタスクで media job と
  Gemini を使う重い処理なので、別枠にすると同時に 4 本走ってメモリと Gemini の 429 を踏む。
- 再デプロイ時は ``notify_interrupted`` がこのジョブの宛先にも中断文を送る（登録時に文を渡す）。
- 二重依頼（同じ人・同じ KW の 2 段目が走っている）は登録しない。

段階公開のための env（TD の env で変えられる）:
- ``USE_SURFACE_VIDEO_FOLLOWUP``: 既定 OFF＝今と完全に同じ。
- ``SURFACE_VIDEO_FOLLOWUP_ALLOWED_EMAILS``: カンマ区切り。**空なら誰にも適用しない**。
- ``SURFACE_VIDEO_FOLLOWUP_MAX_VIDEOS``: 既定 5（1〜10 に丸める）。
  動画分析の月間上限を 1 回で最大この本数使う。

利用者向けの文には内部語（job_id・error_code・S3 URL・ツール名）を出さない。
"""

from __future__ import annotations

import asyncio
import functools
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.mcp_gateway import detached_jobs
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn
from teamagent.skills.base import SkillContext

logger = structlog.get_logger(__name__)

TOOL = "search_surface_check"
# usage_events と登録簿の tool 名（1 段目の search_surface_check と分けて数える）。
USAGE_SKILL = "search_surface_check_video"

ENABLED_ENV = "USE_SURFACE_VIDEO_FOLLOWUP"
ALLOWED_EMAILS_ENV = "SURFACE_VIDEO_FOLLOWUP_ALLOWED_EMAILS"
MAX_VIDEOS_ENV = "SURFACE_VIDEO_FOLLOWUP_MAX_VIDEOS"

DEFAULT_MAX_VIDEOS = 5
MIN_MAX_VIDEOS = 1
MAX_MAX_VIDEOS = 10

# 登録簿のキーの接頭辞（video_algorithm の切り離しのキーと衝突させない）。
_KEY_PREFIX = "surface_video"


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes"}


def _max_videos_from_env() -> int:
    raw = os.environ.get(MAX_VIDEOS_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_VIDEOS
    except ValueError:
        value = DEFAULT_MAX_VIDEOS
    return min(MAX_MAX_VIDEOS, max(MIN_MAX_VIDEOS, value))


@dataclass(frozen=True)
class FollowupPolicy:
    """2 段目を使うかどうかの決まり（env から読む）。既定は「使わない」。"""

    enabled: bool = False
    allowed_emails: frozenset[str] = frozenset()
    max_videos: int = DEFAULT_MAX_VIDEOS

    @classmethod
    def from_env(cls) -> FollowupPolicy:
        return cls(
            enabled=_truthy(os.environ.get(ENABLED_ENV)),
            allowed_emails=frozenset(
                e.strip().lower()
                for e in os.environ.get(ALLOWED_EMAILS_ENV, "").split(",")
                if e.strip()
            ),
            max_videos=_max_videos_from_env(),
        )


def load_policy() -> FollowupPolicy:
    """呼び出しごとに env を読む（TD 差し替えだけで段階を進められる。テストの差し替え口）。"""
    return FollowupPolicy.from_env()


def decide(
    policy: FollowupPolicy, *, verified_caller: Any, metadata: dict[str, Any]
) -> tuple[detached_jobs.Destination | None, str]:
    """2 段目を始めてよいかを決める。``(宛先, 理由)``。宛先が None なら始めない（今と同じ）。

    video_algorithm の切り離し（detached_jobs.decide）と同じ考え方: LEGACY（claim 無し）・
    未検証・allowlist 外（空は全員拒否）・宛先が決まらない・DM 以外は始めない。
    """
    if not policy.enabled:
        return None, "disabled"
    if verified_caller is None:
        return None, "legacy"
    if metadata.get("identity_verified") is not True:
        return None, "unverified"
    email = metadata.get("user_email")
    if not isinstance(email, str) or email.strip().lower() not in policy.allowed_emails:
        return None, "not_allowed"
    destination = detached_jobs.destination_from_claim(verified_caller)
    if destination is None:
        return None, "no_destination"
    if not destination.is_dm:
        return None, "not_dm"
    return destination, "ok"


def followup_key(slack_user_id: str, keyword: str) -> str:
    return f"{_KEY_PREFIX}\x1f" + detached_jobs.inflight_key(slack_user_id, keyword)


# ── 利用者向けの文（内部語を出さない）──────────────────────────────────────


def interrupted_text(keyword: str) -> str:
    return (
        f"「{keyword}」上位の動画の中身の分析は、システム更新で中断されました。"
        "お手数ですが、検索上位チェックをもう一度依頼してください。"
    )


def failure_text(keyword: str, error: BaseException) -> str:
    return f"「{keyword}」上位の動画の中身: {detached_jobs.user_message_for_error(error)}"


def busy_line() -> str:
    return (
        "上位の動画の中身の分析は、いま混み合っているため今回は行いませんでした"
        "（動画分析の回数は使っていません）"
    )


def in_progress_line(*, same_conversation: bool) -> str:
    where = "この会話" if same_conversation else "最初にご依頼いただいた会話"
    return f"上位の動画の中身は、前のご依頼の分を分析中です。終わったら{where}に追記します"


_QUEUED_SUFFIX = "。混み合っているため、順番待ちのあと始めます"


# ── 完了処理（ジョブの thread で走る）──────────────────────────────────────


def _complete(
    result: Any,
    error: BaseException | None,
    interrupted: bool,
    *,
    keyword: str,
    destination: detached_jobs.Destination,
    request_id: str,
    started: float,
    user_email: str | None,
    usage_user_id: str | None,
    fallback_user_id: str | None,
    record_usage: Callable[..., None],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """追記の投稿 → usage 記録。順番待ちのまま中断したら（run 前）中断文だけ送る。"""
    if isinstance(error, detached_jobs.DetachInterruptedError):
        logger.warning("surface_video_followup_queued_interrupted", request_id=request_id)
        if not interrupted:
            detached_jobs.post_to_origin(
                interrupted_text(keyword), destination, request_id=request_id
            )
        return
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    delivered = False
    if interrupted:
        # 中断文を送った後に完了した分は、矛盾する 2 通目を出さない。
        logger.warning("surface_video_followup_done_after_interrupt", request_id=request_id)
    else:
        if error is not None:
            text = failure_text(keyword, error)
        else:
            text = str(getattr(result, "slack_text", "") or "")
        if text:
            delivered = detached_jobs.post_to_origin(
                markdown_bold_to_mrkdwn(text),
                destination,
                request_id=request_id,
                fallback_user_id=fallback_user_id,
            )
    cost = float(getattr(result, "total_cost_usd", 0.0) or 0.0) if result is not None else 0.0
    status = "ok" if error is None else "error"
    logger.info(
        "surface_video_followup_finished",
        request_id=request_id,
        latency_ms=elapsed_ms,
        tool_cost_usd=cost,
        followup_status=getattr(result, "status", None),
        delivered=delivered,
        status=status,
        error=None if error is None else type(error).__name__,
    )
    record = functools.partial(
        record_usage,
        request_id=request_id,
        skill=USAGE_SKILL,
        user_email=user_email,
        user_id=usage_user_id,
        cost_usd=cost,
        latency_ms=elapsed_ms,
        skill_args={},
        status=status,
        error_code=None if error is None else type(error).__name__,
    )
    try:
        loop.call_soon_threadsafe(record)
    except RuntimeError:
        logger.warning("usage_event_schedule_failed", request_id=request_id, error="LoopClosed")


# ── 登録（gateway から呼ぶ）─────────────────────────────────────────────


def maybe_schedule(
    *,
    skill: Any,
    output: Any,
    skill_input: Any,
    ctx: SkillContext,
    verified_caller: Any,
    metadata: dict[str, Any],
    usage_user_id: str | None,
    record_usage: Callable[..., None],
    loop: asyncio.AbstractEventLoop,
) -> str:
    """対象なら 2 段目を登録し、1 段目の slack_summary に 1 行足す。理由コードを返す。

    例外は外へ出さない（2 段目の不具合で 1 段目の返却を落とさない）。
    """
    try:
        return _maybe_schedule(
            skill=skill,
            output=output,
            skill_input=skill_input,
            ctx=ctx,
            verified_caller=verified_caller,
            metadata=metadata,
            usage_user_id=usage_user_id,
            record_usage=record_usage,
            loop=loop,
        )
    except Exception as exc:
        logger.warning(
            "surface_video_followup_schedule_failed",
            request_id=ctx.request_id,
            error=type(exc).__name__,
        )
        return "error"


def _maybe_schedule(
    *,
    skill: Any,
    output: Any,
    skill_input: Any,
    ctx: SkillContext,
    verified_caller: Any,
    metadata: dict[str, Any],
    usage_user_id: str | None,
    record_usage: Callable[..., None],
    loop: asyncio.AbstractEventLoop,
) -> str:
    from teamagent.skills.search_surface_check.schema import (
        SearchSurfaceCheckInput,
        SearchSurfaceCheckOutput,
    )
    from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
    from teamagent.skills.search_surface_check.summary import (
        followup_notice_line,
        insert_before_report_line,
    )
    from teamagent.skills.search_surface_check.video_digest import select_followup_videos

    if not (
        isinstance(skill, SearchSurfaceCheckSkill)
        and isinstance(output, SearchSurfaceCheckOutput)
        and isinstance(skill_input, SearchSurfaceCheckInput)
    ):
        return "not_applicable"
    policy = load_policy()
    destination, reason = decide(policy, verified_caller=verified_caller, metadata=metadata)
    if destination is None:
        logger.info("surface_video_followup_decision", request_id=ctx.request_id, reason=reason)
        return reason
    videos = select_followup_videos(output, policy.max_videos)
    if not videos:
        logger.info(
            "surface_video_followup_decision", request_id=ctx.request_id, reason="no_videos"
        )
        return "no_videos"
    shared = detached_jobs.load_policy()  # 同時実行の上限・待ち行列は動画分析の切り離しと共有
    if shared.max_background <= 0:
        logger.info("surface_video_followup_decision", request_id=ctx.request_id, reason="capacity")
        return "capacity"
    registry = detached_jobs.REGISTRY
    keyword = videos[0].keyword
    slack_user_id = str(verified_caller.slack_user_id)
    job_ctx = SkillContext(
        request_id=f"{ctx.request_id}-video",
        user_id=ctx.user_id,
        metadata=dict(ctx.metadata),
    )
    will_wait = registry.active_count() >= shared.max_background
    job, state = registry.start(
        key=followup_key(slack_user_id, keyword),
        max_background=shared.max_background,
        max_queued=shared.max_queued,
        tool=USAGE_SKILL,
        query=keyword,
        request_id=job_ctx.request_id,
        destination=destination,
        target=functools.partial(
            skill.run_video_followup, output, skill_input, job_ctx, videos=videos
        ),
        on_detached_done=functools.partial(
            _complete,
            keyword=keyword,
            destination=destination,
            request_id=job_ctx.request_id,
            started=time.perf_counter(),
            user_email=metadata.get("user_email"),
            usage_user_id=usage_user_id,
            fallback_user_id=slack_user_id,
            record_usage=record_usage,
            loop=loop,
        ),
        interrupted_message=interrupted_text(keyword),
    )
    line: str | None
    if state == "duplicate" and job is not None:
        same = job.destination.channel_id == destination.channel_id
        line = in_progress_line(same_conversation=same)
    elif state == "busy":
        line = busy_line()
    elif state == "started" and job is not None:
        detach_state = job.detach()
        if detach_state == detached_jobs.DETACH_INTERRUPTED:
            state, line = "closing", None  # 終了処理中＝届けられない約束はしない
        else:
            if detach_state == detached_jobs.DETACH_DONE:
                job.deliver_in_background()
            line = followup_notice_line(len(videos)) + (_QUEUED_SUFFIX if will_wait else "")
    else:
        line = None  # closing（終了処理中）
    if line:
        output.slack_summary = insert_before_report_line(output.slack_summary, line)
    logger.info(
        "surface_video_followup_decision",
        request_id=ctx.request_id,
        reason=state,
        videos=len(videos),
        queued=will_wait,
    )
    return state


__all__ = [
    "ALLOWED_EMAILS_ENV",
    "ENABLED_ENV",
    "MAX_VIDEOS_ENV",
    "TOOL",
    "USAGE_SKILL",
    "FollowupPolicy",
    "busy_line",
    "decide",
    "failure_text",
    "followup_key",
    "in_progress_line",
    "interrupted_text",
    "load_policy",
    "maybe_schedule",
]
