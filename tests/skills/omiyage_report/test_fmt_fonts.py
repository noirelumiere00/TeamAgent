"""フォントサブセット埋め込みと missing-glyph ハードゲート。"""

from __future__ import annotations

from pathlib import Path

import pytest

from teamagent.skills.omiyage_report.fmt.fonts import (
    FmtFontError,
    build_embedded_fonts,
    font_dir,
)

pytestmark = pytest.mark.skipif(
    not (font_dir() / "ZenKakuGothicNew-Regular.ttf").is_file(),
    reason="font assets not bundled",
)


def test_subset_css_embeds_all_ben1_faces() -> None:
    embedded = build_embedded_fonts(
        {"mincho": set("検索"), "gothic": set("検索データ"), "latin": set("Q12")}
    )
    assert embedded.css.count("@font-face") == 7  # mincho 700/800 + gothic 400/500/700/900 + latin
    assert embedded.css.count("format('woff2')") == 7
    assert "'Shippori Mincho B1'" in embedded.css
    assert "font-weight:400 700" in embedded.css  # 可変フォントのレンジ宣言
    assert embedded.total_bytes > 0
    # 各スタックは埋め込みフォントのみで解決し、名目フォールバックを最後に置く
    assert embedded.families["gothic"].endswith("sans-serif")
    assert embedded.families["mincho"].startswith("'Shippori Mincho B1'")
    assert "'Zen Kaku Gothic New'" in embedded.families["latin"]


def test_latin_stack_covers_cjk_via_gothic_union() -> None:
    # PART ラベル等は latin スタックだが、CJK は同梱 gothic の cmap 和集合で引ける
    embedded = build_embedded_fonts({"latin": set("PART 1 — 検索面の実態")})
    assert embedded.css.count("@font-face") == 7


def test_missing_glyph_fails_fast() -> None:
    with pytest.raises(FmtFontError, match="missing glyphs"):
        build_embedded_fonts({"gothic": {"あ", "\U0001f984"}})  # 🦄 はcmapに無い


def test_missing_font_asset_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(FmtFontError, match="font asset missing"):
        build_embedded_fonts({"gothic": set("あ")}, base_dir=tmp_path)
