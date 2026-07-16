"""小標本・基準率・相関・保存率の統計的誠実性を固定する。"""

from __future__ import annotations

import re

from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    TelopItem,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)


def _analyzed_video(
    rank: int,
    *,
    kw_telop: bool = True,
    duration: float = 18,
    saves: int = 1500,
    plays: int = 100000,
    desc: str = "新宿 ランチ 名店",
) -> AnalyzedVideo:
    return AnalyzedVideo(
        meta=VideoMeta(
            rank=rank,
            url=f"https://t/{rank}",
            desc=desc,
            play_count=plays,
            collect_count=saves,
            engagement_rate=8.0,
        ),
        analysis=VideoVSEOAnalysis(
            duration_sec=duration,
            hook_type="question",
            telops=[TelopItem(sec=1, text="新宿", kw_match=kw_telop)],
        ),
    )


def test_zero_analyzed_videos_returns_safe_empty_result() -> None:
    cross = cross_analyze([], "新宿 ランチ", board=[])

    assert cross.video_count == 0
    assert cross.win_factors == []
    assert cross.rank_diff_drivers == []
    assert cross.stats is None
    assert "分析できた動画がありません" in cross.summary


def test_missing_analysis_is_excluded_without_crashing() -> None:
    missing = AnalyzedVideo(meta=VideoMeta(rank=1, desc="新宿 ランチ"), analysis=None)
    present = _analyzed_video(2)

    cross = cross_analyze([missing, present], "新宿 ランチ")

    assert cross.video_count == 1
    assert cross.stats is not None
    assert cross.stats.sample_size == 1
    assert cross.rank_diff_drivers == []


def test_kw_caption_winfactor_is_gated_by_board_base_rate() -> None:
    videos = [_analyzed_video(rank) for rank in range(1, 6)]
    board = [
        VideoMeta(rank=rank, url=f"https://t/{rank}", desc="新宿 ランチ") for rank in range(1, 21)
    ]

    cross = cross_analyze(videos, "新宿 ランチ", board=board)

    assert not any("キャプション本文" in factor.factor for factor in cross.win_factors)


def test_kw_caption_winfactor_is_kept_when_lift_is_high() -> None:
    videos = [_analyzed_video(rank) for rank in range(1, 6)]
    board = [
        VideoMeta(
            rank=rank,
            url=f"https://t/{rank}",
            desc="新宿 ランチ" if rank <= 4 else "無関係な動画",
        )
        for rank in range(1, 21)
    ]

    cross = cross_analyze(videos, "新宿 ランチ", board=board)

    assert any("キャプション本文" in factor.factor for factor in cross.win_factors)


def test_board_is_optional_for_backward_compatibility() -> None:
    videos = [_analyzed_video(rank) for rank in range(1, 6)]

    cross = cross_analyze(videos, "新宿 ランチ")

    assert any("キャプション本文" in factor.factor for factor in cross.win_factors)


def test_rank_diff_drivers_are_silent_under_n8() -> None:
    videos = [
        _analyzed_video(rank, kw_telop=rank <= 2, saves=3000 - rank * 500) for rank in range(1, 6)
    ]

    assert cross_analyze(videos, "新宿 ランチ").rank_diff_drivers == []


def test_rank_diff_drivers_require_n8_and_pooled_two_se() -> None:
    videos = [
        _analyzed_video(rank, kw_telop=rank <= 4, saves=5000 if rank <= 4 else 1000)
        for rank in range(1, 9)
    ]

    drivers = cross_analyze(videos, "新宿 ランチ").rank_diff_drivers

    assert any("テロップ" in driver for driver in drivers)
    assert any("保存率" in driver for driver in drivers)


def test_save_rate_driver_is_rejected_within_two_se() -> None:
    videos = [
        _analyzed_video(rank, saves=2 if rank <= 4 else 1, plays=1000) for rank in range(1, 9)
    ]

    drivers = cross_analyze(videos, "新宿 ランチ").rank_diff_drivers

    assert not any("保存率" in driver for driver in drivers)


def test_equal_save_rates_never_create_a_driver() -> None:
    videos = [_analyzed_video(rank, saves=1500, plays=100000) for rank in range(1, 9)]

    drivers = cross_analyze(videos, "新宿 ランチ").rank_diff_drivers

    assert not any("保存率" in driver for driver in drivers)


def test_zero_play_counts_do_not_create_a_save_rate_driver() -> None:
    videos = [_analyzed_video(rank, saves=0, plays=0) for rank in range(1, 9)]

    drivers = cross_analyze(videos, "新宿 ランチ").rank_diff_drivers

    assert not any("保存率" in driver for driver in drivers)


def test_corrupt_save_rate_does_not_crash_or_exceed_100_percent() -> None:
    videos = [
        _analyzed_video(1, saves=200, plays=0),
        _analyzed_video(2, saves=200, plays=50),
        _analyzed_video(3, saves=200, plays=50),
        _analyzed_video(4, saves=200, plays=50),
        *[_analyzed_video(rank, saves=5, plays=100000) for rank in range(5, 9)],
    ]

    drivers = cross_analyze(videos, "新宿 ランチ").rank_diff_drivers

    assert not any("保存率" in driver for driver in drivers)
    for driver in drivers:
        values = re.findall(r"([0-9]+(?:\.[0-9]+)?)%", driver)
        assert all(float(value) <= 100.0 for value in values)


def test_direction_label_is_suppressed_for_small_n() -> None:
    videos = [_analyzed_video(rank, duration=10 + rank * 4) for rank in range(1, 4)]

    stats = cross_analyze(videos, "新宿 ランチ").stats

    assert stats is not None
    assert all(correlation.direction_label == "" for correlation in stats.correlations)


def test_direction_label_is_suppressed_for_weak_rho() -> None:
    durations = [1, 6, 2, 5, 3, 4]
    videos = [
        _analyzed_video(rank, duration=duration) for rank, duration in enumerate(durations, start=1)
    ]

    stats = cross_analyze(videos, "新宿 ランチ").stats

    assert stats is not None
    duration = next(item for item in stats.correlations if item.feature == "尺(秒)")
    assert duration.rho is not None and abs(duration.rho) < 0.5
    assert duration.direction_label == ""


def test_direction_label_is_shown_for_strong_rho_at_n5() -> None:
    videos = [_analyzed_video(rank, duration=8 + rank * 4) for rank in range(1, 6)]

    stats = cross_analyze(videos, "新宿 ランチ").stats

    assert stats is not None
    duration = next(item for item in stats.correlations if item.feature == "尺(秒)")
    assert duration.rho is not None and abs(duration.rho) >= 0.5
    assert duration.direction_label == "値が小さいほど上位"


def _report_html(sample_size: int) -> str:
    out = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[_analyzed_video(rank) for rank in range(1, sample_size + 1)],
    )
    out.cross = cross_analyze(out.videos, out.query)
    return render_report(out)


def test_report_banner_is_staged_by_sample_size() -> None:
    assert "極小サンプル" in _report_html(2)
    report5 = _report_html(5)
    assert "小サンプル" in report5
    assert "極小サンプル" not in report5
    assert '<div class="nbanner">' not in _report_html(6)


def test_report_renders_generated_statistical_caveats() -> None:
    report = _report_html(5)

    assert '<ul class="caveats">' in report
    assert "生存者バイアス" in report
