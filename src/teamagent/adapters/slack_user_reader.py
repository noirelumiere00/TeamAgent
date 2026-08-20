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
  - **fail-open**: `read_thread` / `search` は例外を握って空を返す（下書き生成を絶対に止めない）。
    `get_display_name`（users.info で差出人の実名解決）も同様に失敗は None＝
    「名前が分からなかった」。**推測した名前は絶対に作らない**。
  - **fail-closed 用の別口**: `read_thread_checked` は Slack の error code を返す
    （not_in_channel / channel_not_found 等）。「空スレッド」と「権限なし」を区別しないと
    いけない用途（slack_summary）はこちらを使う。既存 2 メソッドの挙動は変えない。
  - **G8**: ログは件数・latency・error code のみ。本文 / permalink / channel 名は出さない。
"""

from __future__ import annotations

import asyncio
import re
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

# users.info で引いた表示名のキャッシュ TTL（秒）。実名は日単位でしか変わらないので 24h。
_DISPLAY_NAME_TTL = 24 * 60 * 60.0
# 解決できなかった user_id を再試行するまでの間隔（秒）。失敗を 24h 焼き付けると
# 一時的なレート制限が丸一日「名前なし」を固定してしまうので短くする。
_DISPLAY_NAME_TTL_MISS = 10 * 60.0
# Slack の人間 user_id（U…= 通常メンバー / W…= Enterprise Grid）。bot（B…）は対象外。
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")


@dataclass(frozen=True)
class SlackSearchMatch:
    """search.messages の 1 マッチ（本人 token 限定）。"""

    ts: str
    text: str
    channel_id: str
    channel_name: str
    user: str | None = None
    permalink: str = ""


@dataclass(frozen=True)
class SlackThreadRead:
    """error-aware なスレッド取得結果（fail-closed 用）。

    ``error`` は Slack API の error code をそのまま入れる（not_in_channel /
    channel_not_found / thread_not_found / ratelimited …）。成功時は空文字。
    引数不備は ``bad_target``、code を取り出せない例外は ``api_error``。
    """

    messages: tuple[SlackMessage, ...] = ()
    error: str = ""


def _slack_error_code(exc: BaseException) -> str:
    """例外から Slack API の error code を取り出す（取れなければ 'api_error'）。

    slack_sdk は ok:false で ``SlackApiError`` を投げ、``.response`` が dict 互換
    （SlackResponse.get / dict.get のどちらでも同じ経路で読める）。
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return "api_error"
    try:
        code = resp.get("error")
    except Exception:
        return "api_error"
    return str(code) if code else "api_error"


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
    """本人 xoxp で Slack を読む（現スレッド取得 + 横断検索 + 実名解決）。

    読み取り専用・fail-open。
    """

    def __init__(self, xoxp: str, *, client: AsyncWebClient | None = None) -> None:
        if not xoxp or not xoxp.strip():
            raise ValueError("xoxp が空です（本人 Slack 未連携）")
        self._client = client or AsyncWebClient(token=xoxp)
        # user_id -> (表示名 or None, 有効期限 monotonic 秒)。インスタンス単位
        # （＝1 ユーザーの xoxp 単位）に閉じる。他人のトークンの結果と混ぜない。
        self._name_cache: dict[str, tuple[str | None, float]] = {}

    @classmethod
    def from_user_token(cls, xoxp: str, *, client: AsyncWebClient | None = None) -> SlackUserReader:
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

    def read_thread_checked(
        self, channel_id: str, thread_ts: str, request_id: str, *, limit: int = 200
    ) -> SlackThreadRead:
        """conversations.replies を **error code つき** で取得（1 ページ・読み取り専用）。

        `read_thread` は fail-open で「権限なし」と「空スレッド」が両方 `[]` になり、
        呼び出し側が区別できない。要約のように fail-closed が要る用途はこちらを使う。
        既存の `read_thread` / `search` は不変（slack_context / slack_unreplied 影響なし）。
        """
        if not channel_id or not thread_ts:
            return SlackThreadRead(error="bad_target")
        start = time.perf_counter()
        try:
            resp = _run_sync(
                lambda: self._client.conversations_replies(
                    channel=channel_id, ts=thread_ts, limit=limit
                )
            )
        except Exception as e:  # fail-closed（error code を上へ返す）
            code = _slack_error_code(e)
            logger.warning(
                "slack_user_read_thread_checked_failed",
                request_id=request_id,
                error=type(e).__name__,
                slack_error=code,  # G8: channel 名・本文は出さない
            )
            return SlackThreadRead(error=code)
        # slack_sdk は通常 ok:false で例外を投げるが、ok:false が素通りしても落とさない。
        if resp.get("ok") is False:
            return SlackThreadRead(error=str(resp.get("error") or "api_error"))
        raw: list[dict[str, Any]] = resp.get("messages", []) or []
        msgs = tuple(_message_from_raw(m) for m in raw)
        logger.info(
            "slack_user_read_thread_checked",
            request_id=request_id,
            returned=len(msgs),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return SlackThreadRead(messages=msgs)

    def get_display_name(self, user_id: str, request_id: str) -> str | None:
        """``users.info`` で表示名を 1 件引く（24h TTL キャッシュ・失敗は ``None``）。

        用途: search.messages が返す差出人の生 ``user_id`` を人間が読める名前にする。
        xoxp には ``users:read`` が既に付与済み（`slack_oauth_flow.SLACK_USER_SCOPES`）
        なので **再認可は不要**。

        - **fail-open**: 未知 ID・API 失敗・欠損はすべて ``None``。呼び出し側は
          「名前が分からなかった」として扱う（推測した名前を作らない）。
        - **G8**: ログに実名は出さない（解決できたかの真偽と latency だけ）。
        """
        uid = (user_id or "").strip()
        if not uid or not _SLACK_USER_ID_RE.match(uid):
            return None
        now = time.monotonic()
        cached = self._name_cache.get(uid)
        if cached is not None and cached[1] > now:
            return cached[0]
        name = self._fetch_display_name(uid, request_id)
        ttl = _DISPLAY_NAME_TTL if name is not None else _DISPLAY_NAME_TTL_MISS
        self._name_cache[uid] = (name, now + ttl)
        return name

    def _fetch_display_name(self, user_id: str, request_id: str) -> str | None:
        """users.info を 1 回叩いて表示名候補を取り出す（キャッシュ判定は呼び出し側）。"""
        start = time.perf_counter()
        try:
            resp = _run_sync(lambda: self._client.users_info(user=user_id))
        except Exception as e:  # fail-open
            logger.warning(
                "slack_user_display_name_failed",
                request_id=request_id,
                error=type(e).__name__,
            )
            return None
        try:
            if resp.get("ok") is False:
                return None
            user: dict[str, Any] = dict(resp.get("user") or {})
        except Exception:  # 想定外の応答形でも落とさない
            return None
        profile: dict[str, Any] = dict(user.get("profile") or {})
        # 優先順: 本人が設定した表示名 → 本名 → ハンドル。空文字は「無い」として次へ。
        name: str | None = None
        for source, key in (
            (profile, "display_name"),
            (profile, "real_name"),
            (user, "real_name"),
            (user, "name"),
        ):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        logger.info(
            "slack_user_display_name",
            request_id=request_id,
            resolved=name is not None,  # G8: 実名そのものは絶対に出さない
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return name

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
