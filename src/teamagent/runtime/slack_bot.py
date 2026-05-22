"""Slack Bot ランタイム（Socket Mode）。

Sprint 1 時点では mention に対して同じテキストを返す echo Bot。
今後 Skill Registry と接続して Skill 実行のディスパッチを行う。

Usage:
    SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... \\
    python -m teamagent.runtime.slack_bot

CLAUDE.md 6-bis：
- 3層分離：本ファイルは Runtime 層。Slack API は adapters/slack_client 経由
- 構造化ログ：request_id を毎イベント生成して伝播
- prompt のファイル化：今はまだ Skill を呼ばないので不要、後続で追加
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from teamagent.adapters.slack_client import SlackClient

logger = structlog.get_logger(__name__)


def build_app() -> AsyncApp:
    """Bolt AsyncApp を構築する。

    SLACK_BOT_TOKEN は必須。
    Socket Mode で動かすには SLACK_APP_TOKEN も必要（main() でチェック）。
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("SLACK_BOT_TOKEN が未設定です")

    app = AsyncApp(token=bot_token)
    slack = SlackClient(bot_token=bot_token)

    @app.event("app_mention")
    async def handle_app_mention(event: dict[str, Any], say: Any) -> None:
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = event.get("user", "unknown")
        text = event.get("text", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        logger.info(
            "slack_app_mention",
            request_id=request_id,
            user_id=user_id,
            channel=channel,
            text_len=len(text),
        )

        # Sprint 1 時点は echo。次の Sprint で SkillRegistry にルーティング。
        await slack.post_message(
            channel=channel,
            text=f"<@{user_id}> こんにちは。受け取った文字数 = {len(text)}。",
            request_id=request_id,
            thread_ts=thread_ts,
        )

    @app.event("message")
    async def handle_message(event: dict[str, Any]) -> None:
        # bot 自身のメッセージは無視
        if event.get("bot_id"):
            return
        if event.get("channel_type") != "im":
            return  # DM のみ反応（チャンネルメッセージは app_mention でハンドル）

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = event.get("user", "unknown")
        channel = event.get("channel", "")
        text = event.get("text", "")

        logger.info(
            "slack_dm",
            request_id=request_id,
            user_id=user_id,
            channel=channel,
            text_len=len(text),
        )

        await slack.post_message(
            channel=channel,
            text=f"DM 受け取りました（{len(text)} 文字）。",
            request_id=request_id,
        )

    return app


async def _run() -> None:
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError(
            "SLACK_APP_TOKEN が未設定です（xapp- で始まる Socket Mode 用トークン）"
        )

    app = build_app()
    handler = AsyncSocketModeHandler(app, app_token)
    logger.info("slack_bot_start", mode="socket")
    await handler.start_async()  # type: ignore[no-untyped-call]


def main() -> None:
    """CLI エントリポイント。"""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
