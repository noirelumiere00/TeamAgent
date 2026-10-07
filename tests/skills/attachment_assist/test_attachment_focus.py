"""長い資料から依頼の語を含むページを優先して詰める部品（focus.py）の単体テスト。"""

from __future__ import annotations

from teamagent.skills.attachment_assist.focus import focus_pages, focus_terms, format_page_list

PROD_INSTRUCTION = (
    "「0529【ショート動画事例】.pdf」を指定して確認して。以前このSlack投稿に添付されていました。\n"
    "https://vector-workspcae.slack.com/archives/C0B0PQD83N2/p1782437516047339\n"
    "このPDFの日本コカ・コーラ／紅茶花伝の該当事例について、資料に書かれている実績と施策内容を"
    "要約して、元PDFをこのスレッドに添付してほしい。確認できない内容は不明として。"
)


def test_terms_pick_brand_and_product_and_drop_request_words() -> None:
    terms = focus_terms(PROD_INSTRUCTION, exclude=["0529【ショート動画事例】.pdf"])
    assert "紅茶花伝" in terms
    assert "コカ・コーラ" in terms and "コーラ" in terms
    for generic in ("資料", "要約", "実績", "施策内容", "該当事例", "slack", "pdf", "スレッド"):
        assert generic not in terms, generic
    # URL とファイル名からは拾わない（表紙にしか出ない語でページ選びを引きずらない）。
    assert not any("vector" in t or "archives" in t for t in terms)
    assert "ショート" not in terms


def test_terms_match_across_pdf_spacing_and_width() -> None:
    """PDF 抽出で字間に空白・改行が入る／半角の中黒でも当たる（NFKC＋空白除去）。"""
    pages = [(1, "表紙 " * 50), (2, "紅 茶\n花 伝 の事例"), (3, "ｺｶ･ｺｰﾗ"), (4, "別件 " * 50)]
    got = focus_pages(pages, focus_terms("紅茶花伝とコカ・コーラ"), budget=10_000)
    assert got is not None
    assert got.hit_pages == (2, 3)


def test_terms_on_most_pages_are_ignored() -> None:
    """どのページにも出る語（資料全体の話題語）では選ばない＝当たり無しで None。"""
    pages = [(n, f"ショート動画の事例 {n}") for n in range(1, 11)]
    assert focus_pages(pages, ["ショート"], budget=10_000) is None


def test_hit_pages_come_first_then_head_fills_the_rest() -> None:
    pages = [(n, f"p{n}:" + "x" * 98) for n in range(1, 41)]
    pages[34] = (35, "p35:紅茶花伝" + "x" * 94)
    got = focus_pages(pages, ["紅茶花伝"], budget=500)
    assert got is not None
    assert got.hit_pages == (35,)
    assert "p35:紅茶花伝" in got.body and "p34:" in got.body and "p36:" in got.body
    assert got.body.startswith("p1:")  # 余りは先頭から
    assert len(got.body) <= 520


def test_no_terms_or_no_pages_returns_none() -> None:
    assert focus_pages([(1, "a")], [], budget=100) is None
    assert focus_pages([], ["紅茶花伝"], budget=100) is None
    assert focus_terms("要約して") == []


def test_format_page_list() -> None:
    assert format_page_list([3, 4, 5, 9]) == "3〜5・9"
    assert format_page_list([1]) == "1"
    assert format_page_list(list(range(1, 30, 2)), limit=2) == "1・3ほか"
