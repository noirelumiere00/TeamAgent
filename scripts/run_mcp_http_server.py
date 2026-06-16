#!/usr/bin/env python
"""TeamAgent MCP サーバを streamable-http で起動する（隔離コンテナ間トランスポート）。

なぜ stdio ではなく HTTP か（WS-B-B1 一次ソース確認の結論）:
- OpenClaw の stdio MCP は「クライアント(OpenClaw)が MCP サーバを子プロセスとして起動・所有」する。
  つまり MCP サーバが OpenClaw のコンテナ内に同居し、OpenClaw の IAM ロール／ネットワークを共有する。
- これは「自律外殻(OpenClaw)は RDS/Secrets/Google に直接触れない」という不変条件を破る。
- よってコンテナ分離する本番では、creds を持つ TeamAgent-MCP バックエンドを別コンテナに置き、
  OpenClaw からは **streamable-http（私設ネットワーク・bearer・loopback/内部のみ）** で接続する。
  （stdio 版 ``scripts/run_mcp_server.py`` は非分離のローカル/PoC 専用。）

RLS fail-closed・user_context 伝播・エラー隔離は ``teamagent.mcp_gateway.server`` の
``build_server`` / ``dispatch_tool`` をそのまま流用する（トランスポート差し替えのみ）。

環境変数:
- ``TEAMAGENT_MCP_BEARER``   : 必須。これが無いと **fail-closed で起動拒否**（無認証公開を禁止）。
- ``TEAMAGENT_MCP_HOST``     : 既定 ``127.0.0.1``（loopback）。コンテナでは私設ネットワーク IF に限定して bind。
- ``TEAMAGENT_MCP_PORT``     : 既定 ``8787``。
- ``TEAMAGENT_MCP_PATH``     : 既定 ``/mcp``。OpenClaw 側 ``mcp.servers.teamagent.url`` と一致させる。

Usage:
    TEAMAGENT_MCP_BEARER=... uv run python scripts/run_mcp_http_server.py
"""

from __future__ import annotations

import contextlib
import hmac
import os
import sys
from collections.abc import AsyncIterator
from typing import Any

import structlog
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from teamagent.mcp_gateway.server import build_production_server

logger = structlog.get_logger(__name__)


class BearerAuthMiddleware:
    """純 ASGI の bearer 認証。保護パス配下は一致しなければ 401（``/healthz`` は対象外）。"""

    def __init__(self, app: Any, *, token: str, protect_prefix: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}"
        self.protect_prefix = protect_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(self.protect_prefix):
            headers = dict(scope.get("headers") or [])
            presented = headers.get(b"authorization", b"").decode("latin-1")
            # 定数時間比較でタイミング攻撃を避ける。
            if not (presented and hmac.compare_digest(presented, self._expected)):
                await self._reject(send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


def build_app(*, bearer: str, path: str) -> Starlette:
    """streamable-http の ASGI アプリを組む（RLS+本人解決 resolver 必須＝STRICT で起動）。"""
    server = build_production_server()  # SLACK_BOT_TOKEN 未設定なら起動拒否（fail-closed）
    session_manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info("mcp_http_started", path=path)
            yield

    return Starlette(
        routes=[Route("/healthz", healthz), Mount(path, app=handle_mcp)],
        middleware=[Middleware(BearerAuthMiddleware, token=bearer, protect_prefix=path)],
        lifespan=lifespan,
    )


def main() -> None:
    """streamable-http で MCP サーバを起動する CLI エントリポイント。"""
    # 構造化ログの出力形式を最初に確定（STRUCTLOG_FORMAT=json で CloudWatch 向け JSON）。
    from teamagent.observability.logging_config import configure_logging

    configure_logging()

    bearer = os.environ.get("TEAMAGENT_MCP_BEARER")
    if not bearer:
        # fail-closed: 無認証の MCP エンドポイント公開は禁止。
        logger.error("mcp_http_no_bearer", hint="set TEAMAGENT_MCP_BEARER")
        sys.exit(2)

    host = os.environ.get("TEAMAGENT_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("TEAMAGENT_MCP_PORT", "8787"))
    path = os.environ.get("TEAMAGENT_MCP_PATH", "/mcp")

    app = build_app(bearer=bearer, path=path)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
