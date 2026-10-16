"""Search のコードで確定した回答を検証済み DM へ届ける（既定 OFF）。"""

from __future__ import annotations

import re
from typing import Any

import structlog

from teamagent.mcp_gateway import detached_jobs, direct_summary, direct_video_metrics
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn

logger = structlog.get_logger(__name__)
ENABLED_ENV = "SEARCH_DIRECT_ANSWER"
ALLOWED_EMAILS_ENV = "SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS"
CTX_KEY = "_search_direct_answer"
KINDS = frozenset({"not_found", "comparison", "list_fast", "video_metrics"})
POSTED_TEXT = "回答はこの会話に投稿済みです。同じ内容の要約・補足は書かないでください。"
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\(<?(https?://[^\s<>)]+)>?\)")


def load_policy() -> direct_summary.DirectPolicy:
    return direct_summary.DirectPolicy.from_env(
        enabled_env=ENABLED_ENV, allowed_emails_env=ALLOWED_EMAILS_ENV
    )


def deliver(
    answer: str,
    kind: str,
    destination: detached_jobs.Destination,
    *,
    request_id: str,
) -> dict[str, Any] | None:
    """投稿成功だけを抑止の根拠にする。失敗時は元のツール結果を返す。"""
    if kind not in KINDS or not answer.strip():
        return None
    # URL を Markdown の括弧から外し、共通の Slack エスケープを 1 回だけ掛ける。
    text = markdown_bold_to_mrkdwn(_MARKDOWN_LINK.sub(r"\1 \2", answer))
    try:
        status = detached_jobs.post_to_origin_status(text, destination, request_id=request_id)
    except Exception:
        status = direct_summary.FAILED
    logger.info("search_direct_answer_post", request_id=request_id, kind=kind, outcome=status)
    if status != direct_summary.POSTED:
        return {"delivery": {"kind": "search_direct_answer", "answer_kind": kind, "status": status}}
    payload: dict[str, Any] = {
        "status": "posted",
        "delivered": True,
        "delivery": {"kind": "search_direct_answer", "answer_kind": kind},
        "answer": POSTED_TEXT,
    }
    if section := direct_video_metrics.extract_section(answer):
        payload["video_metrics_evidence"] = direct_video_metrics.metrics_evidence(section[1])
    return payload
