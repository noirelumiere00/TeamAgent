"""Explicit issuer-store fake for HTTP/SDK tests; PostgreSQL contracts have separate tests."""

from __future__ import annotations

import copy
import hashlib
import threading
import time
import uuid
from typing import Any


class FakePgMCPStore:
    def __init__(self, pg: Any = None, *, resource: str) -> None:
        self.resource = resource
        self.clients: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.grants: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, dict[str, Any]] = {}
        self.available = True
        self.admit = True
        self.calls = 0
        self._lock = threading.RLock()

    def _check(self) -> None:
        self.calls += 1
        if not self.available:
            raise RuntimeError("database password=private tenant=customer")

    def _grant(self, grant_id: str) -> dict[str, Any] | None:
        grant = self.grants.get(grant_id)
        if grant is None or grant["revoked"] or grant["expires_at"] <= time.time():
            return None
        client = self.clients.get(grant["client_id"])
        return grant if client and not client.get("disabled") else None

    def _credential(self, value: str, kind: str, client_id: str | None = None) -> Any:
        row = self.credentials.get(value)
        if row is None or row["kind"] != kind or row["expires_at"] <= time.time():
            return None
        if client_id is not None and row["client_id"] != client_id:
            return None
        return row if self._grant(row["grant_id"]) else None

    def register_client(self, client: dict[str, Any]) -> None:
        self._check()
        self.clients[client["client_id"]] = copy.deepcopy(client)

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        self._check()
        client = self.clients.get(client_id)
        return copy.deepcopy(client) if client and not client.get("disabled") else None

    def create_request(self, tx_hash: str, client_id: str, params: dict[str, Any]) -> None:
        self._check()
        self.requests[tx_hash] = {
            "client_id": client_id,
            "params": copy.deepcopy(params),
            "email": None,
            "verified_hd": None,
            "expires_at": time.time() + 600,
            "csrf_hash": None,
            "browser_hash": None,
            "consumed": False,
        }

    def get_request(self, tx_hash: str) -> dict[str, Any] | None:
        self._check()
        row = self.requests.get(tx_hash)
        if row is None or row["consumed"] or row["expires_at"] <= time.time():
            return None
        return copy.deepcopy(row)

    def bind_request(
        self,
        tx_hash: str,
        csrf_hash: str,
        email: str | None,
        hd: str | None,
        browser_hash: str,
    ) -> bool:
        self._check()
        with self._lock:
            row = self.requests.get(tx_hash)
            if row is None or row["consumed"] or row["expires_at"] <= time.time():
                return False
            if row["browser_hash"] not in (None, browser_hash):
                return False
            if row["email"] is not None and email not in (None, row["email"]):
                return False
            if row["email"] is not None and email is not None and hd != row["verified_hd"]:
                return False
            row.update(browser_hash=browser_hash, csrf_hash=csrf_hash)
            if email is not None:
                row.update(email=email, verified_hd=hd)
            return True

    def authorize_request(
        self, tx_hash: str, csrf_hash: str, email: str, code_hash: str, browser_hash: str
    ) -> dict[str, Any] | None:
        self._check()
        with self._lock:
            row = self.requests.get(tx_hash)
            if (
                row is None
                or row["consumed"]
                or row["expires_at"] <= time.time()
                or row["browser_hash"] != browser_hash
                or row["csrf_hash"] != csrf_hash
                or not email
                or row["email"] != email
            ):
                return None
            row["consumed"] = True
            now = time.time()
            params = row["params"]
            grant = {
                "grant_id": str(uuid.uuid4()),
                "client_id": row["client_id"],
                "subject": email,
                "verified_hd": row["verified_hd"],
                "resource": self.resource,
                "redirect_uri": params["redirect_uri"],
                "scopes": ["library:read"],
                "created_at": now,
                "last_used_at": now,
                "expires_at": now + 90 * 86400,
                "revoked": False,
            }
            self.grants[grant["grant_id"]] = grant
            self.credentials[code_hash] = {
                **grant,
                "kind": "code",
                "expires_at": now + 60,
                "consumed": False,
                "redirect_uri": params["redirect_uri"],
                "redirect_uri_provided_explicitly": params["redirect_uri_provided_explicitly"],
                "code_challenge": params["code_challenge"],
            }
            return copy.deepcopy(
                {**grant, "redirect_uri": params["redirect_uri"], "state": params.get("state")}
            )

    def cancel_request(self, tx_hash: str, csrf_hash: str, browser_hash: str) -> bool:
        self._check()
        with self._lock:
            row = self.requests.get(tx_hash)
            if (
                row is None
                or row["consumed"]
                or row["expires_at"] <= time.time()
                or row["csrf_hash"] != csrf_hash
                or row["browser_hash"] != browser_hash
            ):
                return False
            row["consumed"] = True
            return True

    def get_code(self, code_hash: str, client_id: str) -> dict[str, Any] | None:
        self._check()
        row = self._credential(code_hash, "code", client_id)
        return copy.deepcopy(row) if row and not row["consumed"] else None

    def _issue(self, old: dict[str, Any], access: str, refresh: str) -> dict[str, Any]:
        old["consumed"] = True
        grant = self.grants[old["grant_id"]]
        now = time.time()
        grant.update(expires_at=now + 90 * 86400, last_used_at=now)
        self.credentials[access] = {
            **grant,
            "kind": "access",
            "consumed": False,
            "expires_at": now + 600,
        }
        self.credentials[refresh] = {**grant, "kind": "refresh", "consumed": False}
        return copy.deepcopy(
            {
                "status": "ok",
                **grant,
                "expires_at": now + 600,
                "refresh_expires_at": grant["expires_at"],
            }
        )

    def exchange_code(
        self, code_hash: str, client_id: str, access: str, refresh: str
    ) -> dict[str, Any]:
        self._check()
        with self._lock:
            row = self._credential(code_hash, "code", client_id)
            if not row or row["consumed"]:
                return {"status": "invalid_grant"}
            return self._issue(row, access, refresh)

    def get_refresh(
        self, token_hash: str, client_id: str, include_consumed: bool = True
    ) -> dict[str, Any] | None:
        self._check()
        row = self._credential(token_hash, "refresh", client_id)
        return copy.deepcopy(row) if row and (include_consumed or not row["consumed"]) else None

    def rotate_refresh(
        self, token_hash: str, client_id: str, scopes: list[str], access: str, refresh: str
    ) -> dict[str, Any]:
        self._check()
        with self._lock:
            row = self._credential(token_hash, "refresh", client_id)
            if row is None:
                return {"status": "invalid_grant"}
            if row["consumed"]:
                self.grants[row["grant_id"]]["revoked"] = True
                return {"status": "replay"}
            if scopes != ["library:read"]:
                return {"status": "invalid_grant"}
            return self._issue(row, access, refresh)

    def get_access(self, token_hash: str) -> dict[str, Any] | None:
        self._check()
        row = self._credential(token_hash, "access")
        return copy.deepcopy(row) if row else None

    def revoke_token(self, token_hash: str, client_id: str) -> None:
        self._check()
        with self._lock:
            row = self.credentials.get(token_hash)
            if row and row["client_id"] == client_id:
                self.grants[row["grant_id"]]["revoked"] = True

    def list_grants(self, email: str) -> list[dict[str, Any]]:
        self._check()
        return [
            copy.deepcopy({**g, "client_name": self.clients[g["client_id"]].get("client_name")})
            for g in self.grants.values()
            if g["subject"] == email and self._grant(g["grant_id"])
        ]

    def revoke_grant(self, grant_id: str, email: str) -> bool:
        self._check()
        grant = self.grants.get(grant_id)
        if grant is None or grant["subject"] != email:
            return False
        grant["revoked"] = True
        return True

    def rate_limit(self, key_hash: str, limit: int, window_seconds: int = 60) -> bool:
        self._check()
        return self.admit

    def cleanup(self, limit: int = 100) -> int:
        self._check()
        return 0

    def expire_access(self, token: str) -> None:
        self.credentials[hashlib.sha256(token.encode()).hexdigest()]["expires_at"] = time.time() - 1


def patch_store(monkeypatch: Any, *, resource: str) -> FakePgMCPStore:
    from teamagent.connect_web import library_mcp_oauth, library_mcp_spike

    store = FakePgMCPStore(resource=resource)

    def factory(*args: Any, **kwargs: Any) -> FakePgMCPStore:
        return store

    monkeypatch.setattr(library_mcp_oauth, "PgMCPStore", factory)
    if hasattr(library_mcp_spike, "PgMCPStore"):
        monkeypatch.setattr(library_mcp_spike, "PgMCPStore", factory)
    return store
