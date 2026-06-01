"""TikTokSearchSkill の単体テスト (searcher と Gemini をモック)。

実ブラウザ / Node / google-genai を起動せずに、検索結果 → 整形 → 横断分析の
配線を検証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.adapters.tiktok_scraper import (
    TikTokAuthor,
    TikTokScrapeError,
    TikTokSearchResult,
    TikTokVideo,
)
from teamagent.skills.base import SkillContext
from teamagent.skills.tiktok_search.schema import TikTokSearchInput
from teamagent.skills.tiktok_search.skill import TikTokSearchSkill


def _video(vid: str, plays: int, likes: int) -> TikTokVideo:
    return TikTokVideo(
        id=vid,
        url=f"https://www.tiktok.com/@u/video/{vid}",
        desc=f"動画{vid}の説明 #新宿グルメ",
        create_time=1764061063,
        duration=20,
        cover_url="",
        author=TikTokAuthor(unique_id="u", nickname="U", follower_count=10000),
        play_count=plays,
        digg_count=likes,
        comment_count=10,
        share_count=5,
        collect_count=50,
        hashtags=("新宿グルメ", "新宿ランチ"),
        music_title="曲",
    )


def _fake_result(n: int = 3) -> TikTokSearchResult:
    return TikTokSearchResult(
        query="新宿 ランチ",
        search_type="keyword",
        videos=tuple(_video(str(i), 100000 * (i + 1), 1000 * (i + 1)) for i in range(n)),
    )


@pytest.fixture
def fake_gemini() -> MagicMock:
    mock = MagicMock()
    mock.generate_text.return_value = GeminiResponse(
        text="### 1. この検索結果のサマリ\n新宿グルメは破格訴求が強い",
        input_tokens=2000,
        output_tokens=300,
        cost_usd=0.0005,
        model_id="gemini-2.5-flash",
        latency_ms=3000,
    )
    return mock


def test_search_returns_videos_and_analysis(fake_gemini: MagicMock) -> None:
    searcher = MagicMock(return_value=_fake_result(3))
    skill = TikTokSearchSkill(gemini=fake_gemini, searcher=searcher)
    out = skill.run(TikTokSearchInput(query="新宿 ランチ", max_videos=10), ctx=SkillContext())

    assert out.query == "新宿 ランチ"
    assert out.count == 3
    assert out.videos[0].rank == 1
    assert out.videos[0].author == "u"
    assert out.videos[0].play_count == 100000
    assert "サマリ" in (out.analysis or "")
    assert out.model_id == "gemini-2.5-flash"
    assert out.total_cost_usd == pytest.approx(0.0005)
    # searcher が正しい引数で呼ばれた
    kwargs = searcher.call_args.kwargs
    assert kwargs["search_type"] == "keyword"
    assert kwargs["max_videos"] == 10


def test_search_passes_hashtag_type(fake_gemini: MagicMock) -> None:
    searcher = MagicMock(return_value=_fake_result(2))
    skill = TikTokSearchSkill(gemini=fake_gemini, searcher=searcher)
    skill.run(TikTokSearchInput(query="新宿", search_type="hashtag"), ctx=SkillContext())
    assert searcher.call_args.kwargs["search_type"] == "hashtag"


def test_analyze_false_skips_gemini(fake_gemini: MagicMock) -> None:
    searcher = MagicMock(return_value=_fake_result(3))
    skill = TikTokSearchSkill(gemini=fake_gemini, searcher=searcher)
    out = skill.run(TikTokSearchInput(query="新宿 ランチ", analyze=False), ctx=SkillContext())
    assert out.count == 3
    assert out.analysis is None
    fake_gemini.generate_text.assert_not_called()


def test_empty_result_skips_analysis(fake_gemini: MagicMock) -> None:
    searcher = MagicMock(return_value=TikTokSearchResult("x", "keyword", ()))
    skill = TikTokSearchSkill(gemini=fake_gemini, searcher=searcher)
    out = skill.run(TikTokSearchInput(query="x"), ctx=SkillContext())
    assert out.count == 0
    assert out.analysis is None
    fake_gemini.generate_text.assert_not_called()


def test_searcher_error_propagates(fake_gemini: MagicMock) -> None:
    searcher = MagicMock(side_effect=TikTokScrapeError("TIKTOK_EMPTY_RESULT: captcha"))
    skill = TikTokSearchSkill(gemini=fake_gemini, searcher=searcher)
    with pytest.raises(TikTokScrapeError, match="TIKTOK_EMPTY_RESULT"):
        skill.run(TikTokSearchInput(query="x"), ctx=SkillContext())


def test_analysis_prompt_includes_metrics(fake_gemini: MagicMock) -> None:
    searcher = MagicMock(return_value=_fake_result(2))
    skill = TikTokSearchSkill(gemini=fake_gemini, searcher=searcher)
    skill.run(TikTokSearchInput(query="新宿 ランチ"), ctx=SkillContext())
    prompt = fake_gemini.generate_text.call_args.args[0]
    assert "新宿 ランチ" in prompt
    assert "再生" in prompt  # メタデータが prompt に整形されている
    assert "@u" in prompt
