"""Realistic TikTok hydration HTML, fake HTTP/S3/DB only."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from teamagent.adapters import video_library_page
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
        self.paused = False
        self.consecutive_failures = 0

    @contextmanager
    def connection(self, **kwargs: Any) -> Any:
        assert kwargs["app_role"] == "teamagent_app"
        assert kwargs["user_role"] == "admin"
        yield self

    def commit(self) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> MagicMock:
        self.calls.append((sql, params))
        result = MagicMock(fetchall=lambda: self.rows)
        if "pg_try_advisory_lock" in sql:
            result.fetchone.return_value = {"locked": True}
        elif "SELECT * FROM vl_public_fetch_control" in sql:
            result.fetchone.return_value = {"paused": self.paused}
        elif "consecutive_failures=CASE" in sql:
            self.consecutive_failures = 0 if params[0] else self.consecutive_failures + 1
            self.paused |= params[1] is not None or self.consecutive_failures >= 5
        return result


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
    updates = [params for sql, params in pg.calls if "UPDATE vl_videos SET thumbnail_s3_key" in sql]
    assert updates[-1] == ("video-library/thumbs/tiktok/2.jpg", "tiktok", "2")


@pytest.mark.parametrize(
    "status,expected",
    [
        (403, {"attempted": 1, "http_403": 1}),
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
    assert not any("UPDATE vl_videos" in sql for sql, _ in pg.calls)


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
    assert not any("UPDATE vl_videos" in sql for sql, _ in pg.calls)
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
    assert not any("UPDATE vl_videos" in sql for sql, _ in pg.calls)


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


def test_page_user_agent_is_a_full_browser_string() -> None:
    # 短い UA だと TikTok は JSON の無い空ページを返す（実測）。完全なブラウザ UA を送ること

    assert (
        "Chrome/" in video_library_page.PAGE_USER_AGENT
        and "AppleWebKit" in video_library_page.PAGE_USER_AGENT
    )
    with video_library_page.page_client() as client:
        assert client.headers["User-Agent"] == video_library_page.PAGE_USER_AGENT


def test_shared_pause_prevents_thumbnail_http() -> None:
    pg = Pg(1)
    pg.paused = True
    transport = MagicMock(return_value=httpx.Response(200, text=page({"cover": COVER})))
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        assert thumbs.fill_thumbnails(pg, MagicMock(), client, limit=1) == {"fetch_paused": 1}
    transport.assert_not_called()


def test_thumbnail_challenge_persists_shared_pause() -> None:
    pg = Pg(2)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="<html>Security challenge</html>")
        )
    ) as client:
        assert thumbs.fill_thumbnails(pg, MagicMock(), client, limit=2) == {
            "attempted": 1,
            "challenge": 1,
        }
        assert pg.paused
        assert thumbs.fill_thumbnails(pg, MagicMock(), client, limit=2) == {"fetch_paused": 1}


def test_a_real_page_without_cover_is_not_a_challenge() -> None:
    from teamagent.adapters.video_library_page import page_is_challenge

    real = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        '{"__DEFAULT_SCOPE__":{"webapp.video-detail":{"statusCode":10204}}}</script>'
        "<script>loadCaptchaSdk()</script>"
    )
    assert not page_is_challenge(real)
    assert page_is_challenge("<html><title>Security Check</title>captcha verify to continue</html>")


def test_real_pages_without_cover_do_not_pause_fetching() -> None:
    # Deleted/private posts still ship the rehydration data; five in a row must not stop the run.
    real_no_cover = (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        '{"__DEFAULT_SCOPE__":{"webapp.video-detail":{"statusCode":10204}}}</script>'
    )
    pg = Pg(6)
    storage = MagicMock()
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=real_no_cover))
    ) as client:
        counts = thumbs.fill_thumbnails(pg, storage, client, limit=6, sleep=lambda _: None)
    assert counts == {"attempted": 6, "cover_missing": 6}
