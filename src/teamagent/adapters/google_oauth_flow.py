"""per-user OAuth 同意フロー（`/teamagent connect` の中核・W2）。

各メンバーが自分の Google を認可するための「本人専用の同意URL生成」と「authorization
code → refresh token 交換」を提供する。**CSRF 対策の署名付き state（HMAC）**で、callback で
「誰の認可か」を改竄なく検証する（エージェント協議の落とし穴・必須要件）。

- state 署名/検証は stdlib のみ（テスト可・課金0）。
- `google-auth-oauthlib` の Flow は遅延 import（実認可は W1 の OAuth クライアント完了後）。
設計: docs/poc/workspace_integration_design.md §4。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

from teamagent.adapters.oauth_token_store import OAuthToken

# Workspace 連携スコープ（W1 同意画面・OAuth 同意画面 User Type=Internal＝審査不要）。
# Gmail のみ **modify**（読み＋下書き作成＋ラベル）。送信/削除は GmailClient の adapter-layer
# denylist で物理封鎖し、create_draft（下書き専用）だけ通す＝「AIは要約・提案・下書きまで、
# 送信は人間」をコードで強制。他6サービスは readonly。
# ⚠️ 既に readonly で connect 済みの人は、下書き作成(gmail.modify)を使うには再 connect が必要。
WORKSPACE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
)
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _state_secret() -> bytes:
    secret = os.environ.get("OAUTH_STATE_SECRET")
    if not secret:
        raise ValueError("OAUTH_STATE_SECRET が未設定です（CSRF state 署名に必要）")
    return secret.encode("utf-8")


def make_state(user_email: str, *, secret: bytes | None = None) -> str:
    """user_email を HMAC 署名して state にする（callback で本人性検証・CSRF/改竄対策）。"""
    sec = secret or _state_secret()
    email = user_email.strip().lower()
    sig = hmac.new(sec, email.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{email}.{sig}".encode()).decode("ascii")


def verify_state(state: str, *, secret: bytes | None = None) -> str | None:
    """state を検証し、正しければ user_email を返す。改竄/CSRF/壊れた値なら None。"""
    sec = secret or _state_secret()
    try:
        raw = base64.urlsafe_b64decode(state.encode("ascii")).decode("utf-8")
        email, sig = raw.rsplit(".", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    expect = hmac.new(sec, email.encode("utf-8"), hashlib.sha256).hexdigest()
    return email if hmac.compare_digest(sig, expect) else None


class OAuthConsentFlow:
    """`google-auth-oauthlib` の Flow を薄くラップ（同意URL生成 + code交換）。"""

    def __init__(self, redirect_uri: str, scopes: tuple[str, ...] = WORKSPACE_SCOPES) -> None:
        self._redirect_uri = redirect_uri
        self._scopes = scopes

    def _flow(self) -> Any:
        from google_auth_oauthlib.flow import Flow

        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if not (client_id and client_secret):
            raise ValueError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET が未設定です（W1）")
        config = {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": _AUTH_URI,
                "token_uri": _TOKEN_URI,
                "redirect_uris": [self._redirect_uri],
            }
        }
        # PKCE は無効化する。本フローは「URL生成」と「code交換」が別プロセス(別 Flow 実体)に
        # 分かれるため、PKCE を使うと code_verifier が一致せず交換に失敗する。Web型(機密)
        # クライアントは client_secret で保護されるため PKCE は不要。
        return Flow.from_client_config(
            config,
            scopes=list(self._scopes),
            redirect_uri=self._redirect_uri,
            autogenerate_code_verifier=False,
        )

    def authorization_url(self, user_email: str) -> tuple[str, str]:
        """本人専用の同意URLと state を返す。

        access_type=offline + prompt=consent で refresh token を確実に取得する。
        """
        state = make_state(user_email)
        # include_granted_scopes は使わない（他アプリで許可済みの scope=gmail.modify 等まで
        # 合算され、要求と返却が食い違う＋readonly の約束に反する write scope が混ざるため）。
        url, _ = self._flow().authorization_url(
            access_type="offline",
            prompt="consent",
            state=state,
        )
        return str(url), state

    def exchange(self, code: str) -> OAuthToken:
        """authorization code を refresh token に交換して OAuthToken を返す。"""
        # Google はアカウントが既に許可済みの scope を追加で返すことがある。oauthlib は既定で
        # 「要求と返却の scope 不一致」を例外にするため、緩和しておく（交換自体は成功させる）。
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        flow = self._flow()
        flow.fetch_token(code=code)
        creds = flow.credentials
        if not creds.refresh_token:
            raise ValueError(
                "refresh_token を取得できません（access_type=offline / prompt=consent を確認）"
            )
        return OAuthToken(
            refresh_token=str(creds.refresh_token),
            scopes=tuple(creds.scopes or self._scopes),
        )


__all__ = [
    "WORKSPACE_SCOPES",
    "OAuthConsentFlow",
    "make_state",
    "verify_state",
]
