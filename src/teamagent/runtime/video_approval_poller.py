"""動画一次FB審査 Phase 2: 投稿管理シートを定期監視し、納品済みの新規動画を
自動で一次審査して Slack に通知する poller。

安全第一の設計（ライブ 16 名環境を勝手に荒らさない）:
- 既定 OFF。`USE_VIDEO_APPROVAL_POLLING=true` かつ `VIDEO_APPROVAL_POLL_CHANNEL`/
  `VIDEO_APPROVAL_SHEET_ID` が揃ったときだけ起動（配線は slack_bot._run）。
- 冪等性: 処理済み management_no を JSON に保存。既定の /tmp は task 内のみ保持し、
  再起動をまたぐ必要がある環境は VIDEO_APPROVAL_STATE_PATH を永続 volume に向ける。
- 初回ベースライン: 既存の納品済みは「処理せず既読化」のみ（バックログの一斉投稿を防ぐ）。
- 投稿のみ。**シート書込はしない**（spreadsheets 再認証までは Phase1 同様 Slack 通知に留める）。
- per-item / per-tick の例外を隔離し、loop は決して落とさない（成功時のみ既読化＝失敗は再試行）。

ロジックは注入された async callable（list_creatives/run_one/post）に依存し、
slack_bot から独立してテストできる。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ProcessedStore:
    """処理済み management_no の永続セット（JSON 配列）。破損/欠落は空で開始。"""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._seen: set[str] = set()
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._seen = {str(x) for x in data}
        except (OSError, json.JSONDecodeError, ValueError):
            logger.warning("video_approval_store_load_failed", path=str(self._path))

    def seen(self, key: str) -> bool:
        return key in self._seen

    def mark(self, key: str) -> None:
        self._seen.add(key)

    def unmark(self, key: str) -> None:
        self._seen.discard(key)  # claim 取り消し（処理失敗時に次ティックへ戻す）

    def __len__(self) -> int:
        return len(self._seen)

    def save(self) -> None:
        """アトミック書込（tmp → os.replace）。失敗は警告のみ（loop を止めない）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(sorted(self._seen), f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("video_approval_store_save_failed", path=str(self._path))


# 注入する callable の型（テスト容易化）
ListCreatives = Callable[[], Awaitable[list[Any]]]
RunOne = Callable[[str], Awaitable[str]]
Post = Callable[[str], Awaitable[None]]


async def poll_once(
    *,
    list_creatives: ListCreatives,
    run_one: RunOne,
    post: Post,
    store: ProcessedStore,
    baseline: bool,
) -> dict[str, int]:
    """1 ティック。baseline=True なら処理せず既読化のみ。集計 dict を返す。

    対象 = 納品動画あり(has_drive_video) ∧ management_no あり ∧ 未処理(store)。
    """
    refs = await list_creatives()
    targets = [
        r
        for r in refs
        if getattr(r, "has_drive_video", False)
        and getattr(r, "management_no", "")
        and not store.seen(r.management_no)
    ]
    stats = {"new": len(targets), "processed": 0, "baselined": 0, "errors": 0}
    for r in targets:
        # claim-before-await: await を挟む前に mark で所有権を確定（並行 poll_once でも
        # 二重処理しない。asyncio は await 間が非分割なので seen→mark はアトミック）。
        if store.seen(r.management_no):  # 別コルーチンが先に claim 済み
            continue
        store.mark(r.management_no)
        if baseline:
            stats["baselined"] += 1
            continue
        try:
            text = await run_one(r.management_no)
            await post(text)
            stats["processed"] += 1
        except Exception:
            store.unmark(r.management_no)  # 失敗は claim を戻し次ティックで再試行
            logger.exception("video_approval_poll_item_failed", management_no=r.management_no)
            stats["errors"] += 1
    if stats["baselined"] or stats["processed"]:
        store.save()
    return stats


async def poll_loop(
    *,
    list_creatives: ListCreatives,
    run_one: RunOne,
    post: Post,
    store: ProcessedStore,
    interval_sec: int,
    baseline_first: bool = True,
) -> None:
    """永続ループ。ティック/アイテムの例外を隔離し、決して停止しない。"""
    first = True
    while True:
        try:
            stats = await poll_once(
                list_creatives=list_creatives,
                run_one=run_one,
                post=post,
                store=store,
                baseline=first and baseline_first,
            )
            logger.info("video_approval_poll_tick", first=first, **stats)
        except Exception:
            logger.exception("video_approval_poll_tick_failed")
        first = False
        await asyncio.sleep(interval_sec)
