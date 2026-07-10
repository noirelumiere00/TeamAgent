"""schedule_propose Skill＋slot_finder（v0.3 Task4）のテスト（外部I/O無し）。

検証主眼: 空き枠計算（土日除外・翌営業日以降・busy重複除外・別日優先・上限）、
候補本文の決定的生成、skill フロー（下書き＋透明ホールド・旧連携の graceful degradation・
冪等・fail-closed）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from teamagent.adapters.gcalendar_client import (
    DuplicateEventError,
    FreeBusyBlock,
    InsertedEvent,
)
from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.draft_token import encode_draft_token
from teamagent.skills.schedule_propose.schema import ScheduleProposeInput
from teamagent.skills.schedule_propose.skill import ScheduleProposeSkill
from teamagent.skills.schedule_propose.slot_finder import (
    build_proposal_body,
    find_slots,
    format_candidates_ja,
)

ME = "me@vectorinc.co.jp"
_JST = _dt.timezone(_dt.timedelta(hours=9))
# 2026-07-10 は金曜（翌営業日=7/13 月曜）。
_NOW = _dt.datetime(2026, 7, 10, 9, 0, tzinfo=_JST)
_CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"


@pytest.fixture(autouse=True)
def _hmac_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "test-secret")


# ── slot_finder ─────────────────────────────────────────────────────────────


def test_slots_skip_weekend_and_start_next_business_day() -> None:
    slots = find_slots([], now=_NOW)
    assert len(slots) == 3
    # 金曜起点 → 土日を飛ばし月/火/水（別日優先）。
    assert [s.date().isoformat() for s, _ in slots] == ["2026-07-13", "2026-07-14", "2026-07-15"]
    assert all(s.weekday() < 5 for s, _ in slots)
    assert slots[0][0].hour == 10 and (slots[0][1] - slots[0][0]) == _dt.timedelta(hours=1)


def test_slots_avoid_busy_blocks() -> None:
    busy = [
        # 月曜 10-12 が埋まっている → 月曜の最初の空きは 12:00。
        FreeBusyBlock(start="2026-07-13T10:00:00+09:00", end="2026-07-13T12:00:00+09:00"),
    ]
    slots = find_slots(busy, now=_NOW)
    assert slots[0][0] == _dt.datetime(2026, 7, 13, 12, 0, tzinfo=_JST)


def test_slots_partial_overlap_excluded() -> None:
    busy = [FreeBusyBlock(start="2026-07-13T10:30:00+09:00", end="2026-07-13T10:45:00+09:00")]
    slots = find_slots(busy, now=_NOW)
    assert slots[0][0].hour == 11  # 10:00-11:00 は 30-45 分と重なるので不可


def test_slots_fully_busy_week_returns_empty() -> None:
    busy = [
        FreeBusyBlock(start=f"2026-07-{d}T00:00:00+09:00", end=f"2026-07-{d}T23:59:00+09:00")
        for d in range(13, 20)
    ]
    assert find_slots(busy, now=_NOW) == []


def test_slots_second_pass_fills_same_day() -> None:
    # 火水木金が全日 busy → 月曜だけ空き → 同日から3枠補完。
    busy = [
        FreeBusyBlock(start=f"2026-07-{d}T00:00:00+09:00", end=f"2026-07-{d}T23:59:00+09:00")
        for d in (14, 15, 16, 17)
    ]
    slots = find_slots(busy, now=_NOW)
    assert len(slots) == 3
    assert all(s.date().isoformat() == "2026-07-13" for s, _ in slots)


def test_candidates_ja_format_and_body() -> None:
    slots = find_slots([], now=_NOW)
    text = format_candidates_ja(slots)
    assert "①7/13(月) 10:00〜11:00" in text
    body = build_proposal_body(slots)
    assert "以下の日程でご都合はいかがでしょうか" in body and "①7/13(月)" in body
    assert "様" not in body  # 宛名は書かない（本人が整える）


# ── skill ───────────────────────────────────────────────────────────────────


@dataclass
class _Tok:
    scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.modify", _CAL_SCOPE)


class _Store:
    def __init__(self, tok: Any) -> None:
        self._tok = tok

    def get(self, email: str) -> Any:
        return self._tok


@dataclass
class _FakeGCal:
    busy: list[FreeBusyBlock] = field(default_factory=list)
    inserts: list[dict[str, Any]] = field(default_factory=list)
    dup_after: int = 10**9  # この回数目以降 DuplicateEventError

    def freebusy(self, request_id: str, **kw: Any) -> list[FreeBusyBlock]:
        return self.busy

    def insert_event(self, request_id: str, **kw: Any) -> InsertedEvent:
        self.inserts.append(kw)
        if len(self.inserts) > self.dup_after:
            raise DuplicateEventError("dup")
        return InsertedEvent(
            event_id="e",
            html_link="https://cal/x",
            summary="",
            start="",
            end="",
            status="tentative",
        )


class _FakeDigestSkill:
    """generate_draft_for_thread の差し替え（下書き経路はダイジェスト側でテスト済み）。"""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def generate_draft_for_thread(
        self, thread_id: str, requester: str, ctx: Any, *, body_override: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"thread_id": thread_id, "body": body_override})
        return self.result


@pytest.fixture
def _fake_md(monkeypatch: pytest.MonkeyPatch) -> _FakeDigestSkill:
    """MorningDigestSkill を fake に差し替え（monkeypatch＝テスト間リークなし）。"""
    import teamagent.skills.morning_digest.skill as md_mod

    fake_digest = _FakeDigestSkill(
        {"created": True, "already": False, "thread_url": "https://mail/x"}
    )

    class _FakeMD:
        def __init__(self, token_store: Any = None) -> None:
            pass

        def generate_draft_for_thread(self, *a: Any, **kw: Any) -> dict[str, Any]:
            return fake_digest.generate_draft_for_thread(*a, **kw)

    monkeypatch.setattr(md_mod, "MorningDigestSkill", _FakeMD)
    return fake_digest


def _skill(
    gcal: _FakeGCal | None = None,
    tok: Any = "default",
) -> tuple[ScheduleProposeSkill, _FakeGCal]:
    gcal = gcal or _FakeGCal()
    token = _Tok() if tok == "default" else tok
    skill = ScheduleProposeSkill(
        token_store=_Store(token),
        gcalendar_factory=lambda _t: gcal,
        now_factory=lambda: _NOW,
    )
    return skill, gcal


def _run(skill: ScheduleProposeSkill, token: str) -> Any:
    return skill.run(
        ScheduleProposeInput(schedule_token=token),
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )


def _tok() -> str:
    return encode_draft_token("T1", ME)


def test_happy_path_draft_and_transparent_holds(_fake_md: _FakeDigestSkill) -> None:
    skill, gcal = _skill()
    fake_digest = _fake_md
    out = _run(skill, _tok())
    assert out.created and out.holds_created == 3
    assert "候補 3 件" in out.message and "仮予定 3 件" in out.message
    # 下書き本文は決定的な候補入りテキスト。
    assert "以下の日程でご都合はいかがでしょうか" in fake_digest.calls[0]["body"]
    # ホールドは tentative かつ transparent（自分の空き枠を潰さない）。
    for ins in gcal.inserts:
        assert ins["tentative"] is True and ins["transparent"] is True
        assert ins["summary"].startswith("仮:")
        assert ins["event_id"]  # 冪等キー


def test_invalid_token_fail_closed(_fake_md: _FakeDigestSkill) -> None:
    skill, gcal = _skill()
    out = _run(skill, "garbage.token")
    assert out.error == "expired" and gcal.inserts == []


def test_old_scope_creates_draft_without_holds(_fake_md: _FakeDigestSkill) -> None:
    old_tok = _Tok(scopes=("https://www.googleapis.com/auth/calendar.readonly",))
    skill, gcal = _skill(tok=old_tok)
    out = _run(skill, _tok())
    assert out.created and out.holds_created == 0
    assert gcal.inserts == []  # 書込スコープ無し→ホールドはスキップ（graceful）
    assert "仮予定は未作成" in out.message


def test_no_slots_returns_guidance(_fake_md: _FakeDigestSkill) -> None:
    busy = [
        FreeBusyBlock(start=f"2026-07-{d}T00:00:00+09:00", end=f"2026-07-{d}T23:59:00+09:00")
        for d in range(13, 20)
    ]
    skill, _ = _skill(_FakeGCal(busy=busy))
    out = _run(skill, _tok())
    assert out.error == "no_slots" and _fake_md.calls == []  # 下書きは作らない


def test_existing_draft_short_circuits(_fake_md: _FakeDigestSkill) -> None:
    _fake_md.result = {"created": False, "already": True, "thread_url": "u"}
    skill, gcal = _skill()
    out = _run(skill, _tok())
    assert out.already and gcal.inserts == []  # 既存下書きありならホールドも置かない


def test_duplicate_holds_counted_as_success(_fake_md: _FakeDigestSkill) -> None:
    skill, _ = _skill(_FakeGCal(dup_after=1))  # 2件目以降は既存ホールド
    out = _run(skill, _tok())
    assert out.created and out.holds_created == 3  # 連打でも数は変わらない


def test_hold_and_confirm_ids_do_not_collide() -> None:
    """F1 回帰: 同一スロットでも 🗓ホールドと 📅本登録は別 id（衝突すると本登録が 409 で死ぬ）。"""
    from teamagent.skills.morning_digest.event_token import stable_event_id

    s, e = "2026-07-15T14:00:00+09:00", "2026-07-15T15:00:00+09:00"
    assert stable_event_id(s, e, ME, kind="hold") != stable_event_id(s, e, ME)


def test_freebusy_failure_distinct_from_no_slots(_fake_md: _FakeDigestSkill) -> None:
    """F3 回帰: API 障害を「空き枠なし」と誤報しない（偽の事実を断言しない）。"""

    class _BoomGCal:
        def freebusy(self, request_id: str, **kw: Any) -> Any:
            raise RuntimeError("api down")

    skill = ScheduleProposeSkill(
        token_store=_Store(_Tok()),
        gcalendar_factory=lambda _t: _BoomGCal(),
        now_factory=lambda: _NOW,
    )
    out = _run(skill, _tok())
    assert out.error == "freebusy_failed"
    assert "取得できませんでした" in out.message and "空き枠が見つかりません" not in out.message


def test_auto_draft_excludes_scheduling_request() -> None:
    """F2 回帰: 朝の自動下書きは scheduling_request スレッドを対象外にする
    （汎用下書きが先に付くと 🗓 が already 短絡で永久に沈黙するため）。"""
    import inspect

    from teamagent.skills.morning_digest.skill import MorningDigestSkill

    src = inspect.getsource(MorningDigestSkill._create_drafts)
    assert "scheduling_request" in src  # 除外条件が存在する（結合は下の実挙動でも担保）

    from teamagent.skills.morning_digest.schema import MailDigestItem

    skill = MorningDigestSkill(token_store=None)
    items = [
        MailDigestItem(
            counterpart_masked="a***@x",
            importance="high",
            to_self=True,
            scheduling_request=True,
        )
    ]

    class _Msg:
        headers: ClassVar[dict[str, str]] = {"From": "c@x.com", "To": ME, "Subject": "s"}

    class _GmailNoCall:
        def list_drafts(self, rid: str, **_: Any) -> list[Any]:
            raise AssertionError("対象ゼロなら gmail に触れないはず")

    from teamagent.skills.morning_digest.schema import MorningDigestInput

    # 対象が scheduling_request のみ → targets 空 → (0, 0.0) 即返し（gmail 未接触）。
    created, cost = skill._create_drafts(
        object(),
        ME,
        MorningDigestInput(max_drafts=3),
        [_Msg()],
        items,
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )
    assert created == 0 and cost == 0.0
