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


@pytest.fixture
def fake_gemini() -> MagicMock:
    mock = MagicMock()
    mock.analyze_video_url.return_value = GeminiResponse(
        text="### 1. 一行サマリ\n冒頭1秒の衝撃ビジュアルで離脱を防ぐ [0:00]",
        input_tokens=5000,
        output_tokens=400,
        cost_usd=0.00099,
        model_id="gemini-2.5-flash",
        latency_ms=8000,
    )
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
