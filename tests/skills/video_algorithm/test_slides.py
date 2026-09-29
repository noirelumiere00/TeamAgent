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
    assert "ブランドの現在地（区分・カテゴリは未指定）" in unspecified
    assert "クライアント" not in unspecified.replace("（クライアント商品）", "")
    named = _text(
        _slide(
            render_slides(_output(client=CLIENT, competitors=COMPETITORS), generated_at=STAMP),
            "conclusion",
        )
    )
    assert "SPICIA: #1（目立つ）" in named
    # R3-14: 名簿の別名（T&K／ティーケー食品）は S4 と同じく 1 つにまとめる
    assert "T&K（ティーケー食品） #2（主役）・#3（付随）" in named
    assert "ハーブ専科 #4（主役・PR）" in named
    assert "ティーケー食品 #2" not in named


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
    # R3-3: #4 の AI の comment（文言も秒も無い）は使わず、71 秒のテロップの呼びかけを出す
    assert "71秒 試してみて（テロップ）" in text
    assert "型だけの申告" not in text  # R3-15: 内部の言葉を出さない
    assert "目立つ映り込み" in text and "映る商品" not in text  # 名簿なし＝カテゴリ未判定
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
    assert "目立つ映り込み（カテゴリ未判定）" in text
    assert "ハーブ専科・主役・21.5〜23秒（AI推定）" in text
    assert "71秒「ぜひ試してみて」（試してみて・テロップから検出）" in text
    assert "AIの申告（コメント）は文言も秒も無く除外" in text
    assert "痩せたい動機から入る" in text  # LLM の win_line（検査済み）
    assert "映り込み◎" in text and "商品◎" not in text  # 名簿なしは「商品」と呼ばない
    # 呼びかけの無い #1 は CTA△（型だけの申告も無い）
    assert "CTA△" in _text(_slide(render_slides(_output(), generated_at=STAMP), "video-1"))


def test_breakdown_for_video_2_close_and_quantity() -> None:
    text = _text(_slide(render_slides(_output(), generated_at=STAMP), "video-2"))
    assert "42秒「詳しくはプロフィールから見てね」（プロフィール誘導）" in text
    assert "分量の置き場所: なし" in text


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
    # 役割が推定のとき、最初と CTA 以外は「本編」（手順と決めつけない）
    assert [r for _f, r in picked] == ["body", "body", "cta"]


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
    assert "#4 25秒 テロップ「大さじ8杯」" in body
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
    assert "翌日と7日後" in body
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


# ── 反証レビューの指摘（R2・R3）──────────────────────────────────────────────


def _named_html() -> str:
    return render_slides(_output(client=CLIENT, competitors=COMPETITORS), generated_at=STAMP)


def test_products_are_only_roster_brands_when_the_roster_is_given() -> None:
    """R2-4・R3-12: 名簿があるときは、名簿（クライアント・競合）のブランドだけを「商品」と呼ぶ。

    カテゴリ外で目立つもの（#3 のノンアルY・#5 の OLIVE-M／MIXER-B）は商品にしない。#3 は競合の
    T&K（付随）を商品の見せ方に出す。壊し方: 目立つ先頭のブランドに戻す → ノンアルY で赤。
    """
    html = _named_html()
    v3 = _text(_slide(html, "video-3"))
    assert "商品の見せ方" in v3 and "T&K（競合）・付随・33〜35秒（AI推定）" in v3
    assert "ノンアルY・目立つ" not in v3
    v5 = _text(_slide(html, "video-5"))
    assert "クライアント・競合の商品は映らない（映るのはOLIVE-M・MIXER-B）" in v5
    assert "商品—" in v5 and "商品◎" not in v5
    compare = _text(_slide(html, "compare"))
    assert "映る商品" in compare and "OLIVE-M" not in compare and "ノンアルY" not in compare
    structure = _slide(html, "structure")
    assert "▼" in structure and "映=" not in _text(structure)  # 名簿ありは「商」だけ
    template_text = _text(_slide(html, "template"))
    assert "目立つ商品の初出 多数派 3/5（#1・#2・#4）" in template_text


def test_unspecified_roster_calls_brands_visible_items_without_tiers() -> None:
    """R2-4: 名簿もカテゴリも無いときは「商品」と呼ばず「目立つ映り込み」とし、段階の名前を付けない。"""
    html = render_slides(_output(), generated_at=STAMP)
    text = _text(_slide(html, "template"))
    assert "目立つ映り込みの初出 4/5（#1・#2・#4・#5）" in text
    assert "目立つ商品" not in text and "カテゴリ未判定）多数派" not in text
    assert "映=目立つ映り込み" in _text(_slide(html, "structure"))


def test_best_video_alert_links_pr_and_competitor() -> None:
    """R3-9: 最も見られた 1 本が競合のタイアップ投稿なら、結論にその事実を 1 文で出す。"""
    body = _text(_slide(_named_html(), "conclusion"))
    assert (
        "最も見られ保存された#4は、競合（ハーブ専科）のタイアップ投稿（キャプション @ハーブ専科 #PR）。"
        "検索上位の最良枠を競合が取っている"
    ) in body
    unspecified = _text(_slide(render_slides(_output(), generated_at=STAMP), "conclusion"))
    assert "最も見られ保存された#4はタイアップ投稿（キャプション @ハーブ専科 #PR）" in unspecified


def test_surface_compares_top_videos_with_the_rest_of_the_board() -> None:
    """R3-8: 上位 n 本とボードの残りのメタの差（フォロワー・再生・保存率・字数・投稿年・KW 率・PR）。"""
    out = _output()
    body = _text(_slide(render_slides(out, generated_at=STAMP), "surface"))
    assert "上位5本とほかの25本の差" in body
    assert "キャプションに「スパイスカレー」4/522/25" in body.replace(" ", "")
    assert "タイアップ表記2/53/25" in body.replace(" ", "")
    conclusion = _text(_slide(render_slides(out, generated_at=STAMP), "conclusion"))
    assert "メタの差: フォロワー（中央値） 上位5本 5.22万・ほか 1,018" in conclusion
    assert "（動画の中身の差ではない）" in conclusion


def test_unanalyzed_ranks_are_listed_from_the_set_not_assumed_contiguous() -> None:
    """R2-11: #3・#4 だけ分析できたとき「3位以下は未分析」と書かない（#1・#2・#5 と 6〜30位）。"""
    out = _output()
    for v in out.videos:
        if v.meta.rank in (1, 2, 5):
            v.error = "動画取得失敗・サムネのみ軽量分析"
    html = render_slides(out, generated_at=STAMP)
    conclusion = _text(_slide(html, "conclusion"))
    assert "差の要因: 未特定（#1・#2・5〜30位は動画を未分析）" in conclusion
    assert "#1・#2・5〜30位は動画を見ていない" in _text(_slide(html, "surface"))


def test_small_samples_get_no_tier_names_or_chips() -> None:
    """R2-10: 1〜2 本の観測は「必須条件 1/1」にしない。チップ・コードの指示も出さない。"""
    out = _output()
    for v in out.videos:
        if v.meta.rank != 1:
            v.error = "動画取得失敗・サムネのみ軽量分析"
    html = render_slides(out, generated_at=STAMP)
    text = _text(html)
    assert "必須条件 1" not in text and "必須条件1" not in text
    conclusion = _text(_slide(html, "conclusion"))
    assert "上位1本の観測（本数が少ないため共通点の段階は付けない）" in conclusion
    assert "段階（必須条件・多数派）と共通点のチップは出していません" in conclusion


def test_front_loaded_frames_show_the_missing_scenes() -> None:
    """R2-6・R3-13・R1-10: 本番のコマ（#2 は 12 秒以内だけ）でも、本編と締めを「コマ未取得」で見せる。

    隙間のある場面（#1 の 12→13 秒）の秒は、境目が近い場面の役割にする。壊し方: コマ未取得の枠を
    出さない → 12〜42 秒の本編が見えず赤。
    """
    out = _output()
    out.videos = prod_videos("prod")
    html = render_slides(out, generated_at=STAMP)
    v2 = _slide(html, "video-2")
    assert "コマ未取得<br>12〜42秒" in v2 and "コマ未取得<br>42〜46秒" in v2
    assert "0:42｜CTA" in _text(v2)
    assert v2.count("<figure>") == 6  # コマ 4 枚＋未取得 2 枠
    v1 = _slide(html, "video-1")
    assert "コマ未取得<br>45〜52秒" in v1  # 7 秒の場面にコマが無い
    from teamagent.skills.video_algorithm.facts import scene_index_at

    scenes = sorted(out.videos[0].analysis.scenes, key=lambda s: s.start_sec)  # type: ignore[union-attr]
    assert scene_index_at(scenes, 12.2) == 0  # 隙間の 12→13 秒は近い方（12 秒で終わる場面）
    assert scene_index_at(scenes, 12.8) == 1


def test_quote_frames_are_labelled_with_their_own_second() -> None:
    """R2-5・R3-17: 根拠の横のコマは、根拠の秒の直後にあるときだけ・自分の秒を添えて出す。

    コマが無ければ黒い「—」の枠は出さない（文だけ）。
    """
    html = render_slides(_output(avoid=AVOID), generated_at=STAMP)
    directives = _slide(html, "directives")
    assert "<figcaption>1秒</figcaption>" in directives  # 0 秒の根拠の横に 0.8 秒のコマ
    for kind in ("directives", "template", "storyboard-案A"):
        assert '<div class="ph">—</div>' not in _slide(html, kind), kind
    assert "引用のテロップが写っているとは限らない" in _text(directives)


def test_no_internal_words_on_client_slides() -> None:
    """R3-15: 社内の言葉・システムの内部語（v3で計測・型だけの申告・AI の切り口の語が無い・無し）。

    #4 の締めのテロップを呼びかけでない文にした形（AI の comment だけが残り、動画内の CTA が
    無い）も確かめる。
    """
    no_close = _output()
    a4 = no_close.videos[3].analysis
    assert a4 is not None
    a4.telops[-1].text = "ごちそうさまでした"
    outs = (_output(avoid=AVOID), _output(synthesis="v2"), _output(synthesis="none"), no_close)
    assert build_deck(no_close).ctx.fact(4).cta_in_video is None  # type: ignore[union-attr]
    for out in outs:
        text = _text(render_slides(out, generated_at=STAMP))
        for word in (
            "v3で計測",
            "型だけの申告",
            "AI の切り口の語が無い",
            "AI の仮説が無い",
            "無し",
        ):
            assert word not in text, word


def test_grade_marks_and_axes_are_explained_once() -> None:
    """R3-16: ◎○△— と軸（テンポ・一致など）の説明は構成比較（S6）に 1 回だけ出す。"""
    html = render_slides(_output(), generated_at=STAMP)
    structure = _text(_slide(html, "structure"))
    assert "◎よい・○ふつう・△弱い・—判定なし" in structure
    assert (
        "テンポ＝平均カット秒" in structure
        and "一致＝テロップ・キャプション・映像の一致" in structure
    )
    assert "記号の意味は5本の構成比較に" in _text(_slide(html, "video-1"))


@pytest.mark.parametrize(
    ("sources", "label"),
    [
        (["cover"] * 5, "表紙"),
        (["frame"] * 5, "冒頭のコマ（表紙の代用）"),
        (["cover", "frame", "cover", "cover", "cover"], "表紙（一部はコマで代用）"),
        ([""] * 5, "サムネ（表紙か冒頭のコマ）"),
    ],
)
def test_cover_row_is_called_a_cover_only_when_it_is_one(sources: list[str], label: str) -> None:
    """R3-18: 0.8 秒のコマで代用したものを「表紙」と呼ばない。"""
    out = _output()
    for v, src in zip(out.videos, sources, strict=True):
        v.cover_data_uri = jpeg_uri(320, 568)
        v.cover_source = src  # type: ignore[assignment]
    body = _slide(render_slides(out, generated_at=STAMP), "compare")
    assert f'<div class="lab c2">{label}</div>' in body


def test_avoid_column_says_the_given_terms_were_excluded() -> None:
    """R3-21: 依頼者の避けたい訴求は「ご指定により除外」と出す（黙って消さない）。"""
    body = _text(_slide(render_slides(_output(avoid=AVOID), generated_at=STAMP), "directives"))
    assert "ご指定の避けたい訴求: ルー卒業（該当する指示・絵コンテは除外済み）" in body


def test_posting_has_pr_rule_metrics_success_placeholder_and_metric_direction() -> None:
    """R3-10・R3-11・R3-1: #PR の行・一次/二次の指標・成功の基準の例・仮説の保存率の向き。"""
    body = _text(_slide(render_slides(_output(), generated_at=STAMP), "posting"))
    assert (
        "依頼して投稿する場合は、キャプションに #PR とブランドの @ を付ける（ステマ規制）" in body
    )
    assert "上位のタイアップ #1・#4 も表記あり" in body
    assert "一次指標: このKWでの表示順位" in body and "二次指標: TikTokの分析画面" in body
    assert "目安: 上位5本の保存率の中央値0.82%・最大1.07%" in body
    assert "A/Bは条件ごとに複数本を投稿して比べる" in body
    assert "例: 7日後に上位10位以内" in body
    assert "保存率の中央値: 該当3本 0.63%・非該当2本 0.89%（逆の傾向）" in body


def test_template_does_not_count_inferred_roles_and_speech_splits_synonyms() -> None:
    """R3-6・R3-5: 推定の役割は段階を付けない。発話の言い換えは完全一致と分ける。"""
    html = render_slides(_output(), generated_at=STAMP)
    template_text = _text(_slide(html, "template"))
    assert "フック 必須条件" not in template_text and "本編 必須条件" not in template_text
    assert "段ごとの役割の本数は出さない" in template_text
    assert "CTA 多数派 3/5（#2・#3・#4）" in template_text  # #4 は 71 秒のテロップ
    v2 = _text(_slide(html, "video-2"))
    assert "発話の検索語（AI）: スパイスカレー 0・10秒" in v2
    assert "スパイスカレー 0・10秒・作り方" not in v2  # 言い換えを完全一致の行に混ぜない
    assert "言い換えだけ（AI）: 作り方 10・42秒" in v2


def test_ai_only_sponsorship_is_marked_differently() -> None:
    """R3-22: キャプションに表記が無く AI の推定だけのものは「PR?」とし、フッタでも分ける。"""
    out = _output()
    a1 = out.videos[0].analysis
    assert a1 is not None
    out.videos[0].meta = out.videos[0].meta.model_copy(update={"desc": "スパイスカレーの記録"})
    a1.brand_detections[0].is_intentional = "likely_sponsored"
    d = build_deck(out, generated_at=STAMP)
    assert footer_text(d).endswith("上位にタイアップ表記1本（#4）・AI推定の提供の可能性1本（#1）")
    body = _slide(render_slides(out, generated_at=STAMP), "compare")
    assert "PR?</span>" in body
