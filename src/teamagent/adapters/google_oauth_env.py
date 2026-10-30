"""Expand the shared ECS Google OAuth secret without logging credential values."""

from __future__ import annotations

import json
import os

import structlog

logger = structlog.get_logger(__name__)


def expand_google_oauth_json() -> None:
    """Expand GOOGLE_OAUTH_JSON; existing non-empty individual env values take precedence."""
    raw = os.environ.get("GOOGLE_OAUTH_JSON", "").strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("google_oauth_json_invalid", reason="malformed_json")
        return
    if not isinstance(payload, dict):
        logger.warning("google_oauth_json_invalid", reason="object_required")
        return
    for key, env_name in (
        ("client_id", "GOOGLE_CLIENT_ID"),
        ("client_secret", "GOOGLE_CLIENT_SECRET"),
        ("refresh_token", "GOOGLE_OAUTH_REFRESH_TOKEN"),
    ):
        value = payload.get(key)
        if isinstance(value, str) and value and not os.environ.get(env_name):
            os.environ[env_name] = value
