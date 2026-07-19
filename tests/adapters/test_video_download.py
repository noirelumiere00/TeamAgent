"""adapters/video_download.py の download_video_chained 3分岐テスト。

実ネットワーク/yt-dlp/ブラウザを起動せず、各取得経路と SSRF 検証をモックして
チェーン（primary→fallback→全滅）と VIDEO_DL_ORDER の順序切替を検証する。
"""

from __future__ import annotations

import pytest

from teamagent.adapters import video_download
from teamagent.adapters.video_download import VideoDownloadError, download_video_chained


@pytest.fixture(autouse=True)
def _enable_explicit_local_media_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """These legacy-chain tests intentionally exercise the local developer runtime."""

    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")


def _pass_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """chained 冒頭の SSRF を通す（実 DNS に依存させない）。"""
    import teamagent.adapters.url_guard as url_guard

    monkeypatch.setattr(url_guard, "validate_scrape_url", lambda url, **k: url)


def _boom(*_a: object, **_k: object) -> tuple[bytes, str]:
    raise RuntimeError("DL_FAILED")


def test_chained_primary_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _pass_guard(monkeypatch)
    monkeypatch.setenv("VIDEO_DL_ORDER", "browser,ytdlp")
    import teamagent.adapters.tiktok_scraper as ts

    monkeypatch.setattr(ts, "download_tiktok_video", lambda url, **k: (b"A", "video/mp4"))
    # primary が成功したら yt-dlp は呼ばれない
    monkeypatch.setattr(video_download, "download_video", _boom)
    data, mime = download_video_chained("https://www.tiktok.com/@u/video/1")
    assert data == b"A"
    assert mime == "video/mp4"


def test_chained_primary_fail_fallback_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _pass_guard(monkeypatch)
    monkeypatch.setenv("VIDEO_DL_ORDER", "browser,ytdlp")
    import teamagent.adapters.tiktok_scraper as ts

    monkeypatch.setattr(ts, "download_tiktok_video", _boom)  # primary 失敗
    monkeypatch.setattr(video_download, "download_video", lambda *a, **k: (b"B", "video/mp4"))
    data, _mime = download_video_chained("https://www.tiktok.com/@u/video/1")
    assert data == b"B"


def test_chained_both_fail_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _pass_guard(monkeypatch)
    monkeypatch.setenv("VIDEO_DL_ORDER", "browser,ytdlp")
    import teamagent.adapters.tiktok_scraper as ts

    monkeypatch.setattr(ts, "download_tiktok_video", _boom)
    monkeypatch.setattr(video_download, "download_video", _boom)
    with pytest.raises(VideoDownloadError, match="ALL_DOWNLOAD_FAILED"):
        download_video_chained("https://www.tiktok.com/@u/video/1")


def test_chained_order_ytdlp_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """VIDEO_DL_ORDER=ytdlp,browser で yt-dlp を先に試す（本番EC2想定）。"""
    _pass_guard(monkeypatch)
    monkeypatch.setenv("VIDEO_DL_ORDER", "ytdlp,browser")
    import teamagent.adapters.tiktok_scraper as ts

    monkeypatch.setattr(video_download, "download_video", lambda *a, **k: (b"Y", "video/mp4"))
    monkeypatch.setattr(ts, "download_tiktok_video", _boom)  # browser 経路は使われないはず
    data, _mime = download_video_chained("https://www.tiktok.com/@u/video/1")
    assert data == b"Y"


def test_chained_blocks_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    """冒頭の SSRF 検証で弾かれたら取得経路を試さず VIDEO_URL_BLOCKED。"""
    import teamagent.adapters.url_guard as url_guard

    def _block(url: str, **_k: object) -> str:
        raise url_guard.UrlGuardError("URL_DOMAIN_BLOCKED")

    monkeypatch.setattr(url_guard, "validate_scrape_url", _block)
    with pytest.raises(VideoDownloadError, match="VIDEO_URL_BLOCKED"):
        download_video_chained("https://attacker.example/x")
