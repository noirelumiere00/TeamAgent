"""標準 Markdown の太字を Slack mrkdwn の太字へ戻す（Slack API へ直接投稿する経路専用）。

skill の ``slack_summary`` は OpenClaw 経由（@openclaw/slack の markdownToSlackMrkdwn が
Markdown→mrkdwn へ変換する）を正として、見出しを標準 Markdown の ``**語**`` で書く。
一方、次の経路は ``chat.postMessage`` へ直接 mrkdwn で出すため変換が掛からず、
``**語**`` がそのまま（記号つきで）表示されてしまう:

- 旧 Socket Mode Bot（``runtime/slack_bot.py``）の video_algorithm 返信
- mcp の切り離し（detach）完了投稿（``mcp_gateway/detached_jobs.py``）

そこで投稿の直前にだけ ``**語**`` → ``*語*`` へ直す。他の記法（``_斜体_`` 等）は素通し。
"""

from __future__ import annotations

import re

# 標準 Markdown の太字 `**語**`（語の前後が空白でないものだけ）。
_MARKDOWN_BOLD_PATTERN = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


def markdown_bold_to_mrkdwn(text: str) -> str:
    """`**語**` を Slack mrkdwn の太字 `*語*` へ直す（直接投稿の経路専用・他の記法は素通し）。"""
    return _MARKDOWN_BOLD_PATTERN.sub(r"*\1*", text)


__all__ = ["markdown_bold_to_mrkdwn"]
