"""Slack 添付ダウンロードの 302 追従テスト（本番実測の失敗モードを再現）。

2026-08-18 本番実測:
  - 小さい .txt → ``files.slack.com`` が **200 直返し**（従来コードでも成功していた）
  - 2.4MB の PDF → ``files.slack.com`` が **302 → https://slack-files.com/files-pri-safe/…**
    （署名 URL）。リダイレクト非追従＋転送先が allowlist 外のため download_failed。

したがってフェイクは「302 + Location ヘッダ + 転送先の 200」というチェーンそのものを
httpx の MockTransport で再現する（レスポンスを返すだけのダミーでは本番の失敗を再現しない）。

死守ライン:
  R1 追従は **許可ホストからの 302/303 のみ・1 回だけ**。
  R2 転送先は ``slack-files.com`` 系だけ（それ以外は従来どおり拒否）。
  R3 転送先へ **Authorization を送らない**（署名 URL は自己完結・token を別ドメインへ出さない）。
  R4 逐次サイズ検査・cap・Content-Length 事前拒否は **転送先レスポンスにも**効く。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from teamagent.adapters.slack_client import SlackClient
from teamagent.adapters.slack_file_guard import SlackFileGuardError

pytestmark = pytest.mark.asyncio

SLACK_URL = "https://files.slack.com/files-pri/T1-F1/big.pdf"
SIGNED_URL = "https://slack-files.com/files-pri-safe/T1-F1/big.pdf?sig=abc"


class _CountingStream(httpx.AsyncByteStream):
    """要求されたぶんだけチャンクを吐く応答本体（読まれた回数を数える）。"""

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk = chunk
        self.count = count
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.count):
            self.yielded += 1
            yield self.chunk


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(**kwargs)

    monkeypatch.setattr("teamagent.adapters.slack_client.httpx.AsyncClient", _factory)


def _redirect_chain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    location: str = SIGNED_URL,
    status: int = 302,
    body: bytes = b"%PDF-1.7 real bytes",
    second_hop: str | None = None,
    final_headers: dict[str, str] | None = None,
    final_stream: httpx.AsyncByteStream | None = None,
) -> list[httpx.Request]:
    """``files.slack.com`` が 302 を返し、転送先が本体を返す本番同型のチェーン。"""
    seen: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "files.slack.com":
            return httpx.Response(status, headers={"location": location})
        if second_hop and req.url.host == httpx.URL(location).host:
            return httpx.Response(302, headers={"location": second_hop})
        if final_stream is not None:
            return httpx.Response(200, stream=final_stream, headers=final_headers or {})
        return httpx.Response(200, content=body, headers=final_headers or {})

    _patch_transport(monkeypatch, _handler)
    return seen


# ── R1/R3: 正常系（本番の 2.4MB PDF と同じ形）─────────────────────────────────


@pytest.mark.parametrize("status", [302, 303])
async def test_guarded_follows_one_redirect_to_signed_url(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    seen = _redirect_chain(monkeypatch, status=status)
    client = SlackClient(bot_token="xoxb-test", client=object())

    data = await client.download_file_guarded(SLACK_URL, max_bytes=1024 * 1024)

    assert data == b"%PDF-1.7 real bytes"
    assert [str(r.url) for r in seen] == [SLACK_URL, SIGNED_URL]
    # R3: 1 段目だけ bot token を載せ、転送先には **一切** 載せない。
    assert seen[0].headers.get("authorization") == "Bearer xoxb-test"
    assert "authorization" not in seen[1].headers


async def test_bounded_follows_one_redirect_to_signed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _redirect_chain(monkeypatch, body=b"video-bytes")
    client = SlackClient(bot_token="xoxb-test", client=object())

    data = await client.download_file_bounded(SLACK_URL, max_bytes=1024 * 1024)

    assert data == b"video-bytes"
    assert [str(r.url) for r in seen] == [SLACK_URL, SIGNED_URL]
    assert seen[0].headers.get("authorization") == "Bearer xoxb-test"
    assert "authorization" not in seen[1].headers


async def test_direct_200_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """小さい .txt の 200 直返し経路は 1 バイトも挙動が変わらない（回帰）。"""
    seen: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"plain text")

    _patch_transport(monkeypatch, _handler)
    client = SlackClient(bot_token="xoxb-test", client=object())

    assert await client.download_file_guarded(SLACK_URL, max_bytes=1024) == b"plain text"
    assert len(seen) == 1
    assert seen[0].headers.get("authorization") == "Bearer xoxb-test"


# ── R2: 転送先 allowlist ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.com/files-pri-safe/x.pdf",
        "https://slack-files.com.evil.example/x.pdf",  # 接尾辞偽装
        "https://evil.example/?x=slack-files.com",  # 部分文字列
        "http://slack-files.com/x.pdf",  # 非 HTTPS
        "https://user:pw@slack-files.com/x.pdf",  # 非 canonical authority
        "https://slack-files.com:8443/x.pdf",  # 非既定ポート
        "/files-pri-safe/relative.pdf",  # 相対 Location
        "",  # Location 欠落
    ],
)
async def test_guarded_rejects_redirect_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    seen = _redirect_chain(monkeypatch, location=location)
    client = SlackClient(bot_token="xoxb-test", client=object())

    with pytest.raises(SlackFileGuardError, match="SLACK_FILE_"):
        await client.download_file_guarded(SLACK_URL, max_bytes=1024 * 1024)

    # 拒否は 1 段目のレスポンスを見た時点＝転送先へは 1 度もリクエストしていない。
    assert len(seen) == 1


async def test_bounded_rejects_redirect_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _redirect_chain(monkeypatch, location="https://evil.example.com/x.mp4")
    client = SlackClient(bot_token="xoxb-test", client=object())

    with pytest.raises(SlackFileGuardError, match="SLACK_FILE_REDIRECT_HOST_BLOCKED"):
        await client.download_file_bounded(SLACK_URL, max_bytes=1024 * 1024)
    assert len(seen) == 1


@pytest.mark.parametrize("status", [307, 308])
async def test_method_preserving_redirects_are_not_followed(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """307/308 は『ヘッダも保って再送』の意味論。Authorization を落とす本実装では追わない。

    追わない＝httpx の ``raise_for_status`` が 3xx をそのままエラーにする（修正前の
    302 と同じ姿＝呼び出し側では download_failed）。転送先へは 1 度も飛ばない。
    """
    seen = _redirect_chain(monkeypatch, status=status)
    client = SlackClient(bot_token="xoxb-test", client=object())

    with pytest.raises(httpx.HTTPStatusError):
        await client.download_file_bounded(SLACK_URL, max_bytes=1024)
    assert len(seen) == 1


# ── R1: 多段リダイレクトは拒否 ────────────────────────────────────────────────


async def test_guarded_refuses_second_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _redirect_chain(monkeypatch, second_hop="https://slack-files.com/hop2/x.pdf")
    client = SlackClient(bot_token="xoxb-test", client=object())

    with pytest.raises(SlackFileGuardError, match="SLACK_FILE_REDIRECT_CHAIN"):
        await client.download_file_guarded(SLACK_URL, max_bytes=1024 * 1024)
    assert len(seen) == 2  # 3 本目は投げていない


async def test_bounded_refuses_second_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _redirect_chain(monkeypatch, second_hop="https://slack-files.com/hop2/x.mp4")
    client = SlackClient(bot_token="xoxb-test", client=object())

    with pytest.raises(RuntimeError, match="SLACK_FILE_REDIRECT_CHAIN"):
        await client.download_file_bounded(SLACK_URL, max_bytes=1024 * 1024)
    assert len(seen) == 2


# ── R4: cap は転送先レスポンスにも効く ────────────────────────────────────────


async def test_guarded_content_length_cap_applies_after_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingStream(b"y" * 1024, 10)
    _redirect_chain(
        monkeypatch,
        final_headers={"content-length": str(99 * 1024 * 1024)},
        final_stream=stream,
    )
    client = SlackClient(bot_token="xoxb-test", client=object())

    with pytest.raises(SlackFileGuardError, match="TOO_LARGE"):
        await client.download_file_guarded(SLACK_URL, max_bytes=1024 * 1024)
    assert stream.yielded == 0  # 転送先の本文を 1 バイトも読んでいない


async def test_guarded_streaming_cap_applies_after_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content-Length を名乗らない転送先でも、受信途中で必ず止まる（OOM 経路）。"""
    stream = _CountingStream(b"x" * 256 * 1024, 400)  # 100MB 相当
    _redirect_chain(monkeypatch, final_stream=stream)
    client = SlackClient(bot_token="xoxb-test", client=object())

    with pytest.raises(SlackFileGuardError, match="TOO_LARGE"):
        await client.download_file_guarded(SLACK_URL, max_bytes=1024 * 1024)
    assert stream.yielded <= 6, f"読み過ぎ: {stream.yielded} チャンク"


async def test_bounded_streaming_cap_applies_after_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingStream(b"x" * 256 * 1024, 400)
    _redirect_chain(monkeypatch, final_stream=stream)
    client = SlackClient(bot_token="xoxb-test", client=object())

    with pytest.raises(RuntimeError, match="TOO_LARGE"):
        await client.download_file_bounded(SLACK_URL, max_bytes=1024 * 1024)
    assert stream.yielded <= 6


# ── 転送先 allowlist の env 拡張（既定を広げず、明示だけ許す）────────────────


async def test_redirect_allowlist_is_env_extendable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_FILE_REDIRECT_ALLOWED_HOSTS", "files-edge.example.net")
    seen = _redirect_chain(monkeypatch, location="https://files-edge.example.net/x.pdf")
    client = SlackClient(bot_token="xoxb-test", client=object())

    data = await client.download_file_guarded(SLACK_URL, max_bytes=1024 * 1024)

    assert data == b"%PDF-1.7 real bytes"
    assert "authorization" not in seen[1].headers  # env で広げても token は出さない
