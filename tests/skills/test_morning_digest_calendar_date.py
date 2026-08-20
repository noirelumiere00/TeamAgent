"""朝ダイジェストの「予定の日付」回帰テスト（2026-08-20 本番事象の根治確認）。

事象: 8/20(木) 朝の DM で、Google カレンダー上 **8/21(金) の終日**である
「江畑半休：14時退勤」が「今日の予定」に出た。

実測で確定した真因（仮説の「終日 date を UTC 変換して前日に寄る」は否定済み）:
  1. 取得窓が `datetime.now(UTC)` 起点の移動 24 時間だった。9:30 JST 実行なら窓は
     [8/20 09:30 JST, 8/21 09:30 JST) で、8/21 の終日予定（JST 8/21 00:00 開始）が
     正当に窓へ入る。同時に当日 00:00〜09:30 の予定は窓の手前に落ちて消えていた。
  2. 下流に日付フィルタが無く、見出しが「今日の予定」決め打ちだった。

⚠️ fake は本番の失敗モードを再現すること: `_FakeGCal` は渡された窓で絞り込まず全件返す
（本物の events.list も「窓に重なる予定」を返す＝窓外が混ざるのが本番の挙動）。
窓の正しさは last_kwargs で、混入を落とせるかは skill 側のフィルタで検証する。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.schema import MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill

ME = "me@vectorinc.co.jp"

# 事象当日（木）と、混入した翌日（金）。
TODAY = _dt.date(2026, 8, 20)
TOMORROW = _dt.date(2026, 8, 21)
# 本番の起動時刻（EventBridge cron(30 0 ? * MON-FRI *) = 0:30 UTC = 9:30 JST）。
RUN_AT_JST = _dt.datetime(2026, 8, 20, 9, 30, tzinfo=calwin.JST)


# ── fakes ───────────────────────────────────────────────────────────────


@dataclass
class _Ev:
    """実 CalendarEvent と同じ属性名（start / end / all_day）。"""

    summary: str = ""
    start: str = ""
    end: str = ""
    location: str = ""
    meeting_url: str = ""
    all_day: bool = False


class _FakeGCal:
    def __init__(self, events: list[_Ev]):
        self._events = events
        self.last_kwargs: dict[str, Any] = {}

    def list_events(self, request_id: str, **kwargs: Any) -> list[_Ev]:
        self.last_kwargs = dict(kwargs)
        return self._events  # ⚠️ 窓で絞らない（本番の失敗モードの再現）


class _NoMail:
    """メール経路を無害化（本テストの関心はカレンダーのみ・LLM 課金ゼロ）。"""

    def list_messages(
        self, query: str, request_id: str, max_results: int = 30
    ) -> tuple[list[Any], None]:
        return ([], None)

    def list_drafts(self, request_id: str, **_: Any) -> list[Any]:
        return []


class _FakeTokenStore:
    def get(self, user_email: str) -> Any:
        return object()


def _run(events: list[_Ev], *, horizon: int = 24) -> tuple[Any, _FakeGCal]:
    gcal = _FakeGCal(events)
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore(),
        gmail=_NoMail(),
        gcalendar=gcal,
        bedrock=None,
    )
    ctx = SkillContext(request_id="req-cal", metadata={"user_email": ME})
    out = skill.run(
        MorningDigestInput(max_drafts=0, calendar_horizon_hours=horizon),
        ctx,
    )
    assert not [e for e in out.errors if e.startswith("calendar")], out.errors
    return out, gcal


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """「今日」を 2026-08-20 09:30 JST（事象当日の実行時刻）に固定する。"""
    monkeypatch.setattr(calwin, "now_jst", lambda: RUN_AT_JST)


def _titles(out: Any) -> list[str]:
    return [e.summary_display or e.summary_scrubbed for e in out.calendar_events]


# ── 取得窓 ───────────────────────────────────────────────────────────────


def test_window_starts_at_jst_midnight_not_execution_time() -> None:
    """窓は「JST 当日 00:00 起点」。実行時刻(9:30)起点の移動 24h ではない（真因の直撃）。"""
    _out, gcal = _run([])
    assert gcal.last_kwargs["time_min"] == "2026-08-20T00:00:00+09:00"
    assert gcal.last_kwargs["time_max"] == "2026-08-21T00:00:00+09:00"
    # 旧実装が投げていた UTC 表記の移動窓ではないこと（回帰の決め手）。
    assert "+00:00" not in gcal.last_kwargs["time_min"]
    assert not gcal.last_kwargs["time_min"].startswith("2026-08-20T00:30")


def test_early_morning_event_before_run_time_is_kept() -> None:
    """当日 9:30 より前に始まる予定が落ちない（旧窓では黙って消えていた副次バグ）。"""
    out, _g = _run(
        [_Ev(summary="朝会", start="2026-08-20T08:00:00+09:00", end="2026-08-20T08:30:00+09:00")]
    )
    assert _titles(out) == ["朝会"]


# ── (a) 翌日の終日予定は今日に出ない ────────────────────────────────────


def test_tomorrow_all_day_event_is_excluded() -> None:
    """🔴本件: 8/21(金) の終日「江畑半休：14時退勤」は 8/20 の予定に出ない。

    Google の終日は start.date="2026-08-21" / end.date="2026-08-22"（排他的）。
    """
    events = [
        _Ev(summary="江畑半休：14時退勤", start="2026-08-21", end="2026-08-22", all_day=True),
        _Ev(
            summary="タスク確認", start="2026-08-20T10:30:00+09:00", end="2026-08-20T11:00:00+09:00"
        ),
    ]
    out, _g = _run(events)
    assert _titles(out) == ["タスク確認"]
    assert "江畑半休：14時退勤" not in str(_titles(out))


def test_tomorrow_all_day_event_with_zulu_shaped_date_is_excluded() -> None:
    """終日の値が "2026-08-21T00:00:00Z" 形に整形されて来ても翌日と判定できる。

    値の "T" 有無ではなく API の date key 由来フラグ（all_day）で判定するため。
    """
    events = [
        _Ev(
            summary="江畑半休：14時退勤",
            start="2026-08-21T00:00:00Z",
            end="2026-08-22T00:00:00Z",
            all_day=True,
        )
    ]
    out, _g = _run(events)
    assert _titles(out) == []


def test_yesterday_all_day_event_is_excluded() -> None:
    """逆方向（前日の終日）も出ない。"""
    events = [_Ev(summary="前日の終日", start="2026-08-19", end="2026-08-20", all_day=True)]
    out, _g = _run(events)
    assert _titles(out) == []


# ── (b) 今日の終日予定は出る ────────────────────────────────────────────


def test_today_all_day_event_is_kept() -> None:
    """8/20 の終日（end.date=8/21・排他的）は「今日の予定」に残る。"""
    events = [_Ev(summary="オフィス", start="2026-08-20", end="2026-08-21", all_day=True)]
    out, _g = _run(events)
    assert _titles(out) == ["オフィス"]
    assert out.calendar_events[0].all_day is True
    assert out.calendar_events[0].start_at == "2026-08-20"
    assert out.calendar_events[0].end_at == "2026-08-21"  # 排他的な生値を保持


def test_today_all_day_event_without_end_is_kept() -> None:
    """end.date 欠損の終日も 1 日ぶんとみなして残す（fail-open）。"""
    out, _g = _run([_Ev(summary="終日(end欠損)", start="2026-08-20", end="", all_day=True)])
    assert _titles(out) == ["終日(end欠損)"]


# ── (c) 日をまたぐ複数日の終日 ──────────────────────────────────────────


def test_multi_day_all_day_event_spanning_today_is_kept() -> None:
    """8/19〜8/21 の終日（end.date=8/22）は 8/20 に出る。"""
    events = [_Ev(summary="夏季休暇", start="2026-08-19", end="2026-08-22", all_day=True)]
    out, _g = _run(events)
    assert _titles(out) == ["夏季休暇"]


def test_multi_day_all_day_event_ending_today_boundary_is_excluded() -> None:
    """8/18〜8/19 の終日（end.date=8/20＝排他的）は 8/20 には出ない。

    end.date を「最終日」と誤読すると 8/20 に出てしまう＝排他性の回帰固定。
    """
    events = [_Ev(summary="先週の連休", start="2026-08-18", end="2026-08-20", all_day=True)]
    out, _g = _run(events)
    assert _titles(out) == []


def test_multi_day_all_day_event_starting_tomorrow_is_excluded() -> None:
    """8/21〜8/23 の終日は 8/20 には出ない（開始が窓の外）。"""
    events = [_Ev(summary="来週の出張", start="2026-08-21", end="2026-08-24", all_day=True)]
    out, _g = _run(events)
    assert _titles(out) == []


# ── (d) 時刻付き予定の既存挙動 ──────────────────────────────────────────


def test_timed_events_of_today_are_unchanged() -> None:
    """時刻付きは start/end の生 ISO をそのまま保持（過去バグ「時刻が空」の回帰固定）。"""
    events = [
        _Ev(
            summary="定例　VideoTimes",
            start="2026-08-20T15:00:00+09:00",
            end="2026-08-20T15:30:00+09:00",
            location="本社3F",
            meeting_url="https://meet.google.com/abc-defg-hij",
        )
    ]
    out, _g = _run(events)
    ev = out.calendar_events[0]
    assert ev.summary_display == "定例　VideoTimes"
    assert ev.start_at == "2026-08-20T15:00:00+09:00"
    assert ev.end_at == "2026-08-20T15:30:00+09:00"
    assert ev.all_day is False
    assert ev.location_display == "本社3F"
    assert ev.meeting_url == "https://meet.google.com/abc-defg-hij"


def test_timed_event_tomorrow_is_excluded_and_utc_offset_is_understood() -> None:
    """UTC 表記でも JST に直して判定する（8/20 15:00Z = 8/21 00:00 JST → 翌日）。"""
    events = [
        _Ev(summary="翌日 0 時の会議", start="2026-08-20T15:00:00Z", end="2026-08-20T16:00:00Z"),
        _Ev(summary="当日の会議", start="2026-08-20T01:00:00Z", end="2026-08-20T02:00:00Z"),
    ]
    out, _g = _run(events)
    assert _titles(out) == ["当日の会議"]


def test_unparsable_event_is_kept_fail_open() -> None:
    """日時が解釈できない予定は落とさない（黙って消す方が有害）。"""
    out, _g = _run([_Ev(summary="壊れた予定", start="not-a-date", end="")])
    assert _titles(out) == ["壊れた予定"]


def test_horizon_hours_extends_window_and_keeps_tomorrow() -> None:
    """horizon を伸ばした時だけ翌日が入る（既定 24h では入らない）。"""
    events = [_Ev(summary="江畑半休：14時退勤", start="2026-08-21", end="2026-08-22", all_day=True)]
    out, gcal = _run(events, horizon=48)
    assert gcal.last_kwargs["time_max"] == "2026-08-22T00:00:00+09:00"
    assert _titles(out) == ["江畑半休：14時退勤"]


# ── (e) 対象日が出力に載る（見出しの日付明示の土台）────────────────────


def test_output_carries_target_date() -> None:
    """calendar_date に JST の対象日が入る（描画の見出し「8/20(木) の予定」に使う）。"""
    out, _g = _run([])
    assert out.calendar_date == "2026-08-20"


def test_target_date_is_set_even_when_calendar_fails() -> None:
    """カレンダー取得が落ちても日付は載る（見出しが「今日」に戻らない）。"""

    class _Boom:
        def list_events(self, *_a: Any, **_k: Any) -> list[_Ev]:
            raise RuntimeError("boom")

    skill = MorningDigestSkill(
        token_store=_FakeTokenStore(), gmail=_NoMail(), gcalendar=_Boom(), bedrock=None
    )
    out = skill.run(
        MorningDigestInput(max_drafts=0),
        SkillContext(request_id="req-boom", metadata={"user_email": ME}),
    )
    assert any(e.startswith("calendar") for e in out.errors)
    assert out.calendar_date == "2026-08-20"


# ── calendar_window 単体（純関数）────────────────────────────────────────


def test_jst_day_window_is_midnight_aligned() -> None:
    start, end = calwin.jst_day_window(RUN_AT_JST, 24)
    assert (start.isoformat(), end.isoformat()) == (
        "2026-08-20T00:00:00+09:00",
        "2026-08-21T00:00:00+09:00",
    )
    # UTC の aware datetime を渡しても JST の暦日に揃う（0:30 UTC = 9:30 JST）。
    start2, _e2 = calwin.jst_day_window(_dt.datetime(2026, 8, 20, 0, 30, tzinfo=_dt.UTC), 24)
    assert start2.isoformat() == "2026-08-20T00:00:00+09:00"


def test_event_bounds_all_day_end_is_exclusive() -> None:
    bounds = calwin.event_bounds("2026-08-21", "2026-08-22", all_day=True)
    assert bounds is not None
    assert bounds[0].isoformat() == "2026-08-21T00:00:00+09:00"
    assert bounds[1].isoformat() == "2026-08-22T00:00:00+09:00"


def test_parse_helpers_are_type_safe() -> None:
    """str 以外が来ても例外にせず None（旧実装の TypeError 握り潰しと同じ耐性）。"""
    assert calwin.parse_jst_datetime(None) is None
    assert calwin.parse_jst_datetime(12345) is None  # type: ignore[arg-type]
    assert calwin.parse_jst_date({"date": "2026-08-21"}) is None  # type: ignore[arg-type]
    assert calwin.event_bounds(None, None) is None


def test_parse_jst_datetime_treats_naive_as_jst() -> None:
    """offset 無しは JST とみなす（コンテナ local tz=UTC 解釈で 9 時間ずれるのを防ぐ）。"""
    parsed = calwin.parse_jst_datetime("2026-08-20T10:30:00")
    assert parsed is not None
    assert parsed.isoformat() == "2026-08-20T10:30:00+09:00"


def test_fmt_jst_date_ja_weekday() -> None:
    assert calwin.fmt_jst_date(TODAY) == "8/20(木)"
    assert calwin.fmt_jst_date(TOMORROW) == "8/21(金)"


def test_event_when_label_variants() -> None:
    # 当日の時刻付き＝日付を出さない（従来表記のまま）
    assert (
        calwin.event_when_label(
            "2026-08-20T10:30:00+09:00", "2026-08-20T11:00:00+09:00", target_date=TODAY
        )
        == "10:30–11:00"
    )
    # 翌日の時刻付き＝日付を明示
    assert (
        calwin.event_when_label(
            "2026-08-21T10:30:00+09:00", "2026-08-21T11:00:00+09:00", target_date=TODAY
        )
        == "8/21(金) 10:30–11:00"
    )
    # 当日の終日
    assert (
        calwin.event_when_label("2026-08-20", "2026-08-21", all_day=True, target_date=TODAY)
        == "終日"
    )
    # 翌日の終日
    assert (
        calwin.event_when_label("2026-08-21", "2026-08-22", all_day=True, target_date=TODAY)
        == "8/21(金) 終日"
    )
    # 複数日の終日（end.date 排他 → 最終日は -1 日）
    assert (
        calwin.event_when_label("2026-08-19", "2026-08-22", all_day=True, target_date=TODAY)
        == "終日(8/19–8/21)"
    )
    # UTC 表記は JST へ直して表示（8/20 01:00Z = 10:00 JST）
    assert (
        calwin.event_when_label("2026-08-20T01:00:00Z", "2026-08-20T02:00:00Z", target_date=TODAY)
        == "10:00–11:00"
    )
