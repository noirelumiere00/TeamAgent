"""Google 認証の共通ヘルパ (gsheets / gdrive / gmail 共有)。

認証の優先順位:
  1. GOOGLE_FORCE_OAUTH=1 → Service Account を無視して必ず個人 OAuth を使う
  2. GOOGLE_APPLICATION_CREDENTIALS (SA 鍵) があれば Service Account
  3. OAuth リフレッシュトークン
     (GOOGLE_OAUTH_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET)

なぜ「OAuth 強制」が要るか (2026-06-01 実機で確定):
  本番 SA `teamagent-vertex@ntv-ai.iam.gserviceaccount.com` は組織ポリシーで
  外部スプレッドシート/ドライブへ共有追加できない (共有先に SA を足すのが拒否)。
  そのため案件シートは「各担当者の個人 Google アカウントの OAuth」で読む。
  ところが共有リフレッシュトークンは固定スコープ集合で発行されているため、
  API ごとの狭いスコープ (例: spreadsheets.readonly) を要求すると
  ``invalid_scope`` で弾かれる。共有トークンが実際に持つスコープ
  (drive.readonly 等。Sheets API も drive.readonly で読める) を要求する。

将来 (#44 方式B: 各人別 OAuth) では、env 依存をこの 1 ファイルに閉じておくことで
per-user のトークン注入 (build_oauth_credentials の引数 or override env) に
差し替えやすくする。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

GOOGLE_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"

# 個人 OAuth リフレッシュトークンが実際に許可されているスコープ (2026-06-01 時点)。
# drive.readonly は Sheets API v4 の読取も認可する (シートは Drive 配下のファイル扱い)。
# 各クライアントはこの集合の「部分集合」だけを要求しないと invalid_scope になる。
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

    優先順位:
      1. GOOGLE_OAUTH_SCOPES (カンマ/空白区切り) — per-user 上書き・実験用
      2. 呼び出し側が渡した preferred (= そのクライアントが OAuth 用に選んだ集合)

    preferred には「共有トークンが実際に持つスコープの部分集合」を渡すこと
    (例: Sheets 読取なら spreadsheets.readonly ではなく drive.readonly)。
    """
    override = os.environ.get("GOOGLE_OAUTH_SCOPES")
    if override:
        scopes = _split_scopes(override)
        if scopes:
            return scopes
    return list(preferred)


def build_oauth_credentials(preferred_scopes: Sequence[str]) -> Any | None:
    """環境変数から個人 OAuth の Credentials を構築する。

    必要な env が揃っていなければ None を返す (呼び出し側でフォールバック/例外化)。
    """
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
