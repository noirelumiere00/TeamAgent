"""search の機械生成した動画数字の節を、改稿せず検証済み DM へ届ける。

USE_DIRECT_VIDEO_METRICS_POST は既定 OFF。DIRECT_VIDEO_METRICS_POST_ALLOWED_EMAILS は
空なら全員拒否、* なら本人確認済みの全員。検索上位チェックの env とは独立している。
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from teamagent.mcp_gateway import detached_jobs, direct_summary
from teamagent.skills.search.campaign_files import VIDEO_METRICS_HEADING

logger = structlog.get_logger(__name__)
ENABLED_ENV = "USE_DIRECT_VIDEO_METRICS_POST"
ALLOWED_EMAILS_ENV = "DIRECT_VIDEO_METRICS_POST_ALLOWED_EMAILS"
POSTED_TEXT = "📊 動画ごとの数字は、この会話に別の投稿でお届けしました（書き直さない）"


def load_policy() -> direct_summary.DirectPolicy:
    return direct_summary.DirectPolicy.from_env(
        enabled_env=ENABLED_ENV, allowed_emails_env=ALLOWED_EMAILS_ENV
    )


def extract_section(answer: str) -> tuple[str, str, str] | None:
    """見出し行から節末までを無加工で切り出す（前文・節・後文）。

    施策間の 1 空行は保持。2 空行、📎 の見出し、検索の範囲説明は次の節とする。
    区切りの改行は後文に残すので、置換時にも他の節の文字列が変わらない。
    """
    heading = re.search(rf"^{re.escape(VIDEO_METRICS_HEADING)}(?=\n|$)", answer, re.MULTILINE)
    if heading is None:
        return None
    boundary = re.search(r"\n{3,}|\n+(?=📎|探した範囲[:：])", answer[heading.end() :])
    end = heading.end() + boundary.start() if boundary else len(answer)
    return answer[: heading.start()], answer[heading.start() : end], answer[end:]


def maybe_post(
    data: dict[str, Any], *, verified_caller: Any, metadata: dict[str, Any], request_id: str
) -> None:
    """投稿成功・到達不明なら answer の節だけ置換。失敗・対象外は元の結果を保持。"""
    answer = data.get("answer")
    section = extract_section(answer) if isinstance(answer, str) else None
    outcome, reason = "skipped", "no_section"
    chars = len(section[1]) if section else 0
    if section is not None:
        destination, reason = direct_summary.decide_destination(
            load_policy(), verified_caller=verified_caller, metadata=metadata
        )
        if destination is not None:
            try:
                # 無加工の節を渡す。Slack 用エスケープは投稿関数の中で 1 回だけ行う。
                outcome = detached_jobs.post_to_origin_status(
                    section[1], destination, request_id=request_id
                )
            except Exception:
                # 例外の本文（URL 等）はログに出さず、結果を失わない。
                outcome, reason = "failed", "post_exception"
            else:
                reason = "post_status"
            if outcome in {direct_summary.POSTED, direct_summary.UNCERTAIN}:
                data["answer"] = section[0] + POSTED_TEXT + section[2]
    logger.info(
        "search_video_metrics_direct_post",
        request_id=request_id,
        outcome=outcome,
        reason=reason,
        section_chars=chars,
    )
