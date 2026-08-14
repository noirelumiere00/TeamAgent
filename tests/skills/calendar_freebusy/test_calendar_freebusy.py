"""calendar_freebusy Skill＋free_windows のテスト（外部I/O無し）。

検証主眼: 土日注記の必達（message 先頭）、busy 減算の境界（隣接・包含・日跨ぎ）、
API 障害と「空きなし」の区別、未連携誘導、fail-closed、候補ゼロでも free_windows は返る。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import FreeBusyBlock
from teamagent.skills.base import SkillContext
from teamagent.skills.calendar_freebusy.free_windows import (
    compute_free_windows,
    enumerate_candidate_starts,
    non_business_note,
)
from teamagent.skills.calendar_freebusy.schema import CalendarFreeBusyInput
from teamagent.skills.calendar_freebusy.skill import CalendarFreeBusySkill

ME = "me@vectorinc.co.jp"
_JST = _dt.timezone(_dt.timedelta(hours=9))
# 2026-08-14 は金曜（翌日=8/15 土曜）。
_NOW = _dt.datetime(2026, 8, 14, 9, 0, tzinfo=_JST)
_MON = _dt.date(2026, 8, 17)  # 月曜（平日ケース用）


# ── free_windows（純関数） ───────────────────────────────────────────────────


def test_free_windows_all_free_day() -> None:
    windows = compute_free_windows([], day=_MON)
    assert windows == [
        (
            _dt.datetime(2026, 8, 17, 9, 0, tzinfo=_JST),
            _dt.datetime(2026, 8, 17, 18, 0, tzinfo=_JST),
        )
    ]


def test_free_windows_adjacent_busy_blocks_no_zero_width_window() -> None:
    """境界1: 隣接。10-11 と 11-12 が隣接していても幅0の偽ウィンドウを作らない。"""
    busy = [
        FreeBusyBlock(start="2026-08-17T10:00:00+09:00", end="2026-08-17T11:00:00+09:00"),
        FreeBusyBlock(start="2026-08-17T11:00:00+09:00", end="2026-08-17T12:00:00+09:00"),
    ]
    windows = compute_free_windows(busy, day=_MON)
    assert windows == [
        (
            _dt.datetime(2026, 8, 17, 9, 0, tzinfo=_JST),
            _dt.datetime(2026, 8, 17, 10, 0, tzinfo=_JST),
        ),
        (
            _dt.datetime(2026, 8, 17, 12, 0, tzinfo=_JST),
            _dt.datetime(2026, 8, 17, 18, 0, tzinfo=_JST),
        ),
    ]


def test_free_windows_containing_busy_returns_empty() -> None:
    """境界2: 包含。営業ウィンドウ全体を覆う busy は空きゼロ。"""
    busy = [FreeBusyBlock(start="2026-08-17T08:00:00+09:00", end="2026-08-17T19:00:00+09:00")]
    assert compute_free_windows(busy, day=_MON) == []


def test_free_windows_overnight_busy_clipped_to_day() -> None:
    """境界3: 日跨ぎ。前夜〜当日10時の busy は当日 9-10 だけを潰す。"""
    busy = [FreeBusyBlock(start="2026-08-16T22:00:00+09:00", end="2026-08-17T10:00:00+09:00")]
    windows = compute_free_windows(busy, day=_MON)
    assert windows == [
        (
            _dt.datetime(2026, 8, 17, 10, 0, tzinfo=_JST),
            _dt.datetime(2026, 8, 17, 18, 0, tzinfo=_JST),
        )
    ]


def test_free_windows_drops_slivers_below_min_window() -> None:
    """min_window_min 未満の細切れ（9:00-9:10 の10分）は返さない。"""
    busy = [FreeBusyBlock(start="2026-08-17T09:10:00+09:00", end="2026-08-17T18:00:00+09:00")]
    assert compute_free_windows(busy, day=_MON) == []


def test_non_business_note_weekend_and_weekday() -> None:
    assert "(土)" in non_business_note(_dt.date(2026, 8, 15))
    assert "(日)" in non_business_note(_dt.date(2026, 8, 16))
    assert "祝日" in non_business_note(_dt.date(2026, 8, 15))  # 祝日未判定の明記
    assert non_business_note(_MON) == ""


def test_candidate_starts_respect_duration() -> None:
    windows = [
        (
            _dt.datetime(2026, 8, 17, 9, 0, tzinfo=_JST),
            _dt.datetime(2026, 8, 17, 10, 0, tzinfo=_JST),
        )
    ]
    # 60分はちょうど1候補・90分は収まらないので0候補。
    assert len(enumerate_candidate_starts(windows, duration_min=60)) == 1
    assert enumerate_candidate_starts(windows, duration_min=90) == []


# ── skill ───────────────────────────────────────────────────────────────────


@dataclass
class _Tok:
    scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar.readonly",)


class _Store:
    def __init__(self, tok: Any) -> None:
        self._tok = tok

    def get(self, email: str) -> Any:
        return self._tok


@dataclass
class _FakeGCal:
    busy: list[FreeBusyBlock] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def freebusy(self, request_id: str, **kw: Any) -> list[FreeBusyBlock]:
        self.calls.append(kw)
        return self.busy


def _skill(
    gcal: Any = None,
    tok: Any = "default",
) -> tuple[CalendarFreeBusySkill, Any]:
    gcal = gcal if gcal is not None else _FakeGCal()
    token = _Tok() if tok == "default" else tok
    skill = CalendarFreeBusySkill(
        token_store=_Store(token),
        gcalendar_factory=lambda _t: gcal,
        now_factory=lambda: _NOW,
    )
    return skill, gcal


def _run(skill: CalendarFreeBusySkill, **kw: Any) -> Any:
    return skill.run(
        CalendarFreeBusyInput(**kw),
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )


def test_tomorrow_saturday_note_always_in_message_head() -> None:
    """① date 省略→サーバが明日(8/15 土)を採用し、message 先頭に (土) 注記が必ず入る。"""
    skill, gcal = _skill()
    out = _run(skill)
    assert out.error == ""
    assert out.date_label == "8/15(土)"
    assert "(土)" in out.non_business_note
    assert out.message.startswith(out.non_business_note)  # 注記は message 先頭
    # freebusy の照会窓は対象日 00:00 JST から days=1 日ぶん。
    assert gcal.calls[0]["time_min"] == "2026-08-15T00:00:00+09:00"
    assert gcal.calls[0]["time_max"] == "2026-08-16T00:00:00+09:00"


def test_busy_subtraction_boundaries_via_skill() -> None:
    """② 隣接・包含・日跨ぎの3種を days=3 で一括検証（土日も営業ウィンドウは出す）。"""
    busy = [
        # 8/15(土): 隣接2連 busy → 9-10 と 12-18 が空く。
        FreeBusyBlock(start="2026-08-15T10:00:00+09:00", end="2026-08-15T11:00:00+09:00"),
        FreeBusyBlock(start="2026-08-15T11:00:00+09:00", end="2026-08-15T12:00:00+09:00"),
        # 8/16(日): 終日包含 → 空きゼロ。
        FreeBusyBlock(start="2026-08-16T00:00:00+09:00", end="2026-08-17T00:00:00+09:00"),
    ]
    # ↑の包含 busy は 8/17(月) 0時までなので日跨ぎとしては 8/17 を潰さない。
    skill, _ = _skill(_FakeGCal(busy=busy))
    out = _run(skill, days=3)
    labels = [w.label for w in out.free_windows]
    assert labels == [
        "8/15(土) 09:00〜10:00",
        "8/15(土) 12:00〜18:00",
        "8/17(月) 09:00〜18:00",
    ]
    assert out.busy_count == 3
    assert "8/16(日) は営業時間内（9:00〜18:00 JST）に空きがありません" in out.message


def test_freebusy_failure_distinct_from_no_free_time() -> None:
    """③ API 障害は「空きなし」と別事象（偽の事実を断言しない・F3 と同裁定）。"""

    class _BoomGCal:
        def freebusy(self, request_id: str, **kw: Any) -> Any:
            raise RuntimeError("api down")

    skill, _ = _skill(_BoomGCal())
    out = _run(skill)
    assert out.error == "freebusy_failed"
    assert "取得できませんでした" in out.message
    assert "空きがありません" not in out.message


def test_not_connected_guides_to_oauth_connect() -> None:
    """④ 未連携は error='not_connected'＋『連携』への誘導文。"""
    skill, gcal = _skill(tok=None)
    out = _run(skill)
    assert out.error == "not_connected"
    assert "連携" in out.message
    assert gcal.calls == []  # 未連携なら API に触れない


def test_missing_user_email_fails_closed() -> None:
    """⑤ user_email 欠落は PermissionError（fail-closed）。"""
    skill, _ = _skill()
    with pytest.raises(PermissionError):
        skill.run(CalendarFreeBusyInput(), SkillContext(request_id="r", metadata={}))


def test_oversized_duration_returns_windows_without_candidates() -> None:
    """⑥ duration_min が大きすぎて候補ゼロでも free_windows は返る。"""
    busy = [
        # 8/17(月): 空きは 9-10 と 17-18 の各1時間だけ。
        FreeBusyBlock(start="2026-08-17T10:00:00+09:00", end="2026-08-17T17:00:00+09:00"),
    ]
    skill, _ = _skill(_FakeGCal(busy=busy))
    out = _run(skill, date="2026-08-17", duration_min=480)
    assert out.error == ""
    assert out.candidates == []
    assert [w.label for w in out.free_windows] == [
        "8/17(月) 09:00〜10:00",
        "8/17(月) 17:00〜18:00",
    ]
    assert "開始候補はありませんでした" in out.message


def test_explicit_date_used_and_weekday_has_no_note() -> None:
    """利用者指定の具体日はそのまま使い、平日は注記なし。"""
    skill, _ = _skill()
    out = _run(skill, date="2026-08-17")
    assert out.date_label == "8/17(月)"
    assert out.non_business_note == ""
    assert "(土)" not in out.message


def test_nonexistent_date_fails_closed() -> None:
    """pattern を通る形式でも実在しない日付（2026-02-31）は fail-closed。"""
    skill, gcal = _skill()
    out = _run(skill, date="2026-02-31")
    assert out.error == "bad_date"
    assert gcal.calls == []
