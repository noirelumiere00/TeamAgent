"""Slack の薄ラッパー。

CLAUDE.md 6-bis の 3層分離 Adapter 層。
Skill / Runtime から slack_sdk を直接叩かないこと。

Usage:
    client = SlackClient.from_env()
    client.post_message(channel="C123", text="hello", request_id="req-1")
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from slack_sdk.web.async_client import AsyncWebClient

from teamagent.identity import ResolvedIdentity, normalize_email

logger = structlog.get_logger(__name__)

# Slack user id 形式（U=通常 / W=Enterprise Grid）。偽 id は API 前に弾く。
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
# 身元解決キャッシュ TTL（秒）。成功は長め・失敗(None)は短め（退職/取消の反映を早める）。
_IDENTITY_TTL_OK = 3600.0
_IDENTITY_TTL_NONE = 60.0


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
        # user_id → (身元 or None, 失効 monotonic 時刻)。anti-spoof 解決の TTL キャッシュ。
        self._identity_cache: dict[str, tuple[ResolvedIdentity | None, float]] = {}
        # SLACK_TEAM_ID 未設定（team 検証 skip＝fail-open）の警告を 1 プロセス 1 回に抑える。
        self._team_check_warned = False

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

    @classmethod
    def from_user_token(cls, xoxp: str, client: AsyncWebClient | None = None) -> SlackClient:
        """各営業「本人」の Slack User Token(xoxp) で動く SlackClient を組む。

        要件B: 各営業が OAuth 同意フローで取得した個人 user token(xoxp) を使い、
        Bot ではなく **本人として** Slack API を叩くための別経路。共有 Bot Token
        (xoxb) を使う ``from_env`` とは用途も権限主体も異なるので混同しないこと。

        - ``from_env``    → 共有 Bot Token(xoxb)。ワークスペース共通の bot 権限。
        - ``from_user_token`` → 個人 User Token(xoxp)。当該営業本人の権限・本人名義。

        実装上は ``__init__(bot_token=...)`` がトークン概念を持つだけで
        AsyncWebClient(token=...) にそのまま流すため、xoxp を bot_token 引数に
        渡しても同型に通る（token の中身が xoxp である点だけが違う）。呼び出し側で
        xoxb/xoxp を取り違えないよう、必ずこの classmethod 経由で構築する。

        Args:
            xoxp: 当該営業本人の Slack User OAuth Token（``xoxp-`` 始まり）。
            client: テスト用に AsyncWebClient を差し替える場合に注入。
        """
        if not xoxp:
            raise ValueError(
                "xoxp(Slack User Token) が空です。OAuth 同意フロー完了後のトークンを渡してください"
            )
        return cls(bot_token=xoxp, client=client)

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
        # unfurl 無効化: ダイジェストは permalink/会議URL を多数含むため、リンクプレビューが
        # 展開されると DM が縦に伸びて可読性が崩れる（2026-07-13 compact 化と同時に恒久設定）。
        kwargs: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
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

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        request_id: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackPostResult:
        """chat.update 呼び出し。既存メッセージを書き換える（タイムラインを汚さない）。

        受付メッセージ → 最終結果 をひと続きで同じ ts に書き換える「段階表示」用。
        ts が空（受付投稿に失敗していた等）なら no-op で ok=False を返す（呼び側で
        フォールバック投稿できるよう本関数は例外を投げない設計）。
        """
        if not ts:
            logger.info("slack_update_skipped_empty_ts", request_id=request_id, channel=channel)
            return SlackPostResult(channel=channel, ts="", ok=False)
        start = time.perf_counter()
        kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks is not None:
            kwargs["blocks"] = blocks
        try:
            resp = await self._client.chat_update(**kwargs)
        except Exception:
            logger.warning(
                "slack_update_message_failed",
                request_id=request_id,
                channel=channel,
                ts=ts,
            )
            return SlackPostResult(channel=channel, ts=ts, ok=False)
        latency_ms = int((time.perf_counter() - start) * 1000)
        ok = bool(resp.get("ok", False))
        result = SlackPostResult(channel=channel, ts=ts, ok=ok)
        logger.info(
            "slack_update_message",
            request_id=request_id,
            channel=channel,
            ok=ok,
            ts=ts,
            latency_ms=latency_ms,
            text_len=len(text),
        )
        return result

    async def delete_message(self, channel: str, ts: str, request_id: str) -> bool:
        """chat.delete 呼び出し。bot が投稿した一時メッセージ（進捗表示等）を消す。

        進捗メッセージ（v0.3.1 Task7）は「ツール実行中だけ見せて完了後に消す」用途。
        ts/channel が空なら no-op。失敗しても例外は投げない（fail-open・古い進捗が
        残るだけで本処理には影響しない）。
        """
        if not ts or not channel:
            return False
        try:
            resp = await self._client.chat_delete(channel=channel, ts=ts)
        except Exception:
            logger.warning("slack_delete_message_failed", request_id=request_id, channel=channel)
            return False
        ok = bool(resp.get("ok", False))
        logger.info("slack_delete_message", request_id=request_id, channel=channel, ok=ok)
        return ok

    async def post_ephemeral(
        self,
        channel: str,
        user: str,
        text: str,
        request_id: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackPostResult:
        """chat.postEphemeral 呼び出し（指定ユーザーにのみ見えるメッセージ）。

        メール等のプライバシー機微な結果を、@メンション元のチャンネルに漏らさず
        「本人だけに」返すために使う（受信箱内容を共有チャンネルにブロードキャストしない）。
        """
        start = time.perf_counter()
        kwargs: dict[str, Any] = {"channel": channel, "user": user, "text": text}
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        if blocks is not None:
            kwargs["blocks"] = blocks

        resp = await self._client.chat_postEphemeral(**kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)
        ok = bool(resp.get("ok", False))
        result = SlackPostResult(channel=channel, ts=str(resp.get("message_ts", "")), ok=ok)
        logger.info(
            "slack_post_ephemeral",
            request_id=request_id,
            channel=channel,
            thread_ts=thread_ts,
            ok=ok,
            latency_ms=latency_ms,
            text_len=len(text),
        )
        return result

    async def upload_file(
        self,
        channel: str,
        file_path: str,
        request_id: str,
        *,
        title: str | None = None,
        initial_comment: str | None = None,
        thread_ts: str | None = None,
    ) -> bool:
        """ローカルファイル（HTMLレポート等）を channel にアップロードする。

        files.upload v2 を使う。失敗しても例外を投げず False を返す（通知本体は別途投稿済の想定）。
        """
        start = time.perf_counter()
        kwargs: dict[str, Any] = {"channel": channel, "file": file_path}
        if title is not None:
            kwargs["title"] = title
        if initial_comment is not None:
            kwargs["initial_comment"] = initial_comment
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        try:
            resp = await self._client.files_upload_v2(**kwargs)
            ok = bool(resp.get("ok", False))
        except Exception:
            logger.exception("slack_upload_file_failed", request_id=request_id, channel=channel)
            return False
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "slack_upload_file",
            request_id=request_id,
            channel=channel,
            ok=ok,
            file=file_path,
            latency_ms=latency_ms,
        )
        return ok

    async def lookup_user_id_by_email(self, email: str, request_id: str) -> str | None:
        """会社メール → Slack user_id を解決（bot scope: users:read.email）。

        morning_digest の DM 配信と同じ経路。解決できなければ None（呼び出し側で fail-open）。
        """
        try:
            resp = await self._client.users_lookupByEmail(email=email)
            user: dict[str, Any] = dict(resp.get("user", {}))
            uid = str(user.get("id", "")) or None
        except Exception:
            logger.warning("slack_lookup_user_by_email_failed", request_id=request_id)
            return None
        return uid

    async def open_dm(self, user_id: str, request_id: str) -> str | None:
        """conversations.open で本人 IM channel を取得（bot scope: im:write）。"""
        try:
            resp = await self._client.conversations_open(users=user_id)
            channel: dict[str, Any] = dict(resp.get("channel", {}))
            ch = str(channel.get("id", "")) or None
        except Exception:
            logger.warning("slack_open_dm_failed", request_id=request_id)
            return None
        return ch

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

    async def resolve_identity(
        self, user_id: str | None, *, request_id: str = "-"
    ) -> ResolvedIdentity | None:
        """Slack ``user_id`` をサーバ側で身元解決する（OC 申告を信用しない anti-spoof の起点）。

        次のいずれかなら **None=fail-closed**: id 形式不正/``unknown``、外部ワークスペース
        （``SLACK_TEAM_ID`` 設定時に ``team_id`` 不一致）、ゲスト（restricted/ultra_restricted）、
        is_stranger（Slack Connect 外部）、is_bot、削除済、email 欠落/不正。
        成功時のみ正規化済み email を持つ ``ResolvedIdentity`` を返す。TTL キャッシュ付き。
        """
        if not user_id or not _SLACK_USER_ID_RE.match(user_id):
            return None
        now = time.monotonic()
        cached = self._identity_cache.get(user_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        identity = await self._resolve_identity_uncached(user_id, request_id=request_id)
        ttl = _IDENTITY_TTL_OK if identity is not None else _IDENTITY_TTL_NONE
        self._identity_cache[user_id] = (identity, now + ttl)
        return identity

    async def _resolve_identity_uncached(
        self, user_id: str, *, request_id: str
    ) -> ResolvedIdentity | None:
        try:
            resp = await self._client.users_info(user=user_id)
        except Exception:
            logger.warning("slack_resolve_identity_failed", request_id=request_id, user_id=user_id)
            return None

        user: dict[str, Any] = dict(resp.get("user") or {})
        if (
            user.get("deleted")
            or user.get("is_bot")
            or user.get("is_restricted")
            or user.get("is_ultra_restricted")
            or user.get("is_stranger")
        ):
            logger.info(
                "slack_resolve_identity_rejected", request_id=request_id, reason="guest_or_bot"
            )
            return None

        expected_team = os.environ.get("SLACK_TEAM_ID")
        if expected_team and str(user.get("team_id") or "") != expected_team:
            logger.info(
                "slack_resolve_identity_rejected", request_id=request_id, reason="foreign_team"
            )
            return None
        if not expected_team and not self._team_check_warned:
            # team 検証は skip される（fail-open）。ゲスト/is_stranger/bot 拒否と email 検証は
            # 効いているが、多人数運用では SLACK_TEAM_ID を必ず設定すること（CLAUDE.md §5）。
            # 警告はプロセスごとに 1 回だけ（毎解決で吐くとログが埋まる）。
            self._team_check_warned = True
            logger.warning(
                "slack_team_check_disabled",
                request_id=request_id,
                hint="set SLACK_TEAM_ID to reject foreign-workspace users (fail-closed)",
            )

        profile: dict[str, Any] = dict(user.get("profile") or {})
        email = normalize_email(profile.get("email"))
        if email is None:
            return None

        raw_display = profile.get("display_name") or profile.get("real_name")
        display = raw_display if isinstance(raw_display, str) and raw_display else None
        return ResolvedIdentity(
            slack_user_id=user_id,
            email=email,
            is_member=True,
            groups=(),
            display=display,
            source="slack_users_info",
        )

    async def resolve_user_email(self, user_id: str | None, *, request_id: str = "-") -> str | None:
        """``resolve_identity`` の email だけを返す後方互換ヘルパ（bot の本人解決用）。"""
        identity = await self.resolve_identity(user_id, request_id=request_id)
        return identity.email if identity else None

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
