"""Slack の薄ラッパー。

CLAUDE.md 6-bis の 3層分離 Adapter 層。
Skill / Runtime から slack_sdk を直接叩かないこと。

Usage:
    client = SlackClient.from_env()
    client.post_message(channel="C123", text="hello", request_id="req-1")
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from slack_sdk.web.async_client import AsyncWebClient

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SlackPostResult:
    """post_message の返り値。"""

    channel: str
    ts: str
    ok: bool


class SlackClient:
    """slack_sdk.WebClient の薄ラッパー。

    Runtime / Skill 層からは boto3 と同じく直接見せない。
    レイテンシ・成功失敗を構造化ログに出す責務もここに集約する。
    """

    def __init__(self, bot_token: str, client: AsyncWebClient | None = None) -> None:
        self._bot_token = bot_token
        self._client = client or AsyncWebClient(token=bot_token)

    @classmethod
    def from_env(cls) -> SlackClient:
        """環境変数から Slack Bot Token を取得して構築する。

        必須: SLACK_BOT_TOKEN
        """
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "SLACK_BOT_TOKEN が未設定です。.env を読み込んでから起動してください"
            )
        return cls(bot_token=token)

    async def post_message(
        self,
        channel: str,
        text: str,
        request_id: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackPostResult:
        """chat.postMessage 呼び出し。

        Args:
            channel: 投稿先（チャンネルID または DM ID）
            text: 通知文（フォールバック用、blocks があっても必要）
            request_id: トレース ID
            thread_ts: スレッド返信の場合に親メッセージの ts
            blocks: Block Kit ブロック（リッチメッセージ）
        """
        start = time.perf_counter()
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        if blocks is not None:
            kwargs["blocks"] = blocks

        resp = await self._client.chat_postMessage(**kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)

        ok = bool(resp.get("ok", False))
        ts = str(resp.get("ts", ""))
        result = SlackPostResult(channel=channel, ts=ts, ok=ok)

        logger.info(
            "slack_post_message",
            request_id=request_id,
            channel=channel,
            thread_ts=thread_ts,
            ok=ok,
            ts=ts,
            latency_ms=latency_ms,
            text_len=len(text),
        )
        return result

    async def get_user_profile(self, user_id: str, request_id: str) -> dict[str, Any]:
        """users.profile.get 呼び出し。Bot に話しかけた人の情報を取るのに使う。"""
        start = time.perf_counter()
        resp = await self._client.users_profile_get(user=user_id)
        latency_ms = int((time.perf_counter() - start) * 1000)

        profile: dict[str, Any] = dict(resp.get("profile", {}))
        logger.info(
            "slack_get_user_profile",
            request_id=request_id,
            user_id=user_id,
            real_name=profile.get("real_name"),
            latency_ms=latency_ms,
        )
        return profile

    async def download_file(
        self,
        url_private: str,
        *,
        request_id: str | None = None,
        max_mb: int = 20,
    ) -> bytes:
        """Slack にアップロードされたファイルを url_private から取得する。

        url_private は bot token の Authorization ヘッダが必須。動画は大きいので
        タイムアウトを長めにし、max_mb 超は Gemini inline 上限のため拒否する。
        """
        dl_timeout = httpx.Timeout(60.0, connect=10.0)
        async with httpx.AsyncClient(timeout=dl_timeout) as client:
            resp = await client.get(
                url_private,
                headers={"Authorization": f"Bearer {self._bot_token}"},
            )
        resp.raise_for_status()
        data = resp.content
        size_mb = len(data) / 1024 / 1024
        if size_mb > max_mb:
            raise RuntimeError(f"VIDEO_FILE_TOO_LARGE: {size_mb:.0f}MB > {max_mb}MB")
        logger.info("slack_file_downloaded", request_id=request_id, size_mb=round(size_mb, 2))
        return data
