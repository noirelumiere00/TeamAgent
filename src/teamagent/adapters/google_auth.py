"""Google 認証ヘルパ（共有 OAuth と per-user OAuth の両対応）。

■ 共有 OAuth（video_approval 等の案件シート読取・現行）:
  優先順位 1. GOOGLE_FORCE_OAUTH=1 → SA を無視して個人 OAuth 強制
           2. GOOGLE_APPLICATION_CREDENTIALS (SA 鍵)
           3. OAuth リフレッシュトークン (GOOGLE_OAUTH_REFRESH_TOKEN + CLIENT_ID/SECRET)
  本番 SA は組織ポリシーで外部シート/Drive を共有できないため案件シートは個人 OAuth で読む。
  共有トークンは固定スコープ集合で発行されるため drive.readonly 等の集合を要求する。
  → force_oauth_enabled() / resolve_oauth_scopes() / build_oauth_credentials()

■ per-user OAuth（connect 機能・各営業が自分の Google を自己認可・G1「本人のデータのみ」）:
  共有 OAuth クライアント(env)＋各人の refresh token(TokenStore 由来 OAuthToken)から
  Credentials を作る。各アダプタの from_user_token がこれを使う。
  → build_user_credentials(token)
  設計: docs/poc/workspace_integration_design.md §5。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

import structlog

from teamagent.adapters.oauth_token_store import OAuthToken

logger = structlog.get_logger(__name__)

GOOGLE_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"

# 個人 OAuth リフレッシュトークンが実際に許可されているスコープ (2026-06-01 時点)。
# drive.readonly は Sheets API v4 の読取も認可する (シートは Drive 配下のファイル扱い)。
SHARED_OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
)


def _env_truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def force_oauth_enabled() -> bool:
    """GOOGLE_FORCE_OAUTH が真なら SA を飛ばして OAuth を強制する。"""
    return _env_truthy(os.environ.get("GOOGLE_FORCE_OAUTH"))


def _split_scopes(raw: str) -> list[str]:
    return [s for s in re.split(r"[,\s]+", raw.strip()) if s]


def resolve_oauth_scopes(preferred: Sequence[str]) -> list[str]:
    """OAuth で要求するスコープを決める。

    優先順位: 1. GOOGLE_OAUTH_SCOPES(env override) 2. 呼び出し側の preferred。
    preferred は「共有トークンが実際に持つスコープの部分集合」を渡すこと。
    """
    override = os.environ.get("GOOGLE_OAUTH_SCOPES")
    if override:
        scopes = _split_scopes(override)
        if scopes:
            return scopes
    return list(preferred)


def build_oauth_credentials(preferred_scopes: Sequence[str]) -> Any | None:
    """環境変数から共有個人 OAuth の Credentials を構築（必要 env が無ければ None）。"""
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not (refresh_token and client_id and client_secret):
        return None

    from google.oauth2.credentials import Credentials

    scopes = resolve_oauth_scopes(preferred_scopes)
    logger.debug("google_oauth_credentials_built", scopes=scopes)
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_OAUTH_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )


def connect_client_id_secret() -> tuple[str | None, str | None]:
    """per-user 連携(connect)用の OAuth クライアント(client_id, client_secret)を返す。

    連携(ブラウザ→API Gateway リダイレクト)は **ウェブ型**クライアントが必須、一方で共有OAuth
    (動画シート/Drive・loopback・書込スコープ)は **デスクトップ型**が自然＝両者は別クライアント。
    そこで連携用は `CONNECT_GOOGLE_CLIENT_ID/SECRET` を優先し、未設定なら従来の共有
    `GOOGLE_CLIENT_ID/SECRET` にフォールバックする（後方互換）。これで「連携=web / 共有=desktop」を
    1つの secret を奪い合わずに両立できる。
    """
    # ペアで揃える: 連携ID(CONNECT_GOOGLE_CLIENT_ID)があるのに secret だけ欠ける時、共有
    # GOOGLE_CLIENT_SECRET へ部分フォールバックすると id(pgd1)/secret(r194) 食い違いで
    # invalid_client になる。連携IDがあれば連携secret(欠けても None)を返し呼び出し側で明示エラーに。
    # 連携ID未設定のときだけ共有 GOOGLE_* ペアにフォールバック。
    connect_id = os.environ.get("CONNECT_GOOGLE_CLIENT_ID")
    if connect_id:
        return connect_id, os.environ.get("CONNECT_GOOGLE_CLIENT_SECRET")
    return os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")


def build_user_credentials(token: OAuthToken) -> Any:
    """本人の refresh token から OAuth Credentials を組み立てる（per-user・connect 用）。

    連携用 OAuth クライアント（CONNECT_GOOGLE_CLIENT_ID/SECRET 優先・無ければ GOOGLE_*）を使う。
    未設定/未認可は ValueError（fail-closed＝未認可は弾く）。
    """
    client_id, client_secret = connect_client_id_secret()
    if not (client_id and client_secret):
        raise ValueError(
            "連携用 OAuth クライアントが未設定です"
            "（CONNECT_GOOGLE_CLIENT_ID/SECRET または GOOGLE_CLIENT_ID/SECRET を設定）"
        )
    if not token.refresh_token:
        raise ValueError("OAuthToken.refresh_token が空です（本人が未認可）")

    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=token.refresh_token,
        token_uri=GOOGLE_OAUTH_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(token.scopes) or None,
    )
