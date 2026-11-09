"""Persistent, individually revocable OAuth grants for the video library."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

import structlog
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.concurrency import run_in_threadpool

from teamagent.connect_web.library_mcp_store import PgMCPStore

SCOPE = "library:read"
CONSENT_PATH = "/mcp-auth/consent"
logger = structlog.get_logger(__name__)
current_request_id: ContextVar[str | None] = ContextVar("library_mcp_request_id", default=None)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class StoreUnavailableError(Exception):
    """Fixed text only: SDK exception logging must never expose DB error details."""


class PersistentProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self,
        base: str,
        get_pg: Callable[[], Any],
        subject_allowed: Callable[[str, str | None], bool],
    ) -> None:
        self.base = base
        self.resource = base + "/mcp"
        self.get_pg = get_pg
        self.subject_allowed = subject_allowed
        self.allowed_redirects = {"https://claude.ai/api/mcp/auth_callback"} | {
            uri.strip()
            for uri in os.environ.get("CONNECT_MCP_SPIKE_REDIRECT_ALLOW", "").split(",")
            if uri.strip()
        }

    async def db(self, method: str, *arguments: Any) -> Any:
        def call() -> Any:
            store = PgMCPStore(self.get_pg(), resource=self.resource)
            return getattr(store, method)(*arguments)

        try:
            return await run_in_threadpool(call)
        except Exception:
            logger.warning(
                "library_mcp_store_unavailable", request_id=current_request_id.get(), count=1
            )
            raise StoreUnavailableError(
                "Video library authentication is temporarily unavailable"
            ) from None

    async def rate_limit(self, key: str, limit: int, window: int = 60) -> bool:
        return bool(await self.db("rate_limit", digest(key), limit, window))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id or len(client_id) > 128:
            return None
        row = await self.db("get_client", client_id)
        return OAuthClientInformationFull.model_validate(row) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirects = [str(uri) for uri in client_info.redirect_uris or []]
        if (
            not redirects
            or len(redirects) > 4
            or any(uri not in self.allowed_redirects for uri in redirects)
        ):
            raise RegistrationError("invalid_redirect_uri", "Callback is not allowed")
        if (
            client_info.token_endpoint_auth_method != "none"
            or client_info.client_secret
            or set(client_info.grant_types) != {"authorization_code", "refresh_token"}
            or client_info.response_types != ["code"]
            or client_info.scope != SCOPE
            or not client_info.client_id
        ):
            raise RegistrationError("invalid_client_metadata", "Use public client authentication")
        if client_info.client_name and len(client_info.client_name) > 100:
            raise RegistrationError("invalid_client_metadata", "Client name is too long")
        await self.db("cleanup", 20)
        await self.db("register_client", client_info.model_dump(mode="json"))

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource not in {None, self.resource}:
            raise AuthorizeError("invalid_request", "Invalid resource")
        if (
            not client.client_id
            or params.scopes != [SCOPE]
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge)
            or (params.state is not None and len(params.state) > 1024)
        ):
            raise AuthorizeError("invalid_request", "Invalid authorization request")
        if not await self.rate_limit("authorize:" + client.client_id, 30):
            raise AuthorizeError("temporarily_unavailable", "Please try again later")
        await self.db("cleanup", 20)
        tx = secrets.token_urlsafe(32)
        stored = params.model_copy(update={"resource": self.resource})
        await self.db(
            "create_request", digest(tx), client.client_id, stored.model_dump(mode="json")
        )
        return self.base + CONSENT_PATH + "?tx=" + tx

    async def _allowed(self, row: dict[str, Any] | None, token: str) -> bool:
        if not row:
            return False
        email = row.get("subject")
        if (
            not isinstance(email, str)
            or not email
            or row.get("resource") != self.resource
            or row.get("scopes") != [SCOPE]
            or not self.subject_allowed(email, row.get("verified_hd"))
        ):
            if row.get("client_id"):
                await self.db("revoke_token", digest(token), row["client_id"])
            return False
        return True

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        if len(authorization_code) > 256 or not client.client_id:
            return None
        row = await self.db("get_code", digest(authorization_code), client.client_id)
        if not await self._allowed(row, authorization_code):
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=self.resource,
            subject=row["subject"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not client.client_id or authorization_code.client_id != client.client_id:
            raise TokenError("invalid_grant", "Code is unavailable")
        await self.db("cleanup", 20)
        access, refresh = "lm_at_" + secrets.token_urlsafe(32), "lm_rt_" + secrets.token_urlsafe(32)
        result = await self.db(
            "exchange_code",
            digest(authorization_code.code),
            client.client_id,
            digest(access),
            digest(refresh),
        )
        if result.get("status") != "ok":
            raise TokenError("invalid_grant", "Code is unavailable")
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            token_type="Bearer",
            expires_in=600,
            scope=SCOPE,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        if len(refresh_token) > 256 or not client.client_id:
            return None
        row = await self.db("get_refresh", digest(refresh_token), client.client_id)
        if not await self._allowed(row, refresh_token):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"]),
            subject=row["subject"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if not client.client_id or refresh_token.client_id != client.client_id:
            raise TokenError("invalid_grant", "Refresh token is unavailable")
        if scopes != [SCOPE]:
            raise TokenError("invalid_scope", "Invalid scope")
        await self.db("cleanup", 20)
        access, refresh = "lm_at_" + secrets.token_urlsafe(32), "lm_rt_" + secrets.token_urlsafe(32)
        result = await self.db(
            "rotate_refresh",
            digest(refresh_token.token),
            client.client_id,
            scopes,
            digest(access),
            digest(refresh),
        )
        # SQL has already committed replay-triggered grant revocation at this point.
        if result.get("status") != "ok":
            raise TokenError("invalid_grant", "Refresh token is unavailable; reconnect")
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            token_type="Bearer",
            expires_in=600,
            scope=SCOPE,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if len(token) > 256:
            return None
        row = await self.db("get_access", digest(token))
        if not await self._allowed(row, token):
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"]),
            resource=self.resource,
            subject=row["subject"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self.db("revoke_token", digest(token.token), token.client_id)


def service_name(redirect: str) -> str:
    host = urlsplit(redirect).hostname
    return (
        "ChatGPT"
        if host == "chatgpt.com"
        else "Claude"
        if host in {"claude.ai", "claude.com"}
        else "MCPクライアント"
    )
