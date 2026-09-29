"""サムネ（一覧の表紙）の事実・特徴・差・指示（cover_facts・LLM を使わない）。

本番の形（09-28「スパイスカレー 作り方」）: 上位 5 本の冒頭テロップとキャプションには 5 本とも
検索語が入っていた。検索語を含むだけで「一部同じ」にしない（kw_only）。
壊し方（→ 赤）は各テストの docstring。
"""

from __future__ import annotations

import pytest

from teamagent.skills.video_algorithm.cover_facts import (
    NOT_COMPARED,
    code_cover_directives,
    cover_facts,
    cover_line,
    cover_tier,
    cover_view,
    fisher_two_sided,
    holm,
    is_effortless,
    is_question,
    is_warning,
    match_norm,
    number_claims,
    text_match,
)
from teamagent.skills.video_algorithm.evidence import (
    TIER_CASE,
    TIER_MAJORITY,
    TIER_OBSERVED,
    TIER_REQUIRED,
    Roster,
)
from teamagent.skills.video_algorithm.schema import AnalyzedVideo, CoverRead, VideoMeta
from teamagent.skills.video_algorithm.synthesis_input import SynthesisContext
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    prod_board,
    prod_board_with_covers,
    prod_cover_read,
    prod_videos,
    rest_cover_read,
)

TERMS = ["スパイスカレー", "作り方"]
ROSTER = Roster.of(CLIENT, COMPETITORS)


# ── 文字の照合 ──────────────────────────────────────────────────────────


def test_match_norm_drops_symbols_and_folds_width() -> None:
    assert match_norm("【ＳＰＩＣＹ】スパイス　カレー！✨") == "spicyスパイスカレー"


def test_kw_only_overlap_is_not_partial() -> None:
    """検索語だけの共通は kw_only（本番では 5 本とも検索語を含んでいた）。

    壊し方: 検索語を除く処理を外す → 「スパイスカレー」の 7 字の共通で partial になり赤。
    """
    assert text_match("スパイスカレー\n5つで作れる", "スパイスカレーを作ってみたい！", TERMS) == (
        "kw_only"
    )
    assert text_match("30分で本格\nスパイスカレー", "調理時間は30分で本格", TERMS) == "same"
    assert text_match("とにかく痩せたい", "とにかく痩せたいから", TERMS) == "same"
    assert text_match("簡単チキン", "バターチキン", TERMS) == "different"
    assert text_match("", "何か", TERMS) == "no_text"


def test_match_threshold_is_four_chars() -> None:
    """壊し方: 閾値を 1 字にする → 「本格」2 字の共通で partial になり赤。"""
    assert text_match("本格派の味わい", "本格カレー", TERMS) == "different"
    assert text_match("週末の本格派ランチ", "本格派ランチを作る", TERMS) == "partial"


# ── 文字の中身 ──────────────────────────────────────────────────────────


def test_numbers_need_units_and_years_do_not_count() -> None:
    assert number_claims("３分で完成・第1位・90％OFF") == (
        ("time", "3分"),
        ("rank", "1位"),
        ("percent", "90%"),
    )
    assert number_claims("2024年の定番 ID 12345") == ()


@pytest.mark.parametrize(
    ("text", "warning", "effortless"),
    [
        ("間違いない美味しさ", False, False),  # 太鼓判
        ("やめられない味", False, False),  # ほめ言葉
        ("失敗しないカレー", False, True),  # 成功の約束＝手間なし型
        ("包丁いらない", False, True),
        ("混ぜるだけ", False, True),
        ("NG例3つ", True, False),
        ("間違えがちな炒め方", True, False),
        ("それ、失敗してます", True, False),
    ],
)
def test_warning_and_effortless_forms(text: str, warning: bool, effortless: bool) -> None:
    assert is_warning(text) is warning
    assert is_effortless(text) is effortless


def test_question_words() -> None:
    assert is_question("なぜ焦げる？") and is_question("どっちが正解")
    assert not is_question("本格スパイスカレー")


# ── 1 枚の事実 ──────────────────────────────────────────────────────────


def _meta(rank: int = 1, desc: str = "", hashtags: list[str] | None = None) -> VideoMeta:
    return VideoMeta(rank=rank, url=f"https://t/{rank}", desc=desc, hashtags=hashtags or [])


def test_size_position_and_lines_come_from_the_box() -> None:
    """大きさは AI の自己申告ではなく枠から計算（1 行の高さ÷幅 ≥ 1/10 で読める大きさ）。"""
    read = CoverRead.model_validate(
        {
            "status": "ok",
            "img_w": 540,
            "img_h": 960,
            "elements": [],
            "face": None,
            "texts": [
                {"text": "小さな注記", "box_2d": [900, 100, 930, 600]},
                {"text": "大きい\n見出し", "box_2d": [100, 50, 300, 950]},
            ],
        }
    )
    c = cover_facts(read, _meta(), None, QUERY)
    assert c.main_text == "大きい\n見出し" and c.lines == 2 and c.position == "top"
    assert c.line_h_pct == 10.0 and c.large_text is True  # 0.1×(960/540)=0.178 ≥ 0.1
    small = CoverRead.model_validate(
        {
            "status": "ok",
            "img_w": 540,
            "img_h": 960,
            "elements": [],
            "face": None,
            "texts": [{"text": "a\nb\nc", "box_2d": [800, 0, 900, 1000]}],
        }
    )
    assert cover_facts(small, _meta(), None, QUERY).large_text is False  # 1 行 3.3%×1.78
    no_box = CoverRead.model_validate(
        {"status": "ok", "elements": [], "face": None, "texts": ["見出し"]}
    )
    c = cover_facts(no_box, _meta(), None, QUERY)
    assert c.large_text is None and c.position == "unknown"  # 分からない（無いと数えない）


def test_brand_names_are_verified_before_classifying() -> None:
    """AI がロゴから推測した商品名に区分を付けない（キャプション・ハッシュタグ・動画の検出で照合）。

    壊し方: 照合を外す → 未照合の「ハーブ専科」が競合と出て赤。
    """
    read = prod_cover_read(4)
    c = cover_facts(read, _meta(4, desc="夕飯 #カレー"), None, QUERY, ROSTER)
    assert c.brands == (("ハーブ専科", "unverified"),)
    c = cover_facts(read, _meta(4, desc="無水カレー @ハーブ専科 #PR"), None, QUERY, ROSTER)
    assert c.brands == (("ハーブ専科", "competitor"),)


def test_opening_match_only_for_watched_videos() -> None:
    board = prod_board()
    videos = prod_videos()
    read = prod_cover_read(3)
    watched = cover_facts(read, board[2], videos[2], QUERY)
    assert watched.opening_match == "same" and watched.own_text is False
    unwatched = cover_facts(read, board[2], None, QUERY)
    assert unwatched.opening_match == "" and unwatched.own_text is None


def test_unreadable_text_is_not_counted_as_no_text() -> None:
    """文字らしいが読めない表紙を「文字なし」と数えない（文字の欄の母数から外す）。"""
    read = CoverRead.model_validate(
        {"status": "ok", "elements": [], "face": None, "texts": [], "unreadable_text": True}
    )
    c = cover_facts(read, _meta(), None, QUERY)
    assert c.has_text is None
    plain = read.model_copy(update={"unreadable_text": False})
    assert cover_facts(plain, _meta(), None, QUERY).has_text is False


def test_failed_read_has_no_facts() -> None:
    read = CoverRead(rank=2, status="fetch_failed", reason="MEDIA_THUMBNAIL_FETCH_FAILED")
    c = cover_facts(read, _meta(2), None, QUERY)
    assert c.has_text is None and c.elements is None and c.face_real is None


# ── 特徴の表 ────────────────────────────────────────────────────────────


def test_feature_denominators_are_per_field() -> None:
    """読めなかった欄はその欄の母数から外す（無いと数えない）。

    壊し方: 母数を読めた本数（5）に固定する → face の n が 5 になり赤。
    """
    board = prod_board_with_covers()
    board[1].cover_read = board[1].cover_read.model_copy(update={"face": None})  # type: ignore[union-attr]
    view = cover_view(board, prod_videos(), QUERY, ROSTER)
    face = view.feature("cover:face")
    assert face is not None and (face.count, face.n) == (2, 4)
    text = view.feature("cover:text")
    assert text is not None and (text.count, text.n) == (4, 5)


def test_required_is_capped_when_some_covers_failed() -> None:
    """読めた 4 本の 4/4 を「必須条件（全員）」と呼ばない（読めなかった 1 本が反例かもしれない）。

    壊し方: cover_tier の上限を外す → 必須条件になり赤。
    """
    assert cover_tier(4, 4, 5) == TIER_MAJORITY
    assert cover_tier(5, 5, 5) == TIER_REQUIRED
    assert cover_tier(2, 2, 5) == TIER_OBSERVED
    board = prod_board_with_covers()
    board[4].cover_read = CoverRead(rank=5, group="top", status="timeout", reason="deadline")
    view = cover_view(board, prod_videos(), QUERY, ROSTER)
    result = view.feature("cover:el:result")
    assert result is not None and (result.count, result.n) == (4, 4)
    assert result.tier == TIER_MAJORITY


def test_top_group_is_display_order_not_analysis_success() -> None:
    """#2 の動画を分析できなくても、#2 の表紙は上位の群（表示順）に入る。

    壊し方: 上位の群を ctx.facts（分析できた本）で切る → #2 が抜けて赤。
    """
    board = prod_board_with_covers()
    videos = [v for v in prod_videos() if v.meta.rank != 2]
    videos.append(AnalyzedVideo(meta=board[1], error="分析失敗: RuntimeError"))
    view = cover_view(board, videos, QUERY, ROSTER)
    assert [c.rank for c in view.top] == [1, 2, 3, 4, 5]
    two = view.by_rank(2)
    assert two is not None and two.ok and not two.watched and two.opening_match == ""


def test_cover_features_are_separate_from_video_features() -> None:
    """表紙の特徴は ctx.features（見出しと R4 の材料）に混ぜない。"""
    ctx = SynthesisContext.build(
        prod_videos(), QUERY, board=prod_board_with_covers(), roster=ROSTER
    )
    assert ctx.cover.features and not any(f.id.startswith("cover:") for f in ctx.features)
    plain = SynthesisContext.build(prod_videos(), QUERY, board=prod_board(), roster=ROSTER)
    assert [f.id for f in plain.features] == [f.id for f in ctx.features]
    assert ctx.cover_n == 5 and plain.cover_n == 0


# ── 上位とほかの差 ─────────────────────────────────────────────────────


def test_fisher_matches_the_known_values() -> None:
    assert fisher_two_sided(5, 5, 10, 25) == pytest.approx(0.0421, abs=1e-4)
    assert fisher_two_sided(5, 5, 13, 25) == pytest.approx(0.0657, abs=1e-4)
    assert fisher_two_sided(4, 5, 5, 25) == pytest.approx(0.0195, abs=1e-4)
    assert fisher_two_sided(3, 5, 5, 25) == pytest.approx(0.1020, abs=1e-4)
    assert fisher_two_sided(5, 5, 5, 25) == pytest.approx(0.0018, abs=1e-4)


def test_holm_step_down() -> None:
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def _gap_board(rest_kw: set[int]) -> list[VideoMeta]:
    return prod_board_with_covers(rest_kw=rest_kw)


def test_gap_marks_only_large_and_corrected_differences() -> None:
    """5/5 対 2/25 は印（Holm 後も残る）・4/5 対 5/25 は印なし（行が多いと 0.0195 は残らない）。

    壊し方: Holm を外す（補正前の p で判定）→ 4/5 対 5/25 に印が付いて赤。
    """
    view = cover_view(_gap_board({6, 7}), prod_videos(), QUERY, ROSTER)
    assert view.mode == "board"
    kw = view.gap_row("cover:kw:スパイスカレー")
    assert kw is not None and (kw.a, kw.n, kw.b, kw.m) == (4, 5, 2, 25)
    assert kw.marked  # 0.80 対 0.08
    text = view.gap_row("cover:text")
    assert text is not None and (text.a, text.n, text.b, text.m) == (4, 5, 25, 25)
    assert not text.marked
    five = cover_view(_gap_board(set(range(6, 11))), prod_videos(), QUERY, ROSTER)
    row = five.gap_row("cover:kw:スパイスカレー")
    assert row is not None and (row.b, row.m) == (5, 25) and not row.marked


def test_gap_excludes_image_posts_and_requires_read_share() -> None:
    """画像投稿（skipped）はほかの群の母数にも読めた割合にも入れない。7 割未満なら印を付けない。

    壊し方: 読めた割合に skipped を入れる → 16/25＝0.64 で印が消えて赤。
    """
    board = _gap_board({20})
    for m in board[5:14]:  # 6〜14 位の 9 本は画像投稿
        m.cover_read = CoverRead(rank=m.rank, group="rest", status="skipped", reason="image_post")
    view = cover_view(board, prod_videos(), QUERY, ROSTER)
    kw = view.gap_row("cover:kw:スパイスカレー")
    assert kw is not None and (kw.b, kw.m) == (1, 16)
    assert kw.marked and "7割未満" not in view.gap_note
    for m in board[10:20]:
        m.cover_read = CoverRead(rank=m.rank, group="rest", status="fetch_failed")
    low = cover_view(board, prod_videos(), QUERY, ROSTER)
    assert not any(g.marked for g in low.gap) and "7割未満" in low.gap_note


def test_gap_needs_same_input_width() -> None:
    board = _gap_board({6, 7})
    for m in board[5:]:
        assert m.cover_read is not None
        m.cover_read = m.cover_read.model_copy(update={"img_w": 1080, "img_h": 1920})
    view = cover_view(board, prod_videos(), QUERY, ROSTER)
    assert not any(g.marked for g in view.gap) and "幅が違う" in view.gap_note


def test_no_gap_rows_when_rest_was_not_read() -> None:
    """6〜30 位を読んでいなければ差の表を出さない（「ほか 1/1」のような行を作らない）。"""
    view = cover_view(prod_board_with_covers(), prod_videos(), QUERY, ROSTER)
    assert view.mode == "top" and view.gap == () and "読んでいない" in view.gap_note


# ── コードが作る指示 ───────────────────────────────────────────────────


def test_code_directives_say_they_were_not_compared() -> None:
    """6〜30 位と比べていない指示にはそう書く。「文字がある」だけの指示は作らない。

    壊し方: 注記を外す → 赤。
    """
    view = cover_view(prod_board_with_covers(), prod_videos(), QUERY, ROSTER)
    dirs = code_cover_directives(view)
    assert 1 <= len(dirs) <= 3
    assert all(NOT_COMPARED in d.text and d.kind == "表紙" for d in dirs)
    assert all(d.origin == "code" for d in dirs)
    assert dirs[0].text.startswith("表紙の文字に「スパイスカレー」を入れる")  # 具体的な言い方が先
    assert not any(d.text.startswith("表紙に文字") for d in dirs)
    assert all(
        r.on == "cover" and r.source in ("cover_text", "cover_note") for d in dirs for r in d.refs
    )
    # 根拠は再生の多い順（#4 → #1）
    assert [r.rank for r in dirs[0].refs] == [4, 1]
    large = next(d for d in dirs if "読める大きさ" in d.text)
    assert "11.5〜14%" in large.text and "中央値13字" in large.text


def test_board_mode_directives_skip_what_is_common_everywhere() -> None:
    """6〜30 位でも同じくらい多い特徴（タップの差と言えない）は指示にしない。差の印は A/B で。"""
    view = cover_view(_gap_board({6, 7}), prod_videos(), QUERY, ROSTER)
    dirs = code_cover_directives(view)
    assert dirs[0].text.startswith("表紙の文字に「スパイスカレー」を入れる形を A/B で確かめる")
    assert not any(NOT_COMPARED in d.text for d in dirs)
    common = cover_view(_gap_board(set(range(6, 31))), prod_videos(), QUERY, ROSTER)
    assert not any("スパイスカレー" in d.text for d in code_cover_directives(common))


def test_cover_line_uses_code_names_and_counts_only() -> None:
    view = cover_view(prod_board_with_covers(), prod_videos(), QUERY, ROSTER)
    line = cover_line(view)
    assert line.startswith("サムネ（一覧の表紙）: ")
    assert "4/5（多数派）" in line and NOT_COMPARED in line
    for third_party in ("わたしとスパイスカレー", "とにかく痩せたい", "ハーブ専科"):
        assert third_party not in line
    failed = prod_board()
    for m in failed[:5]:
        m.cover_read = CoverRead(rank=m.rank, status="fetch_failed")
    assert "すべて読めず" in cover_line(cover_view(failed, prod_videos(), QUERY, ROSTER))


def test_rest_cover_facts_case_tier() -> None:
    """ほかの群の表紙は上位の特徴の表に数えない。"""
    board = prod_board_with_covers()
    board[5].cover_read = rest_cover_read(6, kw=True)
    view = cover_view(board, prod_videos(), QUERY, ROSTER)
    assert view.mode == "board" and [c.rank for c in view.rest] == [6]
    kw = view.feature("cover:kw:スパイスカレー")
    assert kw is not None and 6 not in kw.ranks and kw.tier != TIER_CASE
