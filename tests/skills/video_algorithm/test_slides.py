"""提案スライド（slides.render_slides・仕様 v3 §2）の純関数テスト（外部I/O無し）。

フェイクは本番の失敗の形を再現する（prod_shape: v2 の出力＝場面に役割が無い・#PR が 720 字より後・
Gemini が推測した client・文言も秒も無い comment・横長コマ・実在しないテロップへの言い換え）。
synthesis は本番の JSON の形（ρ タグ・〔上位 2/5〕・御社・スプーンで引き上げる・ルー卒業）を
v3 の検査（finalize）に通したものを使う。

変異テスト（修正を戻したら赤）の対応は各テストの docstring の「壊し方」。
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

import pytest

from teamagent.media.operations import _EXTERNAL_HTML_REF
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.evidence import Roster
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CrossSynthesis,
    FrameShot,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.slides import (
    NOEXPORT_CSS,
    SLIDE_H,
    SLIDE_W,
    build_deck,
    footer_text,
    pick_scene_frames,
    render_slides,
)
from teamagent.skills.video_algorithm.synthesis_checks import finalize
from teamagent.skills.video_algorithm.synthesis_input import SynthesisContext
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    jpeg_uri,
    prod_board,
    prod_synthesis,
    prod_videos,
)
from tests.skills.video_algorithm.test_synthesis_v3 import _V3

AVOID = ["ルー卒業"]
STAMP = "2026-09-28T10:15:00+09:00"


def _output(
    *,
    client: str | None = None,
    competitors: list[str] | None = None,
    avoid: list[str] | None = None,
    synthesis: str = "v3",
) -> VideoAlgorithmOutput:
    roster = Roster.of(client, competitors)
    videos, board = prod_videos(), prod_board()
    cross = cross_analyze(videos, QUERY, board=board, roster=roster)
    if synthesis == "v3":
        ctx = SynthesisContext.build(
            videos, QUERY, board=board, roster=roster, avoid_terms=avoid or []
        )
        cross.synthesis = finalize(CrossSynthesis.model_validate(_V3), ctx)
    elif synthesis == "v2":
        cross.synthesis = prod_synthesis()  # 旧キャッシュ（v3 の検査を通していない）
    return VideoAlgorithmOutput(
        query=QUERY,
        videos=videos,
        board=board,
        cross=cross,
        client_name=roster.client_name,
        competitors=list(roster.competitors),
        avoid_terms=list(avoid or []),
        generated_at=STAMP,
    )


def _sections(html: str) -> list[tuple[str, str]]:
    """(data-slide の種類, 中身) の並び。"""
    return re.findall(
        r'<section class="slide[^"]*" data-slide="([^"]+)"[^>]*>(.*?)</section>', html, re.S
    )


def _slide(html: str, kind: str) -> str:
    """スライドの中身（フッタは除く。フッタにも取得日時・タイアップが出るため）。"""
    body = next(body for k, body in _sections(html) if k == kind)
    return body.split('<div class="foot" data-foot>', 1)[0]


def _text(fragment: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", fragment))


# ── 流れ・枚数 ─────────────────────────────────────────────────────────────


def test_flow_is_ten_plus_n_slides_in_the_spec_order() -> None:
    """S1〜S6 → 構成分解 n 枚 → 型 → 指示 → 絵コンテ A → 投稿設計（10＋n 枚）。"""
    html = render_slides(_output(avoid=AVOID), generated_at=STAMP)
    kinds = [k for k, _b in _sections(html)]
    assert kinds == [
        "cover",
        "conclusion",
        "surface",
        "brands",
        "compare",
        "structure",
        "video-1",
        "video-2",
        "video-3",
        "video-4",
        "video-5",
        "template",
        "directives",
        "storyboard-案A",
        "posting",
    ]
    assert len(kinds) == 10 + 5
    assert f"--w:{SLIDE_W}px" in html and f"--h:{SLIDE_H}px" in html
    assert "contenteditable" in html


def test_storyboard_b_is_added_when_the_llm_gives_two() -> None:
    """ルー卒業の案は避けたい訴求で落ちる。避けたい訴求が無ければ案 B も出る（＋1 枚）。"""
    html = render_slides(_output(avoid=[]), generated_at=STAMP)
    kinds = [k for k, _b in _sections(html)]
    assert "storyboard-案B" in kinds and len(kinds) == 16


def test_empty_output_draws_only_the_cover() -> None:
    html = render_slides(VideoAlgorithmOutput(query="空のKW"))
    assert [k for k, _b in _sections(html)] == ["cover"]
    assert "data:video" not in html


# ── 全体ルール（フッタ・外部参照・動画・文字） ─────────────────────────────────


def test_every_slide_has_the_footer_with_stamp_and_pr() -> None:
    """壊し方: フッタを表紙以外だけにする／取得日時・タイアップを外す → 赤。"""
    out = _output(avoid=AVOID)
    html = render_slides(out, generated_at=STAMP)
    footer = footer_text(build_deck(out, generated_at=STAMP))
    assert footer == (
        "上位5本の観測にもとづく仮説・相関は因果ではない・順位は2026-09-28 10:15（JST）時点・"
        "秒はAI推定（±2秒）・上位にタイアップ表記2本（#1・#4）"
    )
    for kind, body in _sections(html):
        assert f'<div class="foot" data-foot>{footer}</div>' in body, kind  # 全スライド


def test_slides_never_embed_video_or_network_references() -> None:
    """media worker は外部参照のある HTML を拒否する（MEDIA_HTML_NETWORK_REFERENCE）。"""
    out = _output(avoid=AVOID)
    for v in out.videos:
        v.video_data_uri = "data:video/mp4;base64,SHOULD_NOT_APPEAR"
    html = render_slides(out, generated_at=STAMP)
    assert "data:video" not in html and "SHOULD_NOT_APPEAR" not in html
    assert _EXTERNAL_HTML_REF.search(html) is None
    assert "data:image/jpeg;base64," in html  # コマは data URI で載る


def test_css_font_sizes_are_at_least_14px_and_noexport_is_marked() -> None:
    html = render_slides(_output(), generated_at=STAMP)
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    sizes = [float(x) for x in re.findall(r"font-size:\s*([0-9.]+)px", style)]
    assert sizes and min(sizes) >= 14, sorted(set(sizes))
    assert "word-break:auto-phrase" in style
    assert "-webkit-line-clamp" in style
    assert '<div class="edit-tip" data-noexport>' in html
    assert NOEXPORT_CSS == "[data-noexport]{display:none!important}"


def test_no_winning_words_rho_or_client_honorifics_anywhere() -> None:
    """T2・T4・T8: 本番形の synthesis でも ρ・勝ち筋・御社がスライドに出ない。"""
    for out in (_output(avoid=AVOID), _output(synthesis="v2")):
        html = render_slides(out, generated_at=STAMP)
        text = _text(html)
        for word in ("ρ", "勝ち筋", "勝ちパターン", "御社", "貴社", "〔上位", "離脱", "来店"):
            assert word not in text, word


# ── S1・S2 ─────────────────────────────────────────────────────────────────


def test_cover_has_stamp_client_and_counts() -> None:
    """T20: 表紙に取得日時（JST）。クライアント未指定は「未指定（自社/競合の区分なし）」。"""
    html = render_slides(_output(avoid=AVOID), generated_at=STAMP)
    cover = _text(_slide(html, "cover"))
    assert "「スパイスカレー 作り方」検索上位5本の構成分析" in cover
    assert "2026-09-28 10:15（JST）" in cover
    assert "深掘り5本／一覧30本" in cover
    assert "未指定（自社/競合の区分なし）" in cover
    assert "ルー卒業" in cover  # 避けたい訴求
    assert "“なぜ上位か”＝再現すべき勝ちパターン" not in cover


def test_conclusion_uses_code_tiers_and_best_video() -> None:
    """壊し方: 段階を ceil(0.4n) にする／最良を rank==1 にする → 赤。"""
    body = _text(_slide(render_slides(_output(avoid=AVOID), generated_at=STAMP), "conclusion"))
    assert "必須条件" in body and "最初のテロップが0秒台5/5" in body
    assert "多数派" in body and "分量をテロップに出す3/5（#3・#4・#5）" in body
    assert "#4 @muscle_d（再生・保存率・シェアが5本で最大）" in body
    assert "差の要因: 未特定（6〜30位は動画を未分析）" in body
    # LLM の断定の見出し（勝つ）は使わず、コードの見出し
    assert "上位5本の共通点（仮説）" in body and "勝つ" not in body
    # 未指定なら御社は（クライアント商品）に
    assert "（クライアント商品）" in body


def test_conclusion_brand_status_by_roster() -> None:
    """T8/T9: 区分は名簿。未指定なら Gemini の client（#4）を使わない。"""
    unspecified = _text(_slide(render_slides(_output(), generated_at=STAMP), "conclusion"))
    assert "ブランドの現在地（区分は未指定）" in unspecified
    assert "クライアント" not in unspecified.replace("（クライアント商品）", "")
    named = _text(
        _slide(
            render_slides(_output(client=CLIENT, competitors=COMPETITORS), generated_at=STAMP),
            "conclusion",
        )
    )
    assert "SPICIA: #1 目立つ" in named
    assert "ティーケー食品 #2（主役）" in named and "ハーブ専科 #4（主役・PR）" in named


# ── S3・S4・S5・S6 ───────────────────────────────────────────────────────────


def test_surface_map_counts_board_by_code() -> None:
    body = _text(_slide(render_slides(_output(avoid=AVOID), generated_at=STAMP), "surface"))
    assert "@spice_b 4本（#2・#11・#24・#28）" in body
    assert "5/30本（#1・#4・#12・#16・#20）" in body
    assert "「スパイスカレー」キャプション 26/30・ハッシュタグ 23/30" in body
    assert "無水「無水」 4本（#4・#14・#16・#20）" in body  # 語は AI・本数はコード


def test_brand_map_merges_roster_aliases_and_marks_pr() -> None:
    html = render_slides(_output(client=CLIENT, competitors=COMPETITORS), generated_at=STAMP)
    rows = re.findall(r"<tr>(.*?)</tr>", _slide(html, "brands"), re.S)
    cells = [[_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)] for r in rows]
    by_name = {c[0]: c for c in cells if c}
    assert by_name["SPICIA"][1] == "クライアント"
    tk = by_name["T&K（ティーケー食品）"]  # 「T&K」と「ティーケー食品」を 1 行に
    assert tk[1] == "競合" and tk[2] == "#2・#3"
    assert by_name["ハーブ専科"][1:3] == ["競合", "#4"] and by_name["ハーブ専科"][6] == "PR"
    unspecified = render_slides(_output(), generated_at=STAMP)
    labels = {
        _text(c)
        for r in re.findall(r"<tr>(.*?)</tr>", _slide(unspecified, "brands"), re.S)
        for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)[1:2]
    }
    assert labels == {"未指定"}  # #4 の Gemini の client は無視


def test_compare_emphasises_the_best_video_and_translates_labels() -> None:
    """T16: 強調は #4（再生・保存率・シェアが最大）。フックの型は日本語。"""
    body = _slide(render_slides(_output(), generated_at=STAMP), "compare")
    assert '<div class="hd one best">#4 最多' in body
    assert '<div class="hd one best">#1' not in body
    text = _text(body)
    assert "問題提起" in text and "ビジュアル" in text and "problem" not in text
    assert "89秒・横長" in text  # T17
    assert "なし（型だけの申告は無効）" in text  # T14 #4 の comment
    assert "2022-11-14（換算）" in text  # create_time が 0 の 1 位は動画 ID から換算


def test_structure_marks_inferred_roles() -> None:
    body = _text(_slide(render_slides(_output(), generated_at=STAMP), "structure"))
    assert "役割は推定" in body
    assert "テ=最初のテロップ" in body


# ── 構成分解（1 本 1 枚）────────────────────────────────────────────────────


def test_breakdown_facts_for_video_4() -> None:
    """仕様 §2-2 の #4 の期待値（冒頭 3 秒・テロップ設計・商品・PR・締め）。"""
    body = _slide(render_slides(_output(avoid=AVOID), generated_at=STAMP), "video-4")
    text = _text(body)
    assert "構成分解 #4 / 5" in text and "PR" in text
    assert "0秒「とにかく痩せたいから」" in text and "3秒「こっそり食べていた」" in text
    assert "フック: 問題提起" in text and "語りあり" in text
    assert "28枚・1秒あたり0.37枚・下段" in text
    assert "分量テロップ7枚" in text
    assert "ハーブ専科・主役・21.5〜23秒（AI推定）" in text
    assert "コメントは文言も秒も無いため無効" in text
    assert "痩せたい動機から入る" in text  # LLM の win_line（検査済み）
    assert "CTA△" in text  # 型だけの CTA は評価でも無効


def test_breakdown_for_video_2_close_and_quantity() -> None:
    text = _text(_slide(render_slides(_output(), generated_at=STAMP), "video-2"))
    assert "42秒「詳しくはプロフィールから見てね」（プロフィール誘導）" in text
    assert "分量の置き場所: 無し" in text


def test_avoid_terms_drop_llm_items_but_keep_the_fact() -> None:
    """T10: ルー卒業を含む指示・絵コンテは落ち、#3 の冒頭 3 秒の原文は残る。"""
    html = render_slides(_output(avoid=AVOID), generated_at=STAMP)
    assert "0秒「カレールーはもう卒業！」" in _text(_slide(html, "video-3"))
    assert "冒頭でカレールーは卒業と宣言する" not in _text(_slide(html, "directives"))
    assert "ルー卒業の宣言" not in _text(html)


def test_frame_labels_are_seconds_and_role_only() -> None:
    """T18: コマの見出しにブランド名・「KWテロップ」を付けない（pick_timecodes の見出しを使わない）。"""
    out = _output()
    for v in out.videos:
        for i, f in enumerate(v.frames):
            f.caption = ["フック", "KWテロップ 16s", "SPICIA 24s", "ハーブ専科 22s"][i % 4]
    html = render_slides(out, generated_at=STAMP)
    for word in ("KWテロップ", "SPICIA 24s", "ハーブ専科 22s"):
        assert word not in html, word
    assert re.search(r"<figcaption[^>]*>\d:\d\d｜(フック|手順|CTA)</figcaption>", html)


def test_landscape_video_uses_the_landscape_layout() -> None:
    html = render_slides(_output(), generated_at=STAMP)
    land = re.search(r'<section class="slide bd land" data-slide="video-5"', html)
    assert land is not None
    body = _slide(html, "video-5")
    assert body.count("<figure>") <= 4


def test_pick_scene_frames_prefers_first_last_and_excludes_opening() -> None:
    uri = jpeg_uri(320, 568)
    a = VideoVSEOAnalysis.model_validate(
        {
            "duration_sec": 60,
            "scenes": [
                {"start_sec": s, "end_sec": e, "desc": "x"}
                for s, e in ((0, 3), (3, 10), (10, 40), (40, 55), (55, 60))
            ],
            "cta_sec": 57.0,
        }
    )
    frames = [FrameShot(sec=s, data_uri=uri) for s in (0.8, 5.0, 20.0, 30.0, 45.0, 58.0)]
    opening = frames[0]
    picked = pick_scene_frames(a, frames, 3, exclude=opening)
    secs = [f.sec for f, _r in picked]
    assert opening.sec not in secs
    assert secs == [5.0, 20.0, 58.0]  # 最初（に近い）場面・最後の場面・長い場面
    assert [r for _f, r in picked] == ["steps", "steps", "cta"]


# ── v2 キャッシュの互換（T28）────────────────────────────────────────────────


def test_v2_cache_renders_with_inferred_roles_and_code_directives() -> None:
    """役割の無い v2 の出力・v3 の検査を通していない synthesis でも落ちない。

    壊し方: 役割の欄を必須にする／v2 の文を描く → 赤。
    """
    html = render_slides(_output(synthesis="v2"), generated_at=STAMP)
    kinds = [k for k, _b in _sections(html)]
    assert "storyboard-案A" not in kinds and len(kinds) == 14  # 絵コンテは LLM が無いので省く
    assert "（役割は推定）" in _text(_slide(html, "video-1"))
    directives = _text(_slide(html, "directives"))
    assert "最初のテロップを0秒台に出す" in directives and "コードの集計" in directives
    assert "AI の指示は無いため" in directives
    for v2_text in ("スパイス4選と30分調理", "スプーンで引き上げる", "悩む表情"):
        assert v2_text not in _text(html), v2_text


def test_no_synthesis_at_all_still_renders() -> None:
    html = render_slides(_output(synthesis="none"), generated_at=STAMP)
    assert "posting" in [k for k, _b in _sections(html)]


def test_directives_show_verified_evidence_only() -> None:
    """T6: 照合できた根拠だけ（#4 25秒「大さじ8杯」）。スプーンで引き上げる は出ない。"""
    body = _text(_slide(render_slides(_output(avoid=AVOID), generated_at=STAMP), "directives"))
    assert "#4 25秒「大さじ8杯」" in body
    assert "スプーンで引き上げる" not in body
    assert "必須条件 5/5" in body  # コードの事実の指示（最初のテロップ）


def test_template_band_and_kw_matrix() -> None:
    """仕様 §2-3: 尺は全 n 本・meta（中央値60・46〜89）、作り方は完全一致0・言い換え1（#2）。"""
    body = _text(_slide(render_slides(_output(), generated_at=STAMP), "template"))
    assert "尺 中央値60秒（46〜89秒・固定しない）" in body
    assert "1秒あたりのテロップ 中央値0.70枚（0.33〜0.73）" in body
    assert "語りあり 多数派 3/5（#2・#3・#4）" in body
    assert "完全0・言い換え1（#2）" in body
    assert "4/5（上位30本で26）" in body
    assert "#3 再生2.05万（5本の中央値27.4万の1割未満）" in body
    assert "最初のテロップ 必須条件 5/5" in body


def test_posting_slide_has_fixed_verification_and_blank_order() -> None:
    body = _text(_slide(render_slides(_output(), generated_at=STAMP), "posting"))
    assert "翌日と7日後に確認" in body
    assert "中央値0.82%・最大1.07%" in body
    assert "成功の基準（何位・いつ）" in body and "予算" in body
    assert "保存率で検証する" not in body


@pytest.mark.parametrize("n", [1, 2])
def test_small_sample_warning(n: int) -> None:
    videos: list[Any] = [
        AnalyzedVideo(
            meta=VideoMeta(rank=r, url=f"https://t/{r}", play_count=1000 * r, duration_sec=15),
            analysis=VideoVSEOAnalysis(duration_sec=15),
        )
        for r in range(1, n + 1)
    ]
    out = VideoAlgorithmOutput(query="kw", videos=videos)
    html = render_slides(out)
    assert "観測仮説" in html
