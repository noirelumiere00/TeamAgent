"""本人(xoxp)として Slack を読む Adapter（読み取り専用）。

CLAUDE.md 6-bis Adapter 層。Skill から slack_sdk を直接呼ばない。

用途: メール下書き最適化で「本人が参加する現スレッド」「案件名の横断検索」の
文脈を集める。**本人 user token(xoxp) 限定**（bot token では search.messages 不可）。
付与 scope は `slack_oauth_flow.SLACK_USER_SCOPES`（search:read / *:history / users:read）。

設計:
  - 型は `slack_channel_ingest_client` の SlackMessage / HistoryBatch / _message_from_raw
    を再利用（マッピングの単一真実源）。search 用に SlackSearchMatch を追加。
  - Skill は同期実行なので、内部で `_run_sync` により async クライアントを同期呼び出しする。
    実行中ループがある場合（将来 orchestrator が async 文脈で呼ぶ場合）は別スレッドへ退避。
  - **fail-open**: 例外は握って空を返す（下書き生成を絶対に止めない）。
  - **G8**: ログは件数・latency のみ。本文 / permalink / channel 名は出さない。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import structlog
from slack_sdk.web.async_client import AsyncWebClient

from teamagent.adapters.slack_channel_ingest_client import (
    SlackMessage,
    _message_from_raw,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SlackSearchMatch:
    """search.messages の 1 マッチ（本人 token 限定）。"""

    ts: str
    text: str
    channel_id: str
    channel_name: str
    user: str | None = None
    permalink: str = ""


def _run_sync(coro_factory: Callable[[], Any]) -> Any:
    """coroutine を同期実行する。実行中ループがあれば別スレッドの新ループで回す。

    coroutine はターゲットループ内で生成する（ループ跨ぎを避ける）ため factory で受ける。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result()


class SlackUserReader:
    """本人 xoxp で Slack を読む（現スレッド取得 + 横断検索）。読み取り専用・fail-open。"""

    def __init__(self, xoxp: str, *, client: AsyncWebClient | None = None) -> None:
        if not xoxp or not xoxp.strip():
            raise ValueError("xoxp が空です（本人 Slack 未連携）")
        self._client = client or AsyncWebClient(token=xoxp)

    @classmethod
    def from_user_token(
        cls, xoxp: str, *, client: AsyncWebClient | None = None
    ) -> SlackUserReader:
        return cls(xoxp, client=client)

    def read_thread(
        self, channel_id: str, thread_ts: str, request_id: str, *, limit: int = 200
    ) -> list[SlackMessage]:
        """conversations.replies で現スレッドを取得（1 ページ）。fail-open で空返し。"""
        if not channel_id or not thread_ts:
            return []
        start = time.perf_counter()
        try:
            resp = _run_sync(
                lambda: self._client.conversations_replies(
                    channel=channel_id, ts=thread_ts, limit=limit
                )
            )
        except Exception as e:  # fail-open
            logger.warning(
                "slack_user_read_thread_failed",
                request_id=request_id,
                error=type(e).__name__,
            )
            return []
        raw: list[dict[str, Any]] = resp.get("messages", []) or []
        msgs = [_message_from_raw(m) for m in raw]
        logger.info(
            "slack_user_read_thread",
            request_id=request_id,
            returned=len(msgs),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return msgs

    def search(self, query: str, request_id: str, *, count: int = 15) -> list[SlackSearchMatch]:
        """search.messages で横断検索（user token 限定）。fail-open で空返し。"""
        if not query or not query.strip():
            return []
        start = time.perf_counter()
        try:
            resp = _run_sync(
                lambda: self._client.search_messages(query=query, count=count, sort="timestamp")
            )
        except Exception as e:  # fail-open
            logger.warning(
                "slack_user_search_failed",
                request_id=request_id,
                error=type(e).__name__,
            )
            return []
        matches_raw: list[dict[str, Any]] = (resp.get("messages") or {}).get("matches") or []
        out: list[SlackSearchMatch] = []
        for m in matches_raw:
            try:
                ch: dict[str, Any] = m.get("channel") or {}
                out.append(
                    SlackSearchMatch(
                        ts=str(m.get("ts", "")),
                        text=str(m.get("text", "")),
                        channel_id=str(ch.get("id", "")),
                        channel_name=str(ch.get("name", "")),
                        user=m.get("user"),
                        permalink=str(m.get("permalink", "")),
                    )
                )
            except Exception:  # 1 件の欠損で全体を落とさない
                continue
        logger.info(
            "slack_user_search",
            request_id=request_id,
            returned=len(out),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return out
