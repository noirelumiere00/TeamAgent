"""事実層（facts.py）: 本番と同じ形の合成データ（prod_shape）で、本番の値を固定する。

仕様 v3 §5 の T1・T4・T8・T9・T11〜T17・T26 のうち、事実層（コードの集計）の範囲。
変異テスト: 修正を戻すと赤になる（各テストの docstring に「壊し方」を書く）。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest

from teamagent.skills.search_surface_check import video_structure as vs
from teamagent.skills.video_algorithm import facts as vf
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.evidence import (
    TIER_CASE,
    TIER_MAJORITY,
    TIER_REQUIRED,
    Roster,
    kw_hits,
    tier,
    tier_text,
)
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    StatsAnalysis,
    VideoAlgorithmInput,
    VideoAlgorithmOutput,
    VideoMeta,
    WinRange,
)
from teamagent.skills.video_algorithm.slides import render_slides
from teamagent.skills.video_algorithm.synthesis import build_prompt, build_prompt_v2
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    KW1,
    KW2,
    QUERY,
    prod_board,
    prod_videos,
)

ROSTER = Roster.of(CLIENT, COMPETITORS)


def _facts(roster: Roster | None = None) -> list[vf.VideoFacts]:
    return vf.all_facts(prod_videos(), QUERY, roster)


def _by_rank(roster: Roster | None = None) -> dict[int, vf.VideoFacts]:
    return {f.rank: f for f in _facts(roster)}


def _feature(fid: str, roster: Roster | None = None) -> vf.Feature:
    table = vf.feature_table(_facts(roster), prod_board(), QUERY)
    return next(ft for ft in table if ft.id == fid)


# ── T4 段階の名前 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("c", "n", "name"),
    [
        (5, 5, TIER_REQUIRED),
        (4, 5, TIER_MAJORITY),
        (3, 5, TIER_MAJORITY),
        (2, 5, TIER_CASE),
        (1, 5, TIER_CASE),
        (6, 10, TIER_MAJORITY),
        (5, 10, TIER_CASE),
        (3, 3, TIER_REQUIRED),
        (2, 3, TIER_MAJORITY),
        (0, 5, TIER_CASE),
    ],
)
def test_tier_names_follow_ceil_sixty_percent(c: int, n: int, name: str) -> None:
    """壊し方: 閾値を ceil(0.4n) にする → 2/5 が多数派になり赤。"""
    assert tier(c, n) == name


def test_feature_table_names_tiers_and_never_says_winning_path() -> None:
    table = vf.feature_table(_facts(), prod_board(), QUERY)
    by_id = {ft.id: ft for ft in table}
    assert by_id["first_telop_0s"].tier == TIER_REQUIRED  # 5/5
    assert by_id["qty_telop"].tier == TIER_MAJORITY  # 3/5
    assert by_id["pr"].tier == TIER_CASE  # 2/5
    assert tier_text(by_id["pr"].ranks, 5) == "事例 2/5（#1・#4）"
    assert tier_text(by_id["first_telop_0s"].ranks, 5) == "必須条件 5/5"
    for ft in table:
        assert "勝ち筋" not in ft.label and "勝ち" not in ft.tier


# ── T1 上位帯の幅（勝ち筋レンジ）の廃止・分布は全 n 本・meta の尺 ─────────────────────


def test_distribution_uses_all_videos_and_meta_duration() -> None:
    """壊し方: _win_ranges を戻す／尺を analysis から取る → 中央値 59・範囲 46–90 で赤。"""
    cross = cross_analyze(prod_videos(), QUERY, board=prod_board())
    assert cross.median_duration_sec == 60.0
    st = cross.stats
    assert st is not None and st.win_ranges == []
    dur = next(d for d in st.distributions if d.feature == "尺(秒)")
    assert (dur.min, dur.median, dur.max) == (46.0, 60.0, 89.0)
    band = vf.summary_band(_facts())
    assert band.duration == vf.Distribution(median=60.0, min=46.0, max=89.0)
    assert band.telops_per_sec == vf.Distribution(median=0.7, min=0.33, max=0.73)
    assert band.narration_ranks == (2, 3, 4)


def test_llm_input_and_screen_have_no_top_band_range() -> None:
    """LLM の入力（統計ブロック）にも、コードが書く画面の指示にも「以上」の幅を出さない。

    壊し方: _win_ranges を戻す（「保存率0.8% 以上」「32枚以上」「46-59秒」が入る）→ 赤。
    """
    videos = prod_videos()
    cross = cross_analyze(videos, QUERY, board=prod_board())
    legacy = build_prompt_v2(
        videos, QUERY, cross.stats
    )  # 戻し先（VIDEO_ALGO_SYNTHESIS_VERSION=v2）
    stats_block = legacy[legacy.index("## 横断統計") : legacy.index("# 上位 5 本の構造分析")]
    assert "以上" not in stats_block and "勝ち筋レンジ" not in stats_block
    assert "46-59" not in legacy and re.search(
        r"分布\[尺\(秒\)\]: 中央値60\.0 範囲46\.0–89\.0", legacy
    )
    prompt = build_prompt(videos, QUERY, cross.stats, board=prod_board())  # 既定（v3）
    assert not re.search(r"\d+(?:\.\d+)?% ?以上|\d+枚以上|\d+秒以上|46-59", prompt)
    assert "勝ち筋" not in prompt
    assert "尺（TikTokのメタ・全5本）: 中央値60秒（46〜89秒）" in prompt
    out = VideoAlgorithmOutput(query=QUERY, videos=videos, board=prod_board(), cross=cross)
    html = render_report(out)
    assert not re.search(r"\d+(?:\.\d+)?% 以上|\d+枚以上|46-59秒", html)
    assert "<td>尺(秒)</td><td>中央値 60.0</td><td>範囲 46.0–89.0</td>" in html  # 付録の分布
    slides = render_slides(out)
    assert not re.search(r"\d+(?:\.\d+)?% ?以上|\d+枚以上|46-59", slides)
    assert "尺 中央値60秒（46〜89秒・固定しない）" in slides  # 共通する構成の型の帯


def test_old_cache_win_ranges_are_dropped_on_load() -> None:
    """旧キャッシュに残った上位帯の幅は読み込みで捨てる（MCP の返却 JSON にも出さない）。"""
    st = StatsAnalysis.model_validate(
        {"sample_size": 5, "win_ranges": [{"label": "尺", "text": "46-59秒"}]}
    )
    assert st.win_ranges == []
    assert WinRange(label="x").text == ""


# ── T15 尺は meta 優先 ─────────────────────────────────────────────────────


def test_duration_prefers_tiktok_meta() -> None:
    """壊し方: analysis の尺を使う → 59/74/90 で赤。"""
    f = _by_rank()
    assert (f[3].duration_sec, f[4].duration_sec, f[5].duration_sec) == (60.0, 75.0, 89.0)
    cross = cross_analyze(prod_videos(), QUERY)
    assert cross.stats is not None
    assert [r.duration_sec for r in cross.stats.feature_matrix] == [59.0, 46.0, 60.0, 75.0, 89.0]
    no_meta = prod_videos()[0].model_copy(deep=True)
    no_meta.meta.duration_sec = 0.0
    assert vf.video_facts(no_meta, QUERY).duration_sec == 59.0  # meta が 0 のときだけ Gemini


# ── T11 KW を語ごと×層ごと×完全一致/言い換え ─────────────────────────────────


def test_kw_counts_per_term_and_layer() -> None:
    """壊し方: Gemini の kw_match / keyword_matches(telop) を使う → #4 21.5・#5 86・#1「工程」が入り赤。"""
    rows = {(r.term, r.layer): r for r in vf.kw_matrix(_facts(), prod_board(), QUERY)}
    assert rows[(KW1, "telop")].exact == (1, 2, 3, 4, 5)
    assert rows[(KW2, "telop")].exact == ()
    assert rows[(KW2, "telop")].synonym == (2,)  # 10 秒「作れます」・42 秒「レシピ」
    assert rows[(KW1, "caption")].exact == (1, 2, 3, 4)
    assert rows[(KW1, "caption")].board == (26, 30)
    assert rows[(KW2, "caption")].exact == (3,)
    assert rows[(KW2, "caption")].board == (9, 30)
    assert rows[(KW1, "speech")].exact == (2, 3, 4)
    assert rows[(KW1, "speech")].verified is False  # 発話は AI 聞き取り（未照合）
    f = _by_rank()
    kw1_telop = {r: next(h.secs for h in f[r].kw if (h.term, h.layer) == (KW1, "telop")) for r in f}
    assert kw1_telop[4] == (5.0,)  # 21.5 秒「ハーブ専科カレースパイス」は KW ではない
    assert kw1_telop[5] == (0.0, 7.0)  # 86 秒「スパイスチキンカレー」は KW ではない
    assert not any(h.term == KW2 and h.layer == "telop" for h in f[1].kw)  # 「工程」は実在しない
    syn = next(h for h in f[2].kw if (h.term, h.layer, h.match) == (KW2, "telop", "synonym"))
    assert syn.secs == (10.0, 42.0) and syn.verified


def test_kw_per_term_reaches_stats_and_llm_input() -> None:
    cross = cross_analyze(prod_videos(), QUERY, board=prod_board())
    st = cross.stats
    assert st is not None
    per = {(r.term, r.layer): r for r in st.kw_coverage.per_term}
    assert per[(KW2, "telop")].exact_ranks == [] and per[(KW2, "telop")].synonym_ranks == [2]
    assert (per[(KW1, "hashtag")].board_hits, per[(KW1, "hashtag")].board_size) == (23, 30)
    assert dict(st.kw_coverage.layer_fill)["テロップ"] == "5/5"
    legacy = build_prompt_v2(prod_videos(), QUERY, st)
    assert "「作り方」テロップ0/5・言い換え1/5（#2）" in legacy
    assert "「スパイスカレー」テロップ5/5" in legacy and "上位30本では26/30" in legacy
    prompt = build_prompt(prod_videos(), QUERY, st, board=prod_board())
    assert "- 「作り方」: テロップ 完全一致0/5・言い換え1/5（#2）" in prompt
    assert "キャプション 完全一致4/5（#1・#2・#3・#4）・上位30本では26/30" in prompt


# ── T12 ハッシュタグ ──────────────────────────────────────────────────────


def test_hashtag_is_judged_from_tags_or_caption_not_gemini() -> None:
    """壊し方: keyword_matches(hashtag) だけで判定 → #4 が落ちて 2/5 で赤。"""
    ft = _feature(f"kw_hashtag:{KW1}")
    assert ft.ranks == (2, 3, 4) and ft.board_rate == (23, 30)
    tagged = VideoMeta(desc="本文にスパイスカレー", hashtags=["#本格スパイスカレー"])
    no_tag = VideoMeta(desc="#スパイスカレー 本文", hashtags=["カレー"])  # 取得したタグを優先
    assert kw_hits(tagged, None, [KW1])[-1].layer == "hashtag"
    assert not any(h.layer == "hashtag" for h in kw_hits(no_tag, None, [KW1]))


# ── T13 PR ───────────────────────────────────────────────────────────────


def test_pr_is_detected_from_the_full_caption() -> None:
    """壊し方: desc[:220] で判定 → 1 位の #PR（720 字目より後）を見落として赤。"""
    f = _by_rank()
    assert [r for r in f if f[r].pr] == [1, 4]
    assert len(prod_videos()[0].meta.desc) > 720
    assert f[4].pr_evidence == ("キャプション @ハーブ専科 #PR・AI判定: 提供の可能性（ハーブ専科）")
    sm = vf.surface_map(prod_board(), query=QUERY)
    assert sm.pr_ranks == (1, 4, 12, 16, 20)
    cross = cross_analyze(prod_videos(), QUERY, board=prod_board())
    assert cross.stats is not None
    assert any("タイアップ表記2本（#1・#4）" in c for c in cross.stats.caveats)
    assert "タイアップ表記: あり（キャプション @ハーブ専科 #PR" in build_prompt_v2(
        prod_videos(), QUERY, cross.stats
    )
    prompt = build_prompt(prod_videos(), QUERY, cross.stats, board=prod_board())
    assert '"タイアップ表記": "キャプション @ハーブ専科 #PR' in prompt
    assert '"タイアップ表記": "キャプション #PR"' in prompt  # 1 位（720 字より後の #PR）
    # R1-6: 上位 30 本の率（前提の水準）も LLM に渡る。壊し方: 上位 5 本だけで数える → 2/5 で赤。
    pr = _feature("pr")
    assert pr.ranks == (1, 4) and pr.board_rate == (5, 30)
    assert "- pr｜タイアップ表記（キャプション）｜2/5（#1・#4）｜事例｜上位30本では5/30" in prompt
    assert f[4].pr_marked and f[4].pr_caption_evidence == "キャプション @ハーブ専科 #PR"


@pytest.mark.parametrize(
    ("desc", "pr"),
    [
        ("おいしい #pr", True),
        ("#ＰＲ 全角", True),
        ("#タイアップ", True),
        ("#提供 ありがとう", True),
        ("#promotion ではない", False),
        ("#PRO仕様", False),
        ("PR なし", False),
    ],
)
def test_pr_tag_variants(desc: str, pr: bool) -> None:
    assert vf.detect_pr(VideoMeta(desc=desc))[0] is pr


def test_speech_features_split_exact_and_synonyms() -> None:
    """R2-8: 発話の特徴は完全一致だけで数え、言い換え（AI 聞き取り）は別の特徴にする。"""
    ids = {ft.id: ft.ranks for ft in vf.feature_table(_facts(), prod_board(), QUERY)}
    assert ids[f"kw_speech:{KW1}"] == (2, 3, 4)
    assert f"kw_speech:{KW2}" not in ids  # 「作り方」を声に出した動画は完全一致 0 本
    assert ids[f"kw_speech_syn:{KW2}"] == (2,)


def test_empty_board_has_no_board_rate() -> None:
    """R2-16: 一覧が空なら上位ボードの率は None（「上位0本では0/0」と書かない）。"""
    table = vf.feature_table(_facts(), [], QUERY)
    assert all(ft.board_rate is None for ft in table)
    rows = vf.kw_matrix(_facts(), [], QUERY)
    assert all(r.board is None for r in rows)


def test_top_videos_are_compared_with_the_rest_of_the_board() -> None:
    """R3-8: 上位 n 本（分析した本）とボードの残りのメタの差（動画の中身の差ではない）。"""
    rows = {r.label: r for r in vf.top_vs_rest(prod_board(), [1, 2, 3, 4, 5], QUERY)}
    assert (rows["再生（中央値）"].top, rows["再生（中央値）"].rest) == ("27.4万", "4.62万")
    assert (rows["タイアップ表記"].top, rows["タイアップ表記"].rest) == ("2/5", "3/25")
    assert rows["キャプションに「スパイスカレー」"].rest == "22/25"
    assert vf.rank_runs([1, 2, 5, 6, 7, 8, 30]) == "#1・#2・5〜8位・#30"
    assert vf.unanalyzed_ranks([3, 4], prod_board()[:6]) == [1, 2, 5, 6]


# ── T14 CTA ──────────────────────────────────────────────────────────────


def test_cta_without_text_or_second_is_invalid_and_no_majority() -> None:
    """壊し方: 有効性の検査を外す → #4 の comment（文言も秒も無い）が型のまま数えられ赤。

    #4 は AI の CTA が無効でも、最後の 10 秒のテロップ「ぜひ試してみて」（71 秒）を試してみて型の
    CTA とみなす（R3-3: S5・S10・型・絵コンテで同じ事実にそろえる）。出どころはテロップ。
    """
    f = _by_rank()
    assert f[4].cta_dropped == ("comment",)
    assert f[4].cta_in_video == ("try", "ぜひ試してみて", 71.0) and f[4].cta_source == "telop"
    assert f[2].cta_in_video == ("link_bio", "詳しくはプロフィールから見てね", 42.0)
    assert f[2].cta_source == "ai"
    assert f[3].cta_in_video == ("try", "ぜひ一度お試しを", 58.0)
    assert vf.cta_consensus(_facts()) == []  # try は #3・#4 の 2/5（多数派未満）
    assert _feature("cta_video").ranks == (2, 3, 4)
    labels = [vf.CTA_KIND_LABEL[f[r].cta_in_video[0]] for r in (2, 3)]  # type: ignore[index]
    assert "来店" not in labels
    assert f[2].cta_in_caption == ("save", "comment")
    cross = cross_analyze(prod_videos(), QUERY)
    # 動画内の呼びかけ（型は問わない）は #2・#3・#4 の 3/5。型の多数派（来店・保存）は無い。
    cta = [w for w in cross.win_factors if "CTA" in w.factor]
    assert [(w.observed_in, w.total) for w in cta] == [(3, 5)]
    assert not any("来店" in w.factor or "保存" in w.factor for w in cta)


# ── T16 最良の 1 本 ──────────────────────────────────────────────────────────


def test_best_video_is_the_most_viewed_not_rank_one() -> None:
    """壊し方: rank==1 に戻す → #1 になり赤。"""
    assert vf.best_video(_facts()) == (4, ["再生", "保存率", "シェア"])


# ── T17 縦横・外れ値 ─────────────────────────────────────────────────────────


def test_orientation_is_read_from_the_frame_jpeg() -> None:
    """壊し方: 寸法の判定を外す → #5 が unknown で赤。"""
    f = _by_rank()
    assert [f[r].orientation for r in range(1, 6)] == ["portrait"] * 4 + ["landscape"]
    assert _feature("orientation:landscape").ranks == (5,)


def test_outliers_are_facts_only() -> None:
    assert vf.outliers(_facts()) == [
        (3, ["再生2.05万（5本の中央値27.4万の1割未満）"]),
        (5, ["横長（5本中1本）", "89秒（中央値60秒の1.5倍）", "2021年投稿（最古）"]),
    ]


def test_posted_date_falls_back_to_the_video_id() -> None:
    f = _by_rank()
    assert f[1].posted_at == date(2022, 11, 14) and f[1].posted_estimated is True
    assert f[4].posted_at == date(2026, 7, 25) and f[4].posted_estimated is False


# ── T8 / T9 クライアントの区分 ─────────────────────────────────────────────────


def test_without_a_roster_every_brand_is_unspecified() -> None:
    """壊し方: Gemini の brand_relation を通す → #4 がクライアントになり赤。"""
    f = _by_rank()
    assert {b.relation for r in f for b in f[r].brands} == {"unspecified"}
    assert all(b.category_match is None for r in f for b in f[r].brands)
    assert not any(
        ft.id == "brand_category_prominent" for ft in vf.feature_table(_facts(), [], QUERY)
    )
    legacy = build_prompt_v2(prod_videos(), QUERY)
    brief4 = legacy[legacy.index("#4（") : legacy.index("#5（")]
    assert "ハーブ専科(未指定・主役)" in brief4
    assert "client" not in legacy and "クライアント" not in legacy
    prompt = build_prompt(prod_videos(), QUERY)
    assert '"名前": "ハーブ専科", "区分": "未指定", "目立ち方": "主役"' in prompt
    assert "client" not in prompt and '"区分": "クライアント"' not in prompt


def test_roster_decides_client_and_competitors_with_aliases() -> None:
    """壊し方: 別名の | 分割をやめる → #2 ティーケー食品・#3 T&K が競合から外れて赤。"""
    f = _by_rank(ROSTER)
    rel = {r: {b.name: b.relation for b in f[r].brands} for r in f}
    assert rel[1]["SPICIA"] == "client"
    assert rel[2]["ティーケー食品"] == "competitor"
    assert rel[3]["T&K"] == "competitor"
    assert rel[4]["ハーブ専科"] == "competitor"
    assert rel[1]["麦酒X"] == "other"
    assert f[3].brands[1].in_caption is True  # キャプションは全角「T＆K」
    ft = _feature("brand_category_prominent", ROSTER)
    assert ft.ranks == (1, 2, 4) and ft.tier == TIER_MAJORITY  # #3 は付随なので数えない
    legacy = build_prompt_v2(prod_videos(), QUERY, roster=ROSTER)
    assert "SPICIA(クライアント・目立つ)" in legacy and "ハーブ専科(競合・主役)" in legacy
    prompt = build_prompt(prod_videos(), QUERY, roster=ROSTER)
    assert '"名前": "SPICIA", "区分": "クライアント", "目立ち方": "目立つ"' in prompt
    assert '"名前": "ハーブ専科", "区分": "競合", "目立ち方": "主役"' in prompt


def test_brand_facts_of_the_tieup_video() -> None:
    b = _by_rank()[4].brands[0]
    assert (b.name, b.prominence, b.first_sec, b.last_sec, b.total_sec) == (
        "ハーブ専科",
        "hero",
        21.5,
        23.0,
        3.0,
    )
    assert b.in_telop and b.in_caption and b.sponsored


# ── 1 本 1 枚の構成分解の事実（仕様 §2-2 の期待値）──────────────────────────────


def test_per_video_facts_for_the_best_video() -> None:
    f4 = _by_rank()[4]
    assert f4.opening_telops == ((0.0, "とにかく痩せたいから"), (3.0, "こっそり食べていた"))
    assert f4.hook_type == "problem" and f4.narration
    assert (f4.telop_count, f4.telops_per_sec, f4.telop_position_major) == (28, 0.37, "bottom")
    assert len(f4.qty_telops) == 7 and (25.0, "大さじ8杯") in f4.qty_telops
    assert (11.0, "トマト大6つ") not in f4.qty_telops  # 「6つ」は分量に数えない
    f2 = _by_rank()[2]
    assert f2.qty_place == "なし"
    assert _by_rank()[1].qty_place == "キャプション"
    assert ("telop", 5.0, "スーパーでそろう5つのスパイス") in f2.numeric_claims


def test_qty_features_match_production() -> None:
    assert _feature("qty_telop").ranks == (3, 4, 5)
    assert _feature("qty_caption").ranks == (1, 3)
    assert _feature("narration").ranks == (2, 3, 4)
    assert _feature(f"kw_telop_3s:{KW1}").ranks == (1, 2, 3, 5)


# ── 共通する構成の型（仕様 §2-3 の出るべき行）──────────────────────────────────


def _row(rows: list[vf.StageRow], label: str) -> vf.StageRow:
    return next(r for r in rows if r.label == label)


def _event(row: vf.StageRow, key: str) -> tuple[tuple[int, ...], str]:
    ranks, name = next((r, t) for k, r, t in row.events if k == key)
    return ranks, name


def test_template_rows_count_events_by_code() -> None:
    rows = vf.template(prod_videos(), _facts(ROSTER))
    head = _row(rows, "0〜3秒")
    assert _event(head, "first_telop") == ((1, 2, 3, 4, 5), TIER_REQUIRED)
    assert _event(head, "kw_telop") == ((1, 2, 3, 5), TIER_MAJORITY)  # #4 は 5 秒
    assert head.examples[0] == (4, 0.0, "とにかく痩せたいから")  # 再生が最大の動画から
    body = _row(rows, "10秒〜残り10秒")
    assert _event(body, "qty_telop") == ((3, 4, 5), TIER_MAJORITY)
    assert _event(body, "brand_first") == ((1, 2, 4), TIER_MAJORITY)
    assert all(r.roles_inferred for r in rows)  # v2 の出力は役割が推定
    tail = _row(rows, "最後の10秒")
    assert _event(tail, "cta") == ((2, 3, 4), TIER_MAJORITY)  # #4 は 71 秒のテロップ


def test_template_works_on_v2_outputs_without_roles_or_frames() -> None:
    videos = prod_videos()
    for v in videos:
        v.frames = []
        assert v.analysis is not None
        v.analysis.scenes = []
    facts = vf.all_facts(videos, QUERY)
    rows = vf.template(videos, facts)
    assert [r.label for r in rows] == list(vf.STAGE_LABELS)
    assert all(o.role is None for r in rows for o in r.per_video)
    assert {f.orientation for f in facts} == {"unknown"}


# ── 検索面の地図 ─────────────────────────────────────────────────────────────


def test_surface_map_counts_meta_only() -> None:
    sm = vf.surface_map(prod_board(), ["無水", "4種", "ダイソー"], query=QUERY)
    assert sm.size == 30
    assert sm.creators[:3] == (
        ("spice_b", (2, 11, 24, 28)),
        ("recipe_site", (7, 10, 12, 29)),
        ("muscle_d", (4, 16, 20)),
    )
    assert sm.angles == (("無水", (4, 14, 16, 20)), ("4種", (1,)))  # 0 本の語は出さない
    assert sm.top_save[0] == (11, 2.38)
    assert (KW1, "caption", 26, 30) in sm.kw_rates and (KW1, "hashtag", 23, 30) in sm.kw_rates
    assert dict(sm.years)[2021] == 1


# ── T26 評価の修正（search_surface_check と共通の video_structure）──────────────────


def _grades(rank: int) -> dict[str, Any]:
    v = next(v for v in prod_videos() if v.meta.rank == rank)
    return {g.axis: g for g in vs.grade_video(v, query=QUERY)}


def test_grades_on_the_production_shape() -> None:
    """壊し方: 旧判定（見返す理由・一致度そのまま・kw_match・hook_has_caption）に戻す → 赤。"""
    g2 = _grades(2)
    assert g2["一致度"].mark == "○" and "食い違いの指摘あり" in g2["一致度"].reason
    assert g2["保存の仕掛け"].mark == "△"  # 分量なし（「見返す理由」では上げない）
    g4 = _grades(4)
    assert g4["保存の仕掛け"].mark == "○" and "分量をテロップに" in g4["保存の仕掛け"].reason
    assert g4["KWの露出"].reason.startswith("テロップに 5秒")  # 21.5 秒でも 0 秒でもない
    g1 = _grades(1)
    assert g1["保存の仕掛け"].reason == "分量をキャプションに載せている"


# ── 取得済みの欄を捨てない・入力と echo ──────────────────────────────────────────


def test_video_algorithm_input_accepts_roster_and_avoid_terms() -> None:
    inp = VideoAlgorithmInput(
        query=QUERY, client_name=CLIENT, competitors=COMPETITORS, avoid_terms=["ルー卒業"]
    )
    assert inp.competitors == COMPETITORS and inp.avoid_terms == ["ルー卒業"]
    desc = VideoAlgorithmInput.model_fields["client_name"].description or ""
    assert "依頼者本人が同じ会話" in desc
    schema = VideoAlgorithmInput.model_json_schema()["properties"]
    assert {"competitors", "avoid_terms"} <= set(schema)
