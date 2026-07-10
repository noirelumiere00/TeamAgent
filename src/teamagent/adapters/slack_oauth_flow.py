"""per-user Slack OAuth 同意フロー（xoxp 取得・Google の google_oauth_flow と対称）。

各メンバーが自分の Slack を個人認可（User Token Scopes）するための「本人専用の同意URL生成」
と「authorization code → user token(xoxp) 交換」を提供する。取得した xoxp は
「本人としての検索・履歴読取」に使い、ワークスペース共有の bot token(xoxb) とは別経路にする。

state 設計（Google 版との違い・意図的）:
    google_oauth_flow.make_state は HMAC(email) の決定論署名で、同一 email なら常に同値・
    nonce 無し・TTL 無し＝CSRF リプレイに弱い（レッドチーム既知）。本モジュールはこれを踏襲せず、
    per-request nonce ＋ 発行時刻を含めて HMAC 署名し、verify で TTL 検証する（stateless・課金0）。
    ※厳密なワンタイム化（サーバ側 nonce 消費）は後続 PR に切り出す。

- state 署名/検証は stdlib のみ（テスト可・課金0）。
- 実 code 交換は `slack_sdk` の WebClient を遅延 import（Slack app 側 scope 設定完了後）。
設計: docs/poc/workspace_integration_design.md（Google 連携 §3-4 と対称）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from teamagent.adapters.oauth_token_store import SlackOAuthToken

# Slack User Token Scopes（最小権限・読み取り先行）。本人としての横断検索・巡回要約に必要な
# read 系のみ。本人として投稿する chat:write(user) 等の書込系は当初付与しない（誤爆リスク大・
# 要件は横断Q&A/巡回が主）。scope 追加は Slack app 設定の Reinstall を伴うため段階的に。
SLACK_USER_SCOPES: tuple[str, ...] = (
    "search:read",
    "channels:history",
    "groups:history",
    "im:history",
    "mpim:history",
    "users:read",
)
_AUTHORIZE_URI = "https://slack.com/oauth/v2/authorize"

# state の署名鍵は Google の OAUTH_STATE_SECRET と分離する（署名ドメインを分ける）。
_STATE_SECRET_ENV = "SLACK_OAUTH_STATE_SECRET"
# state のデフォルト有効期限（秒）。xoxp は本人なりすまし級のため短めに（漏洩窓を縮小）。
# ※厳密なワンタイム化（サーバ側 nonce 消費）は後続 PR。それまでは短 TTL で残リスクを抑える。
_DEFAULT_STATE_TTL_S = 180
# payload 区切り。email/nonce/数値/hex いずれにも出現しない文字を使う。
_SEP = "|"


def _state_secret() -> bytes:
    secret = os.environ.get(_STATE_SECRET_ENV)
    if not secret:
        raise ValueError(f"{_STATE_SECRET_ENV} が未設定です（CSRF state 署名に必要）")
    return secret.encode("utf-8")


def _slack_client_id_secret() -> tuple[str | None, str | None]:
    """連携用 Slack OAuth クライアント（CONNECT_SLACK_* 優先・無ければ SLACK_*）。"""
    cid = os.environ.get("CONNECT_SLACK_CLIENT_ID") or os.environ.get("SLACK_CLIENT_ID")
    sec = os.environ.get("CONNECT_SLACK_CLIENT_SECRET") or os.environ.get("SLACK_CLIENT_SECRET")
    return cid, sec


def make_state(
    user_email: str,
    *,
    secret: bytes | None = None,
    now: int | None = None,
    nonce: str | None = None,
) -> str:
    """user_email ＋ 発行時刻 ＋ per-request nonce を HMAC 署名して state にする。

    同一 email でも毎回異なる state になる（nonce）。verify_state 側で TTL を検証する。
    """
    sec = secret or _state_secret()
    email = user_email.strip().lower()
    issued = int(now if now is not None else time.time())
    non = nonce or secrets.token_urlsafe(9)
    body = _SEP.join((email, str(issued), non))
    sig = hmac.new(sec, body.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}{_SEP}{sig}".encode()).decode("ascii")


def verify_state(
    state: str,
    *,
    secret: bytes | None = None,
    now: int | None = None,
    max_age_s: int = _DEFAULT_STATE_TTL_S,
) -> str | None:
    """state を検証し、正しく未失効なら user_email を返す。改竄/CSRF/失効/壊れた値なら None。"""
    sec = secret or _state_secret()
    try:
        raw = base64.urlsafe_b64decode(state.encode("ascii")).decode("utf-8")
        body, sig = raw.rsplit(_SEP, 1)
        email, issued_s, _nonce = body.split(_SEP)
        issued = int(issued_s)
    except (ValueError, UnicodeDecodeError):
        return None
    expect = hmac.new(sec, body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    current = int(now if now is not None else time.time())
    # 失効（発行から max_age 超過）／発行時刻が未来すぎる（時計ズレ耐性 60s）なら拒否。
    if current - issued > max_age_s or issued - current > 60:
        return None
    return email


class SlackOAuthConsentFlow:
    """Slack OAuth v2（user_scope）の薄いラッパ（同意URL生成 + code交換）。

    bot scope 欄ではなく **user_scope** 欄を使うのが xoxp 取得の肝。既存の共有 bot token(xoxb)
    は温存し、ここで得る per-user xoxp は別経路（保管トークンから WebClient を組む）で使う。
    """

    def __init__(self, redirect_uri: str, scopes: tuple[str, ...] = SLACK_USER_SCOPES) -> None:
        self._redirect_uri = redirect_uri
        self._scopes = scopes

    def authorization_url(self, user_email: str) -> tuple[str, str]:
        """本人専用の Slack 認可URLと state を返す。"""
        cid, _ = _slack_client_id_secret()
        if not cid:
            raise ValueError(
                "連携用 Slack OAuth クライアントが未設定です"
                "（CONNECT_SLACK_CLIENT_ID または SLACK_CLIENT_ID）"
            )
        state = make_state(user_email)
        params = {
            "client_id": cid,
            "user_scope": ",".join(self._scopes),  # bot scope(scope=) は使わない
            "redirect_uri": self._redirect_uri,
            "state": state,
        }
        return f"{_AUTHORIZE_URI}?{urlencode(params)}", state

    def exchange(self, code: str) -> SlackOAuthToken:
        """authorization code を user token(xoxp) に交換して SlackOAuthToken を返す。"""
        from slack_sdk.web import WebClient

        cid, sec = _slack_client_id_secret()
        if not (cid and sec):
            raise ValueError(
                "連携用 Slack OAuth クライアントが未設定です"
                "（CONNECT_SLACK_CLIENT_ID/SECRET または SLACK_CLIENT_ID/SECRET）"
            )
        resp = WebClient().oauth_v2_access(
            client_id=cid,
            client_secret=sec,
            code=code,
            redirect_uri=self._redirect_uri,
        )
        # user token は応答トップ(access_token=bot)ではなく authed_user 配下（xoxp）にある。
        authed: dict[str, Any] = resp.get("authed_user") or {}
        xoxp = authed.get("access_token")
        if not xoxp:
            raise ValueError("Slack user token (xoxp) を取得できません（user_scope の同意を確認）")
        team: dict[str, Any] = resp.get("team") or {}
        scope_str = str(authed.get("scope") or "")
        scopes = tuple(s for s in scope_str.split(",") if s) or self._scopes
        return SlackOAuthToken(
            access_token=str(xoxp),
            scopes=scopes,
            slack_user_id=str(authed.get("id") or ""),
            team_id=str(team.get("id") or ""),
        )


__all__ = [
    "SLACK_USER_SCOPES",
    "SlackOAuthConsentFlow",
    "make_state",
    "verify_state",
]
