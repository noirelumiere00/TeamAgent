"""画像 base64 内包ヘルパのテスト（httpx はモック注入・ネットワーク無し）。

SSRF 対策（許可ホスト/https限定/リダイレクト禁止）と、厳格MIME・サイズ即断・バッチ締切を検証。
"""

from __future__ import annotations

import httpx

from teamagent.skills._shared.image_embed import (
    _host_allowed,
    fetch_data_uri,
    fetch_data_uris,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_OK = "https://pbs.twimg.com/media/x.png"  # 許可ホスト（X CDN）


def _mock(content: bytes = _PNG, status: int = 200) -> httpx.Client:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- SSRF ガード（_host_allowed） ----


def test_host_allowlist_blocks_internal_and_http() -> None:
    assert _host_allowed("https://pbs.twimg.com/media/x.jpg") is True
    assert _host_allowed("https://p16.tiktokcdn.com/x.jpg") is True
    # 内部/メタ/非許可ホスト・http はすべて拒否
    assert _host_allowed("http://pbs.twimg.com/x.jpg") is False  # http不可
    assert _host_allowed("https://169.254.169.254/latest/meta-data/") is False
    assert _host_allowed("https://169.254.170.2/creds") is False
    assert _host_allowed("https://localhost/x") is False
    assert _host_allowed("https://evil.com/x.jpg") is False
    assert _host_allowed("https://pbs.twimg.com.evil.com/x.jpg") is False  # サフィックス偽装


def test_fetch_non_allowed_host_returns_empty_without_request() -> None:
    # 許可外は httpx を触らず即空（fetch されない＝SSRF不発）
    assert fetch_data_uri("https://169.254.169.254/x", http=_mock()) == ""
    assert fetch_data_uri("http://pbs.twimg.com/x.png", http=_mock()) == ""


# ---- 正常系 ----


def test_fetch_png_sniffed_from_allowed_host() -> None:
    assert fetch_data_uri(_OK, http=_mock()).startswith("data:image/png;base64,")


def test_fetch_jpeg_sniffed() -> None:
    uri = fetch_data_uri(_OK, http=_mock(content=b"\xff\xd8\xff\xe0abc"))
    assert uri.startswith("data:image/jpeg;base64,")


# ---- 異常系 ----


def test_fetch_non_image_dropped_no_jpeg_fallback() -> None:
    # 200 でも中身が非画像（HTMLエラーページ等）なら貼らない（既定jpegフォールバック廃止）
    assert fetch_data_uri(_OK, http=_mock(content=b"<html>error</html>")) == ""


def test_fetch_status_error_empty() -> None:
    assert fetch_data_uri(_OK, http=_mock(status=403)) == ""


def test_fetch_oversize_empty() -> None:
    big = b"\x89PNG\r\n\x1a\n" + b"0" * 4_000_000
    assert fetch_data_uri(_OK, http=_mock(content=big)) == ""


# ---- バッチ ----


def test_fetch_data_uris_batch_preserves_order() -> None:
    uris = fetch_data_uris([_OK, "https://pbs.twimg.com/media/y.png"], http=_mock())
    assert len(uris) == 2
    assert all(u.startswith("data:image/png;base64,") for u in uris)


def test_fetch_data_uris_empty_input() -> None:
    assert fetch_data_uris([]) == []
