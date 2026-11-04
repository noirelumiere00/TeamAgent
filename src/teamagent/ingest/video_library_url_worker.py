"""Drain URL imports: python -m teamagent.ingest.video_library_url_worker.

Connect-web runs the same drain loop in one opt-in background thread.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.adapters.video_library_page import page_client
from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.ingest.video_library_url_import import process_url
from teamagent.ingest.video_library_url_repository import (
    URLImportRepository,
    rate_limit_cooldown_seconds,
)
from teamagent.observability.logging_config import configure_logging

logger = structlog.get_logger(__name__)


def settings() -> tuple[int, int]:
    rate_limit_cooldown_seconds()
    limit = int(os.environ.get("VIDEO_LIBRARY_IMPORT_DAILY_LIMIT", "300"))
    stale = int(os.environ.get("VIDEO_LIBRARY_IMPORT_STALE_SECONDS", "600"))
    if limit < 0 or stale < 1:
        raise ValueError("invalid_url_worker_limits")
    return limit, stale


def drain_queue(
    repository: URLImportRepository,
    client: httpx.Client,
    storage: VideoLibraryStorage,
    *,
    daily_limit: int = 300,
    stale_seconds: int = 600,
    stop: threading.Event | None = None,
) -> int:
    if daily_limit < 0 or stale_seconds < 1:
        raise ValueError("invalid_url_worker_limits")
    processed = 0
    # The session lock prevents replica/CLI HTTP overlap; SKIP LOCKED also fences row claims.
    with repository.fetch_lock() as locked:
        if not locked:
            return 0
        while stop is None or not stop.is_set():
            row = repository.claim(stale_seconds=stale_seconds)
            if row is None:
                break
            with structlog.contextvars.bound_contextvars(
                request_id=str(row["batch_id"]), skill="video_library_url_import"
            ):
                started = time.perf_counter()
                result = process_url(
                    row["canonical_url"] or row["input_url"],
                    repository,
                    client,
                    storage,
                    source=row["source"],
                    requested_by=row["requested_by"],
                    daily_limit=daily_limit,
                    row=row,
                )
                logger.info(
                    "video_library_url_result",
                    line_no=row["line_no"],
                    status=result.status,
                    error_code=result.error_code,
                    token_usage=0,
                    cost_usd=0,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            processed += 1
            if result.status == "queued":
                break
    return processed


class URLImportWorker:
    """One thread per connect-web process; DB queue survives process and browser closure."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.thread = threading.Thread(target=self._run, name="vl-url-import", daemon=True)

    def wake(self) -> None:
        self.wakeup.set()

    def _run(self) -> None:
        pg = None
        try:
            limit, stale = settings()
            pg = PgVectorClient.from_env()
            repository = URLImportRepository(pg)
            storage = VideoLibraryStorage()
            with page_client() as client:
                while not self.stop.is_set():
                    try:
                        drain_queue(
                            repository,
                            client,
                            storage,
                            daily_limit=limit,
                            stale_seconds=stale,
                            stop=self.stop,
                        )
                    except Exception as exc:
                        logger.error("video_library_url_worker_failed", error=type(exc).__name__)
                    self.wakeup.wait(1)
                    self.wakeup.clear()
        except Exception as exc:
            logger.error("video_library_url_worker_start_failed", error=type(exc).__name__)
        finally:
            if pg:
                pg.close()


@asynccontextmanager
async def worker_lifespan(app: Any) -> AsyncIterator[None]:
    worker = None
    # On by default since 11/04 (URL import is live); "0"/"false" turns the worker off.
    if os.environ.get("VIDEO_LIBRARY_IMPORT_WORKER_ENABLED", "1").lower() not in {"0", "false"}:
        settings()  # Invalid enabled settings fail closed at startup.
        worker = URLImportWorker()
        app.state.video_library_import_worker = worker
        worker.thread.start()
    try:
        yield
    finally:
        if worker:
            worker.stop.set()
            worker.wake()
            # Join off the event loop; in-flight HTTP remains bounded by its 20s timeout.
            import asyncio

            await asyncio.to_thread(worker.thread.join, 25)


def main() -> int:
    configure_logging()
    pg = None
    try:
        limit, stale = settings()
        pg = PgVectorClient.from_env()
        with page_client() as client:
            drain_queue(
                URLImportRepository(pg),
                client,
                VideoLibraryStorage(),
                daily_limit=limit,
                stale_seconds=stale,
            )
        return 0
    except Exception as exc:
        logger.error("video_library_url_worker_failed", error=type(exc).__name__)
        return 1
    finally:
        if pg:
            pg.close()


if __name__ == "__main__":
    raise SystemExit(main())
