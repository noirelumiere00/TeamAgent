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
    intent = intent.replace("スタンプラリー", "").replace("リアクション動画", "")
    return any(word in intent for word in ("スタンプ", "リアクション")) and any(
        word in intent for word in _ACTION_WORDS
    )


def reaction_channel(intent: str) -> dict[str, str]:
    """依頼に明記されたチャンネルだけを返す（発信元から推測しない）。"""
    intent = re.sub(r"(?:https?://|www\.)[^\s<>]+", "", intent, flags=re.I)
    mention = re.search(r"<#([CG][A-Za-z0-9]+)(?:\|[^>]*)?>", intent)
    if mention:
        return {"channel_id": mention[1]}
    for named in re.finditer(r"(?<![\w/#])#([\w-]+)", intent):
        if not named[1].isdecimal():
            return {"channel_name": named[1]}
    labelled = re.search(
        r"チャンネル(?:名)?(?:\s*[:：]\s*|[ \t]+|[「『]|(?=[A-Za-z0-9_-]))"
        r"[*`]*([\w-]+)",
        intent,
    )
    return {"channel_name": labelled[1]} if labelled and not labelled[1].isdecimal() else {}


def reaction_terms(intent: str) -> list[str]:
    intent = intent.replace("**", "").replace("`", "")
    quoted = [a or b for a, b in re.findall(r"「([^」]+)」|『([^』]+)』", intent)]
    labelled = re.findall(r"(?:含む文字列|文字列)\s*[:：]\s*([^\n\r]+)", intent)
    bare = re.findall(r"文字列\s*(?![:：\sを])([^\s、。「『:：]+)", intent)
    before = re.findall(r"([^\s、。]+?)\s*を含む\s*(?:投稿|メッセージ)", intent)
    # 明記された検索語があるとき、「ユニーク」等の依頼中の引用を検索語に混ぜない。
    terms = labelled or [*quoted, *bare, *before]
    return list(dict.fromkeys(t.strip(" *_「」『』") for t in terms))


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
    terms = reaction_terms(intent)
    unique = any(w in intent for w in ("ユニーク", "重複なし", "重複を除", "一意", "列挙"))
    guest_only = any(w in intent for w in ("マルチチャンネルゲスト", "ゲストだけ", "ゲストのみ"))
    reacted = [m for m in result.messages if m.reactions]
    posts = [m for m in reacted if not terms or any(t in m.text for t in terms)]
    # 「」はモデルが focus に足した語のこともある。絞って 0 件なら絞らずに出し、そう書く。
    widened = bool(terms) and not posts and bool(reacted)
    if widened:
        posts = reacted
    limited = not unique and len(posts) > MAX_POSTS
    truncated = result.truncated or limited
    names: dict[str, tuple[str, bool]] = {}
    user_posts: dict[str, set[str]] = {}
    omitted = False
    blocks: list[str] = []
    stamp_lines = 0
    selected = posts if unique else posts[:MAX_POSTS]
    for post in selected:
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
                    names[uid] = (label or _UNKNOWN_NAME, guest)
                label, guest = names[uid]
                if guest_only and not guest:
                    continue
                user_posts.setdefault(uid, set()).add(post.ts)
                labels.append(label + ("（ゲスト）" if guest else ""))
            count = len(labels) if guest_only else reaction.count
            line = f"・:{_safe_text(reaction.name, 80)}: {count}人 — " + "、".join(labels)
            if reaction.count > len(reaction.users):
                line += f" ほか{reaction.count - len(reaction.users)}人（Slack が一覧を省略）"
                omitted = truncated = True
            lines.append(line)
            stamp_lines += 1
        blocks.append("\n".join(lines))
    if unique:
        users = sorted(user_posts, key=lambda uid: (not names[uid][1], names[uid][0], uid))
        message = (
            "\n".join(
                f"{names[uid][0]}{'（ゲスト）' if names[uid][1] else ''}"
                f" — {len(user_posts[uid])} 投稿"
                for uid in users
            )
            or "該当するユーザーは見つかりませんでした。"
        )
    else:
        message = "\n\n".join(blocks) or "スタンプの付いた投稿は見つかりませんでした。"
    if widened:
        message = (
            "指定の語を含む投稿には無かったので、期間内でスタンプの付いた投稿をすべて出します。\n\n"
            + message
        )
    guests = sum(names[uid][1] for uid in user_posts)
    if omitted:
        message += "\n※ Slack が一覧を省略した分（ほか N 人）は人数に含めていません。"
        if guest_only:
            message += "省略された人がゲストかは不明です。"
    if limited:
        message += "\n上限のため一部だけ表示"
    elif result.truncated:
        message += "\n※ 取得できた一部の投稿だけを表示しています。"
    message += f"\nゲスト {guests} 人"
    if not guest_only:
        message += f"・社員 {len(user_posts) - guests} 人"
    logger.info(
        "slack_summary_reactions",
        request_id=request_id,
        read=len(result.messages),
        reacted=len(reacted),
        terms=len(terms),
        widened=widened,
        messages=len(blocks),
        stamp_lines=stamp_lines,
        users=len(names),
        truncated=truncated,
        latency_ms=int((time.perf_counter() - start) * 1000),
    )
    return SlackSummaryOutput(
        message=message, message_count=len(blocks), scope=scope, truncated=truncated
    )
