"""Google Calendar / People アダプタのテスト（課金0・fake service）。

抽出ロジックと per-user from_user_token の fail-closed を検証する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gcalendar_client import GCalendarClient, extract_events
from teamagent.adapters.gpeople_client import GPeopleClient, extract_contacts
from teamagent.adapters.oauth_token_store import OAuthToken

# ── Calendar ────────────────────────────────────────────────────────────────


def test_extract_events() -> None:
    items: list[dict[str, Any]] = [
        {
            "id": "e1",
            "summary": "森ビル 商談",
            "start": {"dateTime": "2026-05-01T10:00:00+09:00"},
            "end": {"dateTime": "2026-05-01T11:00:00+09:00"},
            "attendees": [{"email": "a@x.com"}, {"organizer": True}],  # email 無しはスキップ
        },
        {
            "id": "e2",
            "summary": "終日",
            "start": {"date": "2026-05-02"},
            "end": {"date": "2026-05-03"},
        },
    ]
    events = extract_events(items)
    assert [e.event_id for e in events] == ["e1", "e2"]
    assert events[0].summary == "森ビル 商談"
    assert events[0].attendees == ("a@x.com",)
    assert events[1].start == "2026-05-02"  # 終日は date


def test_gcalendar_list_events() -> None:
    svc = MagicMock()
    svc.events().list().execute.return_value = {
        "items": [{"id": "e1", "summary": "面談", "start": {"date": "2026-05-01"}, "end": {}}]
    }
    client = GCalendarClient(service=svc)
    events = client.list_events("t", query="森ビル", max_results=5)
    assert len(events) == 1
    assert events[0].summary == "面談"


# ── People ──────────────────────────────────────────────────────────────────


def test_extract_contacts() -> None:
    results: list[dict[str, Any]] = [
        {
            "person": {
                "names": [{"displayName": "田中太郎"}],
                "emailAddresses": [{"value": "tanaka@client.co.jp"}],
                "organizations": [{"name": "クライアント社"}],
            }
        },
        {"person": {"names": [{"displayName": "名前のみ"}]}},  # email/org 無し
    ]
    contacts = extract_contacts(results)
    assert contacts[0].display_name == "田中太郎"
    assert contacts[0].emails == ("tanaka@client.co.jp",)
    assert contacts[0].organization == "クライアント社"
    assert contacts[1].emails == ()


def test_gpeople_search_contacts() -> None:
    svc = MagicMock()
    svc.people().searchContacts().execute.return_value = {
        "results": [{"person": {"names": [{"displayName": "山田"}]}}]
    }
    client = GPeopleClient(service=svc)
    contacts = client.search_contacts("山田", "t")
    assert len(contacts) == 1
    assert contacts[0].display_name == "山田"


# ── per-user fail-closed ─────────────────────────────────────────────────────


def test_from_user_token_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECT_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CONNECT_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    tok = OAuthToken(refresh_token="rt")
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        GCalendarClient.from_user_token(tok)
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        GPeopleClient.from_user_token(tok)
