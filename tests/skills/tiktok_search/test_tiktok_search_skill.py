"""TikTokSearchSkill の単体テスト (searcher と Gemini をモック)。

実ブラウザ / Node / google-genai を起動せずに、検索結果 → 整形 → 横断分析の
配線を検証する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.adapters.report_publish import PublishedObject
from teamagent.adapters.tiktok_scraper import (
    TikTokAuthor,
    TikTokScrapeError,
    TikTokSearchResult,
    TikTokVideo,
    _parse_media_post,
    _parse_video,
)
from teamagent.mcp_gateway import payload_offload
from teamagent.mcp_gateway import server as mcp_server
from teamagent.media.operations import _post_row
from teamagent.orchestrator.tools import tool_from_skill
from teamagent.skills.base import SkillContext
from teamagent.skills.tiktok_search.schema import TikTokSearchInput
from teamagent.skills.tiktok_search.skill import TikTokSearchSkill


@pytest.fixture(autouse=True)
def fake_report_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teamagent.skills.tiktok_search.skill.html_reports_enabled", lambda _: False
    )
    monkeypatch.setattr("teamagent.skills.tiktok_search.skill.publish_report", lambda *a, **k: None)


def _video(vid: str, plays: int, likes: int) -> TikTokVideo:
    return TikTokVideo(
        id=vid,
        url=f"https://www.tiktok.com/@u/video/{vid}",
        desc=f"動画{vid}の説明 #新宿グルメ",
        create_time=1764061063,
        duration=20,
        author=TikTokAuthor(unique_id="u", nickname="U", follower_count=10000),
        play_count=plays,
        digg_count=likes,
        comment_count=10,
        share_count=5,
        collect_count=50,
        hashtags=("新宿グルメ", "新宿ランチ"),
        music_title="曲",
        cover_url=f"https://cdn/{vid}.jpg",
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
    assert out.videos[0].cover_url == "https://cdn/0.jpg"  # サムネが skill 出力まで通る
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


@pytest.mark.parametrize("via_worker", [False, True], ids=["browser", "media-worker"])
@pytest.mark.parametrize("max_videos", [10, 3])
@pytest.mark.parametrize("offload", [False, True])
async def test_tiktok_search_slack_summary_reaches_gateway(
    monkeypatch: pytest.MonkeyPatch,
    fake_gemini: MagicMock,
    via_worker: bool,
    max_videos: int,
    offload: bool,
) -> None:
    """実取得境界の欠損 → 10本の機械生成文 → MCP 返却を外部 I/O 無しで通す。"""
    videos = []
    for i in range(1, 11):
        stats = {
            "playCount": 100000,
            "diggCount": 1000,
            "collectCount": 50,
            "commentCount": 10,
            "shareCount": 5,
        }
        if i == 2:
            stats = dict.fromkeys(stats, 0)  # 実測の 0 は欠損と区別する。
        raw = {
            "id": str(i),
            "url": f"https://www.tiktok.com/@u/video/{i}",
            "author": {"uniqueId": "u"},
            "stats": stats,
        }
        if i == 3:
            raw["stats"] = {"playCount": None, "commentCount": 10, "shareCount": 5}
        if i == 4:
            # ブラウザが互換用の 0 と欠損フラグを返すケース。
            stats["commentCount"] = 0
            raw["missingFields"] = ["stats.commentCount"]
        videos.append(
            _parse_media_post(_post_row(raw, "ミールキット", i, str(i), "keyword"))
            if via_worker
            else _parse_video(raw)
        )

    result = TikTokSearchResult("ミールキット", "keyword", tuple(videos))
    skill = TikTokSearchSkill(gemini=fake_gemini, searcher=MagicMock(return_value=result))
    report_url = "https://reports.example.test/tiktok?signature=abc%2Bdef"
    publish = MagicMock(return_value=report_url)
    monkeypatch.setattr("teamagent.skills.tiktok_search.skill.publish_report", publish)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return datetime(2026, 11, 9, 15, 2, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("teamagent.skills.tiktok_search.skill.datetime", FrozenDatetime)
    monkeypatch.setattr(mcp_server, "_record_usage", MagicMock())
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "1" if offload else "0")
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "example.test")
    monkeypatch.setenv("PAYLOAD_OFFLOAD_MAX_CHARS", "1000")
    monkeypatch.setenv("PAYLOAD_OFFLOAD_FIELD_CHARS", "50")
    monkeypatch.setattr(
        "teamagent.adapters.report_publish.publish_text_result",
        lambda *a, **k: PublishedObject(
            url="https://reports.example.test/raw", bucket="fake", key="fake", region="fake"
        ),
    )
    monkeypatch.setattr(payload_offload, "_short_url", lambda *a, **k: None)
    spec = tool_from_skill(TikTokSearchSkill, factory=lambda: skill)
    contents = await mcp_server.dispatch_tool(
        {spec.name: spec},
        "tiktok_search",
        {"query": "ミールキット", "max_videos": max_videos},
        require_rls=False,
    )
    data = json.loads(contents[0].text)
    lines = data["slack_summary"].splitlines()
    assert lines[0] == (
        f"📊 動画ごとの数字（TikTok「ミールキット」検索 上位 {max_videos} 本・2026-11-10 JST）"
    )
    expected_metrics = [
        "再生 100,000・いいね 1,000・保存 50・ENG 1.1%",
        "再生 0・いいね 0・保存 0・ENG 0.0%",
        "再生 —・いいね —・保存 —・ENG —",
        "再生 100,000・いいね 1,000・保存 50・ENG —",
        *["再生 100,000・いいね 1,000・保存 50・ENG 1.1%"] * 6,
    ]
    expected_rows = [
        f"{i}. @u｜{metrics}｜<https://www.tiktok.com/@u/video/{i}|動画を見る>"
        for i, metrics in enumerate(expected_metrics[:max_videos], 1)
    ]
    analysis = fake_gemini.generate_text.return_value.text
    assert lines[1 : max_videos + 1] == expected_rows
    assert data["slack_summary"] == (
        "\n".join([lines[0], *expected_rows])
        + f"\n\n\n{analysis}\n📎 詳細レポート: <{report_url}|7日有効リンク>"
    )
    assert data["count"] == max_videos
    assert data.get("offloaded", False) is offload
    if not offload:
        assert len(data["videos"]) == max_videos
    assert data["report_url"] == report_url
    publish.assert_called_once()
    fake_gemini.generate_text.assert_called_once()


def test_tiktok_search_slack_summary_truncates_only_analysis(
    monkeypatch: pytest.MonkeyPatch, fake_gemini: MagicMock
) -> None:
    fake_gemini.generate_text.return_value = GeminiResponse(
        text="分析" * 301,
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        model_id="fake",
        latency_ms=0,
    )
    monkeypatch.setattr(
        "teamagent.skills.tiktok_search.skill.publish_report",
        lambda *a, **k: "https://reports.example.test/report",
    )
    skill = TikTokSearchSkill(gemini=fake_gemini, searcher=MagicMock(return_value=_fake_result(10)))
    out = skill.run(TikTokSearchInput(query="新宿 ランチ"), SkillContext())
    assert out.analysis == "分析" * 301
    assert out.slack_summary.endswith(
        "\n\n\n"
        + "分析" * 300
        + "\n📎 詳細レポート: <https://reports.example.test/report|7日有効リンク>"
    )
    assert out.slack_summary.count("|動画を見る>") == 10
