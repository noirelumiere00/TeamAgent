"""管理画面の認証（Google id_token 検証 + allowlist + HMAC 署名セッション）。

依存を増やさないため、セッションは stdlib の HMAC-SHA256 署名 Cookie（itsdangerous 不要）。
Google id_token の検証は google-auth（導入済）を使い、テストでは verifier を注入して
ネットワークを排除する。「email_verified + 会社ドメイン(hd) + allowlist」の三段で本人に絞る。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

from teamagent.dashboard.config import DashboardConfig


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_session(email: str, secret: bytes, *, ttl_s: int, now: float | None = None) -> str:
    """email を含む署名済みセッション値を作る（``<payload>.<sig>``）。"""
    ts = int(time.time() if now is None else now)
    raw = json.dumps({"email": email, "exp": ts + ttl_s}, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(sig)}"


def verify_session(value: str, secret: bytes, *, now: float | None = None) -> str | None:
    """セッション値を検証し email を返す。署名不一致/期限切れ/壊れは None。"""
    ts = time.time() if now is None else now
    try:
        raw_b64, sig_b64 = value.split(".", 1)
        raw = _b64d(raw_b64)
        sig = _b64d(sig_b64)
    except Exception:
        return None
    expected = hmac.new(secret, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if int(payload.get("exp", 0)) < int(ts):
        return None
    email = payload.get("email")
    return email if isinstance(email, str) and email else None


# id_token 検証関数の型（token, client_id -> claims）。テストで注入する。
Verifier = Callable[[str, str], dict[str, Any]]


def verify_google_id_token(
    token: str, client_id: str, *, verifier: Verifier | None = None
) -> dict[str, Any]:
    """Google id_token(JWT) を検証して claims を返す。aud/iss/exp は google-auth が検証する。

    verifier を渡すとそれを使う（テストでネットワークを排除）。本番は google-auth を使う。
    """
    if verifier is not None:
        return verifier(token, client_id)
    from google.auth.transport import requests as greq
    from google.oauth2 import id_token as gid

    req = greq.Request()
    # google-auth は型注釈が無く、かつ CI では google 未導入で override により Any 化する。
    # 両環境で安定させるため Any 経由で呼ぶ（strict の no-untyped-call / unused-ignore を回避）。
    verify_fn: Any = gid.verify_oauth2_token
    claims: dict[str, Any] = verify_fn(token, req, client_id)
    return claims


def check_allowed(claims: dict[str, Any], config: DashboardConfig) -> tuple[bool, str | None]:
    """claims が許可条件（email_verified + hd + allowlist）を満たすか。

    Returns: (allowed, normalized_email)。allowed=False でも email は監査ログ用に返す。
    """
    email = str(claims.get("email", "")).strip().lower()
    if not email:
        return False, None
    # email_verified は bool/"true" 両対応
    verified = claims.get("email_verified")
    if not (verified is True or str(verified).lower() == "true"):
        return False, email
    hd = str(claims.get("hd", "")).strip().lower()
    # 会社ドメイン全体に開放するモード（connect-web の全社共有）:
    # allowlist に居る か hd が会社ドメインに一致すれば許可。id_token は Google 署名済みで
    # hd は Workspace のみ付与・詐称不可、かつ上で email_verified を確認済みなので安全。
    if config.allowed_hd_opens_domain and config.allowed_hd:
        if email in config.allowed_emails or hd == config.allowed_hd:
            return True, email
        return False, email
    # 従来（絞り込み）: hd は AND の追加条件、allowlist は必須（ダッシュボードのオーナー限定）。
    if config.allowed_hd and hd != config.allowed_hd:
        return False, email
    if email not in config.allowed_emails:
        return False, email
    return True, email


def authenticate_id_token(
    token: str, config: DashboardConfig, *, verifier: Verifier | None = None
) -> tuple[bool, str | None]:
    """id_token を検証 → 許可判定までを一括で行う。検証失敗は (False, None)。"""
    if not config.google_client_id:
        return False, None
    try:
        claims = verify_google_id_token(token, config.google_client_id, verifier=verifier)
    except Exception:
        return False, None
    return check_allowed(claims, config)


__all__ = [
    "authenticate_id_token",
    "check_allowed",
    "make_session",
    "verify_google_id_token",
    "verify_session",
]
