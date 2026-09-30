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
    cover_count_text,
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
    unsupported_concept,
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


@pytest.mark.parametrize(
    "text", ["本格スパイスカレー", "簡単スパイスカレー", "スパイスカレーの作り方", "スパイスカレー"]
)
def test_identical_text_is_same_even_when_only_a_short_word_remains(text: str) -> None:
    """表紙の文字と冒頭のテロップが 1 字違わず同じなら same（検索語＋短い修飾でも kw_only にしない）。

    09-29 本番 #4 の表紙は「本格スパイスカレー」。検索語を除くと「本格」2 字になり kw_only に落ち、
    own_text（表紙だけの文字）が多数派になって「表紙だけの文字を入れる」という逆の指示が出ていた。
    壊し方: 元の文字どうしの一致の判定を外す → kw_only になり赤。
    """
    assert text_match(text, text, TERMS) == "same"
    assert text_match(f"【{text}】", f"{text}！", TERMS) == "same"  # 記号の違いは無視
    # 一方がもう一方を含む（残りも含む）: 表紙の文字がそのままテロップに入っている
    assert text_match("本格スパイスカレー", "本格スパイスカレーの作り方", TERMS) == "same"


def test_identical_opening_telop_is_not_own_text() -> None:
    """表紙の文字＝0 秒のテロップなら own_text は False（「表紙だけの文字」の指示を出さない）。"""
    board = prod_board_with_covers()
    videos = prod_videos()
    same = {1: "本格スパイスカレー", 2: "簡単スパイスカレー", 3: "スパイスカレーの作り方"}
    for v in videos:
        if v.meta.rank in same and v.analysis is not None:
            v.analysis.telops[0].text = same[v.meta.rank]
            v.analysis.telops[0].sec = 0.0
    for m in board:
        if m.rank in same and m.cover_read is not None and m.cover_read.texts:
            first = m.cover_read.texts[0].model_copy(update={"text": same[m.rank]})
            m.cover_read = m.cover_read.model_copy(update={"texts": [first]})
    view = cover_view(board, videos, QUERY, ROSTER)
    for rank in same:
        c = view.by_rank(rank)
        assert c is not None and c.opening_match == "same" and c.own_text is False
    assert view.feature("cover:own_text") is None
    assert not any("表紙だけの文字" in d.text for d in code_cover_directives(view))


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
        ("ＮＧな切り方", True, False),  # 全角も NFKC で拾う
        ("間違えがちな炒め方", True, False),
        ("それ、失敗してます", True, False),
        # 英語の -ING（COOKING・MORNING）の「NG」は警告ではない（壊し方: IGNORECASE に戻す → 赤）
        ("MORNING ROUTINE", False, False),
        ("Cooking Vlog", False, False),
        ("スパイスカレー COOKING", False, False),
        ("EATING SHOW", False, False),
        ("ダメ元で作ったら絶品", False, False),  # ほめる文脈
        ("これはダメ", True, False),
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
                {"text": "小さな注記", "box_2d": [900, 100, 930, 600], "vertical": False},
                {"text": "大きい\n見出し", "box_2d": [100, 50, 300, 950], "vertical": False},
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
            "texts": [{"text": "a\nb\nc", "box_2d": [800, 0, 900, 1000], "vertical": False}],
        }
    )
    assert cover_facts(small, _meta(), None, QUERY).large_text is False  # 1 行 3.3%×1.78
    no_box = CoverRead.model_validate(
        {"status": "ok", "elements": [], "face": None, "texts": ["見出し"]}
    )
    c = cover_facts(no_box, _meta(), None, QUERY)
    assert c.large_text is None and c.position == "unknown"  # 分からない（無いと数えない）


def _prod5_vertical(vertical: bool | None) -> CoverRead:
    """09-29 本番 #5 の形（240×426・縦書き 3 列「市販の／カレールーは／卒業！」）。"""
    return CoverRead.model_validate(
        {
            "status": "ok",
            "img_w": 240,
            "img_h": 426,
            "elements": ["process", "text_main"],
            "face": {"kind": "none"},
            "texts": [
                {
                    "text": "市販の\nカレールーは\n卒業！",
                    "box_2d": [293, 467, 739, 667],
                    "vertical": vertical,
                }
            ],
        }
    )


def test_vertical_text_size_comes_from_the_column_width() -> None:
    """縦書きの字の大きさは枠の幅÷列の数（高さ÷行で測ると列の長さになり 3 倍前後に出る）。

    本番 #5: 実際の字は画像で 1 字約 20px（高さ 426 の約 4.5%）。高さ÷3 行だと 14.9%・
    「読める大きさ」になっていた。
    壊し方: 縦書きでも高さ÷行で測る → 14.9 になり赤。
    """
    c = cover_facts(_prod5_vertical(True), _meta(5), None, QUERY)
    assert c.vertical is True and c.lines == 3
    # 幅 200/1000×240px÷3 列＝16px → 高さ 426 の 3.8%
    assert c.line_h_pct == pytest.approx(3.8, abs=0.05)
    assert c.large_text is False  # 16px は幅 240 の 1/10（24px）に届かない
    horizontal = cover_facts(_prod5_vertical(False), _meta(5), None, QUERY)
    assert horizontal.line_h_pct == pytest.approx(14.9, abs=0.05)
    unknown = cover_facts(_prod5_vertical(None), _meta(5), None, QUERY)
    # 縦横が分からなければ字の大きさは測らない（母数から外す）。行数は改行から数える。
    assert unknown.line_h_pct is None and unknown.large_text is None and unknown.lines == 3


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
    """但し書き（6〜30位とは比べていない）は先頭に置く（スライドの 1 行で末尾が切れても見える）。

    「表紙に文字がある」は情報が少ないので最後（上位 3 つに入らない）。
    壊し方: _LINE_ORDER の先頭に cover:text を戻す → 1 つ目が「表紙に文字がある」になり赤。
    """
    view = cover_view(prod_board_with_covers(), prod_videos(), QUERY, ROSTER)
    line = cover_line(view)
    assert line.startswith(f"サムネ（一覧の表紙・{NOT_COMPARED}）: 表紙の文字に「スパイスカレー」")
    assert "4/5（多数派）" in line and "表紙に文字がある" not in line
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


# ── 差の向き・比べられなかった 6〜30 位・根拠の選び方・母数・中身の照らし合わせ ─────────


def _faces_in_rest() -> list[VideoMeta]:
    """上位は #1 だけ実写の顔（1/5）・6〜30 位は 25 本とも実写の顔（逆向きの差）。"""
    board = prod_board_with_covers(rest_kw=set())
    four = board[3].cover_read
    assert four is not None and four.face is not None
    board[3].cover_read = four.model_copy(
        update={"face": four.face.model_copy(update={"kind": "none", "box": None})}
    )
    for m in board[5:]:
        assert m.cover_read is not None and m.cover_read.face is not None
        m.cover_read = m.cover_read.model_copy(
            update={
                "face": m.cover_read.face.model_copy(update={"kind": "real", "gaze": "camera"}),
                "elements": ["result", "person"],
            }
        )
    return board


def test_reverse_gap_marks_do_not_become_directives() -> None:
    """上位 1/5・ほか 25/25 の差の印は「ほかが多い」。「入れる」の指示にしない（表示だけ）。

    壊し方: 差の印の向きを見ない（abs だけ）→ 「顔を入れる形を A/B」が出て赤。
    """
    view = cover_view(_faces_in_rest(), prod_videos(), QUERY, ROSTER)
    row = view.gap_row("cover:face")
    assert row is not None and (row.a, row.n, row.b, row.m) == (1, 5, 25, 25)
    assert row.marked and row.direction == "rest" and row.mark_text == "ほかが多い"
    texts = [d.text for d in code_cover_directives(view)]
    assert not any("顔" in t or "人を入れる" in t for t in texts)
    assert "ほかが多い" in cover_line(view)  # 1 行でも向きを出す


def test_marked_rows_need_a_top_majority() -> None:
    """上位が多い向きの印でも、上位で多数派でなければ指示にしない（事例 2/5 を最優先にしない）。

    壊し方: 印の分岐から多数派の条件を外す → 事例の「顔を入れる形を A/B」が出て赤。
    """
    from dataclasses import replace

    from teamagent.skills.video_algorithm.cover_facts import GapRow

    view = cover_view(prod_board_with_covers(), prod_videos(), QUERY, ROSTER)
    face = view.feature("cover:face")
    assert face is not None and face.tier == TIER_CASE
    forced = replace(
        view,
        mode="board",
        gap=(GapRow("cover:face", face.label, 2, 5, 0, 25, 0.01, True),),
    )
    assert forced.gap[0].direction == "top"
    assert not any("顔" in d.text for d in code_cover_directives(forced))


def test_reverse_marked_row_is_not_a_directive_even_with_a_top_majority() -> None:
    """上位で多数派（3/5）でも、ほかのほうが多い（25/25）印の行は「入れる」の指示にしない。

    上位 5 本では Holm の後に印が残りにくいが、上位 10 本などでは起きうる形を直接作って確かめる。
    壊し方: 印の向き（direction）を見ない → 「主役に寄った画にする形を A/B」が出て赤。
    """
    from dataclasses import replace

    from teamagent.skills.video_algorithm.cover_facts import GapRow

    view = cover_view(prod_board_with_covers(), prod_videos(), QUERY, ROSTER)
    closeup = view.feature("cover:closeup")
    assert closeup is not None and closeup.tier == TIER_MAJORITY and closeup.count == 3
    forced = replace(
        view,
        mode="board",
        gap=(GapRow("cover:closeup", closeup.label, 3, 5, 25, 25, 0.01, True),),
    )
    assert forced.gap[0].direction == "rest"
    assert not any("寄った画" in d.text for d in code_cover_directives(forced))


def test_board_mode_with_no_rest_read_is_not_compared() -> None:
    """6〜30 位が全部 timeout なら比べていない（「差の印なし」と出さない）。

    壊し方: mode を「rest があれば board」に戻す → 「6〜30位との差の印なし」と出て赤。
    """
    board = prod_board_with_covers()
    for m in board[5:]:
        m.cover_read = CoverRead(rank=m.rank, group="rest", status="timeout", reason="deadline")
    view = cover_view(board, prod_videos(), QUERY, ROSTER)
    assert view.mode == "top" and view.gap == ()
    assert view.gap_note == "6〜30位の表紙は読めなかった（時間内に読めず25本・比べていない）"
    line = cover_line(view)
    assert NOT_COMPARED in line and "差の印なし" not in line
    assert all(NOT_COMPARED in d.text for d in code_cover_directives(view))


def test_example_quotes_skip_covers_with_avoid_terms() -> None:
    """避けたい訴求の語を含む表紙の文字は、コードの指示の根拠（お手本）に引用しない。

    壊し方: _example_refs で avoid_terms を見ない → #4 の「とにかく痩せたい」が根拠に出て赤。
    """
    view = cover_view(prod_board_with_covers(), prod_videos(), QUERY, ROSTER)
    plain = code_cover_directives(view)
    assert [r.rank for r in plain[0].refs] == [4, 1]
    avoided = code_cover_directives(view, ["痩せたい"])
    refs = [r for d in avoided for r in d.refs]
    assert refs and not any("痩せたい" in r.quote for r in refs)
    assert 4 not in [r.rank for r in avoided[0].refs]
    # 画の特徴（主役の説明を引く）でも、避けたい語を含む説明は使わず次に再生の多い本にする
    from teamagent.skills.video_algorithm.cover_facts import _example_refs

    ranks = [1, 2, 3, 4, 5]
    assert [r.rank for r in _example_refs("cover:el:result", ranks, view)] == [4, 1]
    image = _example_refs("cover:el:result", ranks, view, ["湯気の立つ皿"])
    assert [(r.rank, r.quote) for r in image] == [(4, "カレーを食べる人"), (5, "店の皿のカレー")]


def test_count_text_shows_the_denominator_when_it_is_smaller() -> None:
    """母数が上位の本数より少ないときは母数の名前と「上位 n 本中 c 本」を添える。

    壊し方: 母数の名前を出さない → 2/3 が上位 5 本中の多数派に見えて赤。
    """
    from teamagent.skills.video_algorithm.facts import Feature

    legible = Feature("cover:legible", "文字が背景から読みやすい（AI判定）", (4, 5), 3, "多数派")
    assert cover_count_text(legible, 5) == (
        "多数派 2/3（文字のある表紙のうち・#4・#5・上位5本中2本）"
    )
    kw = Feature("cover:kw:スパイスカレー", "x", (1, 2, 3, 4), 5, "多数派")
    assert cover_count_text(kw, 5) == "多数派 4/5（#1・#2・#3・#4）"
    assert cover_count_text(Feature("cover:el:result", "x", (1, 2, 3, 4, 5), 5, "必須条件"), 5) == (
        "必須条件 5/5"
    )


def test_concepts_in_a_directive_must_be_on_the_quoted_covers() -> None:
    """「顔をカメラ目線で」を顔の無い表紙（#2・#3・#5）の引用で通さない。打ち消しは逆に確かめる。"""
    view = cover_view(prod_board_with_covers(), prod_videos(), QUERY, ROSTER)
    assert unsupported_concept("表紙は実写の人の顔をカメラ目線で入れる", [2, 3, 5], view) == (
        "cover:gaze_camera"
    )
    assert unsupported_concept("表紙に顔を入れる", [2, 3, 5], view) == "cover:face"
    assert unsupported_concept("表紙に顔を入れる", [1], view) is None
    assert unsupported_concept("表紙に顔は入れず料理を大きく見せる", [2], view) is None
    assert unsupported_concept("表紙で湯気を見せる", [4], view) == "cover:sizzle"
    assert unsupported_concept("表紙で湯気を見せる", [3], view) is None
    assert unsupported_concept("表紙の文字は短くする", [2], view) is None
    # 文字の位置の「上寄り」は寄りの画ではない（#4 は寄りではないが落とさない）
    assert unsupported_concept("表紙の文字は上寄りに置く", [4], view) is None
    assert unsupported_concept("表紙は料理に寄った画にする", [4], view) == "cover:closeup"
