"""Limited trial: in-memory OAuth + read-only library tools. Restart loses all grants.

Enable with CONNECT_MCP_ENABLED=true and CONNECT_MCP_PUBLIC_BASE_URL=https://… .
CONNECT_MCP_SPIKE_REDIRECT_ALLOW adds comma-separated exact callback URIs for the trial.
"""

from __future__ import annotations

import html
import os
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import structlog
from fastapi import FastAPI
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import (
    build_metadata,
    build_resource_metadata_url,
    cors_middleware,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from teamagent.connect_diagnostics import mask_email

logger = structlog.get_logger(__name__)
_request_id: ContextVar[str | None] = ContextVar("mcp_spike_request_id", default=None)
CONSENT_PATH = "/mcp-auth/consent"
_SCOPE = "library:read"


@dataclass
class _Consent:
    client_id: str
    params: AuthorizationParams
    expires_at: float
    csrf: str = ""
    email: str | None = None


class MemoryProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """shortcut: volatile, single-process storage; replace with PR1's durable grants."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.resource = base + "/mcp"
        self.allowed_redirects = {"https://claude.ai/api/mcp/auth_callback"} | {
            uri.strip()
            for uri in os.environ.get("CONNECT_MCP_SPIKE_REDIRECT_ALLOW", "").split(",")
            if uri.strip()
        }
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.pending: dict[str, _Consent] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.tokens: dict[str, AccessToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            parsed = urlsplit(str(uri))
            logger.info(
                "mcp_spike_redirect_observed",
                request_id=_request_id.get(),
                host=parsed.hostname,
                path=parsed.path,
                count=1,
            )
        if not client_info.redirect_uris or any(
            str(uri) not in self.allowed_redirects for uri in client_info.redirect_uris
        ):
            raise RegistrationError("invalid_redirect_uri", "Callback is not allowed")
        if client_info.token_endpoint_auth_method != "none":
            raise RegistrationError("invalid_client_metadata", "Use public client authentication")
        assert client_info.client_id is not None
        self.clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource not in {None, self.resource}:
            raise AuthorizeError("invalid_request", "Invalid resource")
        assert client.client_id is not None
        tx = secrets.token_urlsafe(32)
        self.pending[tx] = _Consent(client.client_id, params, time.time() + 600)
        return self.base + CONSENT_PATH + "?tx=" + tx

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self.codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code = self.codes.pop(authorization_code.code, None)
        if code is None or code.expires_at <= time.time() or code.client_id != client.client_id:
            raise TokenError("invalid_grant", "Code is unavailable")
        token = secrets.token_urlsafe(32)
        self.tokens[token] = AccessToken(
            token=token,
            client_id=code.client_id,
            scopes=code.scopes,
            expires_at=int(time.time()) + 600,
            resource=self.resource,
            subject=code.subject,
        )
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=600, scope=_SCOPE)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "Reconnect for this spike")

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.tokens.get(token)
        if access is None or access.expires_at is None or access.expires_at <= time.time():
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.tokens.pop(token.token, None)


class _SpikeRequests:
    def __init__(self, app: ASGIApp, request_id: Callable[[Request], str | None]) -> None:
        self.app = app
        self.request_id = request_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in {
            "/authorize",
            "/token",
            "/register",
            CONSENT_PATH,
            "/mcp",
            "/mcp/",
        }:
            await self.app(scope, receive, send)
            return
        marker = _request_id.set(self.request_id(Request(scope)))
        try:
            logger.info("mcp_spike_request", request_id=_request_id.get(), count=1)
            await self.app(scope, receive, send)
        finally:
            _request_id.reset(marker)


class _SlashlessMCP:
    def __init__(self, child: ASGIApp) -> None:
        self.child = child

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Mount normally redirects /mcp to /mcp/ using Host; dispatch internally instead.
        scope = dict(scope, path=scope["path"] + "/", root_path=scope.get("root_path", "") + "/mcp")
        await self.child(scope, receive, send)


def install_spike(
    app: FastAPI,
    *,
    authenticate: Callable[[Request], str | None],
    safe_next: Callable[[str | None], str],
    request_id: Callable[[Request], str | None],
    get_pg: Callable[[], Any],
    admins: Callable[[], frozenset[str]],
) -> None:
    base = os.environ.get("CONNECT_MCP_PUBLIC_BASE_URL", "").rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("CONNECT_MCP_PUBLIC_BASE_URL must be an HTTPS origin")
    provider = MemoryProvider(base)
    registration = ClientRegistrationOptions(
        enabled=True, valid_scopes=[_SCOPE], default_scopes=[_SCOPE]
    )
    issuer, resource = AnyHttpUrl(base), AnyHttpUrl(provider.resource)
    mcp = FastMCP(
        "動画ライブラリ",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource,
            client_registration_options=registration,
            required_scopes=[_SCOPE],
        ),
        transport_security=TransportSecuritySettings(
            allowed_hosts=[parsed.netloc],
            allowed_origins=[base, "https://claude.ai", "https://chatgpt.com"],
        ),
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    def ping() -> str:
        access = get_access_token()
        if access is None or access.subject is None:
            raise ValueError("Authentication required")
        return f"{mask_email(access.subject)}: 動画ライブラリとの接続を確認しました。"

    from teamagent.connect_web.library_mcp_tools import install_tools

    install_tools(mcp, base=base, get_pg=get_pg, admins=admins)
    child = mcp.streamable_http_app()
    child.router.redirect_slashes = False
    child.router.default = JSONResponse({"detail": "Not Found"}, status_code=404)
    # SDK creates ordinary Starlette routes: move AS/PRM to the parent, keep only transport.
    for route in list(child.routes):
        if isinstance(route, Route) and route.path != "/":
            child.routes.remove(route)
            if route.path != "/.well-known/oauth-authorization-server":
                app.router.routes.append(route)
    metadata = build_metadata(issuer, None, registration, RevocationOptions())
    metadata.token_endpoint_auth_methods_supported = ["none"]
    metadata.grant_types_supported = ["authorization_code"]
    app.router.routes.append(
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
    )
    child.router.routes.insert(
        0,
        Route(
            "/",
            methods=["GET", "HEAD"],
            endpoint=RequireAuthMiddleware(
                Response(status_code=405, headers={"Allow": "POST"}),
                [_SCOPE],
                build_resource_metadata_url(resource),
            ),
        ),
    )

    async def consent(request: Request) -> Response:
        tx = request.query_params.get("tx", "")
        pending = provider.pending.get(tx)
        if pending is None or pending.expires_at <= time.time():
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        email = authenticate(request)
        target = safe_next(CONSENT_PATH + "?tx=" + tx)
        if email is None:
            if request.method == "POST":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/search/login?next=" + quote(target, safe=""), status_code=303)
        if request.method == "GET":
            callback_host = urlsplit(str(pending.params.redirect_uri)).hostname or ""
            service = (
                "ChatGPT"
                if callback_host == "chatgpt.com"
                else "Claude"
                if callback_host in {"claude.ai", "claude.com"}
                else "MCPクライアント"
            )
            pending.email, pending.csrf = email, secrets.token_urlsafe(32)
            return HTMLResponse(
                '<!doctype html><html lang="ja"><meta charset="utf-8">'
                "<title>動画ライブラリ 接続</title>"
                f"<h1>動画ライブラリを {service} に接続</h1>"
                f"<p>ログイン中: {html.escape(mask_email(email))}</p>"
                "<p>本人の閲覧権限で動画を検索・参照します。広告費・動画ファイルは返しません。変更・削除はできません。</p>"
                f"<p>戻り先: {html.escape(callback_host)}。"
                "この試作の接続は10分で失効し、再起動でも再認証が必要です。</p>"
                f'<form method="post" action="{html.escape(target, quote=True)}">'
                f'<input type="hidden" name="csrf" value="{pending.csrf}">'
                f'<button type="submit">{service} に読み取り接続を許可</button></form></html>',
                headers={"Referrer-Policy": "same-origin"},
            )
        form = await request.form()
        if (
            request.headers.get("origin") != base
            or pending.email != email
            or not pending.csrf
            or not secrets.compare_digest(str(form.get("csrf", "")).encode(), pending.csrf.encode())
        ):
            return JSONResponse({"error": "access_denied"}, status_code=403)
        if provider.pending.pop(tx, None) is not pending:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        code = secrets.token_urlsafe(32)
        params = pending.params
        provider.codes[code] = AuthorizationCode(
            code=code,
            client_id=pending.client_id,
            scopes=params.scopes or [_SCOPE],
            expires_at=time.time() + 60,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=provider.resource,
            subject=email,
        )
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=303,
        )

    app.router.routes.append(Route(CONSENT_PATH, consent, methods=["GET", "POST"]))
    app.router.routes.append(Route("/mcp", endpoint=_SlashlessMCP(child)))
    app.mount("/mcp", child)
    app.add_middleware(_SpikeRequests, request_id=request_id)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(parent: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(parent), mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
