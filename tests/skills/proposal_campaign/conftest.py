"""proposal_campaign テストの共有 fixture（ネット非依存・埋め込み JPEG・DI mock）。"""

from __future__ import annotations

import base64
from collections.abc import Callable

import pytest

from teamagent.adapters.tiktok_scraper import TikTokAuthor, TikTokVideo

# test_renderer_images.py と同じ 1x1 最小有効 JPEG（python-pptx/PIL が寸法を読める良品）。
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsO"
    "CwkJDRENDg8QEBEQCgwSExIQEw8QEBD/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQQAQAA"
    "AAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)


def _make_video(
    url: str = "https://www.tiktok.com/@x/video/1",
    cover_url: str = "https://cdn.example/x.jpg",
) -> TikTokVideo:
    """テスト用 TikTokVideo（.url/.cover_url 契約を実型で固定）。"""
    return TikTokVideo(
        id="1",
        url=url,
        desc="desc",
        create_time=0,
        duration=15,
        cover_url=cover_url,
        author=TikTokAuthor(unique_id="x", nickname="X", follower_count=0),
        play_count=100,
        digg_count=10,
        comment_count=1,
        share_count=1,
        collect_count=1,
        hashtags=(),
        music_title="",
    )


@pytest.fixture
def tiny_jpeg() -> bytes:
    return _TINY_JPEG


@pytest.fixture
def make_video() -> Callable[..., TikTokVideo]:
    return _make_video


@pytest.fixture
def mock_searcher() -> Callable[[str, int, str], list[TikTokVideo]]:
    """KW ごとに固定の 1 位動画を返す検索（ネット非依存）。"""

    def _search(query: str, max_videos: int, request_id: str) -> list[TikTokVideo]:
        return [
            _make_video(
                url=f"https://www.tiktok.com/@x/video/{query}",
                cover_url=f"https://cdn.example/{query}.jpg",
            )
        ]

    return _search


@pytest.fixture
def mock_fetcher() -> Callable[[str | None, str], bytes | None]:
    """常に埋め込み JPEG を返す cover 取得（ネット非依存）。"""

    def _fetch(cover_url: str | None, request_id: str) -> bytes | None:
        return _TINY_JPEG

    return _fetch
