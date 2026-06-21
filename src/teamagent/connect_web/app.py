"""連携コールバック FastAPI アプリ（``/oauth2/callback``）。

Google の同意後リダイレクトを受け、state 検証 → code 交換 → KMS暗号化して RDS 保存する。
exchange_fn / store はテストで注入可能（実 Google / 実 KMS / 実 DB を排除）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import hmac
import html
import json
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import structlog
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from teamagent.adapters.google_oauth_flow import OAuthConsentFlow, verify_state
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.runtime.interactivity import (
    DraftResult,
    InteractivityRouter,
    ParsedAction,
    parse_block_actions,
)

logger = structlog.get_logger(__name__)

# Slack 署名の許容時刻ずれ（リプレイ防止）。
_SLACK_SIG_MAX_SKEW_S = 60 * 5


def verify_slack_signature(
    signing_secret: str, timestamp: str, signature: str, body: bytes, *, now: float | None = None
) -> bool:
    """Slack の `v0` 署名を検証する（X-Slack-Signature / X-Slack-Request-Timestamp）。

    署名ベースは `v0:{timestamp}:{raw_body}`。HMAC-SHA256 を signing_secret で計算し
    `v0=<hexdigest>` を定数時間比較する。古い timestamp（>5分）はリプレイとして拒否。
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    current = now if now is not None else time.time()
    if abs(current - ts) > _SLACK_SIG_MAX_SKEW_S:
        return False
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


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
    signing_secret: str | None = None,
    state_store: Any | None = None,
    slack_client: Any | None = None,
    draft_maker: Callable[[str, str], DraftResult] | None = None,
    http_post: Callable[[str, dict[str, Any]], Any] | None = None,
) -> FastAPI:
    """連携コールバックアプリを構築する。redirect_uri/kms_key_id は env 既定、注入も可。

    interactivity（メールサマリーのボタン）も同じアプリでホストする:
      signing_secret/state_store/slack_client/draft_maker は env 既定・テストで注入可。
    """
    redirect = redirect_uri or os.environ.get("OAUTH_REDIRECT_URI", "")
    sign_secret = (
        signing_secret if signing_secret is not None else os.environ.get("SLACK_SIGNING_SECRET", "")
    )
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
        except Exception as exc:
            # トークン/本文は出さない。診断用に例外の型と短い説明のみ。
            logger.warning(
                "connect_callback_store_failed",
                user_email=email,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
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

    # ── interactivity（メールサマリーのボタン）────────────────────────────────

    def _get_state_store() -> Any:
        if state_store is not None:
            return state_store
        from teamagent.adapters.mail_thread_state_store import RdsMailThreadStateStore
        from teamagent.adapters.pgvector_client import PgVectorClient

        return RdsMailThreadStateStore(PgVectorClient.from_env(), app_role=app_role)

    def _get_slack() -> Any:
        if slack_client is not None:
            return slack_client
        from teamagent.adapters.slack_client import SlackClient

        token = os.environ.get("INTERACTIVE_MAIL_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError("INTERACTIVE_MAIL_BOT_TOKEN / SLACK_BOT_TOKEN が未設定です")
        return SlackClient(bot_token=token)

    def _default_draft_maker(user_email: str, thread_id: str) -> DraftResult:
        """「対応する」: 当該スレッドの返信下書きを作成（mail_reply・送信しない）。"""
        from teamagent.orchestrator.factory import _build_token_store
        from teamagent.skills.base import SkillContext
        from teamagent.skills.mail_reply.schema import MailReplyInput
        from teamagent.skills.mail_reply.skill import MailReplySkill

        skill = MailReplySkill(token_store=_build_token_store())
        ctx = SkillContext(
            request_id=f"intr-{int(time.time())}", metadata={"user_email": user_email}
        )
        try:
            out = skill.run(MailReplyInput(target_thread_id=thread_id), ctx)
        except PermissionError as exc:
            return DraftResult(created=False, message=str(exc))
        return DraftResult(
            created=out.created,
            thread_url=out.thread_url,
            draft_subject=out.draft_subject,
            message=out.note,
        )

    async def _apply(response_url: str, outcome_text: str, blocks: list[dict[str, Any]]) -> None:
        body = {"replace_original": True, "text": outcome_text, "blocks": blocks}
        if http_post is not None:
            res = http_post(response_url, body)
            if asyncio.iscoroutine(res):
                await res
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(response_url, json=body)

    async def _process(action: ParsedAction) -> None:
        rid = f"intr-{int(time.time())}"
        try:
            slack = _get_slack()
            email = await slack.resolve_user_email(action.user_id, request_id=rid)
            if not email:
                logger.warning("interactivity_identity_unresolved", user_id=action.user_id)
                return
            router = InteractivityRouter(
                _get_state_store(), draft_maker=draft_maker or _default_draft_maker
            )
            now = _dt.datetime.now(_dt.UTC)
            loop = asyncio.get_running_loop()
            outcome = await loop.run_in_executor(
                None, lambda: router.handle(action, email, now=now)
            )
            if not outcome.handled:
                return
            if action.response_url:
                await _apply(action.response_url, outcome.text, outcome.blocks)
            logger.info(
                "interactivity_handled",
                action_id=action.action_id,
                status=outcome.status_written,
            )
        except Exception as exc:
            logger.warning("interactivity_process_failed", err=type(exc).__name__)

    @app.post("/slack/interactivity")
    async def slack_interactivity(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        ts = request.headers.get("X-Slack-Request-Timestamp", "")
        sig = request.headers.get("X-Slack-Signature", "")
        if not verify_slack_signature(sign_secret, ts, sig, body):
            logger.warning("interactivity_bad_signature")
            return JSONResponse({"ok": False}, status_code=401)
        form = parse_qs(body.decode("utf-8"))
        payload_raw = (form.get("payload") or [""])[0]
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except (ValueError, TypeError):
            return JSONResponse({"ok": True})  # 解釈不能でも 200（Slack の再送を避ける）
        action = parse_block_actions(payload)
        if action is None:
            return JSONResponse({"ok": True})
        # 3秒以内に ack し、重い処理（本人解決・下書き生成・状態更新）は背後で実施。
        background.add_task(_process, action)
        return JSONResponse({"ok": True})

    return app


__all__ = ["create_app"]
