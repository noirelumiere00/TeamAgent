"""在籍者名簿（DM 本人メモ v1・M5）。

本人メモには「社内の同僚の名前は覚えてよい・先方担当者の名前は覚えない」（09-25 裁定）。
書き込み前の検査（teamagent.personal_memory.guard）は、
敬称の前の語が ``member_names`` に完全一致すれば同僚として通す。
その名簿を Slack の ``users.list`` から作る。

- 在籍の人間だけを数える（削除済み・bot・アプリ・ゲスト・外部・別ワークスペースは除く）
- 名前は real_name・display_name・first_name・last_name を空白（半角・全角）で分けた語の集合
- 取得は learner と掃除スレッドからだけ（``refresh_if_stale``）。context の再検査は I/O をしない
  ``cached_member_names`` を使う
- 取得に失敗したら直前の成功結果を ``stale_max_s`` まで使い、その後は空集合にする
  （空なら guard は敬称付きの人名をすべて落とす＝fail-closed）。失敗後は ``retry_s`` 空けて
  取り直す（掃除スレッドが 15 秒ごとに呼んでも users.list を連打しない）
- ログは件数・ページ数・型名だけ（名前は出さない）
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

import structlog

logger = structlog.get_logger(__name__)

MAX_PAGES: Final = 20
PAGE_LIMIT: Final = 200
_SPLIT_RE: Final = re.compile(r"[\s　・]+")

FetchPage = Callable[[str | None], Mapping[str, Any]]


def _default_fetch_page(token: str) -> FetchPage:
    from slack_sdk import WebClient

    client = WebClient(token=token, timeout=10)

    def fetch(cursor: str | None) -> Mapping[str, Any]:
        kwargs: dict[str, Any] = {"limit": PAGE_LIMIT}
        if cursor:
            kwargs["cursor"] = cursor
        return client.users_list(**kwargs).data  # type: ignore[return-value]

    return fetch


def _is_active_member(member: Mapping[str, Any], team_id: str) -> bool:
    if member.get("deleted") or member.get("is_bot") or member.get("is_app_user"):
        return False
    if (
        member.get("is_restricted")
        or member.get("is_ultra_restricted")
        or member.get("is_stranger")
    ):
        return False
    if member.get("id") == "USLACKBOT":
        return False
    return member.get("team_id") == team_id


def _name_tokens(member: Mapping[str, Any]) -> set[str]:
    profile = member.get("profile") or {}
    raw = [
        member.get("real_name"),
        profile.get("real_name"),
        profile.get("real_name_normalized"),
        profile.get("display_name"),
        profile.get("display_name_normalized"),
        profile.get("first_name"),
        profile.get("last_name"),
    ]
    tokens: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        for part in _SPLIT_RE.split(value.strip()):
            part = part.strip()
            if part and len(part) <= 30:
                tokens.add(part)
    return tokens


class SlackMemberDirectory:
    def __init__(
        self,
        *,
        team_id: str,
        token: str | None = None,
        fetch_page: FetchPage | None = None,
        ttl_s: float = 6 * 3600,
        stale_max_s: float = 24 * 3600,
        retry_s: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if fetch_page is None:
            if not token:
                raise ValueError("token か fetch_page が必要")
            fetch_page = _default_fetch_page(token)
        self._fetch_page = fetch_page
        self._team_id = team_id
        self._ttl_s = ttl_s
        self._stale_max_s = stale_max_s
        self._retry_s = retry_s
        self._clock = clock
        self._lock = threading.Lock()
        self._names: frozenset[str] = frozenset()
        self._fetched_at: float | None = None
        self._failed_at: float | None = None

    def cached_member_names(self) -> frozenset[str]:
        """I/O をしない。古すぎる名簿は空集合を返す（guard が人名を落とす側に倒れる）。"""
        with self._lock:
            if self._fetched_at is None:
                return frozenset()
            if self._clock() - self._fetched_at > self._stale_max_s:
                return frozenset()
            return self._names

    def refresh_if_stale(self) -> frozenset[str]:
        """TTL を過ぎていれば取り直す（I/O あり。learner と掃除スレッドからだけ呼ぶ）。"""
        with self._lock:
            now = self._clock()
            fresh = self._fetched_at is not None and now - self._fetched_at <= self._ttl_s
            backing_off = self._failed_at is not None and now - self._failed_at < self._retry_s
        if not fresh and not backing_off:
            self._refresh()
        return self.cached_member_names()

    def _refresh(self) -> None:
        names: set[str] = set()
        cursor: str | None = None
        pages = 0
        try:
            while pages < MAX_PAGES:
                data = self._fetch_page(cursor)
                pages += 1
                if not data.get("ok", False):
                    raise RuntimeError("users_list_not_ok")
                for member in data.get("members") or []:
                    if isinstance(member, Mapping) and _is_active_member(member, self._team_id):
                        names |= _name_tokens(member)
                cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "") or None
                if not cursor:
                    break
            else:
                # 上限ページで打ち切った名簿は不完全なので使わない（前回の結果を保つ）
                raise RuntimeError("users_list_too_many_pages")
        except Exception as exc:
            logger.warning(
                "slack_member_directory_refresh_failed", error_type=type(exc).__name__, pages=pages
            )
            with self._lock:
                self._failed_at = self._clock()
            return
        with self._lock:
            self._names = frozenset(names)
            self._fetched_at = self._clock()
            self._failed_at = None
        logger.info("slack_member_directory_refreshed", names=len(names), pages=pages)
