"""検索上位チェック → DeckSpec の組み立て（python-pptx を使わない mcp 側）。"""

from __future__ import annotations

import pytest

from teamagent.media.deck_contracts import (
    ChartFill,
    DeckSpec,
    PictureFill,
    SlideSpec,
    TableFill,
    TextFill,
)
from teamagent.skills._deck.layouts import TITLE_MAX
from teamagent.skills._deck.spec import report_deck_enabled
from teamagent.skills.search_surface_check.deck import build_surface_deck
from tests.skills.search_surface_check.deck_fixtures import (
    CLIENT,
    NOW,
    REPORT_ID,
    ai_conclusion,
    covers,
    surface,
)


def _build(surfaces, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("include_appendix", True)
    kw.setdefault("client_name", CLIENT)
    kw.setdefault("measured_epoch", NOW)
    kw.setdefault("report_id", REPORT_ID)
    return build_surface_deck(surfaces, **kw)


def _slide(spec: DeckSpec, slide_id: str) -> SlideSpec:
    return next(s for s in spec.slides if s.slide_id == slide_id)


def _ids(spec: DeckSpec) -> list[str]:
    return [s.slide_id for s in spec.slides]


def _text(fill: TextFill) -> str:
    return "\n".join("".join(r.text for r in p.runs) for p in fill.paragraphs)


def _fill(slide: SlideSpec, box: str):  # type: ignore[no-untyped-def]
    return next(f for f in slide.fills if f.box == box)


def test_summary_pages_then_appendix_in_named_sections() -> None:
    s = surface(15)
    deck = _build([s], covers=covers(s.posts), client_accounts=["kurashiru.com"])
    assert _ids(deck.spec) == [
        "SS-01", "SS-04", "SS-05", "SS-06", "SS-07", "SS-08", "SS-09",
        "SS-11", "SS-12-1", "SS-12-2", "SS-13",
    ]  # fmt: skip
    assert [name for name, _ in deck.spec.sections()] == ["表紙", "本編", "付録"]
    layouts = {s.slide_id: s.layout for s in deck.spec.slides}
    assert layouts["SS-01"] == "R_表紙"
    assert layouts["SS-04"] == "R_結論"
    assert layouts["SS-06"] == "R_数字"
    assert layouts["SS-07"] == "R_グラフ"
    assert layouts["SS-08"] == "R_動画カード"
    assert layouts["SS-12-1"] == "R_付録の表"


def test_every_page_has_title_within_limit_condition_line_and_notes() -> None:
    s = surface(15)
    deck = _build([s], covers=covers(s.posts))
    for slide in deck.spec.slides:
        title = _text(_fill(slide, "title"))
        assert 0 < len(title.replace("\n", "")) <= TITLE_MAX, (slide.slide_id, title)
        condition = _text(_fill(slide, "condition"))
        assert condition.startswith("「スパイスカレー 作り方」で検索した上位 15 本／取得日 ")
        assert condition.endswith("／TikTok・未ログイン")
        assert slide.notes.startswith("【このページは何を見て何を出しているか】")
    cover = _slide(deck.spec, "SS-01")
    assert (
        _text(_fill(cover, "title")) == "「スパイスカレー 作り方」\nTikTok 検索 上位 15 本の顔ぶれ"
    )


def test_page_counts_follow_post_count() -> None:
    few = surface(3)
    deck = _build([few], covers=covers(few.posts))
    cards = _slide(deck.spec, "SS-08")
    assert {f.box for f in cards.fills if isinstance(f, PictureFill)} == {
        "card_1",
        "card_2",
        "card_3",
    }
    top = _fill(_slide(deck.spec, "SS-09"), "table")
    assert isinstance(top, TableFill) and len(top.rows) == 3
    assert [i for i in _ids(deck.spec) if i.startswith("SS-12")] == ["SS-12-1"]

    many = surface(30)
    deck = _build([many], covers=covers(many.posts))
    top = _fill(_slide(deck.spec, "SS-09"), "table")
    assert len(top.rows) == 10 and top.font_pt == 12
    assert [i for i in _ids(deck.spec) if i.startswith("SS-12")] == [
        "SS-12-1",
        "SS-12-2",
        "SS-12-3",
    ]
    appendix = _fill(_slide(deck.spec, "SS-12-2"), "table")
    assert appendix.font_pt == 11 and len(appendix.rows) == 10
    assert appendix.rows[0][0].text == "11"


def test_instagram_drops_unmeasurable_columns_and_says_unmeasured() -> None:
    ig = surface(12, platform="instagram")
    deck = _build([ig])
    top = _fill(_slide(deck.spec, "SS-09"), "table")
    assert "フォロワー" not in top.columns and "保存率" not in top.columns
    assert "再生" in top.columns
    note = _text(_fill(_slide(deck.spec, "SS-09"), "note"))
    assert "未計測（0 件ではない）" in note
    numbers = _slide(deck.spec, "SS-06")
    small = [_text(f) for f in numbers.fills if f.box.startswith("small_number")]
    assert "未計測" in small
    assert "0" not in small
    labels = [_text(f) for f in numbers.fills if f.box.startswith("small_label")]
    assert any("Instagramでは取れない・0 件ではない" in label for label in labels)
    assert "／Instagram・未ログイン" in _text(_fill(numbers, "condition"))
    csv = deck.spec.csv
    assert csv is not None
    followers_col = csv.columns.index("フォロワー")
    assert {row[followers_col] for row in csv.rows} == {"未計測"}


def test_roster_page_only_when_there_is_a_roster_or_a_mention() -> None:
    plain = surface(15, client_name=None)
    deck = _build([plain], client_name=None)
    assert "SS-05" not in _ids(deck.spec)
    assert any(d.startswith("SS-05") for d in deck.dropped)

    with_roster = _build([plain], client_name=None, competitor_accounts=["nobody_here"])
    roster = _fill(_slide(with_roster.spec, "SS-05"), "table")
    assert ["@nobody_here", "競合"] == [c.text for c in roster.rows[-1][:2]]
    assert roster.rows[-1][2].text.startswith("圏外")


def test_roster_lists_client_rows_with_links() -> None:
    s = surface(15)
    deck = _build([s], client_accounts=["kurashiru.com"])
    slide = _slide(deck.spec, "SS-05")
    table = _fill(slide, "table")
    assert table.columns == ("アカウント", "区分", "順位", "言及", "PR")
    first = table.rows[0]
    assert first[0].text == "@kurashiru.com" and first[1].text == "自社"
    assert first[0].link and first[0].link.startswith("https://www.tiktok.com/@kurashiru.com/")
    assert _text(_fill(slide, "title")).startswith("「クラシル」 は 8・11 位に 2 本")


def test_overflow_is_cut_at_sentence_and_full_text_goes_to_notes() -> None:
    long_text = "最初の文はここで終わる。" + "二つ目の文はとても長く続く" * 15 + "。三つ目。"
    s = surface(15, conclusion=ai_conclusion(winning=long_text))
    deck = _build([s])
    slide = _slide(deck.spec, "SS-04")
    body = _text(_fill(slide, "body"))
    assert "勝ち筋：最初の文はここで終わる。（根拠 2・9 位）" in body
    assert "…" not in body
    assert "【全文】" in slide.notes and long_text in slide.notes
    assert "一部省略" in _text(_fill(slide, "source"))


def test_ai_headline_over_limit_falls_back_to_rule_headline() -> None:
    long_head = "料理系クリエイターが上位15本中7本を持ち、再生の68%を取る面だと言い切れる"
    assert len(long_head) > TITLE_MAX
    s = surface(15, conclusion=ai_conclusion(headline=long_head))
    title = _text(_fill(_slide(_build([s]).spec, "SS-04"), "title"))
    assert title != long_head and len(title) <= TITLE_MAX

    short = surface(15, conclusion=ai_conclusion())
    assert _text(_fill(_slide(_build([short]).spec, "SS-04"), "title")) == (
        "料理系クリエイターが上位を持つ面"
    )


def test_rule_path_writes_facts_only_without_actions() -> None:
    s = surface(15)
    slide = _slide(_build([s]).spec, "SS-04")
    body = _text(_fill(slide, "body"))
    assert "打ち手" not in body
    assert "最も見られている：" in body
    assert _text(_fill(slide, "source")) == "集計"


def test_chart_is_100_percent_stacked_with_types_and_dropped_without_types() -> None:
    s = surface(15)
    slide = _slide(_build([s]).spec, "SS-07")
    chart = _fill(slide, "chart")
    assert isinstance(chart, ChartFill)
    assert chart.chart_type == "bar_stacked_100" and chart.number_format == "0%"
    assert chart.categories == ("本数", "再生")
    assert sum(1 for se in chart.series if se.color == "accent1") == 1
    for i in range(2):
        assert sum(se.values[i] for se in chart.series) == pytest.approx(1.0, abs=0.01)
    assert _text(_fill(slide, "source")) == "AI の推定（未照合）"
    assert "注意：" in _text(_fill(slide, "reading"))

    unknown = surface(15, classify=False)
    deck = _build([unknown])
    assert "SS-07" not in _ids(deck.spec)
    top = _fill(_slide(deck.spec, "SS-09"), "table")
    assert "タイプ（推定）" not in top.columns


def test_angles_from_ai_only() -> None:
    s = surface(15, conclusion=ai_conclusion())
    slide = _slide(_build([s]).spec, "SS-10")
    assert slide.layout == "R_結論"
    assert "スパイスの配合" in _text(_fill(slide, "body"))
    deck = _build([surface(15)])
    assert "SS-10" not in _ids(deck.spec)
    assert any("切り口" in d for d in deck.dropped)


def test_cover_without_local_image_keeps_the_frame_only() -> None:
    s = surface(5)
    deck = _build([s])
    pic = _fill(_slide(deck.spec, "SS-01"), "cover")
    assert isinstance(pic, PictureFill) and pic.image is None
    assert pic.alt_text == "1 位 @gonosara 表紙"
    assert deck.images == {}


def test_images_are_registered_once_per_post() -> None:
    s = surface(5)
    deck = _build([s], covers=covers(s.posts))
    assert set(deck.images) == {f"cover_s0_r{r}" for r in range(1, 6)}
    assert {(i.width_px, i.height_px) for i in deck.spec.images} == {(9, 16), (3, 4)}


def test_two_keywords_put_comparison_first_and_repeat_pages() -> None:
    a = surface(10, keyword="スパイスカレー 作り方")
    b = surface(8, keyword="無水カレー")
    deck = _build([a, b])
    ids = _ids(deck.spec)
    assert ids[:3] == ["SS-01", "SS-02", "SS-04-1"]
    assert "SS-04-2" in ids and "SS-09-2" in ids
    assert ids.index("SS-09-1") < ids.index("SS-04-2")
    compare = _fill(_slide(deck.spec, "SS-02"), "table")
    assert [r[0].text for r in compare.rows] == ["スパイスカレー 作り方", "無水カレー"]


def test_banned_wording_in_ai_text_drops_only_the_point() -> None:
    s = surface(15, conclusion=ai_conclusion(winning="前回お見せした通り、配合の解説が強い"))
    deck = _build([s])
    assert "前回" not in _text(_fill(_slide(deck.spec, "SS-04"), "body"))
    assert any("言い方" in d for d in deck.dropped)


def test_csv_keeps_full_text_and_unmeasured() -> None:
    s = surface(15)
    csv = _build([s]).spec.csv
    assert csv is not None and len(csv.rows) == 15
    assert csv.columns[:3] == ("検索語", "媒体", "順位")
    assert csv.rows[0][csv.columns.index("本文")] == s.posts[0].desc


def test_switch_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_REPORT_DECK_PPTX", raising=False)
    assert report_deck_enabled() is False
    monkeypatch.setenv("USE_REPORT_DECK_PPTX", "1")
    assert report_deck_enabled() is True


def test_mcp_side_does_not_import_python_pptx() -> None:
    """mcp イメージには python-pptx が無い。DeckSpec を組む側は pptx を import せずに動く。"""
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['pptx'] = None\n"
        "import teamagent.skills._deck.spec, teamagent.skills.search_surface_check.deck\n"
        "import teamagent.media.deck_contracts\n"
        "assert 'pptx' not in {m.split('.')[0] for m, v in sys.modules.items() if v is not None}\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_summary_is_default_and_evidence_pages_are_main() -> None:
    deck = _build([surface(15, conclusion=ai_conclusion())], include_appendix=False)
    assert len(deck.spec.slides) == 8
    assert [s for s, _ in deck.spec.sections()] == ["表紙", "本編"]
    for sid in ("SS-08", "SS-09", "SS-10"):
        assert _slide(deck.spec, sid).section == "本編"
    default = build_surface_deck(
        [surface(15)], client_name=CLIENT, measured_epoch=NOW, report_id=REPORT_ID
    )
    assert not any(s.section == "付録" for s in default.spec.slides)


@pytest.mark.parametrize("keywords,appendix", [(3, False), (3, True), (5, False), (5, True)])
def test_normal_requests_fit_slide_image_and_section_limits(keywords: int, appendix: bool) -> None:
    surfaces = [
        surface(
            30 if p == "tiktok" else 50,
            platform=p,
            keyword="長い検索語" * 8 + str(i),
            conclusion=ai_conclusion(),
        )
        for i in range(keywords)
        for p in ("tiktok", "instagram")
    ]
    images = {url: image for s in surfaces for url, image in covers(s.posts).items()}
    deck = _build(surfaces, covers=images, include_appendix=appendix)
    assert len(deck.spec.slides) <= 60
    assert len(deck.spec.images) <= 40
    assert all(len(s.section) <= 40 for s in deck.spec.slides)
    assert set(deck.images) == {i.name for i in deck.spec.images}
    if keywords == 5:
        assert any("上限" in d for d in deck.dropped)
    assert DeckSpec.model_validate_json(deck.spec.model_dump_json())


def test_ig_production_shape_order_and_missing_fields() -> None:
    ig = surface(12, platform="instagram")
    assert all(
        not p.posted_at
        and not p.duration_sec
        and not p.share_count
        and not p.author_followers
        and not p.author_name
        and not p.hashtags
        for p in ig.posts
    )
    assert [p.appearances for p in ig.posts] == sorted(
        (p.appearances for p in ig.posts), reverse=True
    )
    deck = _build([ig])
    for sid in ("SS-09", "SS-12-1", "SS-12-2"):
        table = _fill(_slide(deck.spec, sid), "table")
        assert "出現回数" in table.columns
        assert not {"投稿日", "長さ", "フォロワー", "保存率"}.intersection(table.columns)
    assert "表示順" not in deck.spec.model_dump_json()
    assert "表示の順" not in deck.spec.model_dump_json()
    assert "出現回数" in _slide(deck.spec, "SS-11").notes
    csv = deck.spec.csv
    assert csv is not None
    for column in (
        "表示名",
        "シェア",
        "保存",
        "保存率(%)",
        "フォロワー",
        "投稿日",
        "長さ(秒)",
        "タグ",
    ):
        assert {row[csv.columns.index(column)] for row in csv.rows} == {"未計測"}
    numbers = _slide(deck.spec, "SS-06")
    assert _text(_fill(numbers, "big_number")) != "0回"
    assert "再生が取れた 4 本" in _text(_fill(numbers, "big_label"))
    assert all(_text(_fill(numbers, f"small_number_{i}")) for i in range(1, 4))


def test_ig_no_plays_and_unknown_majority_never_claim_zero() -> None:
    ig = surface(12, platform="instagram")
    ig.posts = [
        p.model_copy(update={"play_count": 0, "category": "creator" if i == 0 else "unknown"})
        for i, p in enumerate(ig.posts)
    ]
    ig.facts = None
    deck = _build([ig])
    numbers = _slide(deck.spec, "SS-06")
    assert _text(_fill(numbers, "big_number")) == "未計測"
    chart = _fill(_slide(deck.spec, "SS-07"), "chart")
    assert chart.categories == ("本数",)
    assert next(s.name for s in chart.series if s.color == "accent1") != "未分類"
    assert "再生の 0%" not in deck.spec.model_dump_json()
    assert "0回" not in deck.spec.model_dump_json()
    assert _text(_fill(_slide(deck.spec, "SS-09"), "title")) == "上位 10 本の一覧"


@pytest.mark.parametrize(
    "platform,classify,client",
    [
        ("instagram", False, None),
        ("tiktok", False, None),
        ("tiktok", True, "HARIO"),
    ],
)
def test_main_titles_are_unique(platform: str, classify: bool, client: str | None) -> None:
    deck = _build(
        [surface(15, platform=platform, classify=classify, client_name=client)], client_name=client
    )
    titles = [_text(_fill(s, "title")) for s in deck.spec.slides if s.section == "本編"]
    assert len(titles) == len(set(titles))


def test_organic_data_and_invalid_ai_do_not_abort() -> None:
    s = surface(
        15,
        keyword="オーガニック シャンプー",
        conclusion=ai_conclusion(
            headline="重要なのは配合",
            winning="先日お見せした資料",
            angles=[("オーガニック成分を見せる", [2, 9]), ("重要なのは香り", [1, 2])],
        ),
    )
    s.posts = [p.model_copy(update={"hashtags": ["オーガニックシャンプー"]}) for p in s.posts]
    s.facts = None
    deck = _build([s], client_name="オーガニックの森")
    assert any("AI の題" in d for d in deck.dropped)
    assert any("AI の文" in d for d in deck.dropped)
    assert any("AI の切り口" in d for d in deck.dropped)
    assert "省略した文" in _slide(deck.spec, "SS-04").notes
    assert "オーガニック成分" in _text(_fill(_slide(deck.spec, "SS-10"), "body"))


def test_normalized_roster_does_not_create_phantom_rows() -> None:
    deck = _build([surface(15)], client_accounts=[" @Kurashiru.com ", "", " @ "])
    table = _fill(_slide(deck.spec, "SS-05"), "table")
    own = [row for row in table.rows if row[1].text == "自社"]
    assert len(own) == 2
    assert not any("圏外" in row[2].text for row in own)


def test_ai_title_with_duplicate_fact_falls_back_and_keeps_deck() -> None:
    s = surface(15, conclusion=ai_conclusion(headline="上位 10 本中、フォロワー 1 万未満が 6 本"))
    # 実測の数字ページと同じ題にする。
    ordinary = _build([surface(15)])
    s.conclusion.headline = _text(_fill(_slide(ordinary.spec, "SS-06"), "title"))
    deck = _build([s])
    assert _text(_fill(_slide(deck.spec, "SS-04"), "title")) != s.conclusion.headline
    assert any("AI の題: 題・数字の重複" in d for d in deck.dropped)


def test_handle_is_data_even_when_it_contains_a_banned_phrase() -> None:
    s = surface(15)
    s.posts = [p.model_copy(update={"author": "重要なのは配合"}) for p in s.posts]
    s.facts = None
    assert _build([s]).spec
