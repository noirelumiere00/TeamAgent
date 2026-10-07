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
  - **fail-closed 用の別口**: `read_thread_checked` / `read_channel_checked` は error code を返す
    （not_in_channel / channel_not_found 等）。「空スレッド」と「権限なし」を区別しないと
    いけない用途（slack_summary）はこちらを使う。既存メソッドの挙動は変えない。
    `read_message_checked` は投稿リンクの先の 1 件だけを同じ流儀で読む（attachment_assist）。
    `search_checked` も同じ考え方で、「0 件」と「トークン切れ・API 障害」を区別する
    （slack_search 用。一致ごとに channel の is_private / is_mpim 等も写す）。
  - **G8**: ログは件数・latency・error code のみ。本文 / permalink / channel 名は出さない。
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
    """search.messages の 1 マッチ（本人 token 限定）。

    ``channel_is_*`` は応答の ``channel`` オブジェクトの真偽値をそのまま写す。
    **値が無い・bool でないときは None**（＝判定できない）。公開/非公開の判定に使う側は
    None を「公開ではない」として扱うこと（fail-closed。推測で公開側へ倒さない）。
    ``match_type`` は応答の ``type``（DM の一致は ``"im"``・Slack API 仕様）。
    """

    ts: str
    text: str
    channel_id: str
    channel_name: str
    user: str | None = None
    permalink: str = ""
    username: str = ""
    match_type: str = ""
    channel_is_private: bool | None = None
    channel_is_mpim: bool | None = None
    channel_is_im: bool | None = None
    channel_is_group: bool | None = None
    # 一致したメッセージの添付ファイル名（応答の ``files[].name``・無ければ ``title``）。
    # 応答に files が無い一致は空タプル（推測で埋めない）。
    file_names: tuple[str, ...] = ()
    thread_ts: str = ""


@dataclass(frozen=True)
class SlackSearchRead:
    """error-aware な検索結果（fail-closed 用・slack_search が使う）。

    ``error`` は Slack API の error code（invalid_auth / missing_scope / ratelimited …）。
    成功時は空文字で、そのときの ``matches == ()`` は **本当に 0 件**。
    応答の形が想定外なら ``bad_response``（0 件と区別する）。
    ``total`` は Slack が申告した総ヒット数（取れなければ返ってきた件数）。
    """

    matches: tuple[SlackSearchMatch, ...] = ()
    total: int = 0
    error: str = ""
    truncated: bool = False


def _opt_bool(value: Any) -> bool | None:
    """bool だけを通す（"false" 等の文字列や欠損は None＝判定不能）。"""
    return value if isinstance(value, bool) else None


def _file_names_from_raw(m: dict[str, Any]) -> tuple[str, ...]:
    """一致の ``files`` から添付ファイル名を拾う（name → title の順・文字列だけ・最大 5 件）。"""
    raw = m.get("files")
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        name = f.get("name") or f.get("title")
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
        if len(names) >= 5:
            break
    return tuple(names)


def _search_match_from_raw(m: dict[str, Any]) -> SlackSearchMatch:
    """search.messages の 1 マッチを SlackSearchMatch へ写す（マッピングの単一真実源）。"""
    ch: dict[str, Any] = m.get("channel") or {}
    return SlackSearchMatch(
        ts=str(m.get("ts", "")),
        text=str(m.get("text", "")),
        channel_id=str(ch.get("id", "")),
        channel_name=str(ch.get("name", "")),
        user=m.get("user"),
        permalink=str(m.get("permalink", "")),
        username=str(m.get("username", "") or ""),
        match_type=str(m.get("type", "") or ""),
        channel_is_private=_opt_bool(ch.get("is_private")),
        channel_is_mpim=_opt_bool(ch.get("is_mpim")),
        channel_is_im=_opt_bool(ch.get("is_im")),
        channel_is_group=_opt_bool(ch.get("is_group")),
        file_names=_file_names_from_raw(m),
        thread_ts=str(
            m.get("thread_ts")
            or (parse_qs(urlsplit(str(m.get("permalink") or "")).query).get("thread_ts") or [""])[0]
        ),
    )


@dataclass(frozen=True)
class SlackThreadRead:
    """error-aware なスレッド取得結果（fail-closed 用）。

    ``error`` は Slack API の error code をそのまま入れる（not_in_channel /
    channel_not_found / thread_not_found / ratelimited …）。成功時は空文字。
    引数不備は ``bad_target``、code を取り出せない例外は ``api_error``。
    """

    messages: tuple[SlackMessage, ...] = ()
    error: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class SlackChannelResolution:
    """検索から確実に識別できた会話だけを返す。未知の公開範囲は非公開扱い。"""

    channel_id: str = ""
    is_public: bool = False
    error: str = ""


def normalize_channel_name(name: str) -> str:
    """全半角・大小文字と利用者の「〇〇のチャンネル」表記を揃える。"""
    name = unicodedata.normalize("NFKC", name).strip().lower().lstrip("#")
    return re.sub(r"(?:の)?チャンネル$", "", name).strip()


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
        self._client = client or AsyncWebClient(token=xoxp, timeout=15, retry_handlers=[])
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

    def read_channel_checked(
        self, channel_id: str, request_id: str, *, limit: int = 200
    ) -> SlackThreadRead:
        """conversations.history を **error code つき** で取得（1 ページ・読み取り専用）。

        Slack は新しい順で返すため、呼び出し側が時系列順に扱えるよう古い順へ直して返す。
        `read_thread_checked` と同じく、権限エラーと空の履歴を区別できる fail-closed 用。
        """
        if not channel_id:
            return SlackThreadRead(error="bad_target")
        start = time.perf_counter()
        try:
            resp = _run_sync(
                lambda: self._client.conversations_history(channel=channel_id, limit=limit)
            )
        except Exception as e:  # fail-closed（error code を上へ返す）
            code = _slack_error_code(e)
            logger.warning(
                "slack_user_read_channel_checked_failed",
                request_id=request_id,
                error=type(e).__name__,
                slack_error=code,  # G8: channel 名・本文は出さない
            )
            return SlackThreadRead(error=code)
        # slack_sdk は通常 ok:false で例外を投げるが、ok:false が素通りしても落とさない。
        if resp.get("ok") is False:
            return SlackThreadRead(error=str(resp.get("error") or "api_error"))
        raw: list[dict[str, Any]] = resp.get("messages", []) or []
        msgs = tuple(_message_from_raw(m) for m in reversed(raw))
        logger.info(
            "slack_user_read_channel_checked",
            request_id=request_id,
            returned=len(msgs),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return SlackThreadRead(messages=msgs)

    def read_message_checked(
        self, channel_id: str, ts: str, request_id: str, *, thread_ts: str = ""
    ) -> SlackThreadRead:
        """投稿 1 件を **本人の権限で** 取得する（error code つき・読み取り専用）。

        投稿リンク（permalink）の先を読む用途。``oldest == latest == ts`` の inclusive 取得で
        その 1 件だけを狙う（スレッド返信なら conversations.replies、それ以外は history）。
        replies は親を常に先頭に含めるので ts 一致で絞る。見つからなければ
        ``message_not_found``（権限エラーと同じく呼び出し側で一様の文へ潰す前提）。
        """
        if not channel_id or not ts:
            return SlackThreadRead(error="bad_target")
        start = time.perf_counter()
        params: dict[str, Any] = {
            "channel": channel_id,
            "oldest": ts,
            "latest": ts,
            "inclusive": True,
            "limit": 2,
        }
        method: Any = self._client.conversations_history
        if thread_ts:
            params["ts"] = thread_ts
            method = self._client.conversations_replies

        async def fetch() -> Any:
            return await asyncio.wait_for(method(**params), timeout=15)

        try:
            resp = _run_sync(fetch)
        except Exception as e:  # fail-closed（error code を上へ返す）
            code = _slack_error_code(e)
            logger.warning(
                "slack_user_read_message_checked_failed",
                request_id=request_id,
                error=type(e).__name__,
                slack_error=code,  # G8: channel 名・本文は出さない
            )
            return SlackThreadRead(error=code)
        if resp.get("ok") is False:
            return SlackThreadRead(error=str(resp.get("error") or "api_error"))
        raw = resp.get("messages")
        if not isinstance(raw, list):
            return SlackThreadRead(error="bad_response")
        hits = tuple(
            _message_from_raw(m) for m in raw if isinstance(m, dict) and str(m.get("ts")) == ts
        )
        logger.info(
            "slack_user_read_message_checked",
            request_id=request_id,
            returned=len(hits),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        if not hits:
            return SlackThreadRead(error="message_not_found")
        return SlackThreadRead(messages=hits[:1])

    def resolve_channel_checked(self, name: str, request_id: str) -> SlackChannelResolution:
        """現在の scope で in:#名前 を検索する（list の read scopes は未付与）。

        検索に投稿が無い会話は解決できない。部分一致の候補が複数／取り切れない場合は
        推測で選ばない。名前を検索演算子として注入できないよう空白・引用符等を拒否する。
        """
        name = normalize_channel_name(name)
        if not name or not re.fullmatch(r"[\w\-]+", name):
            return SlackChannelResolution(error="bad_target")
        for query, partial in ((f"in:#{name}", False), (f"in:#{name}*", True)):
            result = self.search_checked(query, request_id, count=100)
            if result.error:
                return SlackChannelResolution(error=result.error)
            candidates = {
                m.channel_id: m
                for m in result.matches
                if re.fullmatch(r"[CG][A-Za-z0-9]{1,32}", m.channel_id)
                and (
                    name in normalize_channel_name(m.channel_name)
                    if partial
                    else name == normalize_channel_name(m.channel_name)
                )
            }
            if partial and result.total > len(result.matches):
                return SlackChannelResolution(error="ambiguous_channel")
            if len(candidates) > 1:
                return SlackChannelResolution(error="ambiguous_channel")
            if candidates:
                match = next(iter(candidates.values()))
                return SlackChannelResolution(
                    channel_id=match.channel_id,
                    is_public=(
                        match.channel_id.startswith("C")
                        and match.channel_is_private is False
                        and match.channel_is_mpim is False
                        and match.channel_is_im is not True
                        and match.channel_is_group is not True
                    ),
                )
        return SlackChannelResolution(error="channel_not_found")

    def read_period_checked(
        self,
        channel_id: str,
        request_id: str,
        *,
        oldest: str,
        latest: str,
        thread_ts: str = "",
        max_messages: int = 1000,
        max_pages: int = 10,
        deadline: float | None = None,
    ) -> SlackThreadRead:
        """期間の本文をページ送りで読む。API・件数・時間は有界、途中失敗は空にしない。

        history/replies とも半開区間 [oldest, latest)。返信取得でも同じ期間を指定する。
        cursor が無い has_more や循環 cursor は完了と誤認せず truncated として返す。
        """
        if not channel_id:
            return SlackThreadRead(error="bad_target")
        max_messages = max(1, min(max_messages, 1000))
        max_pages = max(1, min(max_pages, 10))
        deadline = deadline if deadline is not None else time.monotonic() + 30
        messages: dict[str, SlackMessage] = {}
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            if time.monotonic() >= deadline:
                return SlackThreadRead(messages=tuple(messages.values()), truncated=True)
            params: dict[str, Any] = {
                "channel": channel_id,
                "oldest": oldest,
                "latest": latest,
                "inclusive": True,
                "limit": min(100, max_messages - len(messages)),
            }
            if cursor:
                params["cursor"] = cursor
            method: Any = self._client.conversations_history
            if thread_ts:
                params["ts"] = thread_ts
                method = self._client.conversations_replies
            try:

                async def fetch(method: Any = method, params: dict[str, Any] = params) -> Any:
                    return await asyncio.wait_for(method(**params), timeout=15)

                resp = _run_sync(fetch)
                if resp.get("ok") is False:
                    return SlackThreadRead(error=str(resp.get("error") or "api_error"))
                raw = resp.get("messages")
                if not isinstance(raw, list) or any(not isinstance(m, dict) for m in raw):
                    return SlackThreadRead(error="bad_response")
                for item in raw:
                    message = _message_from_raw(item)
                    if not message.ts or not re.fullmatch(r"\d+\.\d+", message.ts):
                        return SlackThreadRead(error="bad_response")
                    if float(oldest) <= float(message.ts) < float(latest):
                        messages[message.ts] = message
                    if len(messages) >= max_messages:
                        break
                metadata = resp.get("response_metadata") or {}
                if not isinstance(metadata, dict):
                    return SlackThreadRead(error="bad_response")
                next_cursor = str(metadata.get("next_cursor") or "").strip()
                more = bool(next_cursor or resp.get("has_more"))
            except Exception as exc:
                code = _slack_error_code(exc)
                logger.warning("slack_user_period_failed", request_id=request_id, slack_error=code)
                return SlackThreadRead(error=code)
            ordered = tuple(sorted(messages.values(), key=lambda m: float(m.ts)))
            if not more:
                return SlackThreadRead(messages=ordered, truncated=len(raw) > params["limit"])
            if len(messages) >= max_messages or not next_cursor or next_cursor in seen_cursors:
                return SlackThreadRead(messages=ordered, truncated=True)
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return SlackThreadRead(
            messages=tuple(sorted(messages.values(), key=lambda m: float(m.ts))), truncated=True
        )

    def search_period_checked(
        self,
        channel_id: str,
        request_id: str,
        *,
        oldest: str,
        latest: str,
        max_pages: int = 10,
        deadline: float | None = None,
    ) -> SlackSearchRead:
        """history に現れない古い親への返信を検索で発見する。抜粋は要約本文に使わない。

        Slack 検索の日付境界・タイムゾーン差を避けるため前後1日を検索し、呼び出し側で
        ts を期間に絞る。API 予算は最大10ページ・1000件。
        """
        after = (datetime.fromtimestamp(float(oldest), UTC) - timedelta(days=1)).date()
        before = (datetime.fromtimestamp(float(latest), UTC) + timedelta(days=1)).date()
        query = f"in:{channel_id} after:{after} before:{before}"
        deadline = deadline if deadline is not None else time.monotonic() + 30
        matches: dict[str, SlackSearchMatch] = {}
        for page in range(1, max(1, min(max_pages, 10)) + 1):
            if time.monotonic() >= deadline:
                return SlackSearchRead(matches=tuple(matches.values()), truncated=True)
            result = self.search_checked(query, request_id, count=100, page=page)
            if result.error:
                return SlackSearchRead(error=result.error)
            previous = len(matches)
            matches.update((m.ts, m) for m in result.matches)
            if result.total <= page * 100:
                return SlackSearchRead(
                    matches=tuple(matches.values()),
                    total=result.total,
                    truncated=len(matches) < result.total,
                )
            if len(matches) == previous:
                break
        return SlackSearchRead(matches=tuple(matches.values()), total=result.total, truncated=True)

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
                out.append(_search_match_from_raw(m))
            except Exception:  # 1 件の欠損で全体を落とさない
                continue
        logger.info(
            "slack_user_search",
            request_id=request_id,
            returned=len(out),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return out

    def search_checked(
        self, query: str, request_id: str, *, count: int = 10, page: int = 1
    ) -> SlackSearchRead:
        """search.messages を **error code つき** で呼ぶ（1 ページ・読み取り専用）。

        `search` は fail-open で「トークン切れ」「API 障害」「0 件」が全部 `[]` になる。
        利用者へ「見つかりませんでした」と答える用途（slack_search）は、失敗を 0 件と
        取り違えないようこちらを使う。既存の `search` の挙動は変えない。
        """
        if not query or not query.strip():
            return SlackSearchRead(error="no_query")
        start = time.perf_counter()
        try:
            resp = _run_sync(
                lambda: asyncio.wait_for(
                    self._client.search_messages(
                        query=query, count=count, sort="timestamp", page=page
                    ),
                    timeout=15,
                )
            )
        except Exception as e:  # fail-closed（error code を上へ返す）
            code = _slack_error_code(e)
            logger.warning(
                "slack_user_search_checked_failed",
                request_id=request_id,
                error=type(e).__name__,
                slack_error=code,  # G8: 検索語・本文は出さない
            )
            return SlackSearchRead(error=code)
        # slack_sdk は通常 ok:false で例外を投げるが、ok:false が素通りしても落とさない。
        if resp.get("ok") is False:
            return SlackSearchRead(error=str(resp.get("error") or "api_error"))
        block = resp.get("messages")
        raw = block.get("matches") if isinstance(block, dict) else None
        if not isinstance(raw, list):
            # 形が想定外＝「0 件」とは言えない（黙って空を返さない）。
            logger.warning("slack_user_search_checked_bad_response", request_id=request_id)
            return SlackSearchRead(error="bad_response")
        out: list[SlackSearchMatch] = []
        for m in raw:
            try:
                out.append(_search_match_from_raw(m))
            except Exception:  # 1 件の欠損で全体を落とさない
                continue
        total = _search_total(block, len(out))
        logger.info(
            "slack_user_search_checked",
            request_id=request_id,
            returned=len(out),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return SlackSearchRead(matches=tuple(out), total=total)


def _search_total(block: dict[str, Any], fallback: int) -> int:
    """search.messages の総ヒット数（``total`` → ``paging.total`` → 返ってきた件数）。"""
    paging = block.get("paging")
    for value in (block.get("total"), paging.get("total") if isinstance(paging, dict) else None):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return max(value, fallback)
    return fallback
