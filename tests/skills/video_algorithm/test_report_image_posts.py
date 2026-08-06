"""VSEO HTML レポートの画像投稿タブ。"""

from __future__ import annotations

import pytest

from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)


def _video(rank: int) -> AnalyzedVideo:
    return AnalyzedVideo(
        meta=VideoMeta(
            rank=rank,
            url=f"https://www.tiktok.com/@video{rank}/video/{rank}",
            author=f"video{rank}",
            play_count=100_000 - rank,
            duration_sec=18.0,
        ),
        analysis=VideoVSEOAnalysis(duration_sec=18.0, main_message=f"動画{rank}"),
    )


def _image(rank: int, *, cover_url: str | None = None) -> VideoMeta:
    return VideoMeta(
        rank=rank,
        url=f"https://www.tiktok.com/@photo{rank}/photo/{rank}",
        author=f"photo{rank}",
        follower_count=12_345,
        desc="1行目のキャプション\n省略されない2行目",
        play_count=20_000,
        digg_count=1_200,
        collect_count=400,
        engagement_rate=8.5,
        cover_url=cover_url or f"https://cdn.example.com/{rank}.jpg",
        duration_sec=0.0,
    )


def _output(*image_posts: VideoMeta) -> VideoAlgorithmOutput:
    # rank=3 の画像投稿があっても、深掘り動画は従来どおり5本を保持する。
    videos = [_video(rank) for rank in (1, 2, 4, 5, 6)]
    board_by_rank = {video.meta.rank: video.meta for video in videos}
    board_by_rank.update({meta.rank: meta for meta in image_posts})
    return VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=videos,
        board=[board_by_rank[rank] for rank in sorted(board_by_rank)],
    )


def test_image_post_tab_is_added_only_within_default_top_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_ALGO_IMAGE_POST_TOP_N", raising=False)
    report = render_report(_output(_image(3), _image(7)))

    # 統計1 + 動画5 + 上位N内の画像1。rank=7 はボードだけでタブには出さない。
    assert report.count('<button class="toptab') == 7
    assert report.count('class="toptab imageposttab"') == 1
    assert "📷 #3</button>" in report
    assert "📷 #7</button>" not in report
    assert 'src="https://cdn.example.com/3.jpg"' in report
    assert "@photo3" in report and "1.2万" in report
    assert "2.0万" in report and "1.2K" in report and "400" in report
    assert "2.00%" in report and "8.5%" in report
    assert "1行目のキャプション\n省略されない2行目" in report
    assert (
        "この投稿は画像投稿（カルーセル）のため、動画の深掘り分析"
        "（テロップ・フック・カメラワーク）は行っていません。"
    ) in report

    # タブ対象外を含む画像投稿は、ボード上で深掘り対象外の理由が分かる。
    assert report.count('title="画像投稿（動画深掘り対象外）">📷</span>') == 2
    assert "📷＝画像投稿（動画深掘り対象外）" in report


def test_image_post_tab_respects_configured_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "2")
    report = render_report(_output(_image(3)))

    assert 'class="toptab imageposttab"' not in report
    assert report.count('title="画像投稿（動画深掘り対象外）">📷</span>') == 1


def test_image_post_feature_off_preserves_legacy_html_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_output = _output(_image(3))
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "0")
    disabled = render_report(image_output)

    # duration_sec は従来 HTML に出ないため、動画メタとして扱った出力が旧形式の基準になる。
    legacy_board = [
        meta.model_copy(update={"duration_sec": 18.0}) if meta.rank == 3 else meta
        for meta in image_output.board
    ]
    legacy_output = image_output.model_copy(update={"board": legacy_board})
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "5")
    legacy = render_report(legacy_output)

    assert disabled == legacy
    assert "imageposttab" not in disabled
    assert "sbimage" not in disabled


def test_video_only_input_is_byte_identical_when_feature_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _output()
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "0")
    disabled = render_report(output)
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "5")
    enabled = render_report(output)

    assert enabled == disabled


@pytest.mark.parametrize(
    "cover_url",
    ["javascript:alert(1)", '\"><script>alert(1)</script>'],
)
def test_non_http_image_post_cover_is_not_rendered(
    monkeypatch: pytest.MonkeyPatch, cover_url: str
) -> None:
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "5")
    report = render_report(_output(_image(3, cover_url=cover_url)))

    assert cover_url not in report
    assert '<div class="ipcover ph">' in report


def test_http_image_post_cover_is_html_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "5")
    cover_url = 'https://cdn.example.com/x.jpg?q=\"><script>alert(1)</script>'
    report = render_report(_output(_image(3, cover_url=cover_url)))

    assert cover_url not in report
    assert "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in report


def test_image_tab_does_not_consume_deep_analysis_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "5")
    output = _output(_image(3))
    original_ranks = [video.meta.rank for video in output.videos]

    report = render_report(output)

    assert [video.meta.rank for video in output.videos] == original_ranks == [1, 2, 4, 5, 6]
    assert report.count('class="toptab" type="button" data-tt="v') == 5
    assert report.count('class="toptab imageposttab"') == 1
    assert "取得6本・深掘り分析5本" in report
