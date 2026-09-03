"""per-user OAuth 同意フロー（`/teamagent connect` の中核・W2）。

各メンバーが自分の Google を認可するための「本人専用の同意URL生成」と「authorization
code → refresh token 交換」を提供する。**nonce・30分TTL付きの署名 state（HMAC）**で callback
対象を検証し、既存 hmac-state DynamoDB テーブルの条件付き書き込みで一度だけ消費する。

- state 署名/検証は stdlib のみ（テスト可・課金0）。
- state のワンタイム消費は connect-web callback だけが行う（UpdateItem・fail-closed）。
- `google-auth-oauthlib` の Flow は遅延 import（実認可は W1 の OAuth クライアント完了後）。
設計: docs/poc/workspace_integration_design.md §4。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any, Literal

from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.hmac_durable_state import HMAC_STATE_SCOPE_ENV, HMAC_STATE_TABLE_ENV

# Workspace 連携スコープ（W1 同意画面・OAuth 同意画面 User Type=Internal＝審査不要）。
# Gmail のみ **modify**（読み＋下書き作成＋ラベル）。送信/削除は GmailClient の adapter-layer
# denylist で物理封鎖し、create_draft（下書き専用）だけ通す＝「AIは要約・提案・下書きまで、
# 送信は人間」をコードで強制。Google Workspace API の他6サービスは readonly。
# ⚠️ 既に readonly で connect 済みの人は、下書き作成(gmail.modify)を使うには再 connect が必要。
WORKSPACE_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    # v0.3 Task2: カレンダー書込（MTG登録提案/日程打診の仮予定）。delete/update/acl 等の
    # 破壊的メソッドは gcalendar_client の _GCalSafePolicy が物理封鎖（gmail と同型・G4）。
    # ⚠️ このスコープ追加により既連携ユーザーは再 connect が必要（include_granted_scopes
    # 不使用＝全スコープ一括再要求。再同意イベントは Slack 分と合わせ1回に束ねる＝Step0 裁定）。
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/contacts.readonly",
)
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DEFAULT_STATE_TTL_S = 1800
_STATE_RECORD_PREFIX = "OAUTH_STATE#"
_SEP = "|"


def _state_secret() -> bytes:
    secret = os.environ.get("OAUTH_STATE_SECRET")
    if not secret:
        raise ValueError("OAUTH_STATE_SECRET が未設定です（CSRF state 署名に必要）")
    return secret.encode("utf-8")


def make_state(
    user_email: str,
    *,
    secret: bytes | None = None,
    now: int | None = None,
    nonce: str | None = None,
) -> str:
    """user_email・発行時刻・nonce を HMAC 署名した30分有効の state にする。"""
    sec = secret or _state_secret()
    email = user_email.strip().lower()
    issued = int(now if now is not None else time.time())
    non = nonce or secrets.token_urlsafe(9)
    body = _SEP.join((email, str(issued), non))
    sig = hmac.new(sec, body.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}{_SEP}{sig}".encode()).decode("ascii")


StateStatus = Literal["ok", "bad_signature", "expired", "malformed"]


def inspect_state(
    state: str,
    *,
    secret: bytes | None = None,
    now: int | None = None,
    max_age_s: int = _DEFAULT_STATE_TTL_S,
) -> tuple[StateStatus, str | None]:
    """state を検証し、``(status, email)`` で **失敗理由まで** 返す（診断コードの出し分け用）。

    - ``("ok", email)``: 署名一致・未失効。
    - ``("bad_signature", None)``: 復号はできたが HMAC 不一致（転記・改変）。email は
      署名未検証なので返さない。
    - ``("expired", email)``: 署名は一致するが発行から ``max_age_s`` 超（または未来すぎる）。
      署名済みなので email は信用してよい（期限切れページに本人メールを出せる）。
    - ``("malformed", None)``: base64/区切り/数値として解釈できない。

    ``verify_state`` はこの関数の薄い包みで、挙動（ok のときだけ email）は従来と同一。
    """
    sec = secret or _state_secret()
    try:
        raw = base64.urlsafe_b64decode(state.encode("ascii")).decode("utf-8")
        body, sig = raw.rsplit(_SEP, 1)
        email, issued_s, _nonce = body.split(_SEP)
        issued = int(issued_s)
    except (ValueError, UnicodeDecodeError):
        return "malformed", None
    expect = hmac.new(sec, body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return "bad_signature", None
    current = int(now if now is not None else time.time())
    if current - issued > max_age_s or issued - current > 60:
        return "expired", email
    return "ok", email


def verify_state(
    state: str,
    *,
    secret: bytes | None = None,
    now: int | None = None,
    max_age_s: int = _DEFAULT_STATE_TTL_S,
) -> str | None:
    """state を検証し、正しく未失効なら email を返す。未知・改竄・期限切れは None。"""
    status, email = inspect_state(state, secret=secret, now=now, max_age_s=max_age_s)
    return email if status == "ok" else None


def consume_state_once(
    state: str,
    *,
    client: Any | None = None,
    now: int | None = None,
    table_name: str | None = None,
    scope: str | None = None,
    record_prefix: str = _STATE_RECORD_PREFIX,
) -> bool:
    """署名・TTL検証済み state を hmac-state テーブルで一度だけ消費する。

    UpdateItem の ``attribute_not_exists(record)`` が同時 callback 間の直列化点になる。
    TTL は掃除用で、state 自体の失効判定は verify_state が行う。
    """
    table = table_name or os.environ.get(HMAC_STATE_TABLE_ENV, "").strip()
    state_scope = scope or os.environ.get(HMAC_STATE_SCOPE_ENV, "").strip()
    if not table or not state_scope:
        raise RuntimeError("OAuth state のワンタイム消費先が未設定です")
    if client is None:
        import boto3

        region = os.environ.get("AWS_REGION") or "ap-northeast-1"
        client = boto3.session.Session().client("dynamodb", region_name=region)
    consumed_at = int(now if now is not None else time.time())
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    try:
        client.update_item(
            TableName=table,
            Key={
                "scope": {"S": state_scope},
                "record": {"S": f"{record_prefix}{digest}"},
            },
            UpdateExpression="SET consumed_at = :now, expires_at = :expires",
            ConditionExpression="attribute_not_exists(#record)",
            ExpressionAttributeNames={"#record": "record"},
            ExpressionAttributeValues={
                ":now": {"N": str(consumed_at)},
                ":expires": {"N": str(consumed_at + _DEFAULT_STATE_TTL_S)},
            },
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        error = response.get("Error", {}) if isinstance(response, dict) else {}
        if error.get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _allowed_workspace_hd() -> str | None:
    """既存の会社ドメイン設定から Google OAuth の hd hint を得る。"""
    configured = os.environ.get("CONNECT_SEARCH_ALLOWED_HD", "").strip().lower()
    if configured:
        return configured
    shared = [
        domain.strip().lower()
        for domain in os.environ.get("TEAMAGENT_SHARED_COMPANY_DOMAINS", "").split(",")
        if domain.strip()
    ]
    return shared[0] if len(shared) == 1 else None


class OAuthConsentFlow:
    """`google-auth-oauthlib` の Flow を薄くラップ（同意URL生成 + code交換）。"""

    def __init__(self, redirect_uri: str, scopes: tuple[str, ...] = WORKSPACE_SCOPES) -> None:
        self._redirect_uri = redirect_uri
        self._scopes = scopes

    def _flow(self) -> Any:
        from google_auth_oauthlib.flow import Flow

        from teamagent.adapters.google_auth import connect_client_id_secret

        # 連携(web)用クライアント: CONNECT_GOOGLE_CLIENT_ID/SECRET 優先・無ければ GOOGLE_*。
        # 共有(desktop)クライアントとは分離する（B案）。
        client_id, client_secret = connect_client_id_secret()
        if not (client_id and client_secret):
            raise ValueError(
                "連携用 OAuth クライアントが未設定です"
                "（CONNECT_GOOGLE_CLIENT_ID/SECRET または GOOGLE_CLIENT_ID/SECRET）"
            )
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

    def authorization_url(self, user_email: str, *, state: str | None = None) -> tuple[str, str]:
        """本人専用の同意URLと state を返す。

        access_type=offline + prompt=consent で refresh token を確実に取得する。

        ``state`` を与えると新規発行せずその値で URL を組む。connect-web の
        ``/oauth2/start/{state}`` が、mcp(oauth_connect) の発行した state から **同一の** 認可
        URL をサーバ側で再構成するための口（呼び出し側が verify_state 済みであること）。
        省略時は従来どおり本人メールで署名した state を新規発行する。
        """
        email = user_email.strip().lower()
        state = state if state is not None else make_state(email)
        auth_params = {
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "login_hint": email,
        }
        allowed_hd = _allowed_workspace_hd()
        if allowed_hd:
            auth_params["hd"] = allowed_hd
        # include_granted_scopes は使わない（他アプリで許可済みの scope=gmail.modify 等まで
        # 合算され、要求と返却が食い違う＋readonly の約束に反する write scope が混ざるため）。
        url, _ = self._flow().authorization_url(**auth_params)
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
        id_token = getattr(creds, "id_token", None)
        return OAuthToken(
            refresh_token=str(creds.refresh_token),
            scopes=tuple(creds.scopes or self._scopes),
            id_token=str(id_token) if id_token else None,
        )


__all__ = [
    "WORKSPACE_SCOPES",
    "OAuthConsentFlow",
    "StateStatus",
    "consume_state_once",
    "inspect_state",
    "make_state",
    "verify_state",
]
