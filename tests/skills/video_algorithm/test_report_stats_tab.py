"""レポートの統計タブの止血（仕様 v3 §4-3 の一部）と、レポートに残っていた Gemini の申告の置き換え。

- 結論の帯: 勝ち筋のチップ → 段階の名前つきの事実（スライドと同じ facts・synthesis v3）
- ρ は本文に出さず、付録の「特徴×表示順位（参考・n=…・有意性なし）」表だけ。向きの文はコード
- 共通の導線: 見出し「共通の導線（保存・誘導）」・多数派はコードの集計（来店・保存の多数派は誤り）
- 取得ボード: キャプションは先頭 46 字＋「…」（句点で切らない）・PR と投稿日
- テロップ全文の表は秒の昇順・KW 列は照合済み（✓ 完全一致・≈ 言い換え）
- 英語の内部値の和訳（フックの型・テンポ・エンゲージメント率）
- コマの見出しは「秒｜役割」だけ（T18）・競合の縞は名簿で決める（Gemini の relation は使わない）
"""

from __future__ import annotations

import html as html_lib
import re

from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.evidence import Roster
from teamagent.skills.video_algorithm.report import render_report, rho_direction
from teamagent.skills.video_algorithm.schema import CrossSynthesis, VideoAlgorithmOutput
from teamagent.skills.video_algorithm.synthesis_checks import finalize
from teamagent.skills.video_algorithm.synthesis_input import SynthesisContext
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    prod_board,
    prod_synthesis,
    prod_videos,
)
from tests.skills.video_algorithm.test_synthesis_v3 import _V3

STAMP = "2026-09-28T10:15:00+09:00"


def _out(*, synthesis: str = "v3", client: bool = False) -> VideoAlgorithmOutput:
    roster = Roster.of(CLIENT, COMPETITORS) if client else Roster()
    videos, board = prod_videos(), prod_board()
    cross = cross_analyze(videos, QUERY, board=board, roster=roster)
    if synthesis == "v3":
        ctx = SynthesisContext.build(videos, QUERY, board=board, roster=roster)
        cross.synthesis = finalize(CrossSynthesis.model_validate(_V3), ctx)
    elif synthesis == "v2":
        cross.synthesis = prod_synthesis()
    return VideoAlgorithmOutput(
        query=QUERY,
        videos=videos,
        board=board,
        cross=cross,
        client_name=roster.client_name,
        competitors=list(roster.competitors),
        generated_at=STAMP,
    )


def _overview(html: str) -> str:
    return html.split('data-ttp="ov">', 1)[1].split('<div class="ttpane" data-ttp="v', 1)[0]


def _text(fragment: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", fragment))


def test_report_header_has_the_stamp() -> None:
    """T20: レポート冒頭に取得日時（JST）。"""
    html = render_report(_out(), generated_at=STAMP)
    meta = html.split("<div class='meta'>", 1)[1].split("</div>", 1)[0]
    assert "取得 2026-09-28 10:15（JST）" in meta
    assert "順位は2026-09-28 10:15（JST）時点" in html  # フッタの注意書き


def test_verdict_band_uses_code_tiers_and_checked_directives() -> None:
    """壊し方: 勝ち筋チップ（win_factors）に戻す／v2 の文を描く → 赤。"""
    html = render_report(_out(), generated_at=STAMP)
    band = html.split('<section class="verdict planner">', 1)[1].split("</section>", 1)[0]
    text = _text(band)
    assert "必須条件最初のテロップが0秒台5/5" in text
    assert "多数派分量をテロップに出す3/5（#3・#4・#5）" in text
    assert (
        "最初のテロップを0秒台に出す〔必須条件 5/5｜根拠 #4 0秒「とにかく痩せたいから」〕" in text
    )
    assert "最も見られ保存された1本は#4（再生・保存率・シェアが5本で最大）" in text
    assert "勝ち筋" not in text and "勝つ" not in text and "御社" not in text


def test_legacy_v2_synthesis_text_is_not_rendered() -> None:
    """v3 の検査を通していない旧キャッシュの文（ρ・〔上位 2/5〕・来店）は出さない（T2・T14）。"""
    html = render_report(_out(synthesis="v2"), generated_at=STAMP)
    overview = _overview(html)
    for word in ("スパイス4選と30分調理", "〔上位", "御社", "来店", "visit", "problem_solving"):
        assert word not in overview, word
    assert "最初のテロップを0秒台に出す" in overview  # コードの事実の指示に代わる


def test_rho_appears_only_in_the_appendix_table() -> None:
    """T2: ρ は本文に出さず、付録の「特徴×表示順位（参考・n=5・有意性なし）」表だけ。"""
    for kind in ("v3", "v2"):
        html = render_report(_out(synthesis=kind), generated_at=STAMP)
        overview = _overview(html)
        body, appendix = overview.split('<details class="appendix">', 1)
        assert "ρ" not in body and "相関" not in _text(body).replace("相関は因果ではない", "")
        assert "特徴×表示順位（参考・n=5・有意性なし）" in appendix
        assert "向き（コードの判定）" in appendix


def test_rho_direction_text_is_decided_by_code() -> None:
    assert rho_direction(-0.8) == "値が大きい動画ほど上位（参考）"
    assert rho_direction(0.82) == "値が小さい動画ほど上位（参考）"
    assert rho_direction(0.1) == "向きは弱い（参考にならない）"
    assert rho_direction(None) == "判定できない"


def test_shared_funnel_is_counted_by_code() -> None:
    """T14: 多数派なし（来店・保存ではない）。#4 の comment は無効。見出しは「保存・誘導」。"""
    html = render_report(_out(), generated_at=STAMP)
    funnel = html.split("共通の導線（保存・誘導）", 1)[1].split("</section>", 1)[0]
    text = _text(funnel)
    assert "動画内 CTA の多数派なし（過半数の型が無い）" in text
    assert "#2 プロフィール誘導・#3 試してみて" in text
    assert "#4 コメント（文言も秒も無い型だけの申告）" in text
    assert "保存 1/5（#2）" in text  # キャプションでの呼びかけ
    assert "来店" not in html and "保存→来店設計" not in html


def test_board_caption_is_cut_at_46_chars_not_at_the_period() -> None:
    """T30: 「【4つでいい。本格スパイスカレー】…」を句点で切らない。壊し方: _shorten に戻す → 赤。"""
    out = _out()
    head = "【4つでいい。本格スパイスカレー】 スパイスカレー。 料理が好きになったきっかけの一皿。"
    out.board[0] = out.board[0].model_copy(update={"desc": head + "あいうえお" * 20})
    html = render_report(out, generated_at=STAMP)
    cells = re.findall(r'<td class="sbcap">(.*?)</td>', html)
    assert cells[0] == html_lib.escape((head + "あいうえお" * 20)[:46] + "…")
    assert '<td class="sbcap">【4つでいい</td>' not in html


def test_board_marks_pr_and_post_date() -> None:
    html = render_report(_out(), generated_at=STAMP)
    board = html.split('<table class="sboard">', 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr>(.*?)</tr>", board, re.S)
    pr_ranks = [
        int(m.group(1)) for r in rows if (m := re.search(r'class="sbr">#(\d+)', r)) and "sbpr" in r
    ]
    assert pr_ranks == [1, 4, 12, 16, 20]  # T13
    assert "2022-11-14（換算）" in board and "<th>投稿日</th>" in board


def test_telop_table_is_in_time_order_with_verified_kw_marks() -> None:
    """T24: テロップ全文の表は秒の昇順。KW は照合済み（#4 21.5 秒のブランド名に ✓ を付けない）。

    壊し方: KW 優先の並べ替え（kw_match）に戻す → 赤。
    """
    html = render_report(_out(), generated_at=STAMP)
    pane = html.split('data-pane="t" data-i="3">', 1)[1].split("</table>", 1)[0]
    secs = [float(s) for s in re.findall(r"<tr[^>]*><td>([0-9.]+)秒</td>", pane)]
    assert secs == sorted(secs) and len(secs) == 28
    marks = dict(re.findall(r"<td>([0-9.]+)秒</td><td>([✓≈]?)</td>", pane))
    assert marks["5.0"] == "✓"  # 「無水スパイスカレー」
    assert marks["21.5"] == ""  # ブランド名（Gemini は kw_match=True と申告）
    pane2 = html.split('data-pane="t" data-i="1">', 1)[1].split("</table>", 1)[0]
    marks2 = dict(re.findall(r"<td>([0-9.]+)秒</td><td>([✓≈]?)</td>", pane2))
    assert marks2["10.0"] == "✓" and marks2["42.0"] == "≈"  # 言い換え（照合済み）


def test_internal_english_values_are_translated() -> None:
    html = render_report(_out(), generated_at=STAMP)
    top5 = html.split('<div class="board"', 1)[1].split("</section>", 1)[0]
    assert "問題提起" in top5 and "ビジュアル" in top5 and ">problem<" not in top5
    assert "<i>エンゲージメント率</i>" in html and "<i>エンゲージ</i>" not in html
    assert "<b>テンポ</b>ふつう" in html and "<b>テンポ</b>moderate" not in html


def test_top5_board_emphasises_the_best_video() -> None:
    """T16: 強調（上の青線）は #4。#1 ではない。"""
    html = render_report(_out(), generated_at=STAMP)
    cols = re.findall(r'<div class="bcol( is-top)?"><div class="brank">#(\d+)', html)
    assert [rank for top, rank in cols if top] == ["4"]


def test_frame_labels_in_the_report_are_seconds_and_role_only() -> None:
    """T18: コマの見出しにブランド名・「KWテロップ」を付けない。"""
    out = _out()
    for v in out.videos:
        for i, f in enumerate(v.frames):
            f.caption = ["フック", "KWテロップ 16s", "SPICIA 24s"][i % 3]
    html = render_report(out, generated_at=STAMP)
    for word in ("KWテロップ", "SPICIA 24s"):
        assert word not in html, word
    assert "0.8秒｜フック" in html


def test_competitor_stripe_uses_the_roster_not_gemini() -> None:
    """区分は名簿。名簿が無ければ競合の縞を出さない（Gemini の brand_relation は使わない）。"""
    unspecified = render_report(_out(), generated_at=STAMP)
    assert 'class="nclip c-brand comp"' not in unspecified
    named = render_report(_out(client=True), generated_at=STAMP)
    assert 'class="nclip c-brand comp"' in named
    assert "競合ブランドの映り込み" in named and "ハーブ専科" in named


def test_unmeasured_win_factors_are_hidden() -> None:
    out = _out()
    a = out.videos[0].analysis
    assert a is not None
    a.win_factors = ["視聴維持率を高く保つ", "0秒に題名のテロップ"]
    html = render_report(out, generated_at=STAMP)
    assert "視聴維持率" not in html and "0秒に題名のテロップ" in html
