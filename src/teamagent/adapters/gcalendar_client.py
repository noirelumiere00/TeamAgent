"""Google Calendar API（v3）readonly アダプタ（Workspace 統合・W2）。

`events.list` で予定を取得する（商談タイミング・面談履歴・接触頻度＝営業文脈）。per-user OAuth
（`from_user_token`）対応＝本人のカレンダーにしか触れない（G1）。書込スコープは持たない（G4）。

認証パターンは他の Google アダプタと統一。googleapiclient / google ライブラリは遅延 import。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.adapters.oauth_token_store import OAuthToken

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CalendarEvent:
    """予定1件（営業文脈に必要な最小フィールド）。"""

    event_id: str
    summary: str
    start: str  # ISO 文字列（dateTime か date）
    end: str
    attendees: tuple[str, ...]  # 参加者メール（マスクは上位層の責務）


def extract_events(items: list[dict[str, Any]]) -> list[CalendarEvent]:
    """events.list の items[] を CalendarEvent 群へ変換する。"""
    out: list[CalendarEvent] = []
    for it in items or []:
        start = (it.get("start") or {}).get("dateTime") or (it.get("start") or {}).get("date") or ""
        end = (it.get("end") or {}).get("dateTime") or (it.get("end") or {}).get("date") or ""
        attendees = tuple(
            str(a.get("email")) for a in (it.get("attendees") or []) if a.get("email")
        )
        out.append(
            CalendarEvent(
                event_id=str(it.get("id", "")),
                summary=str(it.get("summary", "")),
                start=str(start),
                end=str(end),
                attendees=attendees,
            )
        )
    return out


class GCalendarClient:
    """Google Calendar API v3 の薄ラッパー（readonly）。"""

    SCOPES_READONLY: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar.readonly",)

    def __init__(
        self,
        credentials: Any | None = None,
        *,
        service: Any | None = None,
        scopes: tuple[str, ...] | None = None,
    ) -> None:
        self._credentials = credentials
        self._service = service
        self._scopes = scopes or self.SCOPES_READONLY

    @classmethod
    def from_env(cls) -> GCalendarClient:
        if not (
            os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "gcalendar_credentials_missing",
                hint="GOOGLE_CLIENT_ID + refresh token、または GOOGLE_APPLICATION_CREDENTIALS",
            )
        return cls(credentials=None)

    @classmethod
    def from_user_token(cls, token: OAuthToken) -> GCalendarClient:
        """per-user: 本人の refresh token から構築（本人のカレンダーのみ参照可）。"""
        from teamagent.adapters.google_auth import build_user_credentials

        return cls(credentials=build_user_credentials(token), scopes=cls.SCOPES_READONLY)

    def list_events(
        self,
        request_id: str,
        *,
        query: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 20,
        calendar_id: str = "primary",
    ) -> list[CalendarEvent]:
        """events.list で予定を取得（q でクライアント名等に絞れる）。"""
        service = self._ensure_service()
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if query:
            params["q"] = query
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max

        start = time.perf_counter()
        resp = service.events().list(**params).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)

        events = extract_events(resp.get("items", []) or [])
        logger.info(
            "gcalendar_list_events",
            request_id=request_id,
            query_len=len(query) if query else 0,
            returned=len(events),
            latency_ms=latency_ms,
        )
        return events

    def _ensure_service(self) -> Any:
        from googleapiclient.discovery import build

        if self._service is None:
            if self._credentials is None:
                self._credentials = self._build_credentials()
            self._service = build(
                "calendar", "v3", credentials=self._credentials, cache_discovery=False
            )
        return self._service

    def _build_credentials(self) -> Any:
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if sa_path:
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                sa_path, scopes=self._scopes
            )
        refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if refresh_token and client_id and client_secret:
            from google.oauth2.credentials import Credentials

            return Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=list(self._scopes),
            )
        raise ValueError("Google 資格情報が未設定です（from_user_token か env を設定してください）")
