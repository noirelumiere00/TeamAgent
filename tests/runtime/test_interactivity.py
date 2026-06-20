"""interactivity ルーター + payload 解析の単体テスト（外部 I/O なし）。"""

from __future__ import annotations

import datetime as _dt

from teamagent import mail_action_ui as ui
from teamagent.adapters.mail_thread_state_store import (
    STATUS_DONE,
    STATUS_MUTED,
    STATUS_OPEN,
    STATUS_SNOOZED,
    InMemoryMailThreadStateStore,
)
from teamagent.runtime.interactivity import (
    DraftResult,
    InteractivityRouter,
    parse_block_actions,
)

NOW = _dt.datetime(2026, 6, 19, 0, 0, 0, tzinfo=_dt.UTC)
USER = "s-komata@vectorinc.co.jp"


def _payload(action_id: str, *, thread="thr-1", sub="") -> dict:
    value = ui.encode_value(thread, "見積の件", "t***@ex.com", sub=sub)
    action: dict = {"type": "button", "action_id": action_id, "value": value}
    if action_id == ui.ACTION_MENU:
        # overflow は selected_option.value で来る
        action = {
            "type": "overflow",
            "action_id": action_id,
            "selected_option": {"value": value},
        }
    return {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "D1"},
        "message": {"ts": "1716000000.0001"},
        "response_url": "https://hooks.slack.com/actions/x",
        "actions": [action],
    }


def test_parse_button() -> None:
    a = parse_block_actions(_payload(ui.ACTION_DONE))
    assert a is not None
    assert a.action_id == ui.ACTION_DONE
    assert a.thread_id == "thr-1"
    assert a.subject == "見積の件"
    assert a.user_id == "U1"
    assert a.message_ts == "1716000000.0001"


def test_parse_overflow_sub_action() -> None:
    a = parse_block_actions(_payload(ui.ACTION_MENU, sub=ui.MENU_MUTE))
    assert a is not None
    assert a.sub_action == ui.MENU_MUTE


def test_parse_non_block_actions_returns_none() -> None:
    assert parse_block_actions({"type": "view_submission"}) is None
    assert parse_block_actions({"type": "block_actions", "actions": []}) is None


def test_done_sets_status_and_shows_undo() -> None:
    store = InMemoryMailThreadStateStore()
    router = InteractivityRouter(store)
    out = router.handle(parse_block_actions(_payload(ui.ACTION_DONE)), USER, now=NOW)
    assert out.status_written == STATUS_DONE
    assert store.get(USER, "thr-1").status == STATUS_DONE
    dump = str(out.blocks)
    assert "対応済み" in dump
    assert ui.ACTION_UNDO in dump


def test_snooze_sets_until_3days() -> None:
    store = InMemoryMailThreadStateStore()
    router = InteractivityRouter(store, snooze_days=3)
    out = router.handle(parse_block_actions(_payload(ui.ACTION_SNOOZE)), USER, now=NOW)
    assert out.status_written == STATUS_SNOOZED
    st = store.get(USER, "thr-1")
    assert st.status == STATUS_SNOOZED
    assert st.snooze_until == NOW + _dt.timedelta(days=3)
    assert "3日後に再通知" in str(out.blocks)


def test_mute_sets_muted() -> None:
    store = InMemoryMailThreadStateStore()
    router = InteractivityRouter(store)
    out = router.handle(
        parse_block_actions(_payload(ui.ACTION_MENU, sub=ui.MENU_MUTE)), USER, now=NOW
    )
    assert out.status_written == STATUS_MUTED
    assert store.get(USER, "thr-1").status == STATUS_MUTED
    assert "今後通知しません" in str(out.blocks)


def test_undo_restores_open_and_buttons() -> None:
    store = InMemoryMailThreadStateStore()
    store.set_status(USER, "thr-1", STATUS_DONE)
    router = InteractivityRouter(store)
    out = router.handle(parse_block_actions(_payload(ui.ACTION_UNDO)), USER, now=NOW)
    assert out.status_written == STATUS_OPEN
    assert store.get(USER, "thr-1").status == STATUS_OPEN
    # open に戻ると [対応する] ボタンが復活する
    assert ui.ACTION_TAKE in str(out.blocks)


def test_take_with_draft_maker_shows_thread_link() -> None:
    store = InMemoryMailThreadStateStore()

    def maker(email: str, thread_id: str) -> DraftResult:
        assert email == USER
        assert thread_id == "thr-1"
        return DraftResult(
            created=True,
            thread_url="https://mail.google.com/mail/u/0/#all/thr-1",
            draft_subject="Re: 見積の件",
        )

    router = InteractivityRouter(store, draft_maker=maker)
    out = router.handle(parse_block_actions(_payload(ui.ACTION_TAKE)), USER, now=NOW)
    dump = str(out.blocks)
    assert "返信下書きを作成しました" in dump
    assert "https://mail.google.com/mail/u/0/#all/thr-1" in dump
    assert "Gmailでスレッドを開く" in dump


def test_take_draft_failure_keeps_buttons_and_warns() -> None:
    store = InMemoryMailThreadStateStore()

    def maker(email: str, thread_id: str) -> DraftResult:
        return DraftResult(created=False, message="メール連携が未完了です")

    router = InteractivityRouter(store, draft_maker=maker)
    out = router.handle(parse_block_actions(_payload(ui.ACTION_TAKE)), USER, now=NOW)
    dump = str(out.blocks)
    assert "⚠️" in dump
    assert "連携" in dump
    assert ui.ACTION_TAKE in dump  # 元のボタンが残る


def test_take_draft_maker_exception_is_graceful() -> None:
    store = InMemoryMailThreadStateStore()

    def maker(email: str, thread_id: str) -> DraftResult:
        raise RuntimeError("boom")

    router = InteractivityRouter(store, draft_maker=maker)
    out = router.handle(parse_block_actions(_payload(ui.ACTION_TAKE)), USER, now=NOW)
    assert out.handled is True
    assert "⚠️" in str(out.blocks)
