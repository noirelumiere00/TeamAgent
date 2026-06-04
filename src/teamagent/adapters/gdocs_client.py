"""Google Docs API（v1）readonly アダプタ（Workspace 統合・W2）。

`documents.get` でドキュメントを取得し plain text を抽出する。per-user OAuth
（`from_user_token`）対応＝本人のドキュメントにしか触れない（G1）。書込スコープは持たない（G4）。

認証パターンは gsheets_client / gdrive_client / gmail_client と統一（OAuth 優先・SA も可）。
googleapiclient / google ライブラリは遅延 import。
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
class DocContent:
    """ドキュメント本文（plain text 抽出済み）。"""

    document_id: str
    title: str
    text: str


def extract_doc_text(body: dict[str, Any]) -> str:
    """Docs API の body（structuralElements）から plain text を連結抽出する。

    body.content[].paragraph.elements[].textRun.content を順に連結。表や見出しも
    paragraph として含まれる（テキストのみ・書式は捨てる）。
    """
    out: list[str] = []
    for el in body.get("content", []) or []:
        para = el.get("paragraph")
        if not para:
            continue
        for pe in para.get("elements", []) or []:
            text_run = pe.get("textRun")
            if text_run and text_run.get("content"):
                out.append(str(text_run["content"]))
    return "".join(out)


class GDocsClient:
    """Google Docs API v1 の薄ラッパー（readonly）。"""

    SCOPES_READONLY: tuple[str, ...] = ("https://www.googleapis.com/auth/documents.readonly",)

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
    def from_env(cls) -> GDocsClient:
        """env の共有資格情報から構築（単一トークン・移行用ブリッジ）。"""
        if not (
            os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "gdocs_credentials_missing",
                hint="GOOGLE_CLIENT_ID + refresh token、または GOOGLE_APPLICATION_CREDENTIALS",
            )
        return cls(credentials=None)

    @classmethod
    def from_user_token(cls, token: OAuthToken) -> GDocsClient:
        """per-user: 本人の refresh token から構築（本人のドキュメントのみ参照可）。"""
        from teamagent.adapters.google_auth import build_user_credentials

        return cls(credentials=build_user_credentials(token), scopes=cls.SCOPES_READONLY)

    def get_document_text(self, document_id: str, request_id: str) -> DocContent:
        """documents.get で本文を取得し plain text を返す。"""
        service = self._ensure_service()
        start = time.perf_counter()
        resp = service.documents().get(documentId=document_id).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)

        title = str(resp.get("title", ""))
        text = extract_doc_text(resp.get("body", {}) or {})
        logger.info(
            "gdocs_get_document",
            request_id=request_id,
            document_id=document_id,
            text_len=len(text),  # 本文そのものは出さない（PII/長さのみ）
            latency_ms=latency_ms,
        )
        return DocContent(document_id=document_id, title=title, text=text)

    def _ensure_service(self) -> Any:
        from googleapiclient.discovery import build

        if self._service is None:
            if self._credentials is None:
                self._credentials = self._build_credentials()
            self._service = build(
                "docs", "v1", credentials=self._credentials, cache_discovery=False
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
