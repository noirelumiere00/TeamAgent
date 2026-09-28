"""動画分析の HTML レポート／提案スライドがデジタル庁デザインシステム（DADS）に沿うことの固定。

見た目だけの変更（構造・文言・データは不変）なので、ここでは CSS の約束事を検査する:
- 色は DADS トークン経由（レポート固有の CSS に hex の直書きが無い）。
- 薄くて読めない文字色（#9aa3ad・#c4ccd4）が無い。本文 16px・14px 未満の文字指定が無い。
- 出典（DADS_CREDIT）をフッタに出す。
- スライドは撮影サイズ（1280x720）と文字サイズを据え置き、配色だけ DADS に寄せる。
"""

from __future__ import annotations

import re

from teamagent.skills._html.dads import DADS_BASE_CSS, DADS_CREDIT, DADS_TOKENS_CSS
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    BrandDetection,
    CrossSynthesis,
    FrameShot,
    TelopItem,
    ThumbColor,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
    WinHypothesis,
)
from teamagent.skills.video_algorithm.slides import SLIDE_H, SLIDE_W, render_slides

_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_FONT_PX = re.compile(r"font(?:-size)?:\s*(?:[0-9]+\s+)?([0-9]+(?:\.[0-9]+)?)px")


def _out() -> VideoAlgorithmOutput:
    vids = [
        AnalyzedVideo(
            meta=VideoMeta(
                rank=i,
                url=f"https://t/{i}",
                author=f"u{i}",
                play_count=100000 // i,
                collect_count=1500,
            ),
            analysis=VideoVSEOAnalysis(
                duration_sec=18,
                hook_type="question",
                telops=[TelopItem(sec=1, text="新宿", kw_match=True)],
                brand_detections=[
                    BrandDetection(
                        brand_name="しまむら", appear_sec=[8.0], brand_relation="competitor"
                    )
                ],
            ),
            frames=[FrameShot(sec=1.0, caption="フック", data_uri="data:image/jpeg;base64,AAAA")],
            cover_data_uri="data:image/jpeg;base64,BBBB",
            thumb=ThumbColor(swatches=["#e8c8a0"], brightness01=0.72, warmth=0.3),
        )
        for i in (1, 2, 3)
    ]
    out = VideoAlgorithmOutput(query="新宿 ランチ", videos=vids, board=[v.meta for v in vids])
    out.cross = cross_analyze(out.videos, "新宿 ランチ")
    out.cross.synthesis = CrossSynthesis(
        headline="価格×ボリュームで勝つ",
        client_pitch="値ごろ感で攻める",
        win_hypotheses=[WinHypothesis(hypothesis="価格テロップ型", supported_by=[1, 2])],
    )
    return out


def _style(html: str) -> str:
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


def _own_css(style: str) -> str:
    """DADS の共通部品（トークン・基本部品）を除いた、このレポート固有の CSS。"""
    return style.replace(DADS_TOKENS_CSS, "").replace(DADS_BASE_CSS, "")


def test_report_uses_dads_tokens_and_credit() -> None:
    html = render_report(_out())
    style = _style(html)
    assert DADS_TOKENS_CSS in style and DADS_BASE_CSS in style
    assert "--color-primitive-blue-900" in style
    assert "var(--color-primitive-blue-900)" in _own_css(style)  # キーカラー
    assert DADS_CREDIT in html  # 出典（MIT・デジタル庁）をフッタに
    assert "<footer class='dads-footnote'>" in html and "相関≠因果" in html


def test_report_has_no_hardcoded_or_faint_colors() -> None:
    style = _style(render_report(_out()))
    for faint in ("#9aa3ad", "#c4ccd4"):  # 白地・黒地で読めない薄い文字色
        assert faint not in style.lower()
    assert _HEX.findall(_own_css(style)) == []  # 色はすべて DADS トークン経由


def test_report_text_is_16px_body_and_never_below_14px() -> None:
    style = _style(render_report(_out()))
    assert "font-size:16px;line-height:1.7" in style  # DADS 本文
    sizes = [float(x) for x in _FONT_PX.findall(_own_css(style))]
    assert sizes, "font-size 指定を1つも拾えていない（正規表現の前提崩れ）"
    assert min(sizes) >= 14, sorted(sizes)[:5]
    assert "font-size:13px" not in style


def test_report_image_post_style_also_uses_tokens(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "5")
    out = _out()
    out.board.append(
        VideoMeta(rank=4, url="https://t/p4", author="p4", duration_sec=0.0, cover_url=None)
    )
    style = _style(render_report(out))
    assert ".imagepostpane" in style
    assert _HEX.findall(_own_css(style)) == []
    assert min(float(x) for x in _FONT_PX.findall(_own_css(style))) >= 14


def test_report_scrolls_wide_parts_inside_their_frame_on_phone() -> None:
    """390px 幅でページ全体が横にはみ出さないよう、幅の要る部品は枠内スクロールにする。"""
    own = _own_css(_style(render_report(_out())))
    for sel in (".board{", ".tbrow{", ".nle{"):
        rule = own.split(sel, 1)[1].split("}", 1)[0]
        assert "overflow-x:auto" in rule, sel
    assert "table.tbl{display:block;overflow-x:auto}" in own


def test_slides_keep_size_and_move_colors_to_dads() -> None:
    html = render_slides(_out(), generated_at="2026-09-28")
    style = _style(html)
    assert f"--w:{SLIDE_W}px" in style and f"--h:{SLIDE_H}px" in style
    assert f"size:{SLIDE_W}px {SLIDE_H}px" in style
    assert "--accent:var(--color-primitive-blue-900)" in style
    assert "#e8362f" not in html.lower()  # 旧・赤アクセント
    assert _HEX.findall(style.replace(DADS_TOKENS_CSS, "")) == []
    # 本文サイズは撮影前提で据え置き（lead 21px・タイトル 38px）
    assert ".lead{font-size:21px" in style and "font-size:38px" in style
