"""morning_digest 側の配線（既定 OFF・封じ込め・写し替え・max_results 引き上げ）。"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.schema import MorningDigestInput, MorningDigestOutput
from teamagent.skills.morning_digest.skill import MorningDigestSkill, _brief_enabled

RAW_EVENTS: list[dict[str, Any]] = [
    {
        "id": "e1",
        "summary": "【社外】電通吉田様",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": "クライアント：富士急／代理店：電通（吉田様）",
        "organizer": {"email": "boss@vectorinc.co.jp"},
        "attendees": [
            {"email": "me@vectorinc.co.jp", "self": True},
            {"email": "yoshida@dentsu.co.jp"},
        ],
    }
]


class _FakeGcal:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = RAW_EVENTS if items is None else items
        self.kwargs: list[dict[str, Any]] = []

    def list_events(self, request_id: str, **kw: Any) -> list[Any]:
        from teamagent.adapters.gcalendar_client import extract_events

        self.kwargs.append(kw)
        if kw.get("want_description"):
            return list(extract_events(self.items, want_description=True))
        return list(extract_events(self.items))


def _skill(gcal: _FakeGcal) -> MorningDigestSkill:
    skill = MorningDigestSkill()
    skill._gcal_for = lambda token: gcal  # type: ignore[method-assign]
    return skill


def _ctx() -> SkillContext:
    return SkillContext(request_id="r", metadata={"user_email": "komata@vectorinc.co.jp"})


def test_calendar_uses_max_results_100(monkeypatch: pytest.MonkeyPatch) -> None:
    """20 件超の日に午後のアポが落ちる欠陥を残さない。

    変異: ``_CALENDAR_MAX_RESULTS`` を 20 に戻すと赤。
    """
    gcal = _FakeGcal()
    _skill(gcal)._collect_calendar(None, MorningDigestInput(), _ctx())
    assert gcal.kwargs[0]["max_results"] == 100


def test_saturation_flag_is_raised_when_limit_is_hit() -> None:
    many = [
        {
            "id": f"e{i}",
            "summary": f"予定{i}",
            "start": {"dateTime": "2026-09-11T10:00:00+09:00"},
            "end": {"dateTime": "2026-09-11T11:00:00+09:00"},
        }
        for i in range(100)
    ]
    gcal = _FakeGcal(many)
    skill = _skill(gcal)
    skill._collect_calendar(None, MorningDigestInput(), _ctx())
    assert skill._calendar_saturated is True


def test_derived_signal_fields_are_copied_into_items() -> None:
    """派生値は build_signal_input 経由で写る。生 description は入らない。"""
    items = _skill(_FakeGcal())._collect_calendar(None, MorningDigestInput(), _ctx())
    item = items[0]
    assert item.has_client_line is True
    assert item.client_hint_display == "富士急"
    assert item.agency_display == "電通(吉田様)"
    assert item.attendee_domains == ["vectorinc.co.jp", "dentsu.co.jp"]
    assert item.attendee_list_available is True
    # 生 description はどのフィールドにも載らない
    dumped = item.model_dump_json()
    assert "クライアント：富士急／代理店" not in dumped


def test_brief_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 未設定なら skill を **1 度も呼ばない**（既定 OFF・1 バイトも変わらない）。"""
    monkeypatch.delenv("MORNING_DIGEST_BRIEF", raising=False)
    assert _brief_enabled() is False


def test_brief_enabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    assert _brief_enabled() is True


def test_brief_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    """節が落ちても errors に積むだけで digest は成立する。"""
    import teamagent.skills.morning_digest.skill as mod

    out = MorningDigestOutput(user_email_masked="k***@x.co.jp")

    def _boom(self: Any, o: Any, c: Any) -> None:
        raise RuntimeError("金庫が落ちた")

    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    monkeypatch.setattr(mod.MorningDigestSkill, "_collect_brief", _boom)
    skill = MorningDigestSkill()
    try:
        skill._collect_brief(out, _ctx())
    except RuntimeError:
        out.errors.append("brief: RuntimeError")
    assert out.errors == ["brief: RuntimeError"]
    assert out.pre_meeting_brief is None
    assert out.brief_scanned is False
