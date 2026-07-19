"""Process-generation-bound functional health for the EC2 Slack bot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_BOT_HEARTBEAT = Path("/run/teamagent/bot-heartbeat.json")


def _process_start_ticks(pid: int) -> int | None:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close = stat_line.rfind(")")
        fields = stat_line[close + 2 :].split()
        value = int(fields[19])
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    return value if value > 0 else None


def _boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if len(value) == 36 else None


def _generation(pid: int, start_ticks: int, boot_id: str) -> str:
    return hashlib.sha256(f"{boot_id}:{pid}:{start_ticks}".encode()).hexdigest()


def _boot_monotonic() -> float:
    clock = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
    return time.clock_gettime(clock)


def _write_heartbeat(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


async def run_bot_heartbeat(
    *,
    socket_client: Any,
    web_client: Any,
    path: Path = DEFAULT_BOT_HEARTBEAT,
    interval_seconds: float = 2.0,
    auth_interval_seconds: float = 30.0,
) -> None:
    """Write health only while this event loop owns a connected, authenticated Slack client."""

    if interval_seconds <= 0 or auth_interval_seconds <= 0:
        raise ValueError("heartbeat intervals must be positive")
    pid = os.getpid()
    start_ticks = _process_start_ticks(pid)
    boot_id = _boot_id()
    if start_ticks is None or boot_id is None:
        raise RuntimeError("process generation is unavailable")
    generation = _generation(pid, start_ticks, boot_id)
    last_auth = float("-inf")
    auth_ok = False
    while True:
        now = _boot_monotonic()
        connected = bool(await socket_client.is_connected())
        if connected and now - last_auth >= auth_interval_seconds:
            try:
                response = await web_client.auth_test()
                auth_ok = bool(response.get("ok")) if isinstance(response, dict) else bool(response)
            except Exception:
                auth_ok = False
            last_auth = now
        if connected and auth_ok:
            _write_heartbeat(
                path,
                {
                    "auth_ok": True,
                    "boot_id": boot_id,
                    "boot_monotonic_s": now,
                    "generation": generation,
                    "pid": pid,
                    "process_start_ticks": start_ticks,
                    "schema": 1,
                    "socket_connected": True,
                },
            )
        else:
            if not connected:
                # A reconnect is a new functional session and must earn a fresh auth_test.
                auth_ok = False
                last_auth = float("-inf")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        await asyncio.sleep(interval_seconds)


def bot_heartbeat_healthy(
    *,
    pid: int,
    process_start_ticks: int,
    path: Path = DEFAULT_BOT_HEARTBEAT,
    maximum_age_seconds: float = 10.0,
) -> bool:
    """Validate a fresh functional heartbeat from the exact MainPID generation."""

    if pid <= 1 or process_start_ticks <= 0 or maximum_age_seconds <= 0:
        return False
    boot_id = _boot_id()
    if boot_id is None:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if type(payload) is not dict or set(payload) != {
        "auth_ok",
        "boot_id",
        "boot_monotonic_s",
        "generation",
        "pid",
        "process_start_ticks",
        "schema",
        "socket_connected",
    }:
        return False
    emitted = payload.get("boot_monotonic_s")
    now = _boot_monotonic()
    return (
        payload.get("schema") == 1
        and payload.get("pid") == pid
        and payload.get("process_start_ticks") == process_start_ticks
        and payload.get("boot_id") == boot_id
        and payload.get("generation") == _generation(pid, process_start_ticks, boot_id)
        and payload.get("socket_connected") is True
        and payload.get("auth_ok") is True
        and type(emitted) is float
        and 0 <= now - emitted <= maximum_age_seconds
        and _process_start_ticks(pid) == process_start_ticks
    )


__all__ = [
    "DEFAULT_BOT_HEARTBEAT",
    "bot_heartbeat_healthy",
    "run_bot_heartbeat",
]
