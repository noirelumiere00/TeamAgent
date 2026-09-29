"""サムネ（一覧の表紙）の描画（レポート・スライド・Slack）。

- 第三者の文字（表紙の文字・主役の説明・商品名）はエスケープする（`<img onerror>` の形）。
- 以前のキャッシュ（表紙の読み取りが無い v2 の出力）でも落ちずに「以前の分析」と出す。
- スライドは表紙を読めたときだけ 2 枚足す（12＋n 枚）。以前のキャッシュは 10＋n 枚のまま。
  6〜30 位の表紙は外部 URL なので画像を載せない。
- 色は 1 行の参考に縮める（色の格子は出さない）。
壊し方（→ 赤）は各テストの docstring。
"""

from __future__ import annotations

import re

from teamagent.media.operations import _EXTERNAL_HTML_REF
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.evidence import Roster
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import CoverRead, VideoAlgorithmOutput
from teamagent.skills.video_algorithm.slack_render import completion_message
from teamagent.skills.video_algorithm.slides import render_slides
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    prod_board,
    prod_board_with_covers,
    prod_videos,
    prod_videos_with_covers,
)

STAMP = "2026-09-29T10:00:00+09:00"
_XSS = '<img src=x onerror="alert(1)">'


def _out(
    *, board: list | None = None, mode: str = "top", rest_kw: set[int] | None = None
) -> VideoAlgorithmOutput:  # type: ignore[type-arg]
    roster = Roster.of(CLIENT, COMPETITORS)
    videos = prod_videos_with_covers()
    board = board if board is not None else prod_board_with_covers(rest_kw=rest_kw)
    cross = cross_analyze(videos, QUERY, board=board, roster=roster)
    return VideoAlgorithmOutput(
        query=QUERY,
        videos=videos,
        board=board,
        cross=cross,
        client_name=roster.client_name,
        competitors=list(roster.competitors),
        generated_at=STAMP,
        cover_read_mode=mode,  # type: ignore[arg-type]
    )


def _kinds(html: str) -> list[str]:
    return re.findall(r'<section class="slide[^"]*" data-slide="([^"]+)"', html)


def test_third_party_cover_text_is_escaped() -> None:
    """壊し方: 表紙の文字の _esc を外す → 生の <img onerror> が出て赤。"""
    board = prod_board_with_covers()
    read = board[0].cover_read
    assert read is not None
    first = read.texts[0] if read.texts else None
    assert first is not None
    board[0].cover_read = read.model_copy(
        update={
            "subject_note": _XSS[:30],
            "texts": [first.model_copy(update={"text": _XSS})],
            "brand_text": [_XSS[:30]],
        }
    )
    out = _out(board=board)
    for html in (render_report(out, generated_at=STAMP), render_slides(out, generated_at=STAMP)):
        assert "<img src=x" not in html
        assert "&lt;img src=x onerror=" in html


def test_report_shows_the_cover_section_and_only_one_color_line() -> None:
    html = render_report(_out(), generated_at=STAMP)
    assert "サムネ（一覧の表紙）の比較" in html
    assert "表紙の文字（AIの読み取り）" in html
    assert "上位の表紙に共通する作り（タップ率は取れない・順位との関係のみ）" in html
    assert "6〜30位の表紙はまだ読んでいない" in html
    assert "表紙の作り方" in html and "表紙の文字（AI読み取り）「" in html
    assert "サムネ色の比較（表紙の色・参考）" not in html  # 色の格子は消した
    assert html.count('class="tbar') == 0
    assert 'class="cvbox' in html  # AI が読んだ枠を画像に重ねる
    assert "<th>表紙の文字（AI）</th>" in html  # 取得ボードの列


def test_old_cache_without_covers_draws_the_previous_analysis_note() -> None:
    """以前のキャッシュ（cover_read も cover_read_mode も無い）でも落ちない。

    壊し方: 表紙が無いときのガードを外す → 空の格子か例外になり赤。
    """
    roster = Roster.of(CLIENT, COMPETITORS)
    videos, board = prod_videos(), prod_board()
    old = VideoAlgorithmOutput.model_validate(
        {
            "query": QUERY,
            "videos": [v.model_dump(mode="json") for v in videos],
            "board": [m.model_dump(mode="json") for m in board],
            "cross": cross_analyze(videos, QUERY, board=board, roster=roster).model_dump(
                mode="json"
            ),
        }
    )
    assert old.cover_read_mode == "" and all(m.cover_read is None for m in old.board)
    html = render_report(old)
    assert "表紙の分析なし（以前の分析）" in html
    kinds = _kinds(render_slides(old))
    assert "thumb-compare" not in kinds and "thumb-plan" not in kinds
    assert "サムネ（一覧の表紙）の分析なし（以前の分析）" in render_slides(old)


def test_slides_add_two_cover_slides_after_the_compare() -> None:
    """壊し方: slide_sections のガード（1 本も読めていなければ出さない）を外す → 以前の分析でも +2 で赤。"""
    kinds = _kinds(render_slides(_out(), generated_at=STAMP))
    i = kinds.index("compare")
    assert kinds[i + 1 : i + 3] == ["thumb-compare", "thumb-plan"]
    before = _kinds(render_slides(_out(board=prod_board(), mode=""), generated_at=STAMP))
    assert len(kinds) == len(before) + 2
    assert [k for k in kinds if not k.startswith("thumb-")] == before
    assert "cover" in kinds and kinds.count("cover") == 1  # 資料の 1 枚目と混ぜない


def test_slides_never_embed_external_urls_even_in_board_mode() -> None:
    out = _out(mode="board", rest_kw={6, 7})
    html = render_slides(out, generated_at=STAMP)
    assert not _EXTERNAL_HTML_REF.search(html)
    assert "p16.example" not in html
    assert "上位とほかの表紙（参考・因果ではない）" in html


def test_off_mode_and_no_cover_url_messages() -> None:
    off = _out(board=prod_board(), mode="off")
    assert "表紙の分析は止めている設定です" in render_report(off)
    board = prod_board()
    for m in board[:5]:
        m.cover_read = CoverRead(rank=m.rank, group="top", status="no_cover", reason="no_cover_url")
    none = _out(board=board)
    assert "表紙の URL が無い取り方のため" in render_report(none)
    assert "thumb-compare" not in _kinds(render_slides(none))
    failed = prod_board()
    for m in failed[:5]:
        m.cover_read = CoverRead(rank=m.rank, group="top", status="fetch_failed")
    assert "表紙を読めず（上位5本すべて・表紙を取得できず5本）" in render_report(_out(board=failed))


def test_slack_line_has_counts_but_no_cover_text() -> None:
    out = _out()
    assert out.cross.cover_line.startswith("サムネ（一覧の表紙・6〜30位とは比べていない）: ")
    msg = completion_message(out)
    assert msg is not None
    assert "サムネ（一覧の表紙・" in msg.text
    for third_party in ("わたしとスパイスカレー", "とにかく痩せたい", "無水スパイスカレー"):
        assert third_party not in msg.text


def _faces_in_rest_out() -> VideoAlgorithmOutput:
    from tests.skills.video_algorithm.test_cover_facts import _faces_in_rest

    return _out(board=_faces_in_rest(), mode="board")


def test_gap_marks_show_which_side_has_more() -> None:
    """差の印に向き（上位が多い／ほかが多い）を出す。スライドの印の列は切れない幅にする。

    壊し方: 印を「差が大きい」だけに戻す → 向きが出ず赤。
    """
    out = _faces_in_rest_out()
    report = render_report(out, generated_at=STAMP)
    assert "<th>差の印（多い側）</th>" in report and "ほかが多い（参考）" in report
    slides = render_slides(out, generated_at=STAMP)
    assert "<th>差の印</th>" in slides and "<td>ほかが多い</td>" in slides
    assert "差が大きい</td>" not in slides


def test_slide_row_names_what_was_read_not_the_hero() -> None:
    """写っている要素の全部を「主役」と呼ばない（主役の説明と読み違えないように）。"""
    html = render_slides(_out(), generated_at=STAMP)
    assert "写っている要素（AI）" in html and '<div class="lab c1">主役</div>' not in html


def test_directive_lines_show_origin_and_denominator() -> None:
    """表紙の指示の行に出どころ（コードの集計／AI の提案）と母数を出す。

    壊し方: 行の札を段階と順位だけに戻す → 「冒頭のテロップと比べられた表紙のうち」が消えて赤。
    """
    html = render_report(_out(), generated_at=STAMP)
    assert (
        "〔コードの集計｜多数派 4/5（#1・#2・#3・#4）｜根拠 #4 表紙の文字（AI読み取り）「" in html
    )
    assert "冒頭のテロップと比べられた表紙のうち・#1・#3・#4・上位5本中3本" in html
    slides = render_slides(_out(), generated_at=STAMP)
    assert "コードの集計" in slides and "AI の指示（根拠は照合済み）" not in slides


def test_conclusion_line_puts_the_scope_first() -> None:
    """S2 の表紙の 1 行は但し書きを先頭に置く（1 行で切れても「比べていない」が見える）。"""
    html = render_slides(_out(), generated_at=STAMP)
    assert '<div class="note c1">サムネ（一覧の表紙・6〜30位とは比べていない）: ' in html
