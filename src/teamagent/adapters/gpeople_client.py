"""Google People API（v1）readonly アダプタ（Workspace 統合・W2）。

`people.searchContacts` で本人の連絡先を検索する（先方担当者・役職＝営業文脈）。per-user OAuth
（`from_user_token`）対応＝本人の連絡先にしか触れない（G1）。書込スコープは持たない（G4）。

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

_READ_MASK = "names,emailAddresses,organizations"


@dataclass(frozen=True)
class Contact:
    """連絡先1件（営業文脈に必要な最小フィールド）。"""

    display_name: str
    emails: tuple[str, ...]
    organization: str


def extract_contacts(results: list[dict[str, Any]]) -> list[Contact]:
    """searchContacts の results[] を Contact 群へ変換する。"""
    out: list[Contact] = []
    for r in results or []:
        person = r.get("person") or {}
        names = person.get("names") or []
        display = str(names[0].get("displayName", "")) if names else ""
        emails = tuple(
            str(e.get("value")) for e in (person.get("emailAddresses") or []) if e.get("value")
        )
        orgs = person.get("organizations") or []
        org = str(orgs[0].get("name", "")) if orgs else ""
        out.append(Contact(display_name=display, emails=emails, organization=org))
    return out


class GPeopleClient:
    """Google People API v1 の薄ラッパー（readonly）。"""

    SCOPES_READONLY: tuple[str, ...] = ("https://www.googleapis.com/auth/contacts.readonly",)

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
    def from_env(cls) -> GPeopleClient:
        if not (
            os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "gpeople_credentials_missing",
                hint="GOOGLE_CLIENT_ID + refresh token、または GOOGLE_APPLICATION_CREDENTIALS",
            )
        return cls(credentials=None)

    @classmethod
    def from_user_token(cls, token: OAuthToken) -> GPeopleClient:
        """per-user: 本人の refresh token から構築（本人の連絡先のみ参照可）。"""
        from teamagent.adapters.google_auth import build_user_credentials

        return cls(credentials=build_user_credentials(token), scopes=cls.SCOPES_READONLY)

    def search_contacts(self, query: str, request_id: str, *, page_size: int = 10) -> list[Contact]:
        """people.searchContacts で連絡先を検索する。"""
        service = self._ensure_service()
        start = time.perf_counter()
        resp = (
            service.people()
            .searchContacts(query=query, pageSize=page_size, readMask=_READ_MASK)
            .execute()
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        contacts = extract_contacts(resp.get("results", []) or [])
        logger.info(
            "gpeople_search_contacts",
            request_id=request_id,
            query_len=len(query),
            returned=len(contacts),
            latency_ms=latency_ms,
        )
        return contacts

    def _ensure_service(self) -> Any:
        from googleapiclient.discovery import build

        if self._service is None:
            if self._credentials is None:
                self._credentials = self._build_credentials()
            self._service = build(
                "people", "v1", credentials=self._credentials, cache_discovery=False
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
