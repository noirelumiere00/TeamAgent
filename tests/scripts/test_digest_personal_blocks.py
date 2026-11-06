"""カレンダーにタスク（作業枠）を入れている人の扱い（2026-10-05 小俣さん指摘）。

固定すること:
- 判定（calendar_window.is_personal_block）: ゲストも会議リンクも無い予定＝タスク枠。
  ゲストがいる・会議リンクがある・参加者が分からない（None）は会議扱い
- 朝のサマリーの送信時刻（planner と早出の注記）は、タスク枠を「最初の予定」に数えない
- 予定を写すとき（morning_digest skill）に本番の CalendarEvent から判定が乗る
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import CalendarEvent
from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.schema import CalendarEventItem, MorningDigestOutput

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    name = "run_morning_digest_personal_blocks_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()
DAY = _dt.date(2026, 10, 5)


@pytest.fixture(autouse=True)
def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(2026, 10, 5, 6, 0, tzinfo=calwin.JST)
    )
    monkeypatch.delenv("MORNING_DIGEST_REMIND_PERSONAL_BLOCKS", raising=False)


def _event(start: str, *, attendees: tuple[str, ...] = (), url: str = "") -> CalendarEvent:
    """本番の CalendarEvent（_base_fields と同じ形：ゲストが無ければ attendees は空タプル）。"""
    return CalendarEvent(
        event_id="e", summary="x", start=start, end=start, attendees=attendees, meeting_url=url
    )


# ── 判定 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("attendees", "url", "expected"),
    [
        ((), "", True),  # 自分で入れた作業枠
        (("me@vectorinc.co.jp", "boss@vectorinc.co.jp"), "", False),  # ゲストあり
        ((), "https://meet.google.com/abc", False),  # 会議リンクだけ
        (None, "", False),  # 参加者が分からない＝会議扱い
        ((), "   ", True),
    ],
)
def test_is_personal_block(attendees: Any, url: str, expected: bool) -> None:
    assert calwin.is_personal_block(attendees, url) is expected


# ── 送信時刻（planner）──────────────────────────────────────


class _Cal:
    def __init__(self, events: list[CalendarEvent]) -> None:
        self.events = events

    def list_events(self, request_id: str, **kw: Any) -> list[CalendarEvent]:
        return self.events


def test_planner_ignores_task_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """7:00 のタスク枠で 6:30 に起こさない。最初の会議（10:00）の 30 分前＝9:30。

    変異: planner の is_personal_block を外すと 7:00 起点の 6:30 になり赤。
    """
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    cal = _Cal(
        [
            _event("2026-10-05T07:00:00+09:00"),  # メール処理（タスク枠）
            _event("2026-10-05T10:00:00+09:00", attendees=("a@x.jp", "b@y.jp")),
        ]
    )
    plan = mod._plan_send_time(cal, DAY, "r")
    assert (plan.fire_at.hour, plan.fire_at.minute) == (9, 30)


def test_planner_with_only_task_blocks_uses_default_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    plan = mod._plan_send_time(_Cal([_event("2026-10-05T07:00:00+09:00")]), DAY, "r")
    assert plan.no_timed_event is True


def test_early_notice_ignores_task_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """発火側の再計算（早出の注記）も planner と同じくタスク枠を数えない。

    変異: _early_notice の personal_block 除外を外すと 6:00 張り付き扱いで True になり赤。
    """
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    monkeypatch.setattr(mod, "_mode", lambda: "single")
    digest = MorningDigestOutput(
        user_email_masked="k***@vectorinc.co.jp",
        calendar_events=[
            CalendarEventItem(start_at="2026-10-05T06:00:00+09:00", personal_block=True),
            CalendarEventItem(start_at="2026-10-05T10:00:00+09:00"),
        ],
    )
    monkeypatch.setattr(mod, "_digest_date", lambda _d: DAY)
    assert mod._early_notice(digest) is False


# 直前リマインドの選別テストは廃止。登録ゼロは test_digest_preferences_runner.py で確認。


# ── 予定を写すときに判定が乗る（本番の CalendarEvent から）─────────────


def test_digest_items_carry_personal_block_from_real_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google の生 JSON → extract_events → _collect_calendar で判定が乗る。

    ゲストのいない予定は Google が attendees を返さない（＝空タプル）。
    変異: skill の personal_block= を外すと既定 False のままで赤。
    """
    from teamagent.adapters.gcalendar_client import extract_events
    from teamagent.skills.base import SkillContext
    from teamagent.skills.morning_digest.schema import MorningDigestInput
    from teamagent.skills.morning_digest.skill import MorningDigestSkill

    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(2026, 10, 5, 8, 0, tzinfo=calwin.JST)
    )
    raw = [
        {
            "id": "task",
            "summary": "タスク確認",
            "start": {"dateTime": "2026-10-05T10:30:00+09:00"},
            "end": {"dateTime": "2026-10-05T11:00:00+09:00"},
        },
        {
            "id": "mtg",
            "summary": "クライアント定例",
            "start": {"dateTime": "2026-10-05T14:00:00+09:00"},
            "end": {"dateTime": "2026-10-05T15:00:00+09:00"},
            "attendees": [{"email": "me@vectorinc.co.jp", "self": True}, {"email": "c@client.jp"}],
        },
        {
            "id": "meet",
            "summary": "1on1",
            "start": {"dateTime": "2026-10-05T16:00:00+09:00"},
            "end": {"dateTime": "2026-10-05T16:30:00+09:00"},
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        },
    ]

    class _GCal:
        def list_events(self, request_id: str, **kw: Any) -> list[Any]:
            return extract_events(raw)

    skill = MorningDigestSkill(gcalendar=_GCal())
    items = skill._collect_calendar(
        object(), MorningDigestInput(), SkillContext(request_id="r", metadata={})
    )
    assert [(i.summary_display, i.personal_block) for i in items] == [
        ("タスク確認", True),
        ("クライアント定例", False),
        ("1on1", False),
    ]
