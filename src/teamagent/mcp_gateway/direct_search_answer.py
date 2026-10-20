"""Search のコードで確定した回答を検証済み DM へ届ける（既定 OFF）。"""

from __future__ import annotations

import os
import re
from html import unescape
from typing import Any

import structlog

from teamagent.mcp_gateway import detached_jobs, direct_summary, direct_video_metrics
from teamagent.skills._shared.slack_blocks import RichMessage, render_or_none, validate
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn

logger = structlog.get_logger(__name__)
ENABLED_ENV = "SEARCH_DIRECT_ANSWER"
ALLOWED_EMAILS_ENV = "SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS"
CTX_KEY = "_search_direct_answer"
KINDS_ENV = "SEARCH_DIRECT_ANSWER_KINDS"
KINDS_CTX_KEY = "_search_direct_answer_kinds"
KINDS = frozenset({"not_found", "comparison", "list_fast", "video_metrics"})
POSTED_TEXT = "回答はこの会話に投稿済みです。同じ内容の要約・補足は書かないでください。"
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\(<?(https?://[^\s<>)]+)>?\)")
_CASE_LINK = re.compile(r"<(?P<url>https://[^\s<>|]+)\|(?P<label>資料を見る|会話の記録)>")


def load_policy() -> direct_summary.DirectPolicy:
    return direct_summary.DirectPolicy.from_env(
        enabled_env=ENABLED_ENV, allowed_emails_env=ALLOWED_EMAILS_ENV
    )


def allowed_kinds() -> frozenset[str]:
    kinds = frozenset(
        value.strip() for value in os.environ.get(KINDS_ENV, "").split(",") if value.strip()
    )
    return kinds if kinds <= KINDS else frozenset()


def _case_rich_message(text: str) -> RichMessage | None:
    from teamagent.skills.search.case_list import case_link

    parts: list[str] = []
    cursor = 0
    for match in _CASE_LINK.finditer(text):
        link = case_link(unescape(match["url"]), match["label"])
        if link is None:
            continue
        parts.extend((detached_jobs.slack_escape(text[cursor : match.start()]), link))
        cursor = match.end()
    if not parts:
        return None
    parts.append(detached_jobs.slack_escape(text[cursor:]))
    safe = "".join(parts)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": safe, "verbatim": True}}]
    validate(blocks)
    return RichMessage(text=safe, blocks=blocks)


def deliver(
    answer: str,
    kind: str,
    destination: detached_jobs.Destination,
    *,
    request_id: str,
    allow_case_links: bool = False,
) -> dict[str, Any] | None:
    """投稿成功だけを抑止の根拠にする。失敗時は元のツール結果を返す。"""
    if kind not in allowed_kinds() or not answer.strip():
        return None
    # URL を Markdown の括弧から外し、共通の Slack エスケープを 1 回だけ掛ける。
    text = markdown_bold_to_mrkdwn(_MARKDOWN_LINK.sub(r"\1 \2", answer))
    rich = (
        render_or_none(
            lambda: _case_rich_message(text), request_id=request_id, kind="search_case_list"
        )
        if allow_case_links and kind == "list_fast"
        else None
    )
    if rich is not None:
        from teamagent.skills.search.case_list import case_link

        # Block Kit が拒否された再試行も、既存の安全な裸URL経路で開ける形にする。
        text = _CASE_LINK.sub(
            lambda m: (
                f"{m['label']} {unescape(m['url'])}"
                if case_link(unescape(m["url"]), m["label"])
                else m[0]
            ),
            text,
        )
    try:
        status = detached_jobs.post_to_origin_status(
            text, destination, request_id=request_id, rich=rich
        )
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
