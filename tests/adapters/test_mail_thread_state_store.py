"""InMemoryMailThreadStateStore の単体テスト（状態遷移・due 抽出・語彙検証）。"""

from __future__ import annotations

import datetime as _dt

import pytest

from teamagent.adapters.mail_thread_state_store import (
    STATUS_MUTED,
    STATUS_OPEN,
    STATUS_SNOOZED,
    InMemoryMailThreadStateStore,
)

NOW = _dt.datetime(2026, 6, 19, 0, 0, 0, tzinfo=_dt.UTC)
USER = "s-komata@vectorinc.co.jp"


def test_set_and_get_normalizes_email() -> None:
    store = InMemoryMailThreadStateStore()
    store.set_status("S-Komata@VectorInc.co.jp ", "t1", STATUS_OPEN, subject_scrubbed="件名")
    got = store.get(USER, "t1")
    assert got is not None
    assert got.user_email == USER
    assert got.subject_scrubbed == "件名"


def test_invalid_status_rejected() -> None:
    store = InMemoryMailThreadStateStore()
    with pytest.raises(ValueError):
        store.set_status(USER, "t1", "bogus")


def test_set_status_keeps_subject_when_blank() -> None:
    store = InMemoryMailThreadStateStore()
    store.set_status(
        USER, "t1", STATUS_OPEN, subject_scrubbed="初期件名", counterpart_masked="a***"
    )
    store.set_status(USER, "t1", STATUS_MUTED)  # subject 空 → 既存維持
    got = store.get(USER, "t1")
    assert got.status == STATUS_MUTED
    assert got.subject_scrubbed == "初期件名"
    assert got.counterpart_masked == "a***"


def test_list_due_only_snoozed_past() -> None:
    store = InMemoryMailThreadStateStore()
    store.set_status(USER, "past", STATUS_SNOOZED, snooze_until=NOW - _dt.timedelta(hours=1))
    store.set_status(USER, "future", STATUS_SNOOZED, snooze_until=NOW + _dt.timedelta(days=1))
    store.set_status(USER, "muted", STATUS_MUTED)
    due = store.list_due(NOW)
    ids = {d.thread_id for d in due}
    assert ids == {"past"}


def test_reopen_after_reminder_clears_snooze() -> None:
    store = InMemoryMailThreadStateStore()
    store.set_status(USER, "t1", STATUS_SNOOZED, snooze_until=NOW - _dt.timedelta(hours=1))
    store.reopen_after_reminder(USER, "t1", NOW)
    got = store.get(USER, "t1")
    assert got.status == STATUS_OPEN
    assert got.snooze_until is None
    assert got.last_notified_at == NOW
    # 再通知後は due に出ない
    assert store.list_due(NOW) == []
