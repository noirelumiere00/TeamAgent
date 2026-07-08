"""管理画面の設定（env から構築）。

機密（Client ID / セッション鍵）はコードに置かず env / Secrets 経由で渡す。
allowlist と会社ドメイン(hd) で「オーナー（＋少数管理者）だけ」に閲覧を絞る。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DashboardConfig:
    """管理画面の実行設定。"""

    allowed_emails: frozenset[str]  # 閲覧を許可するメール（lower 正規化済み）
    allowed_hd: str | None  # 会社ドメイン（Google の hd クレーム照合・任意）
    google_client_id: str | None  # ウェブアプリ型 OAuth クライアント ID（id_token の aud）
    session_secret: bytes  # セッション Cookie の HMAC 署名鍵
    dev_bypass: bool  # True で認証をスキップ（ローカル開発・OAuth 設定前のみ）
    db_app_role: str = "teamagent_dashboard"  # 読み取り用ロール（migration 0007）
    session_ttl_s: int = 8 * 3600  # セッション有効期間（既定8h）
    cookie_secure: bool = False  # 公開(HTTPS)時は True（ローカルHTTPでは False）
    allowed_hd_opens_domain: bool = False
    # True: hd(会社ドメイン)一致なら allowlist 不問で許可＝会社ドメイン全体に開放（connect-web の全社共有用）。
    # False(既定): 従来どおり hd は絞り込みの AND 条件で allowlist 必須（ダッシュボードのオーナー限定を維持）。

    @property
    def auth_enabled(self) -> bool:
        """実認証が有効か（dev_bypass でなく、Client ID が設定されている）。"""
        return not self.dev_bypass and bool(self.google_client_id)


def _split_emails(raw: str) -> frozenset[str]:
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def _env_bool(env: dict[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def load_config(env: dict[str, str] | None = None) -> DashboardConfig:
    """env から DashboardConfig を構築する（テストは env を注入可能）。

    必須/推奨 env:
      - DASHBOARD_ALLOWED_EMAILS: カンマ区切り（例 you@vectorinc.co.jp）
      - DASHBOARD_ALLOWED_HD: 会社ドメイン（例 vectorinc.co.jp・任意）
      - DASHBOARD_GOOGLE_CLIENT_ID: ウェブアプリ型 OAuth クライアント ID
      - DASHBOARD_SESSION_SECRET: セッション署名鍵（未設定なら起動毎ランダム＝再起動で要再ログイン）
      - DASHBOARD_DEV_BYPASS: 1 で認証スキップ（ローカル開発のみ）
      - DASHBOARD_COOKIE_SECURE: 1 で Secure Cookie（HTTPS 公開時）
    """
    e = env if env is not None else dict(os.environ)
    secret_raw = e.get("DASHBOARD_SESSION_SECRET", "").strip()
    # 未設定ならプロセス毎のランダム鍵（再起動でセッション無効化＝安全側）。
    secret = secret_raw.encode("utf-8") if secret_raw else os.urandom(32)
    hd = e.get("DASHBOARD_ALLOWED_HD", "").strip() or None
    return DashboardConfig(
        allowed_emails=_split_emails(e.get("DASHBOARD_ALLOWED_EMAILS", "")),
        allowed_hd=hd.lower() if hd else None,
        google_client_id=e.get("DASHBOARD_GOOGLE_CLIENT_ID", "").strip() or None,
        session_secret=secret,
        dev_bypass=_env_bool(e, "DASHBOARD_DEV_BYPASS"),
        cookie_secure=_env_bool(e, "DASHBOARD_COOKIE_SECURE"),
    )


__all__ = ["DashboardConfig", "load_config"]
