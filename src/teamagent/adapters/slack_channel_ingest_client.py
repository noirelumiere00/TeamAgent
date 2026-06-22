"""Slack チャネル履歴の取り込みアダプター。

CLAUDE.md 6-bis Adapter 層。Skill から slack_sdk を直接呼ばない。
Sprint 3 / PR-4 で雛形を導入。S3-13 で本実装。

設計判断:
- **Slack OAuth scope**: 既存 TeamAgent App の `channels:history`, `groups:history`,
  `users:read` を流用（追加申請不要、CLAUDE.md 2-ter で確認済）
- **取り込み単位**: スレッド 1 つ = 1 document（thread_ts 単位）
  - スレッド外の単発投稿も 1 document（ts 単位）
- **idempotency**: `<channel_id>:<thread_ts or ts>` を documents.external_id に
- **ACL**: channel メンバー全員を documents.acl_emails に写像
  （public channel は workspace 全員、private は招待されたメンバーのみ）
- **取り込み戦略**: oldest（直近 N 日 or 前回取り込みの last ts）以降を増分取得

参考: ユーザー提供の貴重情報源（2026-05-26）
- #proj-ナレッジ共有: 提案情報・資料の共有
- #proj-ショート動画_営業フィードバック情報: 商談 FB・温度感（最重要）

Usage:
    client = SlackChannelIngestClient.from_env()
    msgs, next_cursor = client.list_channel_history("C0XYZ", request_id="r")
    for m in msgs:
        if m.thread_ts and m.thread_ts != m.ts:
            continue  # 親メッセージのみ処理、replies は別途
        replies = client.list_thread_replies("C0XYZ", m.thread_ts or m.ts, request_id="r")
        thread_text = format_thread(m, replies)
        # → documents/chunks に投入
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from slack_sdk.web.async_client import AsyncWebClient

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# データ型
# -----------------------------------------------------------
@dataclass(frozen=True)
class SlackMessage:
    """Slack conversations.history / replies の 1 件。

    Slack API: https://api.slack.com/methods/conversations.history#response
    """

    ts: str  # epoch float with µs ("1700000000.123456")
    user: str | None  # bot メッセージは None
    text: str
    thread_ts: str | None = None  # スレッド内なら親 ts、自身がスレッド親なら ts と同値
    reply_count: int = 0  # スレッド親の場合のみ非ゼロ
    subtype: str | None = None  # 'channel_join' 'bot_message' 等
    bot_id: str | None = None
    files: tuple[dict[str, Any], ...] = ()  # 添付ファイル（PDF / 画像 / 動画）

    @property
    def is_thread_parent(self) -> bool:
        """スレッド親メッセージか（thread_ts == ts かつ reply_count > 0）。"""
        return self.thread_ts is not None and self.thread_ts == self.ts and self.reply_count > 0

    @property
    def is_top_level(self) -> bool:
        """チャネル直書き or スレッド親か（chained リプライを除外する用）。"""
        return self.thread_ts is None or self.thread_ts == self.ts


@dataclass(frozen=True)
class SlackChannelMember:
    """conversations.members + users.info の結合結果。

    documents.acl_emails に email を入れるための型。
    """

    user_id: str
    email: str | None
    display_name: str | None
    is_bot: bool = False
    deleted: bool = False


@dataclass(frozen=True)
class HistoryBatch:
    """conversations.history の 1 ページ。"""

    messages: tuple[SlackMessage, ...]
    next_cursor: str | None = None  # None なら最終ページ
    has_more: bool = False


# 取り込み対象チャネルの設定
@dataclass(frozen=True)
class IngestChannelConfig:
    """ingest_sources.yaml から読む取り込み対象チャネル定義。"""

    channel_id: str
    channel_name: str  # 表示用（"#proj-ナレッジ共有" 等）
    description: str  # 何の情報源か
    include_files: bool = True  # 添付ファイルも取り込むか
    oldest_days: int | None = 90  # 直近 N 日のみ（None で全件）
    extra_acl_emails: tuple[str, ...] = ()  # 追加 ACL（channel メンバー以外で許可）
    extra_metadata: dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------
# クライアント本体
# -----------------------------------------------------------
class SlackChannelIngestClient:
    """Slack チャネル履歴 / メンバー取得の薄ラッパー。"""

    def __init__(self, bot_token: str, *, client: AsyncWebClient | None = None) -> None:
        self._bot_token = bot_token
        self._client = client or AsyncWebClient(token=bot_token)

    @classmethod
    def from_env(cls) -> SlackChannelIngestClient:
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "SLACK_BOT_TOKEN が未設定です。.env もしくは load_secrets.sh で設定してください"
            )
        return cls(bot_token=token)

    # -------------------------------------------------------
    # チャネル履歴（conversations.history）
    # -------------------------------------------------------
    def list_channel_history(
        self,
        channel_id: str,
        request_id: str,
        *,
        oldest: float | None = None,  # epoch sec
        latest: float | None = None,
        cursor: str | None = None,
        limit: int = 100,
        include_all_metadata: bool = False,
    ) -> HistoryBatch:
        """conversations.history を 1 ページ取得する（同期ラッパー）。

        Slack 公式 SDK は async なので、内部で asyncio.run で実行。
        """
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "limit": limit,
            "include_all_metadata": include_all_metadata,
        }
        if oldest is not None:
            kwargs["oldest"] = str(oldest)
        if latest is not None:
            kwargs["latest"] = str(latest)
        if cursor:
            kwargs["cursor"] = cursor

        start = time.perf_counter()
        resp = asyncio.run(self._client.conversations_history(**kwargs))
        latency_ms = int((time.perf_counter() - start) * 1000)

        raw: list[dict[str, Any]] = resp.get("messages", []) or []
        msgs = tuple(_message_from_raw(m) for m in raw)
        meta: dict[str, Any] = resp.get("response_metadata", {}) or {}
        next_cursor = meta.get("next_cursor") or None
        if next_cursor == "":
            next_cursor = None
        has_more = bool(resp.get("has_more", False))

        logger.info(
            "slack_list_channel_history",
            request_id=request_id,
            channel_id=channel_id,
            returned=len(msgs),
            has_more=has_more,
            latency_ms=latency_ms,
        )
        return HistoryBatch(messages=msgs, next_cursor=next_cursor, has_more=has_more)

    def list_thread_replies(
        self,
        channel_id: str,
        thread_ts: str,
        request_id: str,
        *,
        cursor: str | None = None,
        limit: int = 200,
    ) -> HistoryBatch:
        """conversations.replies で 1 スレッドの全リプライを取得（1 ページ）。"""
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": limit,
        }
        if cursor:
            kwargs["cursor"] = cursor

        start = time.perf_counter()
        resp = asyncio.run(self._client.conversations_replies(**kwargs))
        latency_ms = int((time.perf_counter() - start) * 1000)

        raw: list[dict[str, Any]] = resp.get("messages", []) or []
        msgs = tuple(_message_from_raw(m) for m in raw)
        meta: dict[str, Any] = resp.get("response_metadata", {}) or {}
        next_cursor = meta.get("next_cursor") or None
        if next_cursor == "":
            next_cursor = None
        has_more = bool(resp.get("has_more", False))

        logger.info(
            "slack_list_thread_replies",
            request_id=request_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            returned=len(msgs),
            has_more=has_more,
            latency_ms=latency_ms,
        )
        return HistoryBatch(messages=msgs, next_cursor=next_cursor, has_more=has_more)

    # -------------------------------------------------------
    # チャネルメンバー（ACL 用）
    # -------------------------------------------------------
    def list_channel_members(
        self,
        channel_id: str,
        request_id: str,
        *,
        cursor: str | None = None,
        limit: int = 200,
    ) -> tuple[list[str], str | None]:
        """conversations.members で channel 参加者 user_id を返す（1 ページ）。

        email を取るには get_user_emails() で users.info を追加で叩く。
        """
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": limit}
        if cursor:
            kwargs["cursor"] = cursor

        start = time.perf_counter()
        resp = asyncio.run(self._client.conversations_members(**kwargs))
        latency_ms = int((time.perf_counter() - start) * 1000)

        ids: list[str] = list(resp.get("members", []) or [])
        meta: dict[str, Any] = resp.get("response_metadata", {}) or {}
        next_cursor = meta.get("next_cursor") or None
        if next_cursor == "":
            next_cursor = None
        logger.info(
            "slack_list_channel_members",
            request_id=request_id,
            channel_id=channel_id,
            returned=len(ids),
            has_more=bool(next_cursor),
            latency_ms=latency_ms,
        )
        return ids, next_cursor

    def get_user_emails(self, user_ids: list[str], request_id: str) -> list[SlackChannelMember]:
        """user_ids → email の解決（users.info を直列に叩く、N 回 API 呼び出し）。

        rate limit 注意。大規模 channel では batch 化 / cache 必要（Sprint 4）。
        """
        members: list[SlackChannelMember] = []
        for uid in user_ids:
            resp = asyncio.run(self._client.users_info(user=uid))
            user: dict[str, Any] = resp.get("user", {}) or {}
            profile: dict[str, Any] = user.get("profile", {}) or {}
            members.append(
                SlackChannelMember(
                    user_id=uid,
                    email=profile.get("email"),
                    display_name=profile.get("display_name") or profile.get("real_name"),
                    is_bot=bool(user.get("is_bot", False)),
                    deleted=bool(user.get("deleted", False)),
                )
            )
        logger.info(
            "slack_get_user_emails",
            request_id=request_id,
            requested=len(user_ids),
            resolved=sum(1 for m in members if m.email),
        )
        return members

    # -------------------------------------------------------
    # 本人解決 / 参加チャネル（オンデマンド付加機能用・users:read.email 等）
    # -------------------------------------------------------
    def lookup_user_id_by_email(self, email: str, request_id: str) -> str | None:
        """users.lookupByEmail で email → Slack user_id を引く（scope: users:read.email）。

        失敗時は None（呼び出し側は fail-open）。
        """
        if not email or "@" not in email:
            return None
        try:
            resp = asyncio.run(self._client.users_lookupByEmail(email=email))
            user: dict[str, Any] = resp.get("user", {}) or {}
            uid = str(user.get("id", "")) or None
        except Exception:
            logger.warning("slack_lookup_user_by_email_failed", request_id=request_id)
            return None
        return uid

    def list_user_conversations(
        self,
        user_id: str | None,
        request_id: str,
        *,
        types: str = "public_channel,private_channel",
        limit: int = 200,
    ) -> list[tuple[str, str]]:
        """users.conversations で「ある user が参加しているチャネル」を (id, name) で返す。

        user_id=None なら bot 自身の参加チャネル。bot token なので、返るのは bot から
        見えるチャネルに限られる（＝後段の履歴取得が成功し得るものに概ね一致）。
        scope: channels:read / groups:read。失敗時は空リスト（fail-open）。
        """
        out: list[tuple[str, str]] = []
        cursor: str | None = None
        try:
            for _ in range(10):  # 念のためページ上限（最大 ~2000 ch）
                kwargs: dict[str, Any] = {
                    "types": types,
                    "limit": limit,
                    "exclude_archived": True,
                }
                if user_id:
                    kwargs["user"] = user_id
                if cursor:
                    kwargs["cursor"] = cursor
                resp = asyncio.run(self._client.users_conversations(**kwargs))
                raw_channels: list[dict[str, Any]] = list(resp.get("channels", []) or [])
                for ch in raw_channels:
                    cid = str(ch.get("id", ""))
                    name = str(ch.get("name", ""))
                    if cid:
                        out.append((cid, name))
                meta: dict[str, Any] = resp.get("response_metadata", {}) or {}
                cursor = meta.get("next_cursor") or None
                if not cursor:
                    break
        except Exception:
            logger.warning("slack_list_user_conversations_failed", request_id=request_id)
            return out
        logger.info(
            "slack_list_user_conversations",
            request_id=request_id,
            user_scoped=bool(user_id),
            returned=len(out),
        )
        return out


# -----------------------------------------------------------
# 変換ヘルパー
# -----------------------------------------------------------
def _message_from_raw(raw: dict[str, Any]) -> SlackMessage:
    """conversations.history / replies の 1 件を dataclass にマップ。"""
    return SlackMessage(
        ts=str(raw.get("ts", "")),
        user=raw.get("user"),
        text=str(raw.get("text", "")),
        thread_ts=raw.get("thread_ts"),
        reply_count=int(raw.get("reply_count", 0)),
        subtype=raw.get("subtype"),
        bot_id=raw.get("bot_id"),
        files=tuple(raw.get("files", []) or ()),
    )


def format_thread_as_document(parent: SlackMessage, replies: list[SlackMessage]) -> str:
    """1 スレッド（親 + replies）を 1 つのテキストに連結する。

    documents.metadata に投入する形式：
        [HH:MM] <user>: <text>
        [HH:MM] <user>: <text>
        ...
    """
    import datetime as _dt

    def _line(m: SlackMessage) -> str:
        try:
            ts_float = float(m.ts)
            ts_str = _dt.datetime.fromtimestamp(ts_float, tz=_dt.UTC).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            ts_str = m.ts
        user_label = m.user or m.bot_id or "?"
        return f"[{ts_str}] <{user_label}>: {m.text}"

    lines = [_line(parent)]
    # parent と同じ ts は除外（replies に親自身が含まれることがある）
    for r in replies:
        if r.ts == parent.ts:
            continue
        lines.append(_line(r))
    return "\n".join(lines)


def collect_thread_participants(parent: SlackMessage, replies: list[SlackMessage]) -> list[str]:
    """スレッドの全参加者 user_id を重複なしで返す（ACL 構築用）。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in [parent, *replies]:
        uid = m.user
        if uid and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out
