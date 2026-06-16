"""VideoAlgorithmInput の既定値（HTML-first 化）の検証。

- outputs 既定 = ["report", "slides"]（編集可スライドHTMLを既定で発行・pptx は明示要求のみ）。
- max_videos 既定 = env VIDEO_ALGO_MAX_VIDEOS（既定 5・clamp 1〜10）。
"""

from __future__ import annotations

import pytest

from teamagent.skills.video_algorithm.schema import (
    VideoAlgorithmInput,
    _default_max_videos,
    _default_outputs,
)


def test_default_outputs_includes_slides() -> None:
    assert _default_outputs() == ["report", "slides"]
    inp = VideoAlgorithmInput(query="集中")
    assert inp.outputs == ["report", "slides"]
    # pptx は既定に入れない（重い・明示要求のみ）
    assert "pptx" not in inp.outputs


def test_explicit_outputs_still_honored() -> None:
    inp = VideoAlgorithmInput(query="x", outputs=["report", "slides", "pptx"])
    assert inp.outputs == ["report", "slides", "pptx"]
    inp2 = VideoAlgorithmInput(query="x", outputs=["report"])
    assert inp2.outputs == ["report"]


def test_default_max_videos_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_ALGO_MAX_VIDEOS", raising=False)
    assert _default_max_videos() == 5
    assert VideoAlgorithmInput(query="x").max_videos == 5

    monkeypatch.setenv("VIDEO_ALGO_MAX_VIDEOS", "8")
    assert _default_max_videos() == 8
    assert VideoAlgorithmInput(query="x").max_videos == 8


def test_default_max_videos_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_ALGO_MAX_VIDEOS", "20")  # 上限 10 に clamp
    assert _default_max_videos() == 10
    monkeypatch.setenv("VIDEO_ALGO_MAX_VIDEOS", "0")  # 下限 1 に clamp
    assert _default_max_videos() == 1
    monkeypatch.setenv("VIDEO_ALGO_MAX_VIDEOS", "abc")  # 不正値は既定 5
    assert _default_max_videos() == 5


def test_explicit_max_videos_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_ALGO_MAX_VIDEOS", "5")
    # 入力で明示指定すれば env 既定より優先（1〜10 の範囲内）
    assert VideoAlgorithmInput(query="x", max_videos=10).max_videos == 10
    with pytest.raises(ValueError):  # le=10 を超える明示指定は弾く
        VideoAlgorithmInput(query="x", max_videos=11)
