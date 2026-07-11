"""エンゲージ率の単位（%）一貫性のリグレッション（審査所見R1）。

VideoMeta.engagement_rate は%単位（2.9%→2.9）が正。S3経路（eg_rate=既に%）の /100
二重換算と、scraper経路（小数 0.029）の未換算で、表示が 1/100・100倍に狂うバグを固定する:
p0001 実データ相当 eg_rate=2.924 → VideoMeta.engagement_rate==2.924 → レポート整形 "2.9%"。
"""

from __future__ import annotations

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

# p0001 実データ相当（tiktok_acquire posts.normalized.json item・eg_rate は%単位）
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
    """eg_rate は既に%。/100 で 0.029(%) に潰さない（審査所見R1: 二重換算の除去）。"""
    m = VideoAlgorithmSkill()._posts_to_metas([_POST])[0]
    assert m.engagement_rate == pytest.approx(2.924)


def test_search_scraper_path_converts_fraction_to_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tiktok_scraper.engagement_rate は小数（0.0292）なので ×100 で%に揃える（審査所見R1）。"""
    from teamagent.adapters import tiktok_scraper as sc

    video = sc.TikTokVideo(
        id="1",
        url="https://t/1",
        desc="新宿 ランチ",
        create_time=0,
        duration=18,
        cover_url="",
        author=sc.TikTokAuthor(unique_id="u", nickname="U", follower_count=10),
        play_count=100000,
        digg_count=2000,
        comment_count=500,
        share_count=224,
        collect_count=200,  # eng計 2924 / 100000 = 0.0292（小数）
        hashtags=(),
        music_title="",
    )
    result = sc.TikTokSearchResult(query="q", search_type="keyword", videos=(video,))
    monkeypatch.setattr(sc, "search_tiktok", lambda *a, **k: result)
    metas = VideoAlgorithmSkill()._search("新宿 ランチ", 1, "req")
    assert metas[0].engagement_rate == pytest.approx(2.92)  # %単位に揃う


def test_p0001_eg_rate_renders_as_percent_in_report() -> None:
    """p0001 相当（eg_rate=2.924）が個別KPIで "2.9%" と整形される（%前提の描画と整合）。"""
    m = VideoAlgorithmSkill()._posts_to_metas([_POST])[0]
    assert m.engagement_rate == pytest.approx(2.924)
    out = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[AnalyzedVideo(meta=m, analysis=VideoVSEOAnalysis(duration_sec=18))],
    )
    out.cross = cross_analyze(out.videos, "新宿 ランチ")
    html = render_report(out)
    assert "<b>2.9%</b><i>エンゲージ</i>" in html  # /100 が残ると 0.0% になり検出できる


def test_engagement_rate_validator_clamps_and_warns() -> None:
    """%としてあり得ない値（負値/100超=単位取り違えの疑い）は警告つきで clamp（審査所見R1）。"""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        over = VideoMeta(engagement_rate=292.4)  # ×100 二重適用相当
        neg = VideoMeta(engagement_rate=-1.0)
    assert over.engagement_rate == 100.0
    assert neg.engagement_rate == 0.0
    events = [log["event"] for log in logs]
    assert events.count("video_meta_engagement_rate_clamped") == 2  # サイレント補正にしない
    # 正常域（%単位）は素通し
    assert VideoMeta(engagement_rate=2.924).engagement_rate == pytest.approx(2.924)
