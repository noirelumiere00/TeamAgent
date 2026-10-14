"""search の機械生成した動画数字の節を、改稿せず検証済み DM へ届ける。

USE_DIRECT_VIDEO_METRICS_POST は既定 OFF。DIRECT_VIDEO_METRICS_POST_ALLOWED_EMAILS は
空なら全員拒否、* なら本人確認済みの全員。検索上位チェックの env とは独立している。
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from teamagent.mcp_gateway import detached_jobs, direct_summary
from teamagent.skills.search.campaign_files import VIDEO_METRICS_HEADING, strip_video_numbers

logger = structlog.get_logger(__name__)
ENABLED_ENV = "USE_DIRECT_VIDEO_METRICS_POST"
ALLOWED_EMAILS_ENV = "DIRECT_VIDEO_METRICS_POST_ALLOWED_EMAILS"
# 利用者に見えても自然な文で、Aico には「説明で数字を並べ直さない」ことも伝える。
# 10-13 の DM 実機で、別の投稿の後に Aico が同じ数字を丸めて並べ直した（重複）。
POSTED_TEXT = (
    "📊 動画ごとの数字（再生・保存・リンク）は、この会話に別の投稿でお届けしました。"
    "説明では数字を並べ直しません。"
)
# 📊 の節の後ろに来うる見出し（行頭）。ここに無い行は、1 空行の後でも 📊 の節の続き（次の施策）。
# 絵文字は異体字セレクタ（U+FE0F）を付けずに書く。前方一致なので「ℹ️」「🗂️」にも当たる。
# 10-14 の穴①: "\n\n" でつながる ℹ️・🔒 の状態行（search の status_note）が 📊 の節に
# 巻き込まれ、資料リンクの節が無い回に DM への直接投稿の側へ移っていた。
NEXT_SECTION_MARKS = (
    "📎",
    "📁",
    "🏆",
    "🤝",
    "🗂",
    "💬",
    "🗃",
    "🔎",
    "🌐",
    "ℹ",
    "🔒",
    "🕸",
    "探した範囲",
)
_SECTION_END_RE = re.compile(
    r"\n{3,}|\n+(?=" + "|".join(re.escape(mark) for mark in NEXT_SECTION_MARKS) + ")"
)


def load_policy() -> direct_summary.DirectPolicy:
    return direct_summary.DirectPolicy.from_env(
        enabled_env=ENABLED_ENV, allowed_emails_env=ALLOWED_EMAILS_ENV
    )


def extract_section(answer: str) -> tuple[str, str, str] | None:
    """見出し行から節末までを無加工で切り出す（前文・節・後文）。

    施策間の 1 空行は保持。2 空行と、行頭が ``NEXT_SECTION_MARKS``（📎・ℹ️・🔒・探した範囲
    など見出しの許可リスト）の行は次の節とする。
    区切りの改行は後文に残すので、置換時にも他の節の文字列が変わらない。
    """
    heading = re.search(rf"^{re.escape(VIDEO_METRICS_HEADING)}(?=\n|$)", answer, re.MULTILINE)
    if heading is None:
        return None
    boundary = _SECTION_END_RE.search(answer[heading.end() :])
    end = heading.end() + boundary.start() if boundary else len(answer)
    return answer[: heading.start()], answer[heading.start() : end], answer[end:]


def _strip_hit_numbers(data: dict[str, Any], posted: str) -> None:
    """モデルへ返す hits の本文からも、投稿した節に載った動画の数字を抜く。

    answer の置換だけでは並べ直された。

    10-14 の DM 実機（r55）で、answer の節を置き換えても、モデルが hits の本文
    （施策実績の上位投稿の行）から再生・保存を拾って並べ直した。
    """
    hits = data.get("hits")
    if not isinstance(hits, list):
        return
    for hit in hits:
        if isinstance(hit, dict) and isinstance(hit.get("content"), str):
            hit["content"] = strip_video_numbers(hit["content"], posted)


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
                _strip_hit_numbers(data, section[1])
    logger.info(
        "search_video_metrics_direct_post",
        request_id=request_id,
        outcome=outcome,
        reason=reason,
        section_chars=chars,
    )
