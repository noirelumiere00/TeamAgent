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


@pytest.mark.parametrize(
    ("text", "expected", "dropped"),
    [
        ("JTB 海外旅行 おすすめ7選", "JTB 海外旅行 おすすめ7選", 0),  # 描けるものは一切変えない
        ("𝐁𝐞𝐧𝐜𝐡 𝐏𝐑", "Bench PR", 0),  # 装飾用の数学英字は NFKC で ASCII へ
        ("club™", "clubTM", 0),
        ("İstanbul", "Istanbul", 0),  # 書体に無いアクセント付きは基底文字へ
        ("1㌐の動画", "1ギガの動画", 0),  # NFKC を先に試す（結合記号を外すと「キカ」になる）
        ("서울 여행 Seoul", "Seoul", 4),  # 寄せられないものは落として数える
    ],
)
def test_make_renderable(text: str, expected: str, dropped: int) -> None:
    from teamagent.skills.omiyage_report.fmt.fonts import make_renderable

    assert make_renderable(text, "gothic") == (expected, dropped)


def test_made_renderable_text_passes_the_gate() -> None:
    """make_renderable を通した文字列は、どの役割でもゲート（missing glyphs）を通る。"""
    from teamagent.skills.omiyage_report.fmt.fonts import make_renderable

    raw = "𝐁𝐞𝐧𝐜𝐡 서울 İ ™ ① Café ẞ ʟ ꕤ"
    chars = {role: [make_renderable(raw, role)[0]] for role in ("mincho", "gothic", "latin")}
    build_embedded_fonts(chars)  # type: ignore[arg-type]
