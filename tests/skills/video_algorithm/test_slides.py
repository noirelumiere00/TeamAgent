"""§Q-HTML→PPTX: 要点スライドHTML生成（slides.render_slides）の純関数テスト（外部I/O無し）。

不変条件: 16:9 section が出る／contenteditable で編集可／**動画base64(data:video)を絶対に載せない**
（提案資料は軽量であるべき）。空セクションは描画しない。
"""

from __future__ import annotations

from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CrossSynthesis,
    ThumbColor,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
    WinFactor,
    WinHypothesis,
)
from teamagent.skills.video_algorithm.slides import SLIDE_H, SLIDE_W, render_slides


def _rich_out() -> VideoAlgorithmOutput:
    """videos+cross+synthesis が揃った提案向けの出力（全スライドが埋まる）。"""
    vids = [
        AnalyzedVideo(
            meta=VideoMeta(
                rank=i,
                url=f"https://t/{i}",
                author=f"user{i}",
                play_count=100000 * i,
                collect_count=1500,
            ),
            analysis=VideoVSEOAnalysis(duration_sec=18, hook_type="question"),
            # 軽量サムネは載せる・動画プレビューは"絶対に"載せない（混入検査用に値は入れておく）
            cover_data_uri="data:image/jpeg;base64,COVER",
            video_data_uri="data:video/mp4;base64,SHOULD_NOT_APPEAR",
            thumb=ThumbColor(swatches=["#e8c8a0"], brightness01=0.72, warmth=0.3),
        )
        for i in (1, 2, 3)
    ]
    syn = CrossSynthesis(
        headline="冒頭3秒のテロップ焼き込みが勝ち筋",
        strategy="検索KWをテロップ・音声・キャプの3層に通すと検索適合が安定する。",
        creative_brief=["冒頭3秒にKWテロップ", "尺は18秒前後", "保存CTAを1つ"],
        posting_design="保存導線を固定文で1つ入れる",
        client_pitch="この検索面はテロップ設計で獲れます",
        win_hypotheses=[
            WinHypothesis(
                hypothesis="KWテロップ焼き込みが入賞の必要条件",
                supported_by=[1, 2, 3],
                confidence="中",
                so_what="まず2本テストする",
            )
        ],
    )
    out = VideoAlgorithmOutput(query="新宿 ランチ", videos=vids)
    out.cross.video_count = 3
    out.cross.win_factors = [
        WinFactor(factor="テロップにKW", observed_in=3, total=3, confidence="高")
    ]
    out.cross.thumb_consensus = "暖色・高明度で揃っている"
    out.cross.synthesis = syn
    return out


def test_render_slides_has_16x9_sections_and_editable() -> None:
    html = render_slides(_rich_out(), generated_at="2026-06-13")
    assert html.startswith("<!doctype html>")
    assert html.count('class="slide ') == 7  # 全7スライドが埋まる
    # 16:9 の論理サイズ（CSS変数 --w/--h と @page size に出る）
    assert f"--w:{SLIDE_W}px" in html and f"--h:{SLIDE_H}px" in html
    assert f"size:{SLIDE_W}px {SLIDE_H}px" in html
    assert "contenteditable" in html  # 営業がブラウザで直接編集できる
    assert "新宿 ランチ" in html and "冒頭3秒のテロップ焼き込みが勝ち筋" in html


def test_render_slides_never_embeds_video_base64() -> None:
    """🔴 提案スライドに数MBの動画base64を載せない（report.htmlと違い軽量）。"""
    html = render_slides(_rich_out())
    assert "data:video" not in html
    assert "SHOULD_NOT_APPEAR" not in html
    assert "data:image/jpeg;base64,COVER" in html  # 軽量サムネは載る


def test_render_slides_skips_empty_sections() -> None:
    """synthesis 無し・動画無しなら、空セクションは描画しない（表紙/結論など最小限に）。"""
    out = VideoAlgorithmOutput(query="空のKW")
    html = render_slides(out)
    n = html.count('class="slide ')
    assert 1 <= n < 7  # 少なくとも表紙はあるが7枚は埋まらない
    assert "data:video" not in html


def test_render_slides_small_sample_warning() -> None:
    """n<3 は断定でなく観測仮説の注意を結論スライドに出す（誠実さ）。"""
    out = VideoAlgorithmOutput(
        query="kw",
        videos=[
            AnalyzedVideo(
                meta=VideoMeta(rank=1, url="https://t/1", play_count=1000),
                analysis=VideoVSEOAnalysis(duration_sec=15),
            )
        ],
    )
    out.cross.video_count = 1
    html = render_slides(out)
    assert "観測仮説" in html
