"""Shared ChatGPT/Claude MCP transport and persistent, revocable OAuth connections."""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlsplit

import structlog
from fastapi import FastAPI
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.auth.routes import build_metadata, build_resource_metadata_url, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from teamagent.connect_diagnostics import mask_email
from teamagent.connect_web.library_mcp_oauth import (
    CONSENT_PATH,
    SCOPE,
    PersistentProvider,
    StoreUnavailableError,
    current_request_id,
    digest,
    service_name,
)
from teamagent.connect_web.library_mcp_setup import setup_html
from teamagent.dashboard.auth import Verifier, check_allowed, verify_google_id_token
from teamagent.dashboard.config import DashboardConfig

logger = structlog.get_logger(__name__)
_SCOPE = SCOPE
_BROWSER_COOKIE = "__Host-library-mcp-browser"
_NONCE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_AUTH_PATHS = {"/authorize", "/token", "/register", "/revoke"}
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}
_BROWSER_HEADERS = {**_HEADERS, "Referrer-Policy": "same-origin"}


def _error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status, headers=_HEADERS)


def _csp_callback_source(uri: str) -> str:
    if any(ord(char) < 33 or ord(char) == 127 for char in uri):
        raise ValueError("The callback is not a safe CSP source")
    callback = urlsplit(uri)
    source = callback._replace(query="", fragment="").geturl()
    if not re.fullmatch(r"https?://[A-Za-z0-9.\[\]:_-]+(?:/[A-Za-z0-9._~%!$&()*+=:@/-]*)?", source):
        raise ValueError("The callback is not a safe CSP source")
    return source


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate parameter")
        result[key] = value
    return result


async def _bounded_request(request: Request) -> Request:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16384:
            raise ValueError("Request is too large")
        body.extend(chunk)
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    return Request(request.scope, receive=receive)


async def _form(request: Request) -> dict[str, str]:
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        raise ValueError("Expected a form")
    form = await request.form()
    if len(form.multi_items()) != len(form):
        raise ValueError("Duplicate parameter")
    values: dict[str, str] = {}
    for key, value in form.items():
        if (
            not isinstance(value, str)
            or len(key) > 100
            or len(value) > (12000 if key == "credential" else 4096)
        ):
            raise ValueError("Invalid form")
        values[key] = value
    return values


def _browser(request: Request) -> str | None:
    value = request.cookies.get(_BROWSER_COOKIE, "")
    return value if _NONCE.fullmatch(value) else None


def _set_browser(response: Response, browser: str) -> Response:
    response.set_cookie(
        _BROWSER_COOKIE,
        browser,
        max_age=600,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} | 動画ライブラリ</title>"
        "<style>body{font:16px/1.8 system-ui,sans-serif;background:#f7f8fa;color:#20252d;"
        "margin:0;padding:24px}main{max-width:680px;margin:32px auto;background:white;"
        "padding:32px;border:1px solid #e3e6eb;border-radius:12px}h1{font-size:25px}"
        "button{font:inherit;padding:10px 18px;cursor:pointer;margin:8px 8px 0 0}"
        "a{color:#234e89}small{color:#5b6470}li{margin-bottom:14px}"
        ":focus-visible{outline:3px solid #2563eb;outline-offset:4px}"
        "@media(max-width:500px){body{padding:12px}main{padding:20px;margin:12px auto}}"
        "</style></head><body><main>"
        f"{body}</main></body></html>"
    )


class _SpikeRequests:
    def __init__(self, app: ASGIApp, request_id: Callable[[Request], str | None]) -> None:
        self.app, self.request_id = app, request_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not (
            scope["path"] in _AUTH_PATHS
            or scope["path"].startswith("/mcp-auth/")
            or scope["path"] in {"/mcp", "/mcp/"}
        ):
            await self.app(scope, receive, send)
            return
        marker = current_request_id.set(self.request_id(Request(scope)))
        logger.info("mcp_spike_request", request_id=current_request_id.get(), count=1)
        try:
            await self.app(scope, receive, send)
        except StoreUnavailableError:
            await _error("temporarily_unavailable", 503)(scope, receive, send)
        finally:
            current_request_id.reset(marker)


class _StoreErrors:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except StoreUnavailableError:
            await _error("temporarily_unavailable", 503)(scope, receive, send)


class _SlashlessMCP:
    def __init__(self, child: ASGIApp) -> None:
        self.child = child

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
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
    auth_config: DashboardConfig,
    content_security_policy: str,
    google_verifier: Verifier | None = None,
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
    if content_security_policy.count("form-action 'self'") != 1:
        raise ValueError("The baseline consent form policy is unexpected")

    def subject_allowed(email: str, hd: str | None) -> bool:
        allowed, _ = check_allowed(
            {"email": email, "email_verified": True, "hd": hd or ""}, auth_config
        )
        return allowed and auth_config.auth_enabled

    provider = PersistentProvider(base, get_pg, subject_allowed)
    registration = ClientRegistrationOptions(
        enabled=True, valid_scopes=[_SCOPE], default_scopes=[_SCOPE]
    )
    revocation = RevocationOptions(enabled=True)
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
            revocation_options=revocation,
            required_scopes=[_SCOPE],
        ),
        transport_security=TransportSecuritySettings(
            allowed_hosts=[parsed.netloc],
            allowed_origins=[base, "https://claude.ai", "https://chatgpt.com"],
        ),
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
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
    child.add_middleware(_StoreErrors)
    child.router.redirect_slashes = False
    child.router.default = JSONResponse({"detail": "Not Found"}, status_code=404)
    # Retain SDK resource metadata; use bounded wrappers around its OAuth handlers.
    for route in list(child.routes):
        if isinstance(route, Route) and route.path != "/":
            child.routes.remove(route)
            if route.path not in _AUTH_PATHS | {"/.well-known/oauth-authorization-server"}:
                app.router.routes.append(route)
    metadata = build_metadata(issuer, None, registration, revocation)
    metadata.token_endpoint_auth_methods_supported = ["none"]
    metadata.revocation_endpoint_auth_methods_supported = ["none"]
    metadata.grant_types_supported = ["authorization_code", "refresh_token"]
    app.router.routes.append(
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
    )
    authenticator = ClientAuthenticator(provider)

    async def revoke(request: Request) -> Response:
        try:
            client = await authenticator.authenticate_request(request)
        except AuthenticationError:
            return _error("unauthorized_client", 401)
        form = await _form(request)
        token = form.get("token", "")
        if not token or len(token) > 256 or not client.client_id:
            return _error("invalid_request")
        # Lookup by hash includes expired credentials; revocation always kills the grant.
        await provider.db("revoke_token", digest(token), client.client_id)
        return Response(status_code=200, headers=_HEADERS)

    handlers = {
        "/authorize": AuthorizationHandler(provider).handle,
        "/token": TokenHandler(provider, authenticator).handle,
        "/register": RegistrationHandler(provider, options=registration).handle,
        "/revoke": revoke,
    }

    def bounded_endpoint(path: str, handler: Callable[[Request], Awaitable[Response]]) -> Any:
        async def handle(request: Request) -> Response:
            try:
                if request.method == "POST":
                    request = await _bounded_request(request)
                if path == "/register":
                    if (
                        request.headers.get("content-type", "").split(";", 1)[0]
                        != "application/json"
                    ):
                        return _error("invalid_client_metadata")
                    data = json.loads(await request.body(), object_pairs_hook=_unique_object)
                    if not isinstance(data, dict):
                        return _error("invalid_client_metadata")
                    redirects = data.get("redirect_uris")
                    if not isinstance(redirects, list) or not redirects or len(redirects) > 4:
                        return _error("invalid_redirect_uri")
                    for uri in redirects:
                        if not isinstance(uri, str) or len(uri) > 2048:
                            return _error("invalid_redirect_uri")
                        callback = urlsplit(uri)
                        known = callback.hostname in {
                            "chatgpt.com",
                            "claude.ai",
                            "claude.com",
                            "localhost",
                            "127.0.0.1",
                        }
                        safe_path = bool(
                            re.fullmatch(
                                r"/(?:connector/oauth/[A-Za-z0-9_-]{1,128}|api/mcp/auth_callback|callback)",
                                callback.path,
                            )
                        )
                        logger.info(
                            "mcp_spike_redirect_observed",
                            request_id=request_id(request),
                            host=callback.hostname if known else "unsupported",
                            path=callback.path if known and safe_path else "unsupported",
                            count=1,
                        )
                        if uri not in provider.allowed_redirects:
                            return _error("invalid_redirect_uri")
                    if not await provider.rate_limit("registration:day", 100, 86400):
                        return _error("temporarily_unavailable", 429)
                else:
                    if request.method == "GET":
                        query = request.query_params
                        if len(query.multi_items()) != len(query):
                            return _error("invalid_request")
                        values = dict(query)
                    else:
                        values = await _form(request)
                    if any(len(key) > 100 or len(value) > 4096 for key, value in values.items()):
                        return _error("invalid_request")
                    if values.get("resource") not in {None, provider.resource}:
                        return _error("invalid_target")
                    if path == "/authorize" and (
                        values.get("code_challenge_method") != "S256"
                        or not _NONCE.fullmatch(values.get("code_challenge", ""))
                        or len(values.get("state", "")) > 1024
                    ):
                        return _error("invalid_request")
                    if (
                        path == "/token"
                        and values.get("grant_type") == "authorization_code"
                        and not re.fullmatch(
                            r"[A-Za-z0-9._~-]{43,128}", values.get("code_verifier", "")
                        )
                    ):
                        return _error("invalid_request")
                    if not await provider.rate_limit("oauth:" + path, 1000):
                        return _error("temporarily_unavailable", 429)
                return await handler(request)
            except (ValueError, UnicodeError, RecursionError):
                return _error("invalid_request")
            except StoreUnavailableError:
                return _error("temporarily_unavailable", 503)

        return handle

    for path, handler in handlers.items():
        endpoint = bounded_endpoint(path, handler)
        methods = ["GET", "POST"] if path == "/authorize" else ["POST", "OPTIONS"]
        if path != "/authorize":
            endpoint = cors_middleware(endpoint, methods)
        app.router.routes.append(Route(path, endpoint=endpoint, methods=methods))

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

    async def pending_request(request: Request) -> tuple[str, dict[str, Any]] | None:
        if len(request.query_params.multi_items()) != len(request.query_params):
            return None
        tx = request.query_params.get("tx", "")
        if not _NONCE.fullmatch(tx):
            return None
        pending = await provider.db("get_request", digest(tx))
        return (tx, pending) if pending else None

    async def consent(request: Request) -> Response:
        loaded = await pending_request(request)
        if loaded is None:
            return _error("invalid_request")
        tx, pending = loaded
        target = CONSENT_PATH + "?tx=" + tx
        browser = _browser(request)
        if request.method == "POST":
            request = await _bounded_request(request)
            form = await _form(request)
            csrf = form.get("csrf", "")
            if (
                request.headers.get("origin") != base
                or not browser
                or len(csrf) > 128
                or pending.get("browser_hash") != digest(browser)
            ):
                return _error("access_denied", 403)
            params = pending["params"]
            if form.get("decision") == "deny":
                if not await provider.db(
                    "cancel_request", digest(tx), digest(csrf), digest(browser)
                ):
                    return _error("access_denied", 403)
                return RedirectResponse(
                    construct_redirect_uri(
                        params["redirect_uri"], error="access_denied", state=params.get("state")
                    ),
                    status_code=303,
                    headers=_HEADERS,
                )
            email = pending.get("email")
            if not email or not subject_allowed(email, pending.get("verified_hd")):
                return _error("access_denied", 403)
            code = secrets.token_urlsafe(32)
            result = await provider.db(
                "authorize_request", digest(tx), digest(csrf), email, digest(code), digest(browser)
            )
            if not result:
                return _error("access_denied", 403)
            return RedirectResponse(
                construct_redirect_uri(
                    params["redirect_uri"], code=code, state=params.get("state")
                ),
                status_code=303,
                headers=_HEADERS,
            )
        if not auth_config.auth_enabled:
            return _error("temporarily_unavailable", 503)
        browser = browser or secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        if not await provider.db(
            "bind_request",
            digest(tx),
            digest(csrf),
            pending.get("email"),
            pending.get("verified_hd"),
            digest(browser),
        ):
            return _error("access_denied", 403)
        service = service_name(pending["params"]["redirect_uri"])
        callback = urlsplit(pending["params"]["redirect_uri"])
        # Chrome applies form-action to the form's external OAuth redirect too.
        callback_source = _csp_callback_source(pending["params"]["redirect_uri"])
        consent_policy = content_security_policy.replace(
            "form-action 'self'", f"form-action 'self' {callback_source}"
        )
        callback_host = html.escape(callback.netloc)
        escaped_target = html.escape(target, quote=True)
        content = (
            f"<h1>動画ライブラリを {service} に接続</h1>"
            f"<p><small>認証の戻り先: {callback_host}</small></p>"
            "<p>本人の閲覧権限で動画を検索・参照します。広告費・動画ファイルは返しません。変更・削除はできません。</p>"
            "<p>接続は自動更新されます。90日間利用がない場合や接続を解除した場合は、再ログインが必要です。</p>"
        )
        if callback.hostname in {"localhost", "127.0.0.1"}:
            content += (
                "<p>この端末のアプリへ接続します。"
                "操作中のアプリから開始した接続か確認してください。</p>"
            )
        email = pending.get("email")
        if email:
            content += (
                f"<p>接続するアカウント: {html.escape(mask_email(email))}</p>"
                f'<form method="post" action="{escaped_target}">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<button type="submit">{service} に読み取り接続を許可</button>'
                '<button name="decision" value="deny" type="submit">許可しない</button></form>'
            )
        else:
            nonce = digest("google:" + browser + ":" + tx)
            verify_target = "/mcp-auth/verify?tx=" + tx
            client_id = html.escape(auth_config.google_client_id or "", quote=True)
            content += (
                "<p>接続する会社Googleアカウントを確認してください。</p>"
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<div id="g_id_onload" data-client_id="{client_id}" '
                f'data-callback="verifyMCPGoogle" data-nonce="{nonce}" '
                'data-auto_prompt="false"></div>'
                '<div class="g_id_signin" data-type="standard" data-theme="outline"></div>'
                '<p id="login-status" role="status" aria-live="polite"></p>'
                '<script src="https://accounts.google.com/gsi/client" async defer></script>'
                "<script>async function verifyMCPGoogle(result){"
                'const status=document.getElementById("login-status");'
                'status.textContent="確認しています…";'
                "try{const body=new URLSearchParams({credential:result.credential,"
                'csrf:document.querySelector("input[name=csrf]").value});'
                f"const response=await fetch({json.dumps(verify_target)},"
                '{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body,redirect:"follow"});'
                f"if(response.ok){{window.location.assign({json.dumps(target)});}}"
                'else{status.textContent="ログインを確認できませんでした。ページを開き直してください。";}'
                '}catch{status.textContent="接続できませんでした。もう一度お試しください。";}}</script>'
            )
        return _set_browser(
            HTMLResponse(
                _page("接続の確認", content),
                headers={**_BROWSER_HEADERS, "Content-Security-Policy": consent_policy},
            ),
            browser,
        )

    async def verify(request: Request) -> Response:
        request = await _bounded_request(request)
        form = await _form(request)
        loaded = await pending_request(request)
        browser = _browser(request)
        if (
            loaded is None
            or not browser
            or request.headers.get("origin") != base
            or not auth_config.auth_enabled
        ):
            return _error("access_denied", 403)
        tx, pending = loaded
        csrf = form.get("csrf", "")
        if pending.get("browser_hash") != digest(browser) or not secrets.compare_digest(
            pending.get("csrf_hash", ""), digest(csrf)
        ):
            return _error("access_denied", 403)
        credential = form.get("credential", "")
        if not credential or len(credential) > 12000 or not auth_config.google_client_id:
            return _error("access_denied", 403)
        if not await provider.rate_limit("google:global", 100) or not await provider.rate_limit(
            "google:" + digest(tx), 5
        ):
            return _error("temporarily_unavailable", 429)
        try:
            claims = await run_in_threadpool(
                verify_google_id_token,
                credential,
                auth_config.google_client_id,
                verifier=google_verifier,
            )
            allowed, email = check_allowed(claims, auth_config)
            expected = digest("google:" + browser + ":" + tx)
            if (
                not allowed
                or not email
                or not secrets.compare_digest(str(claims.get("nonce", "")), expected)
            ):
                return _error("access_denied", 403)
            raw_hd = claims.get("hd")
            hd = raw_hd.strip().lower() or None if isinstance(raw_hd, str) else None
        except Exception:
            logger.warning("library_mcp_google_denied", request_id=request_id(request), count=1)
            return _error("access_denied", 403)
        if not await provider.db(
            "bind_request", digest(tx), digest(csrf), email, hd, digest(browser)
        ):
            return _error("access_denied", 403)
        return RedirectResponse(CONSENT_PATH + "?tx=" + tx, status_code=303, headers=_HEADERS)

    async def connections(request: Request) -> Response:
        email = authenticate(request)
        if not email:
            if request.method == "POST":
                return _error("unauthorized", 401)
            target = safe_next("/mcp-auth/connections")
            return RedirectResponse(
                "/search/login?next=" + quote(target, safe=""), status_code=303, headers=_HEADERS
            )
        browser = _browser(request)
        if request.method == "POST":
            request = await _bounded_request(request)
            form = await _form(request)
            if not browser or request.headers.get("origin") != base:
                return _error("access_denied", 403)
            csrf = digest("revoke:" + browser + ":" + email)
            if not secrets.compare_digest(form.get("csrf", "").encode(), csrf.encode()):
                return _error("access_denied", 403)
            grant_id = form.get("grant_id", "")
            try:
                if str(uuid.UUID(grant_id)) != grant_id:
                    return _error("invalid_request")
            except ValueError:
                return _error("invalid_request")
            if not await provider.db("revoke_grant", grant_id, email):
                return _error("access_denied", 403)
            return RedirectResponse("/mcp-auth/connections", status_code=303, headers=_HEADERS)
        browser = browser or secrets.token_urlsafe(32)
        csrf = digest("revoke:" + browser + ":" + email)
        grants = await provider.db("list_grants", email)
        content = (
            "<h1>動画ライブラリの接続</h1>"
            f"<p>{html.escape(mask_email(email))} の接続を確認・解除できます。</p><ul>"
        )
        for grant in grants:
            name = service_name(str(grant.get("redirect_uri", "")))
            grant_id = html.escape(str(grant["grant_id"]), quote=True)
            content += (
                f"<li><strong>{name}</strong> — 動画の検索・閲覧"
                '<form method="post" action="/mcp-auth/connections">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="grant_id" value="{grant_id}">'
                '<button type="submit">この接続を解除</button></form></li>'
            )
        if not grants:
            content += "<li>接続はありません。</li>"
        content += '</ul><p><a href="/mcp/setup">接続方法を見る</a></p>'
        return _set_browser(
            HTMLResponse(_page("接続の管理", content), headers=_BROWSER_HEADERS), browser
        )

    async def browser_endpoint(
        handler: Callable[[Request], Awaitable[Response]], request: Request
    ) -> Response:
        try:
            return await handler(request)
        except (ValueError, UnicodeError):
            return _error("invalid_request")
        except StoreUnavailableError:
            return _error("temporarily_unavailable", 503)

    async def consent_endpoint(request: Request) -> Response:
        return await browser_endpoint(consent, request)

    async def verify_endpoint(request: Request) -> Response:
        return await browser_endpoint(verify, request)

    async def connections_endpoint(request: Request) -> Response:
        return await browser_endpoint(connections, request)

    async def setup(request: Request) -> Response:
        return HTMLResponse(setup_html(provider.resource), headers=_HEADERS)

    app.router.routes.extend(
        [
            Route(CONSENT_PATH, consent_endpoint, methods=["GET", "POST"]),
            Route("/mcp-auth/verify", verify_endpoint, methods=["POST"]),
            Route("/mcp-auth/connections", connections_endpoint, methods=["GET", "POST"]),
            Route("/mcp/setup", setup, methods=["GET"]),
            Route("/mcp", endpoint=_SlashlessMCP(child)),
        ]
    )
    app.mount("/mcp", child)
    app.add_middleware(_SpikeRequests, request_id=request_id)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(parent: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(parent), mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
