"""Compatibility exports for process-generation-bound bot health."""

from teamagent.worker_health import (
    DEFAULT_BOT_HEARTBEAT,
    bot_heartbeat_healthy,
    run_bot_heartbeat,
)

__all__ = [
    "DEFAULT_BOT_HEARTBEAT",
    "bot_heartbeat_healthy",
    "run_bot_heartbeat",
]
