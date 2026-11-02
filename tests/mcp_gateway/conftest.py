"""背景の監視を各テストの中に閉じ、次のテストの宛先へ投稿させない。"""

from collections.abc import Iterator

import pytest

from teamagent.mcp_gateway import async_job_notify


@pytest.fixture(autouse=True)
def isolate_long_job_watchers() -> Iterator[None]:
    def stop() -> None:
        with async_job_notify._lock:
            for _, _, origin, _ in async_job_notify._active.values():
                origin.cancelled = True
                origin.discard()
            async_job_notify._active.clear()

    stop()
    yield
    stop()
