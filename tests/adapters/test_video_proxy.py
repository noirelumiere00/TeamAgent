"""adapters/video_proxy.py のテスト（ffmpeg は monkeypatch で差し替え、CIでも安定）。"""

from __future__ import annotations

import pytest

from teamagent.adapters import video_proxy
from teamagent.adapters.video_proxy import VideoProxyError, ensure_under_limit


def test_passthrough_when_under_limit() -> None:
    data = b"small"
    out, mime = ensure_under_limit(data, "video/mp4", limit_mb=18)
    assert out is data
    assert mime == "video/mp4"


def test_raises_when_no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video_proxy.shutil, "which", lambda _name: None)
    big = b"x" * (2 * 1024 * 1024)  # 2MB
    with pytest.raises(VideoProxyError, match="VIDEO_PROXY_NO_FFMPEG"):
        ensure_under_limit(big, "video/mp4", limit_mb=1)


def test_ladder_picks_first_under_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video_proxy.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    # crf 28 → まだ大きい / crf 30 → 収まる
    sizes = iter([2 * 1024 * 1024, 512 * 1024])
    calls: list[int] = []

    def _fake_transcode(data: bytes, *, crf: int, long_edge: int, request_id: str) -> bytes:
        calls.append(crf)
        return b"y" * next(sizes)

    monkeypatch.setattr(video_proxy, "_transcode", _fake_transcode)
    big = b"x" * (3 * 1024 * 1024)
    out, mime = ensure_under_limit(big, "video/quicktime", limit_mb=1)
    assert len(out) == 512 * 1024
    assert mime == "video/mp4"  # proxy 化したら mp4
    assert calls == [28, 30]  # 2 段で収束


def test_ladder_returns_smallest_when_none_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video_proxy.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def _always_big(data: bytes, *, crf: int, long_edge: int, request_id: str) -> bytes:
        return b"z" * (5 * 1024 * 1024)  # 常に 5MB（limit 超）

    monkeypatch.setattr(video_proxy, "_transcode", _always_big)
    out, mime = ensure_under_limit(b"x" * (9 * 1024 * 1024), "video/mp4", limit_mb=1)
    # 全段階超過でも最後の結果を返す（諦めず Gemini に投げる）
    assert len(out) == 5 * 1024 * 1024
    assert mime == "video/mp4"
