"""VideoAnalysisSkill の単体テスト (GeminiClient をモック)。

google-genai / GEMINI_API_KEY が無くても通るよう、Gemini は注入モックにする。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.skills.base import SkillContext
from teamagent.skills.video.schema import VideoAnalysisInput
from teamagent.skills.video.skill import VideoAnalysisSkill


def _resp() -> GeminiResponse:
    return GeminiResponse(
        text="### 1. 一行サマリ\n冒頭1秒の衝撃ビジュアルで離脱を防ぐ [0:00]",
        input_tokens=5000,
        output_tokens=400,
        cost_usd=0.00099,
        model_id="gemini-2.5-flash",
        latency_ms=8000,
    )


@pytest.fixture
def fake_gemini() -> MagicMock:
    mock = MagicMock()
    mock.analyze_video_url.return_value = _resp()
    mock.analyze_video_bytes.return_value = _resp()
    return mock


def test_video_analysis_returns_structured(fake_gemini: MagicMock) -> None:
    skill = VideoAnalysisSkill(gemini=fake_gemini)
    out = skill.run(VideoAnalysisInput(url="https://youtube.com/shorts/abc123"), ctx=SkillContext())
    assert out.url == "https://youtube.com/shorts/abc123"
    assert "一行サマリ" in out.analysis
    assert out.model_id == "gemini-2.5-flash"
    assert out.total_cost_usd == pytest.approx(0.00099)


def test_video_analysis_passes_url_and_system(fake_gemini: MagicMock) -> None:
    skill = VideoAnalysisSkill(gemini=fake_gemini)
    skill.run(
        VideoAnalysisInput(url="https://youtube.com/shorts/x", focus="フック重視"),
        ctx=SkillContext(),
    )
    kwargs = fake_gemini.analyze_video_url.call_args.kwargs
    assert kwargs["url"] == "https://youtube.com/shorts/x"
    assert kwargs["system"]  # system instruction (prompt) が渡る
    assert "フック重視" in kwargs["prompt"]  # focus が user prompt に反映


def test_video_analysis_empty_text_fallback(fake_gemini: MagicMock) -> None:
    fake_gemini.analyze_video_url.return_value = GeminiResponse(
        text="",
        input_tokens=10,
        output_tokens=0,
        cost_usd=0.0,
        model_id="gemini-2.5-flash",
        latency_ms=100,
    )
    skill = VideoAnalysisSkill(gemini=fake_gemini)
    out = skill.run(VideoAnalysisInput(url="https://youtube.com/shorts/y"), ctx=SkillContext())
    assert "確認してください" in out.analysis


def test_youtube_uses_file_uri_not_download(fake_gemini: MagicMock) -> None:
    """YouTube は file_uri 経路（DL しない）。"""
    downloader = MagicMock()
    skill = VideoAnalysisSkill(gemini=fake_gemini, downloader=downloader)
    skill.run(VideoAnalysisInput(url="https://youtu.be/abc"), ctx=SkillContext())
    fake_gemini.analyze_video_url.assert_called_once()
    fake_gemini.analyze_video_bytes.assert_not_called()
    downloader.assert_not_called()


def test_tiktok_downloads_then_analyzes_bytes(fake_gemini: MagicMock) -> None:
    """TikTok は yt-dlp で DL → inline bytes 経路。"""
    downloader = MagicMock(return_value=(b"\x00\x01videobytes", "video/mp4"))
    skill = VideoAnalysisSkill(gemini=fake_gemini, downloader=downloader)
    out = skill.run(
        VideoAnalysisInput(url="https://www.tiktok.com/@u/video/123"), ctx=SkillContext()
    )
    downloader.assert_called_once_with("https://www.tiktok.com/@u/video/123")
    fake_gemini.analyze_video_bytes.assert_called_once()
    fake_gemini.analyze_video_url.assert_not_called()
    kwargs = fake_gemini.analyze_video_bytes.call_args.kwargs
    assert kwargs["data"] == b"\x00\x01videobytes"
    assert kwargs["mime_type"] == "video/mp4"
    assert "一行サマリ" in out.analysis


def test_instagram_reel_uses_download(fake_gemini: MagicMock) -> None:
    downloader = MagicMock(return_value=(b"data", "video/mp4"))
    skill = VideoAnalysisSkill(gemini=fake_gemini, downloader=downloader)
    skill.run(VideoAnalysisInput(url="https://www.instagram.com/reel/abc/"), ctx=SkillContext())
    downloader.assert_called_once()
    fake_gemini.analyze_video_bytes.assert_called_once()


def test_analyze_bytes_path(fake_gemini: MagicMock) -> None:
    """アップロード動画 bytes を直接分析する。"""
    skill = VideoAnalysisSkill(gemini=fake_gemini)
    out = skill.analyze_bytes(b"\x00video", "video/mp4", SkillContext())
    fake_gemini.analyze_video_bytes.assert_called_once()
    assert "一行サマリ" in out.analysis
    assert out.url == "uploaded"


def test_synthesize_batch_single_returns_as_is(fake_gemini: MagicMock) -> None:
    skill = VideoAnalysisSkill(gemini=fake_gemini)
    text, cost = skill.synthesize_batch(["only one analysis"], SkillContext())
    assert text == "only one analysis"
    assert cost == 0.0
    fake_gemini.generate_text.assert_not_called()


def test_synthesize_batch_multiple_calls_gemini(fake_gemini: MagicMock) -> None:
    fake_gemini.generate_text.return_value = GeminiResponse(
        text="### 1. 全体サマリ\n3本に共通する勝ち筋",
        input_tokens=2000,
        output_tokens=300,
        cost_usd=0.0005,
        model_id="gemini-2.5-flash",
        latency_ms=3000,
    )
    skill = VideoAnalysisSkill(gemini=fake_gemini)
    text, cost = skill.synthesize_batch(["a", "b", "c"], SkillContext())
    assert "全体サマリ" in text
    assert cost == pytest.approx(0.0005)
    fake_gemini.generate_text.assert_called_once()
