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
from urllib.parse import urlsplit

import httpx
import structlog
from slack_sdk.web.async_client import AsyncWebClient

from teamagent.adapters.slack_file_guard import (
    SLACK_FILE_MAX_REDIRECTS,
    SlackFileGuardError,
    is_followable_redirect,
    validate_slack_file_redirect,
    validate_slack_file_url,
)
from teamagent.identity import ResolvedIdentity, normalize_email

logger = structlog.get_logger(__name__)

# Production contract accepts canonical Slack member IDs in the U namespace.
_SLACK_USER_ID_RE = re.compile(r"^U[A-Z0-9]{8,}$")
_SLACK_TEAM_ID_RE = re.compile(r"^T[A-Z0-9]{8,}$")
# 身元解決キャッシュ TTL（秒）。署名 claim の最長寿命を超えて membership を信用しない。
_IDENTITY_TTL_OK = 60.0
_IDENTITY_TTL_NONE = 60.0

# url_private へは bot token の Authorization ヘッダが載る。自ワークスペースの
# ファイル配信ホスト以外へ**絶対にトークンを出さない**ための一次防壁（SSRF/トークン漏洩）。
_SLACK_FILE_HOST_SUFFIX = "slack.com"
_SLACK_FILE_CHUNK_BYTES = 256 * 1024


def slack_file_url_allowed(url: str) -> bool:
    """``url_private`` が自 WS のファイル配信ホスト（*.slack.com）の正規 HTTPS か。"""

    parsed = urlsplit(url or "")
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    try:
        if parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    return host == _SLACK_FILE_HOST_SUFFIX or host.endswith(f".{_SLACK_FILE_HOST_SUFFIX}")


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

    @classmethod
    def from_env(cls, *, timeout_seconds: int | None = None) -> SlackClient:
        """環境変数から Slack Bot Token を取得して構築する。

        必須: SLACK_BOT_TOKEN
        """
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "SLACK_BOT_TOKEN が未設定です。.env を読み込んでから起動してください"
            )
        if timeout_seconds is not None:
            if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 900:
                raise ValueError("Slack client timeout_seconds must be between 1 and 900")
            return cls(
                bot_token=token,
                client=AsyncWebClient(token=token, timeout=timeout_seconds),
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

        次のいずれかなら **None=fail-closed**: id 形式不正/``unknown``、``SLACK_TEAM_ID``
        欠落、外部ワークスペース（``team_id`` 不一致）、ゲスト（restricted/ultra_restricted）、
        is_stranger（Slack Connect 外部）、is_bot、削除済、email 欠落/不正。
        成功時のみ正規化済み email を持つ ``ResolvedIdentity`` を返す。成功結果も署名
        claim の最長寿命（60秒）を超えて再利用せず、退職・guest化・取消を再検証する。
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

        expected_team = os.environ.get("SLACK_TEAM_ID", "")
        if not _SLACK_TEAM_ID_RE.fullmatch(expected_team):
            logger.warning(
                "slack_resolve_identity_rejected",
                request_id=request_id,
                reason="missing_expected_team",
            )
            return None
        if str(user.get("team_id") or "") != expected_team:
            logger.info(
                "slack_resolve_identity_rejected", request_id=request_id, reason="foreign_team"
            )
            return None

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

    async def download_file_guarded(
        self,
        url_private: str,
        *,
        request_id: str | None = None,
        max_bytes: int,
        allowed_hosts: frozenset[str] | None = None,
        timeout_s: float = 60.0,
    ) -> bytes:
        """``download_file`` の安全版。**ホスト allowlist + ストリーミング逐次サイズ検査**。

        ``download_file`` との違い（後者の挙動は一切変えない）:

        1. **ホスト検証**: ``url_private`` を ``validate_slack_file_url`` に通してから GET する。
           無検証だと、Slack の ``files`` 配列に混入し得る外部ファイルの URL へ
           **bot token を Authorization ヘッダごと送ってしまう**。
        2. **逐次サイズ検査**: ``httpx.stream`` でチャンク受信し、累積が ``max_bytes`` を
           超えた時点で **接続を切って例外**にする（全量をメモリへ展開してから測る
           ``download_file`` は、共有 mcp タスク（メモリ 3GB・16 名共用）で巨大ファイル
           による OOM を誘発できる）。``Content-Length`` があれば読む前に拒否する。

        Raises:
            SlackFileGuardError: allowlist 外ホスト / 非 HTTPS / 容量超過。
            httpx.HTTPStatusError: Slack が 4xx/5xx を返した。
        """
        safe_url = validate_slack_file_url(
            url_private, allowed=allowed_hosts, request_id=request_id
        )
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        dl_timeout = httpx.Timeout(timeout_s, connect=10.0)
        chunks: list[bytes] = []
        total = 0
        # follow_redirects=False を保つ（httpx に自動追従させない）。302 は下の
        # ループが **1 回だけ・allowlist 済みホストへ・Authorization を外して** 追う。
        async with httpx.AsyncClient(timeout=dl_timeout, follow_redirects=False) as client:
            url = safe_url
            send_auth = True
            hops = 0
            while True:
                async with client.stream("GET", url, headers=self._file_headers(send_auth)) as resp:
                    next_url = self._next_redirect_url(
                        resp, hops=hops, request_id=request_id, error=SlackFileGuardError
                    )
                    if next_url is not None:
                        url, send_auth, hops = next_url, False, hops + 1
                        continue
                    resp.raise_for_status()
                    # ⚠️ SlackFileGuardError は ValueError の派生。int() の失敗だけを
                    #    握るため、パースと判定を必ず分ける（同じ try に入れると
                    #    「大きすぎ」の拒否そのものが黙って握り潰される）。
                    declared = _parse_content_length(resp.headers.get("content-length"))
                    if declared is not None and declared > max_bytes:
                        raise SlackFileGuardError(
                            f"SLACK_FILE_TOO_LARGE: {declared}B > {max_bytes}B"
                        )
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            # ここで with を抜ける＝残りを読まずに接続を閉じる。
                            raise SlackFileGuardError(
                                f"SLACK_FILE_TOO_LARGE: {total}B > {max_bytes}B"
                            )
                        chunks.append(chunk)
                    break
        logger.info(
            "slack_file_downloaded_guarded",
            request_id=request_id,
            size_bytes=total,
            redirects=hops,
        )
        return b"".join(chunks)

    async def download_file_bounded(
        self,
        url_private: str,
        *,
        max_bytes: int,
        request_id: str | None = None,
    ) -> bytes:
        """``url_private`` を**逐次サイズ検査つきストリーミング**で取得する。

        ``download_file`` との違いと、その理由:
        - ホスト allowlist（``*.slack.com`` の正規 HTTPS）を先に強制する。url_private へは
          bot token が載るため、任意ホストへトークンを出す経路を作らない。
        - httpx には**リダイレクトを追わせない**（``follow_redirects=False``）。自動追従は
          転送先ホストへ Authorization ごと再送し得るため。ただし 2026-08-18 の本番実測で
          「2.4MB の PDF は files.slack.com が 302 → slack-files.com の署名 URL を返す」
          （小さい .txt は 200 直返し）とわかった＝非追従のままではサイズ依存で必ず失敗する。
          そこで **allowlist 済み転送先へ 1 回だけ・Authorization を外して**追う。
        - Content-Length と実受信バイトの**両方**で上限を切る。全量をメモリに広げてから
          測る方式は 3GB 共有コンテナで OOM を招く（16名同時利用）。転送先レスポンスにも
          同じ検査が効く（追従で cap が素通りしない）。
        """

        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if not slack_file_url_allowed(url_private):
            logger.warning("slack_file_host_rejected", request_id=request_id)
            raise RuntimeError("SLACK_FILE_HOST_NOT_ALLOWED")

        dl_timeout = httpx.Timeout(120.0, connect=10.0)
        buffer = bytearray()
        async with httpx.AsyncClient(timeout=dl_timeout, follow_redirects=False) as client:
            url = url_private
            send_auth = True
            hops = 0
            while True:
                async with client.stream("GET", url, headers=self._file_headers(send_auth)) as resp:
                    next_url = self._next_redirect_url(
                        resp, hops=hops, request_id=request_id, error=RuntimeError
                    )
                    if next_url is not None:
                        url, send_auth, hops = next_url, False, hops + 1
                        continue
                    resp.raise_for_status()
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise RuntimeError(
                            f"SLACK_FILE_TOO_LARGE: {int(declared)} > {max_bytes}",
                        )
                    async for chunk in resp.aiter_bytes(_SLACK_FILE_CHUNK_BYTES):
                        if len(buffer) + len(chunk) > max_bytes:
                            raise RuntimeError(
                                f"SLACK_FILE_TOO_LARGE: >{max_bytes}",
                            )
                        buffer.extend(chunk)
                    break
        if not buffer:
            raise RuntimeError("SLACK_FILE_EMPTY")
        logger.info(
            "slack_file_downloaded_bounded",
            request_id=request_id,
            size_mb=round(len(buffer) / 1024 / 1024, 2),
            redirects=hops,
        )
        return bytes(buffer)

    # ── 302 追従（1 ホップ・Authorization 非転送）の共通実装 ──────────────────────

    def _file_headers(self, send_auth: bool) -> dict[str, str]:
        """ファイル取得リクエストのヘッダ。

        ``send_auth=False``（＝リダイレクト転送先）では **Authorization を一切載せない**。
        転送先の署名 URL は自己完結しており token を必要としないため、別ドメインへ
        bot token を出す理由が無い（出せば漏洩経路そのもの）。
        """
        return {"Authorization": f"Bearer {self._bot_token}"} if send_auth else {}

    @staticmethod
    def _next_redirect_url(
        resp: Any,
        *,
        hops: int,
        request_id: str | None,
        error: type[Exception],
    ) -> str | None:
        """このレスポンスが「追ってよい 302/303」なら検証済み転送先を返す。それ以外は None。

        - 302 / 303 以外（200 も 307/308 も 4xx/5xx も）は None＝呼び出し側が通常処理へ。
        - 既に 1 ホップ追っていたら **拒否**（多段リダイレクトは allowlist 洗浄の常套手段）。
        - Location は ``validate_slack_file_redirect`` が絶対 https・canonical authority・
          転送先 allowlist（既定 ``slack-files.com`` のみ）で検証する。

        ``error`` は呼び出し側の例外型を合わせるためのもの（``download_file_guarded`` は
        ``SlackFileGuardError``、``download_file_bounded`` は ``RuntimeError``）。転送先
        allowlist 違反は ``validate_slack_file_redirect`` が投げる ``SlackFileGuardError``
        をそのまま伝播させる（どちらの経路でも同じコードで観測できるようにする）。
        """
        status = int(getattr(resp, "status_code", 200) or 200)
        if not is_followable_redirect(status):
            return None
        if hops >= SLACK_FILE_MAX_REDIRECTS:
            logger.warning("slack_file_redirect_chain_rejected", request_id=request_id, hops=hops)
            raise error("SLACK_FILE_REDIRECT_CHAIN: リダイレクトが多すぎます")
        location = str(resp.headers.get("location") or "")
        target = validate_slack_file_redirect(location, request_id=request_id)
        logger.info("slack_file_redirect_followed", request_id=request_id, status=status)
        return target


def _parse_content_length(raw: str | None) -> int | None:
    """``Content-Length`` を int で返す。欠落・不正は None（逐次検査に委ねる）。"""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
