"""workspace_search Skill のオフラインテスト（課金0）。

fail-closed（本人未指定/未連携）・DLP マスク・サービス分岐を、InMemoryTokenStore と
monkeypatch した adapter で検証する。実 Google 不要。
"""

from __future__ import annotations

import pytest

import teamagent.adapters.gcalendar_client as cal
import teamagent.adapters.gpeople_client as ppl
from teamagent.adapters.gcalendar_client import CalendarEvent
from teamagent.adapters.gpeople_client import Contact
from teamagent.adapters.oauth_token_store import InMemoryTokenStore, OAuthToken
from teamagent.skills.base import SkillContext
from teamagent.skills.workspace_search.schema import WorkspaceSearchInput
from teamagent.skills.workspace_search.skill import WorkspaceSearchSkill


def _ctx(email: str | None) -> SkillContext:
    meta = {"user_email": email} if email else {}
    return SkillContext(request_id="t", user_id="u", metadata=meta)


def _store_with(email: str) -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(email, OAuthToken(refresh_token="rt", scopes=("calendar.readonly",)))
    return s


def test_fail_closed_no_user_email() -> None:
    skill = WorkspaceSearchSkill(token_store=InMemoryTokenStore())
    with pytest.raises(PermissionError):
        skill.run(WorkspaceSearchInput(service="calendar", query="x"), _ctx(None))


def test_fail_closed_not_connected() -> None:
    skill = WorkspaceSearchSkill(token_store=InMemoryTokenStore())  # token 無し
    with pytest.raises(PermissionError, match="未連携"):
        skill.run(WorkspaceSearchInput(service="calendar", query="x"), _ctx("a@x.com"))


class _FakeCal:
    def list_events(self, request_id: str, *, query: str, max_results: int) -> list[CalendarEvent]:
        return [
            CalendarEvent(
                event_id="e1",
                summary="森ビル 商談",
                start="2026-05-01",
                end="2026-05-01",
                attendees=("tanaka@client.co.jp",),
            )
        ]


def test_calendar_happy_masks_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, OAuthToken] = {}

    def _fake(cls: object, token: OAuthToken) -> _FakeCal:
        captured["token"] = token
        return _FakeCal()

    monkeypatch.setattr(cal.GCalendarClient, "from_user_token", classmethod(_fake))
    skill = WorkspaceSearchSkill(token_store=_store_with("a@x.com"))
    out = skill.run(WorkspaceSearchInput(service="calendar", query="森ビル"), _ctx("a@x.com"))
    assert out.count == 1
    assert out.hits[0].title == "森ビル 商談"
    assert "tanaka@client.co.jp" not in out.hits[0].detail  # メールは DLP マスク
    assert out.owner_masked == "a***@x.com"
    # G1: skill が本人のトークンを adapter に渡している（他人/None でない）
    assert captured["token"].refresh_token == "rt"


def test_fail_closed_blank_user_email() -> None:
    """空白のみの user_email は fail-closed（空 email で RLS/認可が崩れるのを防ぐ）。"""
    skill = WorkspaceSearchSkill(token_store=InMemoryTokenStore())
    with pytest.raises(PermissionError):
        skill.run(WorkspaceSearchInput(service="calendar", query="x"), _ctx("   "))


class _FakePeople:
    def search_contacts(self, query: str, request_id: str, *, page_size: int) -> list[Contact]:
        return [Contact(display_name="田中", emails=("tanaka@client.co.jp",), organization="客社")]


def test_people_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ppl.GPeopleClient, "from_user_token", classmethod(lambda cls, token: _FakePeople())
    )
    skill = WorkspaceSearchSkill(token_store=_store_with("a@x.com"))
    out = skill.run(WorkspaceSearchInput(service="people", query="田中"), _ctx("a@x.com"))
    assert out.count == 1
    assert out.hits[0].title == "田中"


def test_unsupported_service() -> None:
    skill = WorkspaceSearchSkill(token_store=_store_with("a@x.com"))
    with pytest.raises(ValueError, match="未対応"):
        skill.run(WorkspaceSearchInput(service="drive", query="x"), _ctx("a@x.com"))
