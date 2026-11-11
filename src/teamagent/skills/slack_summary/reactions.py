"""history / replies のリアクションを LLM なしで一覧にする。"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

import structlog

from teamagent.adapters.slack_user_reader import SlackThreadRead
from teamagent.skills._shared.slack_context import _neutralize
from teamagent.skills._shared.source_url import slack_permalink
from teamagent.skills.slack_summary.period import JST
from teamagent.skills.slack_summary.schema import SlackSummaryOutput

logger = structlog.get_logger(__name__)

MAX_POSTS = 20
MAX_USERS = 200
_UNKNOWN_NAME = "（名前を取得できない人）"
# 数字を含む 9 字以上だけを ID とみなす（「UGC」「WEB」のような普通の語は残す）。
_USER_ID = re.compile(r"(?<![A-Za-z0-9_])[UW](?=[A-Z0-9]*[0-9])[A-Z0-9]{8,}(?![A-Za-z0-9_])")


_ACTION_WORDS = ("押し", "つけ", "付け", "反応し", "誰", "した人", "ユーザー", "一覧", "リスト")


def wants_reactions(intent: str) -> bool:
    # 「人」1字や「絵文字」は普通の要約依頼（人気の投稿・絵文字なしで）に当たるので使わない。
    intent = intent.replace("スタンプラリー", "")
    return any(word in intent for word in ("スタンプ", "リアクション")) and any(
        word in intent for word in _ACTION_WORDS
    )


def _safe_text(text: str, limit: int) -> str:
    text = re.sub(r"<@[UW][A-Za-z0-9]+(?:\|[^>]*)?>", _UNKNOWN_NAME, text)
    text = _USER_ID.sub(_UNKNOWN_NAME, text)
    text = _neutralize(text, per_msg=len(text))
    text = text.replace("<", "‹").replace(">", "›").replace("@", "＠")
    return " ".join(text.split())[:limit]


def render_reactions(
    result: SlackThreadRead,
    *,
    reader: Any,
    channel_id: str,
    request_id: str,
    intent: str,
    scope: str,
) -> SlackSummaryOutput:
    start = time.perf_counter()
    terms = [a or b for a, b in re.findall(r"「([^」]+)」|『([^』]+)』", intent)]
    posts = [
        m for m in result.messages if m.reactions and (not terms or any(t in m.text for t in terms))
    ]
    truncated = result.truncated or len(posts) > MAX_POSTS
    limited = len(posts) > MAX_POSTS
    names: dict[str, str] = {}
    blocks: list[str] = []
    stamp_lines = 0
    for post in posts[:MAX_POSTS]:
        try:
            date = datetime.fromtimestamp(float(post.ts), JST).strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            date = "日付不明"
        title = _safe_text(post.text.split("\n", 1)[0], 40)
        link = slack_permalink(channel_id, post.ts)
        lines = [f"*{date}* {title}" + (f"（{link}）" if link else "")]
        for reaction in post.reactions:
            labels: list[str] = []
            for uid in reaction.users:
                if uid not in names:
                    if len(names) >= MAX_USERS:
                        limited = truncated = True
                        continue
                    name, guest = reader.get_user_label(uid, request_id)
                    label = _safe_text(name, 80) if name else ""
                    names[uid] = (label or _UNKNOWN_NAME) + ("（ゲスト）" if guest else "")
                labels.append(names[uid])
            line = f"・:{_safe_text(reaction.name, 80)}: {reaction.count}人 — " + "、".join(labels)
            if reaction.count > len(reaction.users):
                line += f" ほか{reaction.count - len(reaction.users)}人（Slack が一覧を省略）"
                truncated = True
            lines.append(line)
            stamp_lines += 1
        blocks.append("\n".join(lines))
    message = "\n\n".join(blocks) or "スタンプの付いた投稿は見つかりませんでした。"
    if limited:
        message += "\n上限のため一部だけ表示"
    elif result.truncated:
        message += "\n※ 取得できた一部の投稿だけを表示しています。"
    logger.info(
        "slack_summary_reactions",
        request_id=request_id,
        messages=len(blocks),
        stamp_lines=stamp_lines,
        users=len(names),
        truncated=truncated,
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    return SlackSummaryOutput(
        message=message, message_count=len(blocks), scope=scope, truncated=truncated
    )
