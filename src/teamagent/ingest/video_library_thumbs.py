"""Fill missing TikTok covers: python -m teamagent.ingest.video_library_thumbs [--limit N]."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import structlog

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.ingest.video_library import video_identity
from teamagent.observability.logging_config import configure_logging

logger = structlog.get_logger(__name__)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 短い UA（"Mozilla/5.0" だけ）だと TikTok は JSON の無い空ページを返す（2026-10-30 実測）
PAGE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class _HydrationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.active = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.active = dict(attrs).get("id") == "__UNIVERSAL_DATA_FOR_REHYDRATION__"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.parts.append(data)


def cover_url(page: str) -> str | None:
    parser = _HydrationParser()
    parser.feed(page)
    try:
        video = json.loads("".join(parser.parts))["__DEFAULT_SCOPE__"]["webapp.video-detail"][
            "itemInfo"
        ]["itemStruct"]["video"]
        zoom = video.get("zoomCover")
        url = (zoom.get("480") if isinstance(zoom, dict) else None) or video.get("cover")
        if isinstance(url, str):
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            # TikTok image CDN only: embedded third-party data must not become an SSRF target.
            if (
                parsed.scheme == "https"
                and not parsed.username
                and not parsed.password
                and parsed.port in {None, 443}
                and any(
                    host.endswith("." + domain)
                    for domain in (
                        "tiktokcdn.com",
                        "tiktokcdn-us.com",
                        "tiktokcdn-eu.com",
                        "byteoversea.com",
                        "ibytedtos.com",
                    )
                )
            ):
                return url
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


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
    consecutive_failures = 0
    for index, row in enumerate(rows[:limit]):
        if index:
            sleep(interval)
        counts["attempted"] += 1
        try:
            identity = video_identity(row["url"])
            if identity is None or identity[:2] != (row["platform"], row["video_id"]):
                raise ValueError("invalid_video_url")
            # Canonical origin avoids redirects/ports/tracking parameters in imported URLs.
            page = client.get(
                f"https://www.tiktok.com/@{quote(identity[2] or '', safe='')}/video/{identity[1]}"
            )
            page.raise_for_status()
            url = cover_url(page.text)
            if url is None:
                counts["cover_missing"] += 1
                consecutive_failures += 1
            else:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_IMAGE_BYTES:
                            raise ValueError("cover_too_large")
                # The source cover is JPEG. Reject HTML/challenges and other active content.
                if not body.startswith(b"\xff\xd8\xff"):
                    raise ValueError("invalid_cover_image")
                key = storage.put(row["platform"], row["video_id"], bytes(body))
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
        except httpx.HTTPStatusError as exc:
            counts[f"http_{exc.response.status_code}"] += 1
            if exc.response.status_code == 429:
                break
            consecutive_failures += 1
        except Exception as exc:
            counts[type(exc).__name__] += 1
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
            with httpx.Client(
                timeout=20, follow_redirects=False, headers={"User-Agent": PAGE_USER_AGENT}
            ) as client:
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
