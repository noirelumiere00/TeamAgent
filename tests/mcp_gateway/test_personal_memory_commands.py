"""本人のコマンド（一覧・忘れて・止めて・再開・全部消して・告知済み）。"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.personal_memory_store import Principal
from teamagent.mcp_gateway.personal_memory import texts
from teamagent.mcp_gateway.personal_memory.schemas import CommandInput, ObserveInput
from tests.personal_memory.fakes import FakeStore, HermesFake, append, make_runtime

P = Principal("T0123456789", "U0000000A1", "a@vectorinc.co.jp")


def _cmd(runtime: Any, action: str, item_no: int | None = None) -> dict[str, Any]:
    return runtime.command(P, CommandInput(action=action, item_no=item_no))


@pytest.fixture
def runtime() -> Any:
    store = FakeStore()
    store.create(
        P,
        entries=[
            ("user", "返事は短めが好き"),
            ("user", "箇条書きを好む"),
            ("memory", "花王の案件を担当"),
        ],
        views=2,
    )
    rt, _ = make_runtime(store=store, client=HermesFake(append(memory=["資料は PPTX で作る"])))
    return rt


def test_list_numbers_match_forget(runtime: Any) -> None:
    listing = _cmd(runtime, "list")
    assert listing["ok"]
    lines = listing["reply"].splitlines()
    numbered = [line for line in lines if line[:1].isdigit()]
    assert numbered == ["1. 花王の案件を担当", "2. 返事は短めが好き", "3. 箇条書きを好む"]
    assert "管理者に閲覧された回数: 2 回" in listing["reply"]
    assert _cmd(runtime, "forget", 2)["reply"] == texts.forgot(2)
    assert "返事は短めが好き" not in runtime.store.contents(P)
    # 同じ一覧の番号で続けて消せる（消した番号だけ空く）
    assert _cmd(runtime, "forget", 3)["reply"] == texts.forgot(3)
    assert runtime.store.contents(P) == ["花王の案件を担当"]
    assert _cmd(runtime, "forget", 2)["reply"] == texts.NO_SUCH_ITEM
    assert _cmd(runtime, "forget", 9)["reply"] == texts.NO_SUCH_ITEM


def test_forget_requires_listing(runtime: Any) -> None:
    assert _cmd(runtime, "forget", 1)["reply"] == texts.LIST_FIRST
    assert len(runtime.store.contents(P)) == 3


def test_forget_rejected_when_learning_changed_the_list(runtime: Any) -> None:
    _cmd(runtime, "list")
    # 一覧の後に学習が反映された（版が進んだ）
    for i in range(5):
        runtime.observe(P, f"1784424000.{i:06d}", ObserveInput(utterance=f"資料は短めで{i}"))
    runtime.thread_launcher.run_all()
    assert "資料は PPTX で作る" in runtime.store.contents(P)
    assert _cmd(runtime, "forget", 1)["reply"] == texts.LIST_STALE
    assert len(runtime.store.contents(P)) == 4


def test_listing_expires(runtime: Any) -> None:
    now = [0.0]
    runtime.listing._clock = lambda: now[0]
    _cmd(runtime, "list")
    now[0] = 601.0
    assert _cmd(runtime, "forget", 1)["reply"] == texts.LIST_FIRST


def test_list_hides_failing_entries_and_counts_them() -> None:
    store = FakeStore()
    store.create(P, entries=[("memory", "先方の山田様が窓口"), ("memory", "花王の案件を担当")])
    rt, _ = make_runtime(store=store)
    reply = _cmd(rt, "list")["reply"]
    assert "山田" not in reply
    assert "1. 花王の案件を担当" in reply
    assert "ほかに 1 件" in reply


def test_list_without_profile() -> None:
    rt, _ = make_runtime(store=FakeStore())
    assert _cmd(rt, "list")["reply"] == texts.NOT_STARTED


def test_freeze_stops_learning_and_resume_is_explicit(runtime: Any) -> None:
    for i in range(3):
        runtime.observe(P, f"1784424000.{i:06d}", ObserveInput(utterance=f"資料は短めで{i}"))
    assert _cmd(runtime, "freeze")["reply"] == texts.FROZEN
    assert runtime.buffer.pending_utterances(P) == 0
    for i in range(3, 8):
        status = runtime.observe(P, f"1784424000.{i:06d}", ObserveInput(utterance="x"))
        assert status == {"status": "dropped"}
    assert runtime.thread_launcher.pending == 0
    assert runtime.build_context(P)["memo_context"] == ""
    assert "停止中" in _cmd(runtime, "list")["reply"]
    assert _cmd(runtime, "resume")["reply"] == texts.RESUMED
    assert runtime.build_context(P)["items"] == 3


def test_erase_within_window(runtime: Any) -> None:
    now = [1000.0]
    runtime.store.clock = lambda: now[0]
    assert _cmd(runtime, "erase_request")["reply"] == texts.ERASE_CONFIRM
    now[0] += 599
    result = _cmd(runtime, "erase_confirm")
    assert result == {"action": "erase_confirm", "ok": True, "reply": texts.erased(3)}
    assert runtime.store.contents(P) == []
    assert runtime.store.profiles[P.key].state == "frozen"
    assert runtime.store.audit[-1] == "erase_all"


def test_erase_confirm_after_window_does_nothing(runtime: Any) -> None:
    now = [1000.0]
    runtime.store.clock = lambda: now[0]
    _cmd(runtime, "erase_request")
    now[0] += 601
    result = _cmd(runtime, "erase_confirm")
    assert result["ok"] is False
    assert result["reply"] == texts.ERASE_EXPIRED
    assert len(runtime.store.contents(P)) == 3


def test_erase_confirm_without_request(runtime: Any) -> None:
    result = _cmd(runtime, "erase_confirm")
    assert result["reply"] == texts.ERASE_EXPIRED
    assert len(runtime.store.contents(P)) == 3


def test_notice_ack_records_only_when_notice_is_ready() -> None:
    store = FakeStore()
    rt, _ = make_runtime(store=store, notice=None)
    result = _cmd(rt, "notice_ack")
    assert result["ok"] is False
    assert P.key not in store.profiles
    rt2, _ = make_runtime(store=store, notice="告知文")
    assert _cmd(rt2, "notice_ack")["ok"] is True
    assert store.profiles[P.key].noticed is True


def test_store_outage_returns_fixed_reply(runtime: Any) -> None:
    runtime.store.fail = True
    for action in ["list", "freeze", "resume", "erase_request"]:
        result = _cmd(runtime, action)
        assert result == {"action": action, "ok": False, "reply": texts.UNAVAILABLE}


def test_command_phrases_map_to_actions() -> None:
    from teamagent.mcp_gateway.personal_memory.schemas import COMMAND_ACTIONS

    assert set(texts.COMMAND_PHRASES.values()) | {"forget", "notice_ack"} == set(COMMAND_ACTIONS)
    assert texts.FORGET_PHRASE_RE.fullmatch("3番を忘れて")
    assert not texts.FORGET_PHRASE_RE.fullmatch("0番を忘れて")
    assert not texts.FORGET_PHRASE_RE.fullmatch("3番を忘れて、あと資料も")
    # 告知文に書いた操作の語句と、コマンドの正本が一致している
    notice = texts.build_notice("30 日", "総務部")
    assert notice is not None
    for phrase in texts.COMMAND_PHRASES:
        if phrase != "はい、全部消して":
            assert f"「{phrase}」" in notice
    assert "「はい、全部消して」" in texts.ERASE_CONFIRM
