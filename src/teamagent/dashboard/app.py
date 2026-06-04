"""管理画面 FastAPI アプリ（薄いグルー）。

純ロジックは config/auth/queries/render に置き、ここは「認証ゲート + ルーティング + 描画」だけ。
DB を触るルートは sync def（Starlette が threadpool 実行＝同期 psycopg がループを塞がない）。
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from teamagent.dashboard.auth import (
    Verifier,
    authenticate_id_token,
    make_session,
    verify_session,
)
from teamagent.dashboard.config import DashboardConfig, load_config
from teamagent.dashboard.queries import DashboardQueries
from teamagent.dashboard.render import render_dashboard, render_errors, render_login

logger = structlog.get_logger(__name__)

_COOKIE = "ta_dash_session"
_DEV_EMAIL = "(dev-bypass)"


def create_app(
    config: DashboardConfig | None = None,
    *,
    queries: DashboardQueries | None = None,
    verifier: Verifier | None = None,
) -> FastAPI:
    """管理画面アプリを構築する。config / queries / verifier はテストで注入可能。

    verifier を渡すと id_token 検証に使う（テストでネットワークを排除）。本番は None
    （google-auth で実検証）。
    """
    cfg = config or load_config()
    app = FastAPI(title="TeamAgent 管理画面", docs_url=None, redoc_url=None, openapi_url=None)

    # queries は遅延生成（DB 接続は初回アクセス時）。注入時はそれを使う。
    state: dict[str, DashboardQueries | None] = {"q": queries}

    def get_queries() -> DashboardQueries:
        q = state["q"]
        if q is None:
            from teamagent.adapters.pgvector_client import PgVectorClient

            q = DashboardQueries(PgVectorClient.from_env(), app_role=cfg.db_app_role)
            state["q"] = q
        return q

    def current_email(request: Request) -> str | None:
        if cfg.dev_bypass:
            return _DEV_EMAIL
        cookie = request.cookies.get(_COOKIE)
        if not cookie:
            return None
        return verify_session(cookie, cfg.session_secret)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/login", response_class=HTMLResponse)
    def login() -> HTMLResponse:
        return HTMLResponse(render_login(client_id=cfg.google_client_id, dev_bypass=cfg.dev_bypass))

    @app.post("/auth/verify")
    async def auth_verify(request: Request) -> Response:
        form = await request.form()
        credential = str(form.get("credential", ""))
        ok, email = authenticate_id_token(credential, cfg, verifier=verifier)
        if not ok or email is None:
            logger.warning("dashboard_login_denied", email=email)
            return HTMLResponse(
                render_login(
                    client_id=cfg.google_client_id,
                    error="ログインできませんでした（許可されていないアカウントか検証失敗）。",
                ),
                status_code=403,
            )
        logger.info("dashboard_login_ok", email=email)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            _COOKIE,
            make_session(email, cfg.session_secret, ttl_s=cfg.session_ttl_s),
            max_age=cfg.session_ttl_s,
            httponly=True,
            samesite="lax",
            secure=cfg.cookie_secure,
        )
        return resp

    @app.get("/logout")
    def logout() -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(_COOKIE)
        return resp

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        email = current_email(request)
        if email is None:
            return RedirectResponse("/login", status_code=303)
        try:
            q = get_queries()
            data = {
                "email": email,
                "kpis": q.kpis(),
                "daily": q.daily_series(30),
                "skills": q.skill_breakdown(7),
                "users": q.user_breakdown(),
                "runtime_now": q.runtime_now(),
                "runtime_peaks": q.runtime_peaks(1),
                "oauth": q.oauth_status(),
            }
        except Exception:
            logger.warning("dashboard_query_failed", exc_info=True)
            return HTMLResponse(
                render_dashboard(
                    {
                        "email": email,
                        "kpis": {},
                        "daily": [],
                        "skills": [],
                        "users": [],
                        "runtime_now": None,
                        "runtime_peaks": {},
                        "oauth": [],
                        "_db_error": True,
                    }
                ),
                status_code=200,
            )
        return HTMLResponse(render_dashboard(data))

    @app.get("/errors", response_class=HTMLResponse)
    def errors(request: Request) -> Response:
        email = current_email(request)
        if email is None:
            return RedirectResponse("/login", status_code=303)
        try:
            rows = get_queries().error_list(50)
        except Exception:
            logger.warning("dashboard_query_failed", exc_info=True)
            rows = []
        return HTMLResponse(render_errors(rows, email=email))

    return app


__all__ = ["create_app"]
