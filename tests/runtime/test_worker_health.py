from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import teamagent.worker_health as health


class _Socket:
    connected = True

    async def is_connected(self) -> bool:
        return self.connected


class _Web:
    async def auth_test(self) -> dict[str, bool]:
        return {"ok": True}


@pytest.mark.asyncio
async def test_heartbeat_requires_event_loop_socket_auth_and_same_process_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bot-heartbeat.json"
    socket = _Socket()
    monkeypatch.setattr(health, "_process_start_ticks", lambda pid: 777 if pid > 1 else None)
    monkeypatch.setattr(health, "_boot_id", lambda: "12345678-1234-1234-1234-123456789abc")
    monkeypatch.setattr(health, "_boot_monotonic", lambda: 10.0)

    task = asyncio.create_task(
        health.run_bot_heartbeat(
            socket_client=socket,
            web_client=_Web(),
            path=path,
            interval_seconds=0.001,
            auth_interval_seconds=30,
        )
    )
    for _ in range(20):
        if path.exists():
            break
        await asyncio.sleep(0.001)
    assert path.exists()
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert health.bot_heartbeat_healthy(
        pid=int(payload["pid"]),
        process_start_ticks=777,
        path=path,
    )
    assert not health.bot_heartbeat_healthy(
        pid=int(payload["pid"]),
        process_start_ticks=778,
        path=path,
    )

    socket.connected = False
    await asyncio.sleep(0.003)
    assert not path.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_stale_heartbeat_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "bot-heartbeat.json"
    monkeypatch.setattr(health, "_process_start_ticks", lambda _pid: 10)
    monkeypatch.setattr(health, "_boot_id", lambda: "12345678-1234-1234-1234-123456789abc")
    monkeypatch.setattr(health, "_boot_monotonic", lambda: 100.0)
    path.write_text(
        json.dumps(
            {
                "auth_ok": True,
                "boot_id": "12345678-1234-1234-1234-123456789abc",
                "boot_monotonic_s": 1.0,
                "generation": health._generation(
                    321,
                    10,
                    "12345678-1234-1234-1234-123456789abc",
                ),
                "pid": 321,
                "process_start_ticks": 10,
                "schema": 1,
                "socket_connected": True,
            }
        ),
        encoding="utf-8",
    )
    assert not health.bot_heartbeat_healthy(pid=321, process_start_ticks=10, path=path)
