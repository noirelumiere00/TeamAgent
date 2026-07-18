"""proposal_campaign の DI 境界（型エイリアス＋デフォルト実装）。

テストは Searcher / Fetcher / Normalizer を差し替えてネット非依存にする。
skills → adapters(top-level) / 他 skill は一方向 import のみ（runtime は import しない）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from teamagent.adapters.tiktok_scraper import TikTokVideo, search_tiktok
from teamagent.skills.video_algorithm.thumbnails import fetch_cover

# (query, max_videos, request_id) -> 上位動画メタの list
Searcher = Callable[[str, int, str], list[TikTokVideo]]
# (cover_url, request_id) -> 画像バイト | None
Fetcher = Callable[[str | None, str], bytes | None]
# bytes -> bytes（JPEG 正規化。ffmpeg 不在なら素通し）
Normalizer = Callable[[bytes], bytes]


def default_searcher(query: str, max_videos: int, request_id: str) -> list[TikTokVideo]:
    """search_tiktok の薄 wrapper。失敗時は TikTokScrapeError を投げる（呼び出し側で graceful）。"""
    result = search_tiktok(query, max_videos=max_videos, request_id=request_id)
    return list(result.videos)


def default_fetcher(cover_url: str | None, request_id: str) -> bytes | None:
    """Fetch a cover through media; configured service failures stay explicit."""
    from teamagent.adapters.media_job import MediaJobClient

    if not cover_url:
        return None
    if MediaJobClient.is_configured():
        body, _metadata = MediaJobClient().make_thumbnail_from_url(
            cover_url,
            request_fingerprint=f"{request_id}:proposal-cover",
            width=480,
        )
        return body
    if MediaJobClient.local_runtime_enabled():
        return fetch_cover(cover_url, request_id=request_id)
    MediaJobClient.require_configured()
    return None


def default_normalizer(data: bytes) -> bytes:
    """JPEGは保持し、その他の画像変換はmedia workerへ委譲する。"""
    if not data or data.startswith(b"\xff\xd8\xff"):
        return data
    from teamagent.adapters.media_job import MediaJobClient

    MediaJobClient.require_configured()
    content_type = "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/webp"
    body, _metadata = MediaJobClient().make_thumbnail(
        data,
        content_type,
        request_fingerprint=f"proposal-normalize:{hashlib.sha256(data).hexdigest()}",
        width=480,
    )
    return body
