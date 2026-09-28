"""動画分析の HTML レポート／提案スライドがデジタル庁デザインシステム（DADS）に沿うことの固定。

見た目だけの変更（構造・文言・データは不変）なので、ここでは CSS の約束事を検査する:
- 色は DADS トークン経由（レポート固有の CSS に hex の直書きが無い）。
- 薄くて読めない文字色（#9aa3ad・#c4ccd4）が無い。本文 16px・14px 未満の文字指定が無い。
- 出典（DADS_CREDIT）をフッタに出す。
- スライドは撮影サイズ（1280x720）と文字サイズを据え置き、配色だけ DADS に寄せる。
- 崩れの固定（レビュー指摘）: 最後の目盛りでタイムラインに横スクロールが出ない・Top5 ボードは
  折り返しても行がそろう・長い作者名ではみ出さない・表は外側の枠でスクロール（table は table のまま）。
  chromium があれば実描画でも測る（CI は chromium 無しで skip）。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from teamagent.skills._html.dads import (
    DADS_BASE_CSS,
    DADS_CREDIT,
    DADS_LICENSE_COMMENT,
    DADS_TOKENS_CSS,
    dads_style,
)
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


def _out(
    duration_sec: float = 18, hook_type: str = "question", author: str = ""
) -> VideoAlgorithmOutput:
    vids = [
        AnalyzedVideo(
            meta=VideoMeta(
                rank=i,
                url=f"https://t/{i}",
                author=author or f"u{i}",
                play_count=100000 // i,
                collect_count=1500,
            ),
            analysis=VideoVSEOAnalysis(
                duration_sec=duration_sec,
                hook_type=hook_type,
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
    for sel in (".board{", ".tbrow{", ".nle{", ".tblwrap{"):
        assert "overflow-x:auto" in _rule(own, sel), sel


def _rule(css: str, selector: str) -> str:
    """`selector{...}` の中身（最初の一致）。"""
    assert selector in css, selector
    return css.split(selector, 1)[1].split("}", 1)[0]


def test_last_ruler_tick_is_pulled_inside_the_timeline() -> None:
    """尺が目盛り間隔の倍数（20s・step 5）だと最後の目盛りが右端 100% に来る。中央寄せのままだと
    ラベルが枠の外へはみ出し、PC 幅でもタイムラインに横スクロールが出る → 右揃えにする。"""
    html = render_report(_out(duration_sec=20))
    ticks = re.findall(r'<span class="(ntick[^"]*)" style="left:([0-9.]+)%">([^<]+)</span>', html)
    assert ticks, "目盛りを拾えていない（マークアップの前提崩れ）"
    per_video = ticks[: len(ticks) // 3]  # 3 本とも 20s なので 1 本分を見る
    assert per_video[-1] == ("ntick nend", "100.00", "00:20.0")
    assert all(cls == "ntick" for cls, _, _ in per_video[:-1])
    own = _own_css(_style(html))
    assert "transform:translateX(-100%)" in _rule(own, ".ntick.nend{")


class _Children(HTMLParser):
    """Top5 ボードの各列（.blab/.bcol）の直下の子要素の数を数える。"""

    _VOID = frozenset({"img", "br", "input", "meta", "link", "hr"})

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.col_depth: int | None = None
        self.counts: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = (dict(attrs).get("class") or "").split()
        if self.col_depth is not None and len(self.stack) == self.col_depth + 1:
            self.counts[-1] += 1
        if self.col_depth is None and ("blab" in cls or "bcol" in cls):
            self.col_depth = len(self.stack)
            self.counts.append(0)
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID or not self.stack:  # ボードより外側の閉じタグは無視
            return
        self.stack.pop()
        if self.col_depth is not None and len(self.stack) == self.col_depth:
            self.col_depth = None


def test_top5_board_rows_align_without_fixed_height() -> None:
    """固定 height だと 2 行に折り返したセルが下の行に重なる。min-height にしたうえで、
    ラベル列と各動画の列を subgrid の同じ 9 行に載せて横一列の高さをそろえる。"""
    html = render_report(_out())
    own = _own_css(_style(html))
    col = _rule(own, ".blab,.bcol{")
    assert "grid-template-rows:subgrid" in col and "grid-row:span 9" in col
    cell = _rule(own, ".blab>div,.bcol>div{")
    assert "min-height:44px" in cell and ";height:" not in cell and not cell.startswith("height:")
    p = _Children()
    p.feed(html[html.index('<div class="board"') :])
    assert p.counts[:4] == [9, 9, 9, 9], p.counts  # ラベル列＋3 本。span 9 と数が合っていること


def _with_image_post(
    out: VideoAlgorithmOutput, monkeypatch, author: str = "p4"
) -> VideoAlgorithmOutput:
    monkeypatch.setenv("VIDEO_ALGO_IMAGE_POST_TOP_N", "5")
    out.board.append(
        VideoMeta(rank=4, url="https://t/p4", author=author, duration_sec=0.0, cover_url=None)
    )
    return out


def test_long_author_names_wrap_instead_of_overflowing(monkeypatch) -> None:
    own = _own_css(_style(render_report(_with_image_post(_out(), monkeypatch))))
    for sel in (".toptab{", ".vphead a{", ".ipauthor{"):
        rule = _rule(own, sel)
        assert "min-width:0" in rule and "overflow-wrap:anywhere" in rule, sel
    assert "flex:none" in _rule(own, ".toptab .ttrank{")  # 順位バッジは縦に潰さない


def test_competitor_stripe_has_a_legend_entry() -> None:
    html = render_report(_out())
    legend = html.split('<div class="nlegend">', 1)[1].split("</div>", 1)[0]
    assert '<span class="lg c-brand comp"></span>競合ブランド（縞）' in legend


def test_tables_scroll_in_a_wrapper_and_stay_tables() -> None:
    """table 自体を display:block にすると読み上げで表として扱われなくなる。外側の枠でスクロールする。"""
    html = render_report(_out())
    own = _own_css(_style(html))
    assert "table.tbl{display:block" not in own
    starts = [m.start() for m in re.finditer(r'<table class="tbl', html)]
    assert len(starts) >= 3, len(starts)
    for i in starts:
        assert html[:i].endswith(('<div class="tblwrap">', '<div class="tscroll">')), html[
            i - 60 : i
        ]


_LAYOUT_JS = """
() => {
  const r = {doc: document.documentElement.scrollWidth - innerWidth};
  r.nle = [...document.querySelectorAll('.nle')].filter(e => e.offsetParent)
    .map(e => e.scrollWidth - e.clientWidth);
  r.cellOverflow = [...document.querySelectorAll('.blab>div,.bcol>div')].filter(e => e.offsetParent)
    .filter(e => e.scrollHeight > e.clientHeight + 1).length;
  const cols = [...document.querySelectorAll('.blab,.bcol')].filter(e => e.offsetParent);
  let mis = 0;
  if (cols.length) {
    const ref = [...cols[0].children].map(c => c.getBoundingClientRect());
    for (const col of cols.slice(1)) [...col.children].forEach((c, k) => {
      const b = c.getBoundingClientRect();
      if (Math.abs(b.top - ref[k].top) > 1 || Math.abs(b.height - ref[k].height) > 1) mis++;
    });
  }
  r.misaligned = mis;
  return r;
}
"""


def test_rendered_layout_has_no_overflow(tmp_path, monkeypatch) -> None:
    """実描画: 1280px でタイムラインの枠に横スクロールが出ない。390px でページが横にはみ出さない。
    Top5 ボードは長いフック型が折り返しても重ならず行がそろう。"""
    sync_api = pytest.importorskip("playwright.sync_api")
    f = tmp_path / "r.html"
    long_name = "W" * 24  # TikTok の上限 24 文字・最も幅の広い文字
    out = _out(duration_sec=20, hook_type="ビフォーアフター比較型の長いフック", author=long_name)
    f.write_text(render_report(_with_image_post(out, monkeypatch, long_name)), encoding="utf-8")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # CI には chromium が無い
            pytest.skip(f"chromium を起動できない: {e}")
        try:
            for width in (1280, 390):
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.goto(f.as_uri())
                n_tabs = len(page.query_selector_all(".toptab"))
                for ti in range(n_tabs):
                    page.query_selector_all(".toptab")[ti].click()
                    page.wait_for_timeout(50)
                    r = page.evaluate(_LAYOUT_JS)
                    where = (width, ti, r)
                    assert r["doc"] <= 0, where
                    assert r["cellOverflow"] == 0 and r["misaligned"] == 0, where
                    if width == 1280:
                        assert all(d <= 0 for d in r["nle"]), where
                page.close()
        finally:
            browser.close()


def test_slides_keep_size_and_move_colors_to_dads() -> None:
    html = render_slides(_out(), generated_at="2026-09-28")
    style = _style(html)
    assert f"--w:{SLIDE_W}px" in style and f"--h:{SLIDE_H}px" in style
    assert f"size:{SLIDE_W}px {SLIDE_H}px" in style
    assert "--accent:var(--color-primitive-blue-900)" in style
    assert "#e8362f" not in html.lower()  # 旧・赤アクセント
    assert style.startswith(DADS_LICENSE_COMMENT)  # DADS の値を写すので MIT の出典を残す
    assert _HEX.findall(style.replace(DADS_TOKENS_CSS, "")) == []
    # 本文サイズは撮影前提で据え置き（lead 21px・タイトル 38px）
    assert ".lead{font-size:21px" in style and "font-size:38px" in style


def test_dads_style_keeps_the_license_comment() -> None:
    assert dads_style("").startswith(f"<style>{DADS_LICENSE_COMMENT}{DADS_TOKENS_CSS}")
    assert "MIT License" in DADS_LICENSE_COMMENT and "Digital Agency, Japan" in DADS_LICENSE_COMMENT
