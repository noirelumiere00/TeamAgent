"""連携コールバック FastAPI アプリ（``/oauth2/callback``）。

Google の同意後リダイレクトを受け、state 検証 → code 交換 → KMS暗号化して RDS 保存する。
exchange_fn / store はテストで注入可能（実 Google / 実 KMS / 実 DB を排除）。
"""

from __future__ import annotations

import html
import os
from collections.abc import Callable
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from teamagent.adapters.google_oauth_flow import OAuthConsentFlow, verify_state
from teamagent.adapters.oauth_token_store import OAuthToken

logger = structlog.get_logger(__name__)


def _page(title: str, body: str, *, accent: str = "#36c08a") -> str:
    t = html.escape(title)
    b = html.escape(body)
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{t}</title><style>"
        "body{margin:0;background:#0f1420;color:#e8edf7;font-family:-apple-system,"
        "'Hiragino Sans','Noto Sans JP',sans-serif;display:flex;min-height:100vh;"
        "align-items:center;justify-content:center}"
        ".card{background:#1a2233;border:1px solid #283450;border-radius:14px;"
        "padding:36px 40px;max-width:520px;text-align:center}"
        f".card h1{{font-size:22px;margin:0 0 14px;color:{accent}}}"
        ".card p{color:#93a1bd;line-height:1.7;margin:6px 0}"
        "</style></head><body>"
        f'<div class="card"><h1>{t}</h1><p>{b}</p></div></body></html>'
    )


def create_app(
    *,
    redirect_uri: str | None = None,
    kms_key_id: str | None = None,
    app_role: str = "teamagent_app",
    exchange_fn: Callable[[str], OAuthToken] | None = None,
    store: Any | None = None,
) -> FastAPI:
    """連携コールバックアプリを構築する。redirect_uri/kms_key_id は env 既定、注入も可。"""
    redirect = redirect_uri or os.environ.get("OAUTH_REDIRECT_URI", "")
    app = FastAPI(title="TeamAgent Connect", docs_url=None, redoc_url=None, openapi_url=None)

    def _exchange(code: str) -> OAuthToken:
        if exchange_fn is not None:
            return exchange_fn(code)
        return OAuthConsentFlow(redirect_uri=redirect).exchange(code)

    def _get_store() -> Any:
        if store is not None:
            return store
        # 遅延 import（テストは store 注入で本番依存を回避）
        from teamagent.adapters.oauth_token_store import KmsCipher, RdsTokenStore
        from teamagent.adapters.pgvector_client import PgVectorClient

        key_id = kms_key_id or os.environ.get("OAUTH_KMS_KEY_ID")
        if not key_id:
            raise RuntimeError("OAUTH_KMS_KEY_ID が未設定です")
        return RdsTokenStore(PgVectorClient.from_env(), KmsCipher(key_id), app_role=app_role)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/oauth2/callback")
    def oauth2_callback(request: Request) -> Response:
        params = request.query_params
        err = params.get("error", "")
        if err:
            logger.warning("connect_callback_user_denied", error=err)
            return HTMLResponse(
                _page(
                    "認可がキャンセルされました",
                    "もう一度 Slack で /teamagent connect をお試しください。",
                ),
                status_code=400,
            )
        code = params.get("code", "")
        state = params.get("state", "")
        if not code or not state:
            return HTMLResponse(
                _page(
                    "不正なリクエスト",
                    "リンクが壊れています。Slack で /teamagent connect をやり直してください。",
                ),
                status_code=400,
            )
        email = verify_state(state)
        if not email:
            logger.warning("connect_callback_bad_state")
            return HTMLResponse(
                _page(
                    "検証に失敗しました",
                    "リンクが古いか不正です。Slack で /teamagent connect をやり直してください。",
                    accent="#f9667a",
                ),
                status_code=400,
            )
        try:
            token = _exchange(code)
            _get_store().put(email, token)
        except Exception:
            # 本文/トークン/PII はログに出さない（request_id 相当の email のみ）
            logger.warning("connect_callback_store_failed", user_email=email)
            return HTMLResponse(
                _page(
                    "連携に失敗しました",
                    "時間をおいて Slack で /teamagent connect をやり直してください。",
                    accent="#f9667a",
                ),
                status_code=500,
            )
        logger.info("connect_callback_ok", user_email=email, scopes=len(token.scopes))
        return HTMLResponse(
            _page(
                "✅ 連携が完了しました",
                f"{email} の Google 連携が完了しました。Slack に戻って AI に話しかけてください。"
                "このタブは閉じて大丈夫です。",
            ),
            status_code=200,
        )

    return app


__all__ = ["create_app"]
