"""表紙の出どころ（仕様 v3 T19・M18）: 表紙の URL を先に使い、取れなければ冒頭のコマで代える。

本番では media job の経路が抽出済みのコマを先に使い、0.8 秒のコマを「表紙」として色を比べて
いた。表紙を取れたときは出どころ cover、コマで代えたときは frame を残し、描画は代用を表紙と
呼ばない（slides の比較・レポートのサムネ色）。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills.video_algorithm.schema import FrameShot
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

_FRAME = FrameShot(sec=0.8, data_uri="data:image/jpeg;base64,/9j/AAAA")
_META = {"swatches": ["#aa3300"], "brightness01": 0.4, "warmth": 0.2}


class _Media:
    """media job のフェイク（表紙の URL の取得が 403 で落ちる本番の失敗の形も再現する）。"""

    def __init__(self, *, url_fails: bool) -> None:
        self.url_fails = url_fails
        self.calls: list[str] = []

    @staticmethod
    def is_configured() -> bool:
        return True

    def make_thumbnail_from_url(self, url: str, **kw: Any) -> tuple[bytes, dict[str, Any]]:
        self.calls.append("url")
        if self.url_fails:
            raise RuntimeError("MEDIA_THUMBNAIL_URL_FORBIDDEN")
        return b"cover", _META

    def make_thumbnail(self, data: bytes, mime: str, **kw: Any) -> tuple[bytes, dict[str, Any]]:
        self.calls.append("frame")
        return b"frame", _META


def test_cover_url_comes_first_even_when_frames_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """壊し方: コマを先に使う（旧の順）→ 出どころが frame になって赤。"""
    from teamagent.adapters import media_job

    media = _Media(url_fails=False)

    class _Client:
        is_configured = staticmethod(lambda: True)

        def __new__(cls) -> Any:
            return media

    monkeypatch.setattr(media_job, "MediaJobClient", _Client)
    uri, thumb, source = VideoAlgorithmSkill()._build_thumb(
        "https://p16.example/cover.jpg", [_FRAME], "req"
    )
    assert source == "cover" and media.calls == ["url"]
    assert uri.startswith("data:image/jpeg;base64,") and thumb is not None


def test_failed_cover_falls_back_to_the_opening_frame_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.adapters import media_job

    media = _Media(url_fails=True)

    class _Client:
        is_configured = staticmethod(lambda: True)

        def __new__(cls) -> Any:
            return media

    monkeypatch.setattr(media_job, "MediaJobClient", _Client)
    _uri, _thumb, source = VideoAlgorithmSkill()._build_thumb(
        "https://p16.example/cover.jpg", [_FRAME], "req"
    )
    assert source == "frame" and media.calls == ["url", "frame"]
    with pytest.raises(RuntimeError, match="MEDIA_THUMBNAIL_JOB_FAILED"):
        VideoAlgorithmSkill()._build_thumb("https://p16.example/cover.jpg", [], "req")
