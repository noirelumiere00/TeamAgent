"""Google Slides API（v1）readonly アダプタ（Workspace 統合・W2）。

`presentations.get` でプレゼンを取得し、全スライドの plain text を抽出する。per-user OAuth
（`from_user_token`）対応＝本人のスライドにしか触れない（G1）。書込スコープは持たない（G4）。

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
class SlidesContent:
    """プレゼン本文（全スライドの plain text 抽出済み）。"""

    presentation_id: str
    title: str
    text: str
    slide_count: int


def extract_slides_text(slides: list[dict[str, Any]]) -> str:
    """Slides API の slides[] から全テキストを抽出（スライド境界に改行）。

    slide.pageElements[].shape.text.textElements[].textRun.content を連結。
    """
    parts: list[str] = []
    for slide in slides or []:
        for pe in slide.get("pageElements", []) or []:
            shape = pe.get("shape")
            if not shape:
                continue
            text_obj = shape.get("text")
            if not text_obj:
                continue
            for te in text_obj.get("textElements", []) or []:
                text_run = te.get("textRun")
                if text_run and text_run.get("content"):
                    parts.append(str(text_run["content"]))
        parts.append("\n")  # スライド境界
    return "".join(parts).strip()


class GSlidesClient:
    """Google Slides API v1 の薄ラッパー（readonly）。"""

    SCOPES_READONLY: tuple[str, ...] = ("https://www.googleapis.com/auth/presentations.readonly",)

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
    def from_env(cls) -> GSlidesClient:
        """env の共有資格情報から構築（単一トークン・移行用ブリッジ）。"""
        if not (
            os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "gslides_credentials_missing",
                hint="GOOGLE_CLIENT_ID + refresh token、または GOOGLE_APPLICATION_CREDENTIALS",
            )
        return cls(credentials=None)

    @classmethod
    def from_user_token(cls, token: OAuthToken) -> GSlidesClient:
        """per-user: 本人の refresh token から構築（本人のスライドのみ参照可）。"""
        from teamagent.adapters.google_auth import build_user_credentials

        return cls(credentials=build_user_credentials(token), scopes=cls.SCOPES_READONLY)

    def get_presentation_text(self, presentation_id: str, request_id: str) -> SlidesContent:
        """presentations.get で全スライド本文を取得し plain text を返す。"""
        service = self._ensure_service()
        start = time.perf_counter()
        resp = service.presentations().get(presentationId=presentation_id).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)

        slides = resp.get("slides", []) or []
        title = str(resp.get("title", ""))
        text = extract_slides_text(slides)
        logger.info(
            "gslides_get_presentation",
            request_id=request_id,
            presentation_id=presentation_id,
            slide_count=len(slides),
            text_len=len(text),
            latency_ms=latency_ms,
        )
        return SlidesContent(
            presentation_id=presentation_id, title=title, text=text, slide_count=len(slides)
        )

    def _ensure_service(self) -> Any:
        from googleapiclient.discovery import build

        if self._service is None:
            if self._credentials is None:
                self._credentials = self._build_credentials()
            self._service = build(
                "slides", "v1", credentials=self._credentials, cache_discovery=False
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
