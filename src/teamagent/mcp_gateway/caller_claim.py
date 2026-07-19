"""Verify one-use Slack caller claims minted by the OpenClaw ingress plugin.

The MCP bearer authenticates the OpenClaw workload, not the human caller.  A
separate HMAC secret therefore binds the trusted Slack event identity to one
specific MCP tool invocation.  The model-visible ``slack_user_id`` remains a
declaration only and is never an authorization source by itself.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import re
import struct
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

CALLER_CLAIM_FIELD = "caller_claim"
CALLER_CLAIM_AUDIENCE = "teamagent-mcp"
CALLER_CLAIM_ISSUER = "teamagent-openclaw"
CALLER_CLAIM_SECRET_ENV = "TEAMAGENT_CALLER_CLAIM_SECRET"
CALLER_CLAIM_REPLAY_TABLE_ENV = "TEAMAGENT_CALLER_CLAIM_REPLAY_TABLE"
SLACK_TEAM_ID_ENV = "SLACK_TEAM_ID"
USER_CONTEXT_KEY = "_user_context"

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DYNAMODB_TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SLACK_USER_RE = re.compile(r"^U[A-Z0-9]{8,}$")
_SLACK_TEAM_RE = re.compile(r"^T[A-Z0-9]{8,}$")
_SLACK_CHANNEL_RE = re.compile(r"^[CDG][A-Z0-9]{8,}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_OPAQUE_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CLAIM_FIELDS = frozenset(
    {
        "v",
        "iss",
        "aud",
        "sub",
        "team",
        "channel",
        "thread",
        "message",
        "session_sha256",
        "run_id",
        "tool_call_id",
        "tool",
        "arguments_sha256",
        "nonce",
        "iat",
        "exp",
    }
)


class CallerClaimError(ValueError):
    """The signed caller claim is absent, invalid, stale, or already consumed."""


class CallerClaimReplayStore(Protocol):
    """Atomically reject a nonce that was already consumed by any MCP task."""

    async def consume(self, nonce: str, *, expires_at: int, now: int) -> None:
        """Record one nonce or raise ``CallerClaimError`` without granting access."""


class InMemoryCallerClaimReplayStore:
    """Process-local replay store for tests and non-production construction."""

    def __init__(self, *, max_entries: int = 10_000) -> None:
        if max_entries < 1:
            raise CallerClaimError("caller claim replay capacity is invalid")
        self._max_entries = max_entries
        self._seen_nonces: dict[str, int] = {}
        self._lock = threading.Lock()

    async def consume(self, nonce: str, *, expires_at: int, now: int) -> None:
        """Atomically consume a nonce within this process."""

        with self._lock:
            expired = [
                seen_nonce
                for seen_nonce, seen_expiry in self._seen_nonces.items()
                if seen_expiry < now
            ]
            for seen_nonce in expired:
                del self._seen_nonces[seen_nonce]
            if nonce in self._seen_nonces:
                raise CallerClaimError("caller claim replay rejected")
            if len(self._seen_nonces) >= self._max_entries:
                raise CallerClaimError("caller claim replay cache is at capacity")
            self._seen_nonces[nonce] = expires_at


class DynamoDbCallerClaimReplayStore:
    """Cluster-wide one-use nonce store backed by conditional DynamoDB writes.

    ``attribute_not_exists(nonce)`` is the authorization linearization point:
    rolling ECS tasks cannot both accept the same claim.  Any DynamoDB error is
    fail-closed; TTL cleanup is only storage hygiene and is not relied on for
    security.
    """

    def __init__(
        self,
        *,
        table_name: str,
        region_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not _DYNAMODB_TABLE_RE.fullmatch(table_name):
            raise CallerClaimError("caller claim replay table name is invalid")
        self._table_name = table_name
        self._region_name = region_name
        self._client = client
        self._client_lock = threading.Lock()

    def _dynamodb(self) -> Any:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    import boto3
                    from botocore.config import Config

                    self._client = boto3.session.Session().client(
                        "dynamodb",
                        region_name=self._region_name,
                        config=Config(
                            connect_timeout=2,
                            read_timeout=2,
                            retries={"mode": "standard", "max_attempts": 2},
                        ),
                    )
        return self._client

    def _consume_sync(self, nonce: str, expires_at: int) -> None:
        try:
            self._dynamodb().put_item(
                TableName=self._table_name,
                Item={
                    "nonce": {"S": nonce},
                    "expires_at": {"N": str(expires_at)},
                },
                ConditionExpression="attribute_not_exists(#nonce)",
                ExpressionAttributeNames={"#nonce": "nonce"},
            )
        except Exception as error:
            response = getattr(error, "response", None)
            error_code = (
                response.get("Error", {}).get("Code") if isinstance(response, Mapping) else None
            )
            if error_code == "ConditionalCheckFailedException":
                raise CallerClaimError("caller claim replay rejected") from error
            raise CallerClaimError("caller claim replay store is unavailable") from error

    async def consume(self, nonce: str, *, expires_at: int, now: int) -> None:
        """Conditionally consume the nonce; ``now`` is intentionally not trusted by DDB."""

        del now
        await asyncio.to_thread(self._consume_sync, nonce, expires_at)


@dataclass(frozen=True)
class VerifiedCallerClaim:
    """Trusted Slack identity and request binding extracted from a valid claim."""

    slack_user_id: str
    slack_team_id: str
    channel_id: str
    thread_ts: str | None
    message_id: str
    session_sha256: str
    run_id: str
    tool_call_id: str
    nonce: str
    issued_at: int
    expires_at: int


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CallerClaimError(f"caller claim contains duplicate key: {key}")
        result[key] = value
    return result


def _base64url_decode(value: str) -> bytes:
    if not value or not _B64URL_RE.fullmatch(value):
        raise CallerClaimError("caller claim uses invalid base64url")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except Exception as error:
        raise CallerClaimError("caller claim uses invalid base64url") from error


def _canonical_value(value: Any) -> Any:
    """Return the cross-language canonical value used by the Node signer.

    Numbers are encoded as their IEEE-754 binary64 bits.  This avoids Python
    versus JavaScript decimal rendering differences while matching the values
    that crossed OpenClaw's JSON transport.
    """

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            raise CallerClaimError("tool arguments contain a non-finite number")
        return ["float64", struct.pack(">d", numeric).hex()]
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise CallerClaimError("tool arguments contain an invalid Unicode string") from error
        return ["string", value]
    if isinstance(value, list):
        return ["array", [_canonical_value(item) for item in value]]
    if isinstance(value, Mapping):
        items: list[list[Any]] = []
        for key in sorted(value, key=lambda item: str(item).encode("utf-8")):
            if not isinstance(key, str):
                raise CallerClaimError("tool argument object keys must be strings")
            items.append([key, _canonical_value(value[key])])
        return ["object", items]
    raise CallerClaimError(f"unsupported tool argument type: {type(value).__name__}")


def canonical_request_sha256(arguments: Mapping[str, Any]) -> str:
    """Hash all tool arguments except the signed claim itself."""

    sanitized = dict(arguments)
    raw_context = sanitized.get(USER_CONTEXT_KEY)
    if not isinstance(raw_context, Mapping):
        raise CallerClaimError("_user_context must be an object")
    context = dict(raw_context)
    context.pop(CALLER_CLAIM_FIELD, None)
    sanitized[USER_CONTEXT_KEY] = context
    canonical = json.dumps(
        _canonical_value(sanitized),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _required_string(
    payload: Mapping[str, Any],
    key: str,
    *,
    pattern: re.Pattern[str] | None = None,
    max_length: int = 512,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise CallerClaimError(f"caller claim field is invalid: {key}")
    if pattern is not None and not pattern.fullmatch(value):
        raise CallerClaimError(f"caller claim field is invalid: {key}")
    return value


class CallerClaimVerifier:
    """Verify signatures, event/request bindings, expiry, and one-use nonces."""

    def __init__(
        self,
        *,
        secret: str | bytes,
        expected_team_id: str,
        audience: str = CALLER_CLAIM_AUDIENCE,
        clock: Callable[[], float] = time.time,
        max_lifetime_seconds: int = 60,
        clock_skew_seconds: int = 5,
        max_replay_entries: int = 10_000,
        replay_store: CallerClaimReplayStore | None = None,
    ) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(secret_bytes) < 32 or b"${" in secret_bytes:
            raise CallerClaimError("caller claim secret must contain at least 32 bytes")
        if not _SLACK_TEAM_RE.fullmatch(expected_team_id):
            raise CallerClaimError("SLACK_TEAM_ID must be a canonical Slack T ID")
        if not audience or len(audience) > 128:
            raise CallerClaimError("caller claim audience is invalid")
        if max_lifetime_seconds < 1 or max_lifetime_seconds > 300:
            raise CallerClaimError("caller claim lifetime is invalid")
        self._secret = secret_bytes
        self._expected_team_id = expected_team_id
        self._audience = audience
        self._clock = clock
        self._max_lifetime_seconds = max_lifetime_seconds
        self._clock_skew_seconds = clock_skew_seconds
        self._replay_store = replay_store or InMemoryCallerClaimReplayStore(
            max_entries=max_replay_entries
        )

    @classmethod
    def from_env(cls) -> CallerClaimVerifier:
        """Build the production verifier; both values are mandatory."""

        secret = os.environ.get(CALLER_CLAIM_SECRET_ENV, "")
        bearer = os.environ.get("TEAMAGENT_MCP_BEARER", "")
        if bearer and hmac.compare_digest(secret.encode("utf-8"), bearer.encode("utf-8")):
            raise CallerClaimError("caller claim secret must differ from the MCP bearer")
        team_id = os.environ.get(SLACK_TEAM_ID_ENV, "")
        replay_table = os.environ.get(CALLER_CLAIM_REPLAY_TABLE_ENV, "")
        replay_store = DynamoDbCallerClaimReplayStore(
            table_name=replay_table,
            region_name=os.environ.get("AWS_REGION") or None,
        )
        return cls(
            secret=secret,
            expected_team_id=team_id,
            replay_store=replay_store,
        )

    async def verify(
        self,
        *,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> VerifiedCallerClaim:
        """Verify and atomically consume one claim for one exact tool request."""

        raw_context = arguments.get(USER_CONTEXT_KEY)
        if not isinstance(raw_context, Mapping):
            raise CallerClaimError("_user_context is required")
        token = raw_context.get(CALLER_CLAIM_FIELD)
        if not isinstance(token, str) or len(token) > 8192:
            raise CallerClaimError("signed caller claim is required")
        segments = token.split(".")
        if len(segments) != 2:
            raise CallerClaimError("caller claim compact form is invalid")
        payload_segment, signature_segment = segments
        signature = _base64url_decode(signature_segment)
        expected_signature = hmac.new(
            self._secret,
            payload_segment.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise CallerClaimError("caller claim signature is invalid")

        payload_bytes = _base64url_decode(payload_segment)
        if len(payload_bytes) > 4096:
            raise CallerClaimError("caller claim payload is too large")
        try:
            payload = json.loads(
                payload_bytes,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except CallerClaimError:
            raise
        except Exception as error:
            raise CallerClaimError("caller claim payload is invalid JSON") from error
        if not isinstance(payload, dict) or frozenset(payload) != _CLAIM_FIELDS:
            raise CallerClaimError("caller claim fields are not the exact contract")
        if payload.get("v") != 2:
            raise CallerClaimError("caller claim version is invalid")
        if payload.get("iss") != CALLER_CLAIM_ISSUER:
            raise CallerClaimError("caller claim issuer is invalid")
        if payload.get("aud") != self._audience:
            raise CallerClaimError("caller claim audience is invalid")

        slack_user_id = _required_string(payload, "sub", pattern=_SLACK_USER_RE)
        slack_team_id = _required_string(payload, "team", pattern=_SLACK_TEAM_RE)
        if slack_team_id != self._expected_team_id:
            raise CallerClaimError("caller claim Slack team does not match production")
        channel_id = _required_string(payload, "channel", pattern=_SLACK_CHANNEL_RE)
        message_id = _required_string(payload, "message")
        session_sha256 = _required_string(payload, "session_sha256", pattern=_SHA256_RE)
        run_id = _required_string(
            payload,
            "run_id",
            pattern=_OPAQUE_INVOCATION_ID_RE,
            max_length=256,
        )
        tool_call_id = _required_string(
            payload,
            "tool_call_id",
            pattern=_OPAQUE_INVOCATION_ID_RE,
            max_length=256,
        )
        claim_tool = _required_string(
            payload,
            "tool",
            pattern=re.compile(r"^[a-z][a-z0-9_]{0,127}$"),
            max_length=128,
        )
        arguments_sha256 = _required_string(
            payload,
            "arguments_sha256",
            pattern=_SHA256_RE,
        )
        nonce = _required_string(payload, "nonce", pattern=_NONCE_RE, max_length=22)
        thread = payload.get("thread")
        if thread is not None and (not isinstance(thread, str) or not thread or len(thread) > 128):
            raise CallerClaimError("caller claim thread is invalid")
        if claim_tool != tool:
            raise CallerClaimError("caller claim tool binding does not match")

        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if (
            isinstance(issued_at, bool)
            or isinstance(expires_at, bool)
            or not isinstance(issued_at, int)
            or not isinstance(expires_at, int)
        ):
            raise CallerClaimError("caller claim timestamps are invalid")
        now = int(self._clock())
        if issued_at > now + self._clock_skew_seconds:
            raise CallerClaimError("caller claim is not yet valid")
        if expires_at < now:
            raise CallerClaimError("caller claim is expired")
        lifetime = expires_at - issued_at
        if lifetime < 1 or lifetime > self._max_lifetime_seconds:
            raise CallerClaimError("caller claim lifetime is invalid")

        if raw_context.get("slack_user_id") != slack_user_id:
            raise CallerClaimError("declared Slack user does not match signed caller")
        if raw_context.get("slack_team_id") != slack_team_id:
            raise CallerClaimError("declared Slack team does not match signed caller")
        if raw_context.get("channel_id") != channel_id:
            raise CallerClaimError("declared Slack channel does not match signed caller")
        if raw_context.get("thread_ts") != thread:
            raise CallerClaimError("declared Slack thread does not match signed caller")
        actual_arguments_sha256 = canonical_request_sha256(arguments)
        if not hmac.compare_digest(actual_arguments_sha256, arguments_sha256):
            raise CallerClaimError("caller claim request binding does not match")

        await self._replay_store.consume(
            nonce,
            expires_at=expires_at,
            now=now,
        )

        return VerifiedCallerClaim(
            slack_user_id=slack_user_id,
            slack_team_id=slack_team_id,
            channel_id=channel_id,
            thread_ts=thread,
            message_id=message_id,
            session_sha256=session_sha256,
            run_id=run_id,
            tool_call_id=tool_call_id,
            nonce=nonce,
            issued_at=issued_at,
            expires_at=expires_at,
        )
