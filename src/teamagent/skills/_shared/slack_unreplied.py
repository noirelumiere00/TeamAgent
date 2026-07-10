"""Slack 返信漏れ（未返信メンション）検知 Provider（v0.3 Task 1）。

朝ダイジェストの「Slack 返信漏れ」セクション用に、本人 xoxp で
「horizon 日以内に自分がメンションされ、その後そのスレッドで自分が発言していない」
メッセージを集める。判定はビジネスロジックなので skills 層（本モジュール）に置き、
Slack I/O は adapters 層の :class:`SlackUserReader` に委譲する（3層分離）。

設計原則:
  - **fail-open**: トークン無し・scope 不足・API 失敗はすべて空リスト（朝ダイジェスト
    全体を絶対に止めない。コスト/付加機能は fail-open＋可観測性、の統一原則）。
  - **API 呼び出し上限**: search 1 回＋conversations.replies 最大 ``max_thread_checks``
    回に固定（非 Marketplace アプリのレート制限が未実測のため保守的に。制限に当たった
    呼び出しは SlackUserReader 側の fail-open で空になり、本 Provider は判定不能として
    その候補を **skip する**＝証拠なしに「未返信」を主張しない）。
  - **G8**: ログは件数・latency のみ。本文・permalink・channel 名は出さない。

既知の限界（v1・意図的）:
  - スレッド外（チャンネル直下）のメンションに「スレッドを使わず後続メッセージで
    返信した」ケースは検知できず未返信扱いになる（permalink 付きで出すので本人が
    1 クリックで確認できる。過検知は許容、見逃しよりまし）。
  - リアクションだけで済ませたケースも未返信扱い（SlackMessage に reactions が
    無いため。必要になったら adapter 拡張で対応）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import structlog

logger = structlog.get_logger(__name__)

# JST（zoneinfo を引かずに固定オフセットで足りる用途）。
_JST = timezone(timedelta(hours=9))

# search.messages で一度に見るメンション候補の上限（API 1 回に収める）。
_DEFAULT_SEARCH_COUNT = 20
# conversations.replies を呼ぶスレッド数の上限（レート制限への保守設計）。
_DEFAULT_MAX_THREAD_CHECKS = 10
# ダイジェストに載せる未返信件数の上限（DM の読みやすさ）。
_DEFAULT_MAX_ITEMS = 5


@dataclass(frozen=True)
class UnrepliedMention:
    """未返信と判定されたメンション 1 件（マスク前の生値・ログ厳禁）。"""

    channel_id: str
    channel_name: str
    ts: str
    text: str
    permalink: str
    occurred_at: str  # ISO8601（JST）


class _SlackStore(Protocol):
    def get(self, user_email: str) -> Any: ...


def _thread_root(permalink: str, ts: str) -> str:
    """permalink の ``thread_ts`` クエリからスレッド親 ts を得る（無ければ自身が親扱い）。"""
    try:
        qs = parse_qs(urlparse(permalink).query)
        root = (qs.get("thread_ts") or [""])[0]
        return root or ts
    except Exception:
        return ts


def _ts_float(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _iso_jst(ts: str) -> str:
    f = _ts_float(ts)
    if f <= 0:
        return ""
    return datetime.fromtimestamp(f, tz=_JST).isoformat(timespec="seconds")


class SlackUnrepliedProvider:
    """本人 xoxp で「メンションされたのに未返信」のスレッドを集める（読み取り専用）。"""

    def __init__(
        self,
        slack_store: _SlackStore,
        *,
        reader_factory: Callable[[str], Any] | None = None,
        search_count: int = _DEFAULT_SEARCH_COUNT,
        max_thread_checks: int = _DEFAULT_MAX_THREAD_CHECKS,
        max_items: int = _DEFAULT_MAX_ITEMS,
    ) -> None:
        if reader_factory is None:
            from teamagent.adapters.slack_user_reader import SlackUserReader

            reader_factory = SlackUserReader.from_user_token
        self._store = slack_store
        self._reader_factory = reader_factory
        self._search_count = search_count
        self._max_thread_checks = max_thread_checks
        self._max_items = max_items

    def collect(
        self, user_email: str, horizon_days: int, request_id: str
    ) -> list[UnrepliedMention]:
        """未返信メンションを新しい順で最大 ``max_items`` 件返す。失敗はすべて空。"""
        start = time.perf_counter()
        try:
            token = self._store.get(user_email)
        except Exception as e:  # store 障害でもダイジェストは止めない
            logger.warning(
                "slack_unreplied_store_failed", request_id=request_id, error=type(e).__name__
            )
            return []
        if token is None or not getattr(token, "access_token", ""):
            # 未連携（正常系）。エラーにしない＝指示書の「トークン無しは空リスト」。
            return []
        uid = getattr(token, "slack_user_id", "") or ""
        scopes = tuple(getattr(token, "scopes", ()) or ())
        if not uid or "search:read" not in scopes:
            # 旧スコープで連携済みのユーザー。再連携（Reinstall 後の再認可）が必要。
            logger.info(
                "slack_unreplied_scope_missing",
                request_id=request_id,
                has_uid=bool(uid),
            )
            return []

        try:
            reader = self._reader_factory(token.access_token)
        except Exception as e:  # xoxp 空等（防御的・通常は上で弾ける）
            logger.warning(
                "slack_unreplied_reader_failed", request_id=request_id, error=type(e).__name__
            )
            return []

        # Slack 検索の after: は「その日付より後」（日付単位・排他的）。horizon_days ちょうど
        # 前の日を含めるため +1 日遡る（見逃しより過検知を許容する本機能の哲学と整合）。
        after = (datetime.now(tz=_JST) - timedelta(days=horizon_days + 1)).date().isoformat()
        # 自分へのメンションを新しい順に検索。自分の発言は除外（自己メンション対策）。
        matches = reader.search(f"<@{uid}> after:{after}", request_id, count=self._search_count)

        out: list[UnrepliedMention] = []
        seen_roots: set[tuple[str, str]] = set()
        checks = 0
        for m in matches:
            if len(out) >= self._max_items or checks >= self._max_thread_checks:
                break
            if not m.channel_id or not m.ts:
                continue
            if m.user and m.user == uid:
                continue  # 自分の発言内の自己メンション
            root = _thread_root(m.permalink, m.ts)
            key = (m.channel_id, root)
            if key in seen_roots:
                continue
            seen_roots.add(key)
            thread = reader.read_thread(m.channel_id, root, request_id)
            checks += 1
            if not thread:
                # API 失敗（fail-open で空）＝判定不能。証拠なしに「未返信」と言わない。
                continue
            mention_ts = _ts_float(m.ts)
            replied = any(t.user == uid and _ts_float(t.ts) > mention_ts for t in thread)
            if replied:
                continue
            out.append(
                UnrepliedMention(
                    channel_id=m.channel_id,
                    channel_name=m.channel_name,
                    ts=m.ts,
                    text=m.text,
                    permalink=m.permalink,
                    occurred_at=_iso_jst(m.ts),
                )
            )

        logger.info(
            "slack_unreplied_collected",
            request_id=request_id,
            matches=len(matches),
            thread_checks=checks,
            unreplied=len(out),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return out


__all__ = ["SlackUnrepliedProvider", "UnrepliedMention"]
