"""エンゲージ率を百分率ポイントで一貫させる回帰テスト。"""

from __future__ import annotations

import math

import pytest

from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

_POST = {
    "id": "p0001",
    "rank_display": 1,
    "url": "https://www.tiktok.com/@u/video/1",
    "account_id": "u",
    "account_name": "U",
    "followers": 100,
    "title": "テスト動画",
    "plays": 205400,
    "likes": 5000,
    "shares": 500,
    "comments": 300,
    "saves": 206,
    "eg_rate": 2.924,
}


def test_posts_to_metas_keeps_percent_unit() -> None:
    meta = VideoAlgorithmSkill()._posts_to_metas([_POST])[0]
    assert meta.engagement_rate == pytest.approx(2.924)


def test_posts_to_metas_defaults_missing_rate_to_zero() -> None:
    post = {key: value for key, value in _POST.items() if key != "eg_rate"}
    meta = VideoAlgorithmSkill()._posts_to_metas([post])[0]
    assert meta.engagement_rate == 0.0


def test_search_scraper_path_converts_fraction_to_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.adapters import tiktok_scraper as scraper

    video = scraper.TikTokVideo(
        id="1",
        url="https://t/1",
        desc="新宿 ランチ",
        create_time=0,
        duration=18,
        cover_url="",
        author=scraper.TikTokAuthor(unique_id="u", nickname="U", follower_count=10),
        play_count=100000,
        digg_count=2000,
        comment_count=500,
        share_count=224,
        collect_count=200,
        hashtags=(),
        music_title="",
    )
    result = scraper.TikTokSearchResult(query="q", search_type="keyword", videos=(video,))
    monkeypatch.setattr(scraper, "search_tiktok", lambda *args, **kwargs: result)

    metas = VideoAlgorithmSkill()._search("新宿 ランチ", 1, "req")

    assert metas[0].engagement_rate == pytest.approx(2.92)


def test_percent_rate_renders_without_another_conversion() -> None:
    meta = VideoAlgorithmSkill()._posts_to_metas([_POST])[0]
    out = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[AnalyzedVideo(meta=meta, analysis=VideoVSEOAnalysis(duration_sec=18))],
    )
    out.cross = cross_analyze(out.videos, out.query)

    report = render_report(out)

    assert "<b>2.9%</b><i>エンゲージ</i>" in report


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (-1.0, 0.0),
        (math.nan, 0.0),
        (math.inf, 0.0),
        (-math.inf, 0.0),
    ],
)
def test_engagement_rate_invalid_values_are_warned_and_normalized(
    raw: float, expected: float
) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        meta = VideoMeta(engagement_rate=raw)

    assert meta.engagement_rate == expected
    assert [log["event"] for log in logs] == ["video_meta_engagement_rate_clamped"]


def test_engagement_rate_above_100_is_warned_but_preserved() -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        meta = VideoMeta(engagement_rate=292.4)

    assert meta.engagement_rate == 292.4
    assert [log["event"] for log in logs] == ["video_meta_engagement_rate_above_100"]


def test_engagement_rate_boundaries_and_normal_value_pass_through() -> None:
    assert VideoMeta(engagement_rate=0).engagement_rate == 0.0
    assert VideoMeta(engagement_rate=100).engagement_rate == 100.0
    assert VideoMeta(engagement_rate=2.924).engagement_rate == pytest.approx(2.924)
