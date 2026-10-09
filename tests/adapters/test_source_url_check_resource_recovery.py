"""未知の一時的資源不足はキャッシュせず、DNS枠を返す。"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from teamagent.adapters import source_url_check as module
from teamagent.adapters.source_url_check import SourceUrlChecker
from tests.adapters.test_source_url_check_regressions import PUBLIC_IP, URL


@pytest.mark.parametrize(
    "error", [OSError(24, "too many files"), RuntimeError("can't start new thread")]
)
def test_unknown_resource_error_is_not_cached(error: Exception) -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checker = SourceUrlChecker(client=client, resolver=lambda _host: [PUBLIC_IP])
        with pytest.raises(type(error)):
            checker.verify_public_url(URL)
        assert checker.verify_public_url(URL).ok and calls == 2


def test_thread_start_failure_releases_dns_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import BoundedSemaphore

    slots = BoundedSemaphore(1)
    monkeypatch.setattr(module, "_DNS_SLOTS", slots)

    def fail(_self: Any) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(module.Thread, "start", fail)
    checker = SourceUrlChecker(resolver=lambda _host: [PUBLIC_IP])
    try:
        with pytest.raises(RuntimeError, match="start new thread"):
            checker._resolve_bounded("example.com", time.monotonic() + 1)
        assert slots.acquire(blocking=False)
        slots.release()
    finally:
        checker.close()
