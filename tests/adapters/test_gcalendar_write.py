"""GCalendarClient 書込基盤（v0.3 Task2）のテスト（課金0・fake service）。

検証主眼:
1. _GCalSafePolicy が delete/update/patch/move/clear/quickAdd/acl 等を物理封鎖（RuntimeError）
2. insert_event は sendUpdates="none" を強制し attendees を body に含めない（API 面にも無い）
3. tentative / event_id（冪等キー）/ 終日 date の body 組み立て
4. freebusy が busy 区間を FreeBusyBlock へ変換
5. 既存 read（events.list）が policy ラップ後も動く（回帰）
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import (
    _GCAL_DESTRUCTIVE_METHODS,
    FreeBusyBlock,
    GCalendarClient,
    _GCalSafePolicy,
)


class _FakeRequest:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class _FakeEvents:
    """googleapiclient の events() リソースを模す（insert/list/delete チェーン）。"""

    def __init__(self, owner: _FakeService) -> None:
        self._owner = owner

    def insert(self, **kwargs: Any) -> _FakeRequest:
        self._owner.insert_calls.append(kwargs)
        body = kwargs.get("body") or {}
        return _FakeRequest(
            {
                "id": body.get("id") or "ev_generated",
                "htmlLink": "https://calendar.google.com/event?eid=x",
                "summary": body.get("summary", ""),
                "start": body.get("start", {}),
                "end": body.get("end", {}),
                "status": body.get("status", "confirmed"),
            }
        )

    def list(self, **kwargs: Any) -> _FakeRequest:
        return _FakeRequest({"items": []})

    def delete(self, **kwargs: Any) -> _FakeRequest:
        return _FakeRequest({})  # policy が execute 前に止めるので届かないはず

    def update(self, **kwargs: Any) -> _FakeRequest:
        return _FakeRequest({})


class _FakeFreebusy:
    def __init__(self, busy: list[dict[str, str]]) -> None:
        self._busy = busy
        self.bodies: list[dict[str, Any]] = []

    def query(self, body: dict[str, Any]) -> _FakeRequest:
        self.bodies.append(body)
        cal_id = body["items"][0]["id"]
        return _FakeRequest({"calendars": {cal_id: {"busy": self._busy}}})


class _FakeService:
    def __init__(self, busy: list[dict[str, str]] | None = None) -> None:
        self.insert_calls: list[dict[str, Any]] = []
        self._freebusy = _FakeFreebusy(busy or [])

    def events(self) -> _FakeEvents:
        return _FakeEvents(self)

    def freebusy(self) -> _FakeFreebusy:
        return self._freebusy


def _client(svc: _FakeService | None = None) -> tuple[GCalendarClient, _FakeService]:
    svc = svc or _FakeService()
    return GCalendarClient(service=svc), svc


# ── 1. 物理封鎖 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method_path",
    [
        "events.delete",
        "events.update",
        "events.patch",
        "events.move",
        "events.quickAdd",
        "events.import_",
        "calendars.delete",
        "calendars.clear",
        "acl.insert",
        "acl.delete",
        "calendarList.delete",
    ],
)
def test_policy_blocks_destructive(method_path: str) -> None:
    with pytest.raises(RuntimeError, match="blocked by adapter-layer denylist"):
        _GCalSafePolicy().assert_safe(method_path)


def test_policy_allows_read_and_insert() -> None:
    p = _GCalSafePolicy()
    for ok in ["events.list", "events.get", "events.insert", "freebusy.query", "colors.get"]:
        p.assert_safe(ok)  # raise しない


def test_delete_via_service_chain_is_unreachable() -> None:
    """service 経由の属性チェーンでも execute 直前で封鎖される（wrapper 経路の実証）。"""
    client, _svc = _client()
    service = client._ensure_service()
    with pytest.raises(RuntimeError, match=r"events\.delete"):
        service.events().delete(calendarId="primary", eventId="e1").execute()


def test_denylist_covers_all_mutating_families() -> None:
    """封鎖対象ファミリの取りこぼし検知（acl/calendars/calendarList の変更系が全て居るか）。"""
    for fam, ops in {
        "acl": ["insert", "update", "patch", "delete"],
        "calendars": ["insert", "update", "patch", "delete", "clear"],
        "calendarList": ["insert", "update", "patch", "delete"],
    }.items():
        for op in ops:
            assert f"{fam}.{op}" in _GCAL_DESTRUCTIVE_METHODS


# ── 2-3. insert_event ───────────────────────────────────────────────────────


def test_insert_event_forces_send_updates_none_and_no_attendees() -> None:
    client, svc = _client()
    ev = client.insert_event(
        "r1",
        summary="打合せ",
        start_iso="2026-07-15T10:00:00+09:00",
        end_iso="2026-07-15T11:00:00+09:00",
        description="アジェンダ",
        location="会議室A",
    )
    call = svc.insert_calls[0]
    assert call["sendUpdates"] == "none"  # 強制（呼び出し側から変更不能）
    assert "attendees" not in call["body"]  # API 面に attendees が存在しない
    assert call["body"]["status"] == "confirmed"
    assert call["body"]["start"] == {"dateTime": "2026-07-15T10:00:00+09:00"}
    assert ev.event_id and ev.html_link.startswith("https://")
    assert ev.status == "confirmed"


def test_insert_event_tentative_and_idempotency_key() -> None:
    client, svc = _client()
    ev = client.insert_event(
        "r1",
        summary="仮: 日程候補",
        start_iso="2026-07-16T14:00:00+09:00",
        end_iso="2026-07-16T15:00:00+09:00",
        tentative=True,
        event_id="abc123def456",  # Task3/4 のボタン連打対策（呼び出し側が導出）
    )
    body = svc.insert_calls[0]["body"]
    assert body["status"] == "tentative"
    assert body["id"] == "abc123def456"
    assert ev.status == "tentative" and ev.event_id == "abc123def456"


def test_insert_event_all_day_uses_date() -> None:
    client, svc = _client()
    client.insert_event("r1", summary="終日", start_iso="2026-07-20", end_iso="2026-07-21")
    body = svc.insert_calls[0]["body"]
    assert body["start"] == {"date": "2026-07-20"} and body["end"] == {"date": "2026-07-21"}


# ── 4. freebusy ─────────────────────────────────────────────────────────────


def test_freebusy_parses_busy_blocks() -> None:
    svc = _FakeService(
        busy=[
            {"start": "2026-07-15T09:00:00+09:00", "end": "2026-07-15T10:00:00+09:00"},
            {"start": "2026-07-15T13:00:00+09:00", "end": "2026-07-15T14:30:00+09:00"},
            {"start": "", "end": "2026-07-15T15:00:00+09:00"},  # 欠損はスキップ
        ]
    )
    client, _ = _client(svc)
    blocks = client.freebusy(
        "r1", time_min="2026-07-15T00:00:00+09:00", time_max="2026-07-16T00:00:00+09:00"
    )
    assert blocks == [
        FreeBusyBlock(start="2026-07-15T09:00:00+09:00", end="2026-07-15T10:00:00+09:00"),
        FreeBusyBlock(start="2026-07-15T13:00:00+09:00", end="2026-07-15T14:30:00+09:00"),
    ]
    assert svc._freebusy.bodies[0]["items"] == [{"id": "primary"}]


# ── 5. 回帰: read 系が policy ラップ後も動く ────────────────────────────────


def test_list_events_still_works_through_policy_wrapper() -> None:
    client, _ = _client()
    assert client.list_events("r1") == []


def test_scopes_write_constant() -> None:
    assert "https://www.googleapis.com/auth/calendar.events" in GCalendarClient.SCOPES_WRITE
    assert "https://www.googleapis.com/auth/calendar.readonly" in GCalendarClient.SCOPES_WRITE


# ── 6. denylist の実在照合（表記ズレ＝封鎖無効を構造的に検知） ──────────────


def _resolve_method_path(service: Any, path: str) -> bool:
    """'events.import_' のような path が実 discovery 上の属性チェーンとして実在するか。"""
    if path == "new_batch_http_request":
        return hasattr(service, "new_batch_http_request")
    node = service
    parts = path.split(".")
    for part in parts[:-1]:
        node = getattr(node, part)()  # 中間リソースは呼び出しで進む
    return hasattr(node, parts[-1])


def test_denylist_entries_exist_in_real_discovery() -> None:
    """全 denylist entry が googleapiclient の実属性名と一致する（import→import_ 型の
    表記ズレがあると封鎖が永久に効かないため、静的 discovery で照合する）。"""
    from googleapiclient.discovery import build

    service = build("calendar", "v3", developerKey="x", static_discovery=True)
    missing = [m for m in _GCAL_DESTRUCTIVE_METHODS if not _resolve_method_path(service, m)]
    assert missing == [], f"denylist に実在しない属性名: {missing}（封鎖が効いていない）"


def test_gmail_denylist_entries_exist_in_real_discovery() -> None:
    """gmail 側 denylist も同照合（users.messages.import_ / batch 封鎖の回帰防止）。"""
    from googleapiclient.discovery import build

    from teamagent.adapters.gmail_client import _GMAIL_DESTRUCTIVE_METHODS

    service = build("gmail", "v1", developerKey="x", static_discovery=True)
    missing = [m for m in _GMAIL_DESTRUCTIVE_METHODS if not _resolve_method_path(service, m)]
    assert missing == [], f"denylist に実在しない属性名: {missing}（封鎖が効いていない）"


def test_events_import_underscore_blocked_via_real_chain() -> None:
    """F1 回帰: 実 discovery の events().import_() が execute 前に封鎖される。"""
    from googleapiclient.discovery import build

    real = build("calendar", "v3", developerKey="x", static_discovery=True)
    client = GCalendarClient(service=real)
    service = client._ensure_service()
    with pytest.raises(RuntimeError, match=r"events\.import_"):
        service.events().import_(calendarId="primary", body={}).execute()


def test_batch_bypass_blocked() -> None:
    """F2 回帰: new_batch_http_request 経由の一括実行も封鎖される。"""
    with pytest.raises(RuntimeError, match="new_batch_http_request"):
        _GCalSafePolicy().assert_safe("new_batch_http_request")


# ── 7. 入力検証（naive dateTime / event_id 形式） ───────────────────────────


def test_insert_event_rejects_naive_datetime() -> None:
    client, svc = _client()
    with pytest.raises(ValueError, match="UTC offset"):
        client.insert_event(
            "r1", summary="x", start_iso="2026-07-15T10:00:00", end_iso="2026-07-15T11:00:00+09:00"
        )
    assert svc.insert_calls == []  # API に到達しない


def test_freebusy_rejects_naive_datetime() -> None:
    client, _ = _client()
    with pytest.raises(ValueError, match="UTC offset"):
        client.freebusy("r1", time_min="2026-07-15T00:00:00", time_max="2026-07-16T00:00:00+09:00")


def test_insert_event_rejects_invalid_event_id() -> None:
    client, svc = _client()
    for bad in ["ABC123", "with-dash", "xyz", "a" * 4]:  # 大文字 / 記号 / w-z / 短すぎ
        with pytest.raises(ValueError, match="base32hex"):
            client.insert_event(
                "r1",
                summary="x",
                start_iso="2026-07-15T10:00:00+09:00",
                end_iso="2026-07-15T11:00:00+09:00",
                event_id=bad,
            )
    assert svc.insert_calls == []


def test_insert_event_transparent_for_tentative_hold() -> None:
    """Task4 の候補ホールド用: transparent=True で freebusy に乗らない予定にできる。"""
    client, svc = _client()
    client.insert_event(
        "r1",
        summary="仮ホールド",
        start_iso="2026-07-16T14:00:00+09:00",
        end_iso="2026-07-16T15:00:00+09:00",
        tentative=True,
        transparent=True,
    )
    body = svc.insert_calls[0]["body"]
    assert body["transparency"] == "transparent" and body["status"] == "tentative"
