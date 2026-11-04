"""Fill missing TikTok covers: python -m teamagent.ingest.video_library_thumbs [--limit N]."""

from __future__ import annotations

import argparse
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.adapters.video_library_page import (
    cover_url as cover_url,
)
from teamagent.adapters.video_library_page import (
    fetch_page,
    page_client,
    page_is_challenge,
    save_cover,
    stop_reason,
)
from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.ingest.video_library import video_identity
from teamagent.ingest.video_library_url_repository import URLImportRepository
from teamagent.observability.logging_config import configure_logging

logger = structlog.get_logger(__name__)


def fill_thumbnails(
    pg: Any,
    storage: VideoLibraryStorage,
    client: httpx.Client,
    *,
    limit: int,
    interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Counter[str]:
    if limit < 1 or interval < 0:
        raise ValueError("invalid_thumbnail_limits")
    started = time.perf_counter()
    counts: Counter[str] = Counter()
    with pg.connection(app_role="teamagent_app", user_role="admin", user_email=None) as conn:
        # Missing/deleted posts must not permanently starve later videos of their retry turn.
        rows = conn.execute(
            """SELECT platform, video_id, url FROM vl_videos
               WHERE thumbnail_s3_key IS NULL AND platform='tiktok'
               ORDER BY random() LIMIT %s""",
            (limit,),
        ).fetchall()
    repository = URLImportRepository(pg)
    consecutive_failures = 0
    with repository.fetch_lock() as locked:
        if not locked:
            counts["worker_busy"] += 1
            rows = []
        for index, row in enumerate(rows[:limit]):
            if repository.fetch_control()["paused"]:
                counts["fetch_paused"] += 1
                break
            if index:
                sleep(interval)
            counts["attempted"] += 1
            try:
                identity = video_identity(row["url"])
                if identity is None or identity[:2] != (row["platform"], row["video_id"]):
                    raise ValueError("invalid_video_url")
                # Canonical origin avoids redirects/ports/tracking parameters in imported URLs.
                page = fetch_page(
                    client,
                    f"https://www.tiktok.com/@{quote(identity[2] or '', safe='')}"
                    f"/video/{identity[1]}",
                )
                url = cover_url(page)
                if url is None:
                    if page_is_challenge(page):
                        raise ValueError("challenge")
                    counts["cover_missing"] += 1
                    consecutive_failures += 1
                    repository.record_fetch_outcome(success=False)
                else:
                    key = save_cover(client, storage, row["platform"], row["video_id"], url)
                    with pg.connection(
                        app_role="teamagent_app", user_role="admin", user_email=None
                    ) as conn:
                        conn.execute(
                            """UPDATE vl_videos SET thumbnail_s3_key=%s
                               WHERE platform=%s AND video_id=%s AND thumbnail_s3_key IS NULL""",
                            (key, row["platform"], row["video_id"]),
                        )
                    counts["saved"] += 1
                    consecutive_failures = 0
                    repository.record_fetch_outcome(success=True)
            except httpx.HTTPStatusError as exc:
                counts[f"http_{exc.response.status_code}"] += 1
                stop = stop_reason(exc)
                repository.record_fetch_outcome(success=False, stop=stop)
                if stop:
                    break
                consecutive_failures += 1
            except Exception as exc:
                stop = stop_reason(exc)
                counts[stop or type(exc).__name__] += 1
                repository.record_fetch_outcome(success=False, stop=stop)
                if stop:
                    break
                consecutive_failures += 1
            if consecutive_failures >= 5:
                counts["consecutive_failure_stop"] += 1
                break
    logger.info(
        "video_library_thumbnails",
        counts=dict(counts),
        token_usage=0,
        cost_usd=0,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=int(os.environ.get("VIDEO_LIBRARY_THUMB_LIMIT", "300"))
    )
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    configure_logging()
    pg = None
    with structlog.contextvars.bound_contextvars(
        request_id=f"vl-thumbs-{uuid.uuid4().hex}", skill="video_library_thumbs"
    ):
        try:
            pg = PgVectorClient.from_env()
            with page_client() as client:
                fill_thumbnails(pg, VideoLibraryStorage(), client, limit=args.limit)
            return 0
        except Exception as exc:
            logger.error("video_library_thumbnails_failed", error=type(exc).__name__)
            return 1
        finally:
            if pg:
                pg.close()


if __name__ == "__main__":
    raise SystemExit(main())
