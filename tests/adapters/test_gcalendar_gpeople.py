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
    # 終日判定は **date key の有無**（値の文字列形ではない）。
    # 朝ダイジェストの日付ずれ根治で下流が all_day に依存するため回帰固定する。
    assert events[0].all_day is False
    assert events[1].all_day is True
    assert events[1].end == "2026-05-03"  # end.date は排他的（5/2 のみの終日）


def test_extract_events_all_day_flag_is_key_based_not_string_based() -> None:
    """dateTime が "T00:00:00" でも終日にしない／date が Z 付き整形でも終日にする。"""
    items: list[dict[str, Any]] = [
        {
            "id": "midnight",
            "summary": "0時開始の会議",
            "start": {"dateTime": "2026-05-02T00:00:00+09:00"},
            "end": {"dateTime": "2026-05-02T01:00:00+09:00"},
        },
        {
            "id": "allday-z",
            "summary": "整形された終日",
            "start": {"date": "2026-05-02T00:00:00Z"},
            "end": {"date": "2026-05-03T00:00:00Z"},
        },
    ]
    events = extract_events(items)
    assert events[0].all_day is False
    assert events[1].all_day is True


def test_extract_events_location_and_meeting_url() -> None:
    """会議室(location)と会議リンク(hangoutLink / conferenceData)を取り出す。"""
    items: list[dict[str, Any]] = [
        {
            "id": "m1",
            "summary": "定例",
            "start": {"dateTime": "2026-05-01T10:00:00+09:00"},
            "end": {"dateTime": "2026-05-01T11:00:00+09:00"},
            "location": "本社 3F 会議室A",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        },
        {
            "id": "m2",
            "summary": "Zoom MTG",
            "start": {"dateTime": "2026-05-01T12:00:00+09:00"},
            "end": {"dateTime": "2026-05-01T12:30:00+09:00"},
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "phone", "uri": "tel:+81-3-0000"},
                    {"entryPointType": "video", "uri": "https://zoom.us/j/123"},
                ]
            },
        },
        {
            "id": "m3",
            "summary": "リンク無し",
            "start": {"date": "2026-05-02"},
            "end": {"date": "2026-05-03"},
        },
    ]
    events = extract_events(items)
    assert events[0].location == "本社 3F 会議室A"
    assert events[0].meeting_url == "https://meet.google.com/abc-defg-hij"  # Meet
    assert events[1].meeting_url == "https://zoom.us/j/123"  # conferenceData video
    assert events[2].location == "" and events[2].meeting_url == ""  # 無しは空


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
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    tok = OAuthToken(refresh_token="rt")
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        GCalendarClient.from_user_token(tok)
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        GPeopleClient.from_user_token(tok)
