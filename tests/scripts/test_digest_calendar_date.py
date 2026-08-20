"""朝ダイジェスト DM の「日付明示」回帰テスト（2026-08-20 ユーザー要望）。

要望: 「本日の予定」ではなく「8/21(金)の予定」のように日付を出してほしい。
（同日の日付ずれ事象で、"今日" 決め打ちの見出しが誤りを隠していた）

旧描画（_format_block_kit）と密度優先描画（_format_block_kit_compact・本番 ON）の
両方が同じ `_fmt_event_time` / 見出しを使うため、両方を検証する。
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.schema import CalendarEventItem, MorningDigestOutput

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"

ME = "me@vectorinc.co.jp"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("run_md_caldate_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_md_caldate_under_test"] = module
    spec.loader.exec_module(module)
    return module


runner = _load()

RENDERERS = ("_format_block_kit", "_format_block_kit_compact")


def _digest(*events: CalendarEventItem, day: str = "2026-08-20") -> MorningDigestOutput:
    return MorningDigestOutput(
        user_email_masked="m***@x",
        calendar_events=list(events),
        calendar_date=day,
    )


def _dump(name: str, digest: MorningDigestOutput) -> str:
    _text, blocks = getattr(runner, name)(digest, ME)
    return str(blocks)


# ── (e) 見出しに日付が入る ──────────────────────────────────────────────


@pytest.mark.parametrize("renderer", RENDERERS)
def test_heading_shows_explicit_date(renderer: str) -> None:
    d = _digest(
        CalendarEventItem(
            summary_display="タスク確認",
            start_at="2026-08-20T10:30:00+09:00",
            end_at="2026-08-20T11:00:00+09:00",
        )
    )
    dump = _dump(renderer, d)
    assert "8/20(木) の予定（1件）" in dump
    assert "今日の予定" not in dump  # 「今日」決め打ちは撤去


@pytest.mark.parametrize("renderer", RENDERERS)
def test_zero_state_heading_shows_explicit_date(renderer: str) -> None:
    dump = _dump(renderer, _digest())
    assert "8/20(木) の予定" in dump and "なし" in dump
    assert "今日の予定" not in dump


@pytest.mark.parametrize("renderer", RENDERERS)
def test_heading_falls_back_to_today_when_calendar_date_missing(
    renderer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧 output（calendar_date 無し）でも JST の今日で描ける（後方互換）。"""
    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(2026, 8, 21, 9, 30, tzinfo=calwin.JST)
    )
    d = MorningDigestOutput(user_email_masked="m***@x")
    dump = _dump(renderer, d)
    assert "8/21(金) の予定" in dump


def test_compact_header_and_calendar_heading_share_the_same_date() -> None:
    """ヘッダ（📬 8/20(木) の朝ダイジェスト）と予定見出しの日付が一致する。"""
    dump = _dump("_format_block_kit_compact", _digest())
    assert "8/20(木) の朝ダイジェスト" in dump and "8/20(木) の予定" in dump


# ── 各行の日付表記 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("renderer", RENDERERS)
def test_same_day_line_has_no_date_prefix(renderer: str) -> None:
    """当日の予定は従来どおり時刻だけ（行が無駄に長くならない）。"""
    d = _digest(
        CalendarEventItem(
            summary_display="定例　VideoTimes",
            start_at="2026-08-20T15:00:00+09:00",
            end_at="2026-08-20T15:30:00+09:00",
        )
    )
    dump = _dump(renderer, d)
    assert "`15:00–15:30`" in dump
    assert "8/20(木) 15:00" not in dump


@pytest.mark.parametrize("renderer", RENDERERS)
def test_other_day_lines_show_the_date(renderer: str) -> None:
    """対象日と違う予定（horizon 拡張時に混ざる）は行に日付を出す。"""
    d = _digest(
        CalendarEventItem(
            summary_display="江畑半休：14時退勤",
            start_at="2026-08-21",
            end_at="2026-08-22",
            all_day=True,
        ),
        CalendarEventItem(
            summary_display="翌日の打合せ",
            start_at="2026-08-21T10:30:00+09:00",
            end_at="2026-08-21T11:00:00+09:00",
        ),
    )
    dump = _dump(renderer, d)
    assert "`8/21(金) 終日`" in dump
    assert "`8/21(金) 10:30–11:00`" in dump


@pytest.mark.parametrize("renderer", RENDERERS)
def test_today_all_day_line_is_plain_zenjitsu(renderer: str) -> None:
    d = _digest(
        CalendarEventItem(
            summary_display="オフィス", start_at="2026-08-20", end_at="2026-08-21", all_day=True
        )
    )
    assert "`終日`" in _dump(renderer, d)


@pytest.mark.parametrize("renderer", RENDERERS)
def test_multi_day_all_day_line_shows_range(renderer: str) -> None:
    """複数日の終日は範囲を出す（end.date 排他 → 最終日は -1 日）。"""
    d = _digest(
        CalendarEventItem(
            summary_display="夏季休暇", start_at="2026-08-19", end_at="2026-08-22", all_day=True
        )
    )
    assert "`終日(8/19–8/21)`" in _dump(renderer, d)


# ── _fmt_event_time 単体（既存呼び出しの後方互換を含む）────────────────


def test_fmt_event_time_backward_compatible_without_target_date() -> None:
    """target_date 無しの旧シグネチャ呼び出しは従来の出力のまま。"""
    assert (
        runner._fmt_event_time("2026-06-25T10:00:00+09:00", "2026-06-25T11:30:00+09:00")
        == "10:00–11:30"
    )
    assert runner._fmt_event_time("2026-06-25", "2026-06-26") == "終日"
    assert runner._fmt_event_time(None, None) == ""
    assert runner._fmt_event_time("2026-06-25T09:00:00+09:00", None) == "09:00"


def test_fmt_event_time_zulu_all_day_is_not_mistaken_for_time() -> None:
    """終日の値が "…T00:00:00Z" 形でも all_day フラグがあれば 09:00 等と誤表示しない。"""
    assert (
        runner._fmt_event_time(
            "2026-08-21T00:00:00Z",
            "2026-08-22T00:00:00Z",
            all_day=True,
            target_date=_dt.date(2026, 8, 20),
        )
        == "8/21(金) 終日"
    )


def test_all_day_events_are_not_scheduled_as_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    """終日は開始 N 分前リマインドの対象外（"…T00:00:00Z" 形でも all_day フラグで弾く）。"""
    import teamagent.adapters.scheduler_client as sc

    calls: list[str] = []

    class _Scheduler:
        def schedule_reminder(self, **kwargs: Any) -> bool:
            calls.append(str(kwargs.get("start_iso")))
            return True

    monkeypatch.setattr(sc.SchedulerClient, "from_env", classmethod(lambda cls: _Scheduler()))
    d = _digest(
        CalendarEventItem(
            summary_display="終日(Z形)",
            start_at="2026-08-21T00:00:00Z",
            end_at="2026-08-22T00:00:00Z",
            all_day=True,
        )
    )
    assert runner._schedule_event_reminders(d, "D0") == 0
    assert calls == []
