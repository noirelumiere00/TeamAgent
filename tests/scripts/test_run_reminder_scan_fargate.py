"""scripts/run_reminder_scan_fargate.py の単体テスト（実Slack0・実DB0）。

reminder カード生成（純粋）と、due 再通知→状態 open 戻しの配信ロジックを検証する。
scripts/ は package でないため importlib でロードする。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

from teamagent import mail_action_ui as ui
from teamagent.adapters.mail_thread_state_store import (
    STATUS_OPEN,
    STATUS_SNOOZED,
    InMemoryMailThreadStateStore,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_reminder_scan_fargate.py"
NOW = _dt.datetime(2026, 6, 19, 0, 0, 0, tzinfo=_dt.UTC)
USER = "s-komata@vectorinc.co.jp"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("run_reminder_scan_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_reminder_scan_under_test"] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


class _FakeAsyncClient:
    async def users_lookupByEmail(self, *, email: str) -> dict[str, Any]:  # noqa: N802
        return {"ok": True, "user": {"id": "U1"}}

    async def conversations_open(self, *, users: str) -> dict[str, Any]:
        return {"ok": True, "channel": {"id": "D1"}}


class _CapturingSlack:
    def __init__(self) -> None:
        self._client = _FakeAsyncClient()
        self.posts: list[dict[str, Any]] = []

    async def post_message(
        self,
        channel: str,
        text: str,
        request_id: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> Any:
        self.posts.append({"channel": channel, "text": text, "blocks": blocks})

        class _R:
            ok = True

        return _R()


def test_reminder_card_has_buttons_and_thread() -> None:
    store = InMemoryMailThreadStateStore()
    store.set_status(
        USER,
        "thr-7",
        STATUS_SNOOZED,
        snooze_until=NOW - _dt.timedelta(hours=1),
        subject_scrubbed="見積の件",
        counterpart_masked="t***@ex.com",
    )
    item = store.list_due(NOW)[0]
    dump = str(mod.reminder_card_blocks(item))
    assert "再通知" in dump
    assert ui.ACTION_TAKE in dump
    assert ui.ACTION_DONE in dump
    assert "thr-7" in dump


def test_deliver_reminders_posts_and_reopens() -> None:
    store = InMemoryMailThreadStateStore()
    store.set_status(
        USER,
        "thr-7",
        STATUS_SNOOZED,
        snooze_until=NOW - _dt.timedelta(hours=1),
        subject_scrubbed="見積の件",
        counterpart_masked="t***@ex.com",
    )
    # 未来 snooze は対象外
    store.set_status(USER, "thr-future", STATUS_SNOOZED, snooze_until=NOW + _dt.timedelta(days=1))
    slack = _CapturingSlack()
    due = store.list_due(NOW)

    sent = asyncio.run(mod._deliver_reminders(store, slack, due, NOW))

    assert sent == 1
    assert len(slack.posts) == 1
    # 再通知した行は open に戻り、再び due に出ない
    assert store.get(USER, "thr-7").status == STATUS_OPEN
    assert store.list_due(NOW) == []
    # 未来 snooze は触れていない
    assert store.get(USER, "thr-future").status == STATUS_SNOOZED
