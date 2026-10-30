"""Realistic TikTok hydration HTML, fake HTTP/S3/DB only."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from teamagent.adapters.video_library_storage import (
    THUMB_PREFIX,
    VideoLibraryStorage,
    thumbnail_key,
)
from teamagent.ingest import video_library_thumbs as thumbs

COVER = "https://p16-sign-sg.tiktokcdn.com/tos-alisg-p-0037/test.jpeg?x-expires=123&x-signature=abc"
ZOOM = "https://p16-sign-sg.tiktokcdn.com/tos-alisg-p-0037/zoom.jpeg?x-expires=123&x-signature=abc"


def page(video: dict[str, Any]) -> str:
    payload = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {"itemInfo": {"itemStruct": {"id": "123", "video": video}}}
        }
    }
    return (
        '<!doctype html><html><head></head><body><div id="root"></div><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(payload)
        + '</script><script nonce="other">var x=1;</script></body></html>'
    )


@pytest.mark.parametrize(
    "video,expected",
    [
        ({"zoomCover": {"480": ZOOM, "960": COVER}, "cover": COVER}, ZOOM),
        ({"cover": COVER}, COVER),
        ({"zoomCover": {}, "cover": COVER}, COVER),
    ],
)
def test_cover_from_real_hydration_shape(video: dict[str, Any], expected: str) -> None:
    assert thumbs.cover_url(page(video)) == expected


@pytest.mark.parametrize(
    "html",
    [
        "<html>challenge</html>",
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{bad}</script>',
        page({"cover": "http://169.254.169.254/latest/meta-data"}),
        page({"cover": "https://evil.test/p.jpg"}),
    ],
)
def test_no_json_or_untrusted_cover_skips(html: str) -> None:
    assert thumbs.cover_url(html) is None


class Pg:
    def __init__(self, size: int = 10) -> None:
        self.rows = [
            {
                "platform": "tiktok",
                "video_id": str(i),
                "url": f"https://www.tiktok.com/@sample/video/{i}",
            }
            for i in range(size)
        ]
        self.calls: list[Any] = []

    @contextmanager
    def connection(self, **kwargs: Any) -> Any:
        assert kwargs["app_role"] == "teamagent_app"
        assert kwargs["user_role"] == "admin"
        yield self

    def execute(self, sql: str, params: Any) -> MagicMock:
        self.calls.append((sql, params))
        return MagicMock(fetchall=lambda: self.rows)


def test_limit_interval_and_s3_key() -> None:
    pg = Pg()
    s3 = MagicMock()
    storage = VideoLibraryStorage(bucket="private", client=s3)
    sleep = MagicMock()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.tiktok.com":
            return httpx.Response(200, text=page({"zoomCover": {"480": ZOOM}, "cover": COVER}))
        assert str(request.url) == ZOOM
        return httpx.Response(200, content=b"\xff\xd8\xffimage")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        counts = thumbs.fill_thumbnails(pg, storage, client, limit=3, sleep=sleep)
    assert counts == {"attempted": 3, "saved": 3}
    assert sleep.call_count == 2
    sleep.assert_called_with(1.0)
    assert s3.put_object.call_count == 3
    assert s3.put_object.call_args.kwargs["Key"] == "video-library/thumbs/tiktok/2.jpg"
    assert THUMB_PREFIX == "video-library/thumbs/"
    assert pg.calls[0][1] == (3,)
    assert pg.calls[-1][1] == ("video-library/thumbs/tiktok/2.jpg", "tiktok", "2")


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, {"attempted": 1, "http_429": 1}),
        (500, {"attempted": 5, "http_500": 5, "consecutive_failure_stop": 1}),
    ],
)
def test_http_failure_stops(status: int, expected: dict[str, int]) -> None:
    pg = Pg()
    storage = MagicMock()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status))) as client:
        counts = thumbs.fill_thumbnails(pg, storage, client, limit=10, sleep=lambda _: None)
    assert counts == expected
    storage.put.assert_not_called()
    assert len(pg.calls) == 1


def test_no_json_skips_and_is_retried_next_run() -> None:
    pg = Pg(1)
    storage = MagicMock()
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>challenge</html>"))
    ) as client:
        assert thumbs.fill_thumbnails(pg, storage, client, limit=1) == {
            "attempted": 1,
            "cover_missing": 1,
        }
    assert len(pg.calls) == 1
    storage.put.assert_not_called()


def test_cover_429_also_stops() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, text=page({"cover": COVER}))
            if request.url.host == "www.tiktok.com"
            else httpx.Response(429)
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert thumbs.fill_thumbnails(Pg(), MagicMock(), client, limit=10) == {
            "attempted": 1,
            "http_429": 1,
        }


@pytest.mark.parametrize(
    "platform,vid", [("bad", "123"), ("tiktok", "../secret"), ("tiktok", "123/x")]
)
def test_storage_rejects_prefix_escape(platform: str, vid: str) -> None:
    with pytest.raises(ValueError):
        thumbnail_key(platform, vid)


def test_storage_read_closes_body() -> None:
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"jpeg"), "ETag": '"etag"'}
    assert VideoLibraryStorage(bucket="b", client=s3).get("tiktok", "123") == (b"jpeg", '"etag"')
    s3.get_object.return_value["Body"].close.assert_called_once()


def test_failure_logs_counts_without_urls_or_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg = Pg(1)
    storage = MagicMock()
    storage.put.side_effect = RuntimeError("private caption https://example.test/secret")
    logged = MagicMock()
    monkeypatch.setattr(thumbs, "logger", logged)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.tiktok.com":
            return httpx.Response(200, text=page({"cover": COVER}))
        return httpx.Response(200, content=b"\xff\xd8\xffimage")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        counts = thumbs.fill_thumbnails(pg, storage, client, limit=1)
    assert counts == {"attempted": 1, "RuntimeError": 1}
    assert logged.info.call_args.kwargs["counts"] == dict(counts)
    output = str(logged.mock_calls)
    assert "private caption" not in output and "https://" not in output
    assert len(pg.calls) == 1  # A failed upload never records a thumbnail in the DB.


def test_non_image_response_is_not_saved() -> None:
    storage = MagicMock()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.tiktok.com":
            return httpx.Response(200, text=page({"cover": COVER}))
        return httpx.Response(200, content=b"<html>challenge</html>")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        counts = thumbs.fill_thumbnails(Pg(1), storage, client, limit=1)
    assert counts == {"attempted": 1, "ValueError": 1}
    storage.put.assert_not_called()
