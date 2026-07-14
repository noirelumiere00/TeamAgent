"""画像 base64 内包ヘルパのテスト（httpx はモック注入・ネットワーク無し）。"""

from __future__ import annotations

import httpx

from teamagent.skills._shared.image_embed import fetch_data_uri, fetch_data_uris

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _mock(content: bytes = _PNG, status: int = 200) -> httpx.Client:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_data_uri_png_sniffed() -> None:
    uri = fetch_data_uri("https://cdn/img.png", http=_mock())
    assert uri.startswith("data:image/png;base64,")


def test_fetch_data_uri_jpeg_sniffed() -> None:
    uri = fetch_data_uri("https://cdn/x", http=_mock(content=b"\xff\xd8\xff\xe0abc"))
    assert uri.startswith("data:image/jpeg;base64,")


def test_fetch_data_uri_non_http_empty() -> None:
    assert fetch_data_uri("data:image/png;base64,AAAA") == ""
    assert fetch_data_uri("") == ""


def test_fetch_data_uri_status_error_empty() -> None:
    assert fetch_data_uri("https://cdn/a.png", http=_mock(status=403)) == ""


def test_fetch_data_uri_oversize_empty() -> None:
    big = b"\x89PNG\r\n\x1a\n" + b"0" * 4_000_000
    assert fetch_data_uri("https://cdn/a.png", http=_mock(content=big)) == ""


def test_fetch_data_uris_batch_preserves_order() -> None:
    uris = fetch_data_uris(["https://cdn/1.png", "https://cdn/2.png"], http=_mock())
    assert len(uris) == 2
    assert all(u.startswith("data:image/png;base64,") for u in uris)


def test_fetch_data_uris_empty_input() -> None:
    assert fetch_data_uris([]) == []
