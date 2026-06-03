"""per-user OAuth 認証情報ビルダー（Workspace 5サービス共通）。

共有 OAuth クライアント（`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`・env）＋ 各人の
refresh token（TokenStore 由来の `OAuthToken`）から googleapiclient 用 Credentials を作る。
各アダプタの `from_user_token` がこれを使い「本人のデータにしか触れない」（G1）を実現する。

google ライブラリは遅延 import（本モジュールの import を軽量に保つ）。
設計: docs/poc/workspace_integration_design.md §5。
"""

from __future__ import annotations

import os
from typing import Any

from teamagent.adapters.oauth_token_store import OAuthToken

_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def build_user_credentials(token: OAuthToken) -> Any:
    """本人の refresh token から OAuth Credentials を組み立てる（per-user）。

    共有 OAuth クライアント（W1 で作成）の `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`（env）
    を使う。クライアント未設定 or token 空なら ValueError（fail-closed＝未認可は弾く）。
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise ValueError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET が未設定です"
            "（W1: Google Cloud で OAuth クライアントを作成してください）"
        )
    if not token.refresh_token:
        raise ValueError("OAuthToken.refresh_token が空です（本人が未認可）")

    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=token.refresh_token,
        token_uri=_GOOGLE_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(token.scopes) or None,
    )


__all__ = ["build_user_credentials"]
