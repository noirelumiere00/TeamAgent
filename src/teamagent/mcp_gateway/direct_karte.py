"""カルテの完成した answer を、書き直さず検証済み DM へ届ける（既定 OFF）。"""

from __future__ import annotations

from typing import Any

import structlog
from mcp.types import TextContent

from teamagent.mcp_gateway import detached_jobs, direct_summary
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn, outbound_permanent_urls

logger = structlog.get_logger(__name__)
ENABLED_ENV = "USE_DIRECT_KARTE_POST"
ALLOWED_EMAILS_ENV = "DIRECT_KARTE_POST_ALLOWED_EMAILS"


def load_policy() -> direct_summary.DirectPolicy:
    return direct_summary.DirectPolicy.from_env(
        enabled_env=ENABLED_ENV, allowed_emails_env=ALLOWED_EMAILS_ENV
    )


def maybe_post(
    data: dict[str, Any],
    *,
    verified_caller: Any,
    metadata: dict[str, Any],
    request_id: str,
) -> list[TextContent] | None:
    """成功・到達不明なら本文をモデルへ渡さず、対象外・失敗なら従来の返却を続ける。"""
    destination, reason = direct_summary.decide_destination(
        load_policy(), verified_caller=verified_caller, metadata=metadata
    )
    logger.info("direct_karte_decision", request_id=request_id, reason=reason)
    answer = data.get("answer")
    if destination is None or not isinstance(answer, str) or not answer.strip():
        return None
    try:
        status = detached_jobs.post_to_origin_status(
            markdown_bold_to_mrkdwn(answer),
            destination,
            request_id=request_id,
            permanent_urls=outbound_permanent_urls(data),
        )
    except Exception:
        # 例外に本文・URL が含まれていてもログへ出さず、元の回答を保持する。
        status = direct_summary.FAILED
    logger.info("direct_karte_posted", request_id=request_id, status=status)
    if status in {direct_summary.POSTED, direct_summary.UNCERTAIN}:
        return direct_summary.posted_response(status)
    return None
