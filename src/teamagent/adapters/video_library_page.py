"""Shared TikTok public page and safe JPEG thumbnail transport; no browser or paid API."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from teamagent.adapters.video_library_storage import VideoLibraryStorage

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


def item_struct(page: str) -> dict[str, Any]:
    parser = _HydrationParser()
    parser.feed(page)
    try:
        item = json.loads("".join(parser.parts))["__DEFAULT_SCOPE__"]["webapp.video-detail"][
            "itemInfo"
        ]["itemStruct"]
        if isinstance(item, dict):
            return item
    except (ValueError, KeyError, TypeError):
        pass
    raise ValueError("page_data_missing")


def is_cover_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and not any(ord(char) <= 32 or ord(char) == 127 for char in url)
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
        )
    except ValueError:
        return False


def cover_url(page: str) -> str | None:
    try:
        video = item_struct(page)["video"]
        zoom = video.get("zoomCover")
        url = (zoom.get("480") if isinstance(zoom, dict) else None) or video.get("cover")
        if isinstance(url, str) and is_cover_url(url):
            return url
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def page_client() -> httpx.Client:
    return httpx.Client(timeout=20, follow_redirects=False, headers={"User-Agent": PAGE_USER_AGENT})


def page_is_challenge(page: str) -> bool:
    # Real TikTok pages (including deleted/private ones) ship the rehydration data and mention
    # "captcha" in their bundled scripts; only a page without that data is a challenge page.
    # A loose match paused all fetching after one cover-less video (11/05 18:00 run).
    if "__UNIVERSAL_DATA_FOR_REHYDRATION__" in page:
        return False
    return any(
        marker in page.lower() for marker in ("captcha", "verify to continue", "security challenge")
    )


def fetch_page(client: httpx.Client, url: str) -> str:
    with client.stream(
        "GET", url, follow_redirects=False, timeout=20, headers={"User-Agent": PAGE_USER_AGENT}
    ) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("page_too_large")
        return body.decode("utf-8", errors="replace")


def save_cover(
    client: httpx.Client, storage: VideoLibraryStorage, platform: str, video_id: str, url: str
) -> str:
    if not is_cover_url(url):
        raise ValueError("invalid_cover_url")
    with client.stream("GET", url, follow_redirects=False, timeout=20) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_IMAGE_BYTES:
                raise ValueError("cover_too_large")
    if not body.startswith(b"\xff\xd8\xff"):
        raise ValueError("invalid_cover_image")
    return storage.put(platform, video_id, bytes(body))


def stop_reason(exc: Exception) -> str | None:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {403, 429}:
        return f"http_{exc.response.status_code}"
    if isinstance(exc, ValueError) and str(exc) == "challenge":
        return "challenge"
    return None
