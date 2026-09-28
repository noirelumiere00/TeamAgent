"""スライドの版面を実描画で測る（仕様 v3 §5 T22・chromium が無ければ skip）。

全スライドで、フッタ（data-foot）以外の要素の「見えている部分」の下端が y≤664、左右は
x=64〜1216 の内側、文字は 14px 以上であること。見えている部分は、overflow を切る祖先
（line-clamp の箱など）で切った範囲（切れて見えない行は数えない）。

フィクスチャは本番形（クライアント未指定・指定・v2 キャッシュ）と負荷形（n=10・長文・横長・
表紙なし・v3 の欄を上限まで埋めたもの）。壊し方: line-clamp の CSS（.c1〜.c5）を外す → 負荷形の
長文が枠を押し出して赤。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.evidence import Roster
from teamagent.skills.video_algorithm.schema import CrossSynthesis, VideoAlgorithmOutput
from teamagent.skills.video_algorithm.slides import (
    CONTENT_BOTTOM,
    NOEXPORT_CSS,
    SLIDE_H,
    SLIDE_W,
    render_slides,
)
from teamagent.skills.video_algorithm.synthesis_checks import finalize
from teamagent.skills.video_algorithm.synthesis_input import SynthesisContext
from tests.skills.video_algorithm.chromium import launched_browser
from tests.skills.video_algorithm.load_shape import LOAD_N, load_output
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    prod_board,
    prod_synthesis,
    prod_videos,
)
from tests.skills.video_algorithm.test_synthesis_v3 import _V3

_MEASURE_JS = """
([bottom, left, right]) => {
  const out = [];
  document.querySelectorAll('.slide').forEach((s, si) => {
    const sr = s.getBoundingClientRect();
    const bad = [];
    for (const el of s.querySelectorAll('*')) {
      if (el.closest('[data-foot]')) continue;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      let top = r.top, bot = r.bottom, lft = r.left, rgt = r.right;
      for (let p = el.parentElement; p && p !== s; p = p.parentElement) {
        const pc = getComputedStyle(p);
        if (pc.overflowX !== 'visible' || pc.overflowY !== 'visible') {
          const pr = p.getBoundingClientRect();
          top = Math.max(top, pr.top); bot = Math.min(bot, pr.bottom);
          lft = Math.max(lft, pr.left); rgt = Math.min(rgt, pr.right);
        }
      }
      if (bot <= top || rgt <= lft) continue;
      const name = el.tagName.toLowerCase() + '.' + String(el.className || '');
      const text = (el.textContent || '').trim().slice(0, 24);
      if (bot - sr.top > bottom + 0.5) bad.push(['bottom', name, Math.round(bot - sr.top), text]);
      if (rgt - sr.left > right + 0.5 || lft - sr.left < left - 0.5)
        bad.push(['x', name, Math.round(lft - sr.left), Math.round(rgt - sr.left), text]);
      const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
      if (own && parseFloat(cs.fontSize) < 14) bad.push(['font', name, cs.fontSize, text]);
    }
    if (s.scrollWidth > s.clientWidth + 1) bad.push(['scrollWidth', s.scrollWidth]);
    const r = s.getBoundingClientRect();
    if (Math.round(r.width) !== 1280 || Math.round(r.height) !== 720) bad.push(['size', r.width, r.height]);
    if (bad.length) out.push({slide: si + 1, kind: s.dataset.slide, bad: bad.slice(0, 5)});
  });
  return out;
}
"""


def _prod(client: bool, synthesis: str) -> VideoAlgorithmOutput:
    roster = Roster.of(CLIENT, COMPETITORS) if client else Roster()
    videos, board = prod_videos(), prod_board()
    cross = cross_analyze(videos, QUERY, board=board, roster=roster)
    if synthesis == "v3":
        ctx = SynthesisContext.build(videos, QUERY, board=board, roster=roster)
        cross.synthesis = finalize(CrossSynthesis.model_validate(_V3), ctx)
    else:
        cross.synthesis = prod_synthesis()
    return VideoAlgorithmOutput(
        query=QUERY,
        videos=videos,
        board=board,
        cross=cross,
        client_name=roster.client_name,
        competitors=list(roster.competitors),
        generated_at="2026-09-28T10:15:00+09:00",
    )


_FIXTURES: dict[str, Any] = {
    "prod_unspecified": lambda: _prod(False, "v3"),
    "prod_client": lambda: _prod(True, "v3"),
    "prod_v2_cache": lambda: _prod(False, "v2"),
    "load_n10": load_output,
}


@pytest.fixture(scope="module")
def browser() -> Any:
    with launched_browser() as b:
        yield b


def _measure(browser: Any, html: str, tmp_path: Path) -> tuple[list[Any], int]:
    f = tmp_path / "slides.html"
    f.write_text(html, encoding="utf-8")
    page = browser.new_page(viewport={"width": SLIDE_W, "height": SLIDE_H})
    try:
        page.goto(f.as_uri())
        page.add_style_tag(content=NOEXPORT_CSS)
        page.evaluate("() => document.fonts.ready.then(() => undefined)")
        bad = page.evaluate(_MEASURE_JS, [CONTENT_BOTTOM, 64, SLIDE_W - 64])
        count = page.locator(".slide").count()
    finally:
        page.close()
    return bad, count


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_every_slide_fits_the_frame(browser: Any, tmp_path: Path, name: str) -> None:
    out = _FIXTURES[name]()
    html = render_slides(out, generated_at=out.generated_at or "")
    bad, count = _measure(browser, html, tmp_path)
    assert bad == [], bad
    n = LOAD_N if name == "load_n10" else 5
    assert 10 + n - 1 <= count <= 10 + n + 1  # 10＋n（絵コンテ B で ＋1・無ければ −1）
    assert count <= 30  # media worker の撮影の上限


def test_load_fixture_really_overflows_without_the_clamps(browser: Any, tmp_path: Path) -> None:
    """負荷形が本当に負荷になっていること（clamp を外すと枠を押し出す）＝上のテストの実質性。"""
    out = load_output()
    html = render_slides(out, generated_at=out.generated_at or "")
    unclamped = html.replace("-webkit-line-clamp:", "--no-clamp:")
    bad, _count = _measure(browser, unclamped, tmp_path)
    assert bad, "clamp を外しても崩れない＝負荷形が軽すぎる"
