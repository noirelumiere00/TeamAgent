"""非同期 job の完了を、submit 元の Slack 会話へ通知する。"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import structlog

from teamagent.adapters.slack_client import SlackClient
from teamagent.mcp_gateway.progress_notify import _resolve_channel

logger = structlog.get_logger(__name__)

_INITIAL_DELAY_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 30.0
_TIMEOUT_SECONDS = 15 * 60.0
_TERMINAL_STATUSES = frozenset({"done", "failed"})
_KNOWN_STATUSES = frozenset({"queued", "running", "done", "failed", "unknown"})
_STATUS_TOOLS = {
    "tiktok_acquire": "tiktok_acquire_status",
    "proposal_builder_submit": "proposal_builder_status",
}


def enabled() -> bool:
    """USE_ASYNC_JOB_NOTIFY=1/true/yes のときだけ通知を有効にする。"""
    return os.environ.get("USE_ASYNC_JOB_NOTIFY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def schedule_completion_notice(
    *,
    tool: str,
    job_id: str,
    user_context: dict[str, Any] | None,
    request_id: str,
    poll: Callable[[], tuple[str, str]],
) -> None:
    """job 完了待ちを daemon thread に積む。通知障害は呼び出し元へ伝播させない。"""
    if not enabled():
        return
    raw = dict(user_context or {})
    try:
        thread = threading.Thread(
            target=lambda: _run_completion_notice(
                tool=tool,
                job_id=job_id,
                user_context=raw,
                request_id=request_id,
                poll=poll,
            ),
            name=f"async-job-notify-{tool}-{job_id}",
            daemon=True,
        )
        thread.start()
    except Exception as exc:
        logger.warning(
            "async_job_notify_schedule_failed",
            tool=tool,
            job_id=job_id,
            request_id=request_id,
            error=type(exc).__name__,
        )


def _run_completion_notice(
    *,
    tool: str,
    job_id: str,
    user_context: dict[str, Any],
    request_id: str,
    poll: Callable[[], tuple[str, str]],
) -> None:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    try:
        _wait_until_next_poll(deadline, _INITIAL_DELAY_SECONDS)
        while time.monotonic() < deadline:
            try:
                status, message = poll()
            except Exception as exc:
                logger.warning(
                    "async_job_notify_poll_failed",
                    tool=tool,
                    job_id=job_id,
                    request_id=request_id,
                    error=type(exc).__name__,
                )
            else:
                if status not in _KNOWN_STATUSES:
                    logger.warning(
                        "async_job_notify_unknown_status",
                        tool=tool,
                        job_id=job_id,
                        request_id=request_id,
                        status=status,
                    )
                if status in _TERMINAL_STATUSES:
                    _post_notice(
                        message,
                        user_context=user_context,
                        request_id=request_id,
                    )
                    return
            _wait_until_next_poll(deadline, _POLL_INTERVAL_SECONDS)

        status_tool = _STATUS_TOOLS.get(tool, f"{tool}_status")
        _post_notice(
            (
                f"{tool}（job_id={job_id}）はまだ完了していません。"
                f"`{status_tool}` で確認してください。"
            ),
            user_context=user_context,
            request_id=request_id,
        )
    except Exception as exc:
        logger.warning(
            "async_job_notify_failed",
            tool=tool,
            job_id=job_id,
            request_id=request_id,
            error=type(exc).__name__,
        )


def _wait_until_next_poll(deadline: float, interval_seconds: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        threading.Event().wait(min(interval_seconds, remaining))


def _post_notice(
    message: str,
    *,
    user_context: dict[str, Any],
    request_id: str,
) -> None:
    async def _send() -> None:
        slack = SlackClient.from_env()
        channel = await _resolve_channel(slack, user_context, request_id)
        if not channel:
            return
        thread_ts = user_context.get("thread_ts")
        thread_ts = thread_ts if isinstance(thread_ts, str) and thread_ts else None
        await slack.post_message(
            channel=channel,
            text=message,
            request_id=request_id,
            thread_ts=thread_ts,
        )

    try:
        asyncio.run(_send())
    except Exception as exc:
        logger.warning(
            "async_job_notify_post_failed",
            request_id=request_id,
            error=type(exc).__name__,
        )
