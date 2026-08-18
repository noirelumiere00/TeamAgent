"""SlackClient.download_file_bounded の門番テスト。

url_private へは bot token の Authorization ヘッダが載る。よって
「自 WS のファイル配信ホスト以外へ絶対に出さない」「リダイレクトを追わない」
「全量をメモリに広げてから測らない」の 3 点が本番の安全性そのもの。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

from teamagent.adapters import slack_client as slack_module
from teamagent.adapters.slack_client import SlackClient, slack_file_url_allowed

SLACK_URL = "https://files.slack.com/files-pri/T1-F1/movie.mp4"


@pytest.mark.parametrize(
    "url",
    [
        SLACK_URL,
        "https://slack.com/files-pri/x.mp4",
        "https://files-edge.slack.com/x.mp4",
    ],
)
def test_allowed_hosts(url: str) -> None:
    assert slack_file_url_allowed(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://files.slack.com/x.mp4",
        "https://evil.com/x.mp4",
        "https://files.slack.com.evil.com/x.mp4",
        "https://user:pw@files.slack.com/x.mp4",
        "https://files.slack.com:8443/x.mp4",
        "",
    ],
)
def test_rejected_hosts(url: str) -> None:
    assert not slack_file_url_allowed(url)


class _FakeResponse:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
        self._chunks = chunks
        self.headers = headers or {}

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, size: int) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    last_kwargs: ClassVar[dict[str, Any]] = {}
    captured: ClassVar[dict[str, Any]] = {}
    chunks: ClassVar[list[bytes]] = []
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def stream(self, method: str, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        type(self).captured = {"method": method, "url": url, "headers": headers or {}}
        return _FakeResponse(type(self).chunks, type(self).headers)


def _install(
    monkeypatch: pytest.MonkeyPatch, chunks: list[bytes], headers: dict[str, str] | None = None
) -> None:
    _FakeAsyncClient.chunks = chunks
    _FakeAsyncClient.headers = headers or {}
    monkeypatch.setattr(slack_module.httpx, "AsyncClient", _FakeAsyncClient)


@pytest.mark.asyncio
async def test_streams_and_sends_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [b"aaa", b"bbb"])
    client = SlackClient(bot_token="xoxb-test", client=object())
    data = await client.download_file_bounded(SLACK_URL, max_bytes=1024)
    assert data == b"aaabbb"
    assert _FakeAsyncClient.captured["headers"]["Authorization"] == "Bearer xoxb-test"
    # リダイレクトを追わない＝転送先ホストへトークンを渡さない。
    assert _FakeAsyncClient.last_kwargs["follow_redirects"] is False


@pytest.mark.asyncio
async def test_rejects_foreign_host_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [b"x"])
    _FakeAsyncClient.captured = {}
    client = SlackClient(bot_token="xoxb-test", client=object())
    with pytest.raises(RuntimeError, match="SLACK_FILE_HOST_NOT_ALLOWED"):
        await client.download_file_bounded("https://evil.com/x.mp4", max_bytes=1024)
    assert _FakeAsyncClient.captured == {}


@pytest.mark.asyncio
async def test_content_length_over_limit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [b"x" * 10], {"content-length": "999999"})
    client = SlackClient(bot_token="xoxb-test", client=object())
    with pytest.raises(RuntimeError, match="SLACK_FILE_TOO_LARGE"):
        await client.download_file_bounded(SLACK_URL, max_bytes=100)


@pytest.mark.asyncio
async def test_streaming_stops_when_body_exceeds_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Content-Length を名乗らない相手でも、受信途中で必ず止まる。"""

    _install(monkeypatch, [b"x" * 60, b"x" * 60, b"x" * 60])
    client = SlackClient(bot_token="xoxb-test", client=object())
    with pytest.raises(RuntimeError, match="SLACK_FILE_TOO_LARGE"):
        await client.download_file_bounded(SLACK_URL, max_bytes=100)


@pytest.mark.asyncio
async def test_empty_body_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [])
    client = SlackClient(bot_token="xoxb-test", client=object())
    with pytest.raises(RuntimeError, match="SLACK_FILE_EMPTY"):
        await client.download_file_bounded(SLACK_URL, max_bytes=100)
