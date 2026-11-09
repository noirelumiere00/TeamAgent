"""Durable MCP issuer storage; bearer credentials enter PostgreSQL only as hashes.

The synchronous methods must run in a worker thread from async OAuth handlers.
Every call uses the application role and a narrow SECURITY DEFINER function.
In particular, replay revocation commits before the caller raises an OAuth error.
"""

from __future__ import annotations

import re
from typing import Any

from psycopg import sql

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE = ["library:read"]


def _hash(value: str) -> bytes:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Invalid MCP credential hash")
    return bytes.fromhex(value)


def _scopes(value: Any) -> list[str]:
    if value is None:
        return _SCOPE.copy()
    values = value.split() if isinstance(value, str) else list(value)
    if values != _SCOPE:
        raise ValueError("Invalid MCP scope")
    return values


class PgMCPStore:
    """No credentials, DB connection, or state cache is retained by this object."""

    def __init__(self, pg: Any, *, resource: str) -> None:
        if not re.fullmatch(r"https://[^/?#\s]+/mcp", resource):
            raise ValueError("Invalid MCP resource")
        self._pg = pg
        self.resource = resource

    def _call(self, name: str, *arguments: Any) -> Any:
        # Function names originate only in the fixed methods below, never HTTP input.
        statement = sql.SQL("SELECT public.{}({}) AS value").format(
            sql.Identifier("library_mcp_" + name),
            sql.SQL(", ").join(sql.Placeholder() for _ in arguments),
        )
        with self._pg.connection(app_role="teamagent_app") as conn:
            row = conn.execute(statement, arguments).fetchone()
            value = None if row is None else row["value"]
        # A normal context exit commits SQL-side invalid/replay statuses too.
        return value

    def register_client(self, client: dict[str, Any]) -> None:
        if client.get("token_endpoint_auth_method") != "none" or client.get("client_secret"):
            raise ValueError("MCP requires a public client")
        if set(client.get("grant_types", [])) != {"authorization_code", "refresh_token"}:
            raise ValueError("Invalid MCP grant types")
        if client.get("response_types", ["code"]) != ["code"]:
            raise ValueError("Invalid MCP response type")
        self._call(
            "register_client",
            client["client_id"],
            self.resource,
            [str(uri) for uri in client["redirect_uris"]],
            _scopes(client.get("scope")),
            client.get("client_name"),
        )

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        return self._call("get_client", client_id, self.resource)  # type: ignore[no-any-return]

    def create_request(self, tx_hash: str, client_id: str, params: dict[str, Any]) -> None:
        resource = params.get("resource") or self.resource
        if resource != self.resource:
            raise ValueError("Invalid MCP resource")
        self._call(
            "create_request",
            _hash(tx_hash),
            client_id,
            self.resource,
            str(params["redirect_uri"]),
            bool(params.get("redirect_uri_provided_explicitly", True)),
            params.get("state"),
            params["code_challenge"],
            _scopes(params.get("scopes")),
        )

    def get_request(self, tx_hash: str) -> dict[str, Any] | None:
        return self._call("get_request", _hash(tx_hash), self.resource)  # type: ignore[no-any-return]

    def bind_request(
        self,
        tx_hash: str,
        csrf_hash: str,
        email: str | None,
        hd: str | None,
        browser_hash: str,
    ) -> bool:
        return bool(
            self._call(
                "bind_request",
                _hash(tx_hash),
                _hash(csrf_hash),
                email,
                hd,
                _hash(browser_hash),
                self.resource,
            )
        )

    def authorize_request(
        self, tx_hash: str, csrf_hash: str, email: str, code_hash: str, browser_hash: str
    ) -> dict[str, Any] | None:
        return self._call(  # type: ignore[no-any-return]
            "authorize_request",
            _hash(tx_hash),
            _hash(csrf_hash),
            email,
            _hash(code_hash),
            _hash(browser_hash),
            self.resource,
        )

    def cancel_request(self, tx_hash: str, csrf_hash: str, browser_hash: str) -> bool:
        return bool(
            self._call(
                "cancel_request",
                _hash(tx_hash),
                _hash(csrf_hash),
                _hash(browser_hash),
                self.resource,
            )
        )

    def get_code(self, code_hash: str, client_id: str) -> dict[str, Any] | None:
        return self._call(  # type: ignore[no-any-return]
            "get_code", _hash(code_hash), client_id, self.resource
        )

    def exchange_code(
        self, code_hash: str, client_id: str, access_hash: str, refresh_hash: str
    ) -> dict[str, Any]:
        return self._call(  # type: ignore[no-any-return]
            "exchange_code",
            _hash(code_hash),
            client_id,
            _hash(access_hash),
            _hash(refresh_hash),
            self.resource,
        )

    def get_refresh(
        self, token_hash: str, client_id: str, include_consumed: bool = True
    ) -> dict[str, Any] | None:
        return self._call(  # type: ignore[no-any-return]
            "get_refresh", _hash(token_hash), client_id, self.resource, include_consumed
        )

    def rotate_refresh(
        self,
        token_hash: str,
        client_id: str,
        requested_scopes: list[str],
        access_hash: str,
        new_refresh_hash: str,
    ) -> dict[str, Any]:
        return self._call(  # type: ignore[no-any-return]
            "rotate_refresh",
            _hash(token_hash),
            client_id,
            _scopes(requested_scopes),
            _hash(access_hash),
            _hash(new_refresh_hash),
            self.resource,
        )

    def get_access(self, token_hash: str) -> dict[str, Any] | None:
        return self._call(  # type: ignore[no-any-return]
            "get_access", _hash(token_hash), self.resource
        )

    def revoke_token(self, token_hash: str, client_id: str) -> None:
        self._call("revoke_token", _hash(token_hash), client_id, self.resource)

    def list_grants(self, email: str) -> list[dict[str, Any]]:
        return self._call("list_grants", email, self.resource)  # type: ignore[no-any-return]

    def revoke_grant(self, grant_id: str, email: str) -> bool:
        return bool(self._call("revoke_grant", grant_id, email, self.resource))

    def rate_limit(self, key_hash: str, limit: int, window_seconds: int = 60) -> bool:
        if not 1 <= limit <= 1000 or not 1 <= window_seconds <= 86400:
            raise ValueError("Invalid MCP rate limit")
        return bool(self._call("rate_limit", _hash(key_hash), limit, window_seconds))

    def cleanup(self, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid MCP cleanup limit")
        return int(self._call("cleanup", limit))
