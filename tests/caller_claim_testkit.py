"""Test-only signer for the OpenClaw -> MCP Slack caller claim contract."""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
from collections.abc import Mapping
from typing import Any

from teamagent.mcp_gateway.caller_claim import (
    CALLER_CLAIM_AUDIENCE,
    CALLER_CLAIM_FIELD,
    CALLER_CLAIM_ISSUER,
    CallerClaimVerifier,
    canonical_request_sha256,
)
from teamagent.mcp_gateway.server import USER_CONTEXT_KEY

TEST_CALLER_CLAIM_SECRET = "test-caller-claim-secret-is-at-least-32-bytes"
TEST_SLACK_TEAM_ID = "T0123456789"
TEST_SLACK_USER_ID = "U0123456789"
TEST_SLACK_CHANNEL_ID = "C0123456789"
TEST_NOW = 1_784_424_000

_NONCE_COUNTER = itertools.count()


def make_verifier(
    *,
    now: int = TEST_NOW,
    secret: str = TEST_CALLER_CLAIM_SECRET,
    team_id: str = TEST_SLACK_TEAM_ID,
    audience: str = CALLER_CLAIM_AUDIENCE,
) -> CallerClaimVerifier:
    return CallerClaimVerifier(
        secret=secret,
        expected_team_id=team_id,
        audience=audience,
        clock=lambda: now,
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def sign_arguments(
    tool: str,
    business_arguments: Mapping[str, Any],
    *,
    user_id: str = TEST_SLACK_USER_ID,
    team_id: str = TEST_SLACK_TEAM_ID,
    channel_id: str = TEST_SLACK_CHANNEL_ID,
    thread_ts: str | None = None,
    now: int = TEST_NOW,
    expires_at: int | None = None,
    secret: str = TEST_CALLER_CLAIM_SECRET,
    audience: str = CALLER_CLAIM_AUDIENCE,
    declared_context: Mapping[str, Any] | None = None,
    payload_overrides: Mapping[str, Any] | None = None,
    nonce_seed: str | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "slack_user_id": user_id,
        "slack_team_id": team_id,
        "channel_id": channel_id,
    }
    if thread_ts is not None:
        context["thread_ts"] = thread_ts
    if declared_context is not None:
        context.update(declared_context)
    arguments = {
        **business_arguments,
        USER_CONTEXT_KEY: context,
    }
    if nonce_seed is None:
        nonce_seed = f"{tool}:{next(_NONCE_COUNTER)}"
    nonce = _base64url(hashlib.sha256(nonce_seed.encode()).digest()[:16])
    payload: dict[str, Any] = {
        "v": 1,
        "iss": CALLER_CLAIM_ISSUER,
        "aud": audience,
        "sub": user_id,
        "team": team_id,
        "channel": channel_id,
        "thread": thread_ts,
        "message": "1784424000.000001",
        "session_sha256": hashlib.sha256(b"trusted-slack-session").hexdigest(),
        "tool": tool,
        "arguments_sha256": canonical_request_sha256(arguments),
        "nonce": nonce,
        "iat": now,
        "exp": expires_at if expires_at is not None else now + 60,
    }
    if payload_overrides is not None:
        payload.update(payload_overrides)
    payload_segment = _base64url(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    signature = hmac.new(
        secret.encode(),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    context[CALLER_CLAIM_FIELD] = f"{payload_segment}.{_base64url(signature)}"
    return arguments
