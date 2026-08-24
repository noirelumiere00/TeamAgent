"""FMT HTML レンダラの実出力検証（規律ゲート・ゴールデン・縮退規則）。"""

from __future__ import annotations

import re
from typing import Any

import pytest

from teamagent.media.operations import _EXTERNAL_HTML_REF
from teamagent.skills.omiyage_report.fmt.build import render_fmt_deck
from teamagent.skills.omiyage_report.fmt.html import shorten_url, strip_display_symbols
from teamagent.skills.omiyage_report.fmt.spec import load_fmt_spec

from .fmt_fixtures import CTA, make_deck_content

SPEC = load_fmt_spec()


@pytest.fixture(scope="module")
def golden_html() -> str:
    return render_fmt_deck(make_deck_content(), generated_on="2026-08-24").html


def test_slide_sections_match_ben1_composition(golden_html: str) -> None:
    labels = re.findall(r'<section class="slide[^"]*" data-label="([^"]+)"', golden_html)
    assert labels == ["01 A", "02 B", "03 C", "04 D", "05 C", "06 C", "07 D", "08 E", "09 H"]


def test_cta_fixed_text_appears_exactly_once_in_dark_band(golden_html: str) -> None:
    assert golden_html.count(CTA) == 1
    band = re.search(r'<div class="sm-band">.*?</div>', golden_html, flags=re.DOTALL)
    assert band is not None and CTA in band.group(0)


def test_footer_numbering_covers_all_content_slides(golden_html: str) -> None:
    # A/B はフッターなし（canon準拠）・C〜H の7枚に「NN / 09」
    numbering = re.findall(r"<span>(\d{2} / \d{2})</span>", golden_html)
    assert numbering == [f"{n:02d} / 09" for n in (3, 4, 5, 6, 7, 8, 9)]


def test_footer_total_updates_when_slide_count_changes() -> None:
    raw = make_deck_content()
    del raw["slides"][5]  # Q3 を落とす → 8枚
    html = render_fmt_deck(raw, generated_on="2026-08-24").html
    numbering = re.findall(r"<span>(\d{2} / \d{2})</span>", html)
    assert numbering == [f"{n:02d} / 08" for n in (3, 4, 5, 6, 7, 8)]


def test_provided_thumbnail_labels_cover_every_card(golden_html: str) -> None:
    image_count = golden_html.count("data:image/png;base64,")
    assert image_count == 9  # 表紙2 + 実例2 + E 5
    assert golden_html.count("提供サムネ") == image_count


def test_visible_short_urls_are_rendered(golden_html: str) -> None:
    assert golden_html.count('class="imgurl"') == 7  # 実例2 + E 5（表紙は対象外）
    assert "tiktok.com/@someone/video/7300000" in golden_html
    assert "…" in golden_html  # 42字超のURLは短表記


def test_contain_only_no_crop(golden_html: str) -> None:
    assert "object-fit:contain" in golden_html
    assert "object-fit:cover" not in golden_html


def test_no_font_below_24px(golden_html: str) -> None:
    sizes = [int(px) for px in re.findall(r"font-size:(\d+)px", golden_html)]
    assert sizes and min(sizes) >= 24


def test_no_external_references_media_gate(golden_html: str) -> None:
    # media worker の _EXTERNAL_HTML_REF（本物のガード）で自己完結を証明する
    assert _EXTERNAL_HTML_REF.search(golden_html) is None
    assert golden_html.count("format('woff2')") == 7  # mincho2 + gothic4 + latin1


def test_html_fits_media_size_limit(golden_html: str) -> None:
    assert len(golden_html.encode("utf-8")) <= 2 * 1024 * 1024


def test_untrusted_text_is_escaped() -> None:
    raw = make_deck_content()
    raw["slides"][3]["heading"] = "<script>alert(1)</script>を含む見出し"
    html = render_fmt_deck(raw, generated_on="2026-08-24").html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_emoji_is_stripped_from_display_text() -> None:
    raw = make_deck_content()
    raw["slides"][3]["lead"] = "キラキラ✨の階層別パフォーマンス🔥"
    html = render_fmt_deck(raw, generated_on="2026-08-24").html
    assert "✨" not in html and "🔥" not in html
    assert "キラキラの階層別パフォーマンス" in html


def test_bar_normalization_follows_per_unit_global_max(golden_html: str) -> None:
    # Q2: 全%値の最大 1.94 に対し 1.35 → round(1.35/1.94*99) = 69%
    assert "width:69%" in golden_html
    # 最大値そのものは 99%
    assert "width:99%" in golden_html


def test_d_table_highlights_column_max_only(golden_html: str) -> None:
    accent = SPEC.tokens.colors.brand_accents.brand_a.value
    assert f'style="color:{accent};font-weight:700;">2.96%' in golden_html
    assert f'style="color:{accent};font-weight:700;">2.49%' not in golden_html


def test_degradation_removes_slots_without_leaving_holes() -> None:
    raw = make_deck_content()
    raw["slides"][0]["data"] = {}  # 表紙サムネ未入手 → slot除去・左カラム全幅
    for slide in raw["slides"]:
        if isinstance(slide["data"], dict) and slide["data"].get("example"):
            slide["data"]["example"] = None
    h_data = raw["slides"][-1]["data"]
    h_data["cta"] = False
    h_data["conclusion"] = None
    html = render_fmt_deck(raw, generated_on="2026-08-24").html
    assert '<div class="cv-thumbs">' not in html  # 表紙サムネ枠ごと消える
    assert '<div class="excol">' not in html  # 実例パネルごと消える
    assert '<div class="sm-band">' not in html  # 結論バンドも空では出さない
    assert CTA not in html


def test_shorten_url_and_strip_helpers() -> None:
    assert shorten_url("https://www.tiktok.com/@a/video/1") == "tiktok.com/@a/video/1"
    long_url = "https://www.tiktok.com/@averylongaccount/video/7300000000000000001"
    assert shorten_url(long_url).endswith("…")
    assert len(shorten_url(long_url)) == 42
    assert strip_display_symbols("A✨B🦄C") == "ABC"


def test_pr_vocabulary_from_fixture_uses_canonical_labels(golden_html: str) -> None:
    assert "PR表記あり" in golden_html and "PR表記なし" in golden_html
    assert "オーガニック" not in golden_html


def _meta_texts(raw: dict[str, Any]) -> list[str]:
    return [raw["deck_meta"]["addressee"], raw["deck_meta"]["issuer"]]


def test_deck_meta_texts_land_on_cover(golden_html: str) -> None:
    for text in _meta_texts(make_deck_content()):
        assert text in golden_html
    assert "CONFIDENTIAL" in golden_html
