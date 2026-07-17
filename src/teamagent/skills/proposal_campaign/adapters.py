"""proposal_campaign の DI 境界（型エイリアス＋デフォルト実装）。

テストは Searcher / Fetcher / Normalizer を差し替えてネット非依存にする。
skills → adapters(top-level) / 他 skill は一方向 import のみ（runtime は import しない）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
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
    """fetch_cover の薄 wrapper（生 JPEG バイト | None・graceful）。"""
    from teamagent.adapters.media_job import MediaJobClient

    if cover_url and MediaJobClient.is_configured():
        try:
            body, _metadata = MediaJobClient().make_thumbnail_from_url(
                cover_url,
                request_fingerprint=f"{request_id}:proposal-cover",
                width=480,
            )
            return body
        except Exception:
            return None
    return fetch_cover(cover_url, request_id=request_id)


def default_normalizer(data: bytes) -> bytes:
    """画像バイトを JPEG に正規化（webp/png でも安全・幅480px）。ffmpeg 不在なら素通し。

    demo_thumb_to_fmt_mvp.py / thumbnails.py と同じ subprocess(ffmpeg) パターン。
    """
    if not data or not shutil.which("ffmpeg"):
        return data
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        dst = os.path.join(tmp, "out.jpg")
        with open(src, "wb") as f:
            f.write(data)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-vframes", "1", "-vf", "scale=480:-1", dst],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                with open(dst, "rb") as f:
                    return f.read()
        except (OSError, subprocess.SubprocessError):
            return data
    return data
