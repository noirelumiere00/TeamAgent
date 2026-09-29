"""2 段目の詳しい構成（場面の役割・構成表・評価・学べること・比較タブ・大きさ）のテスト。

偽の分析結果は structure_fakes（場面 8〜14・テロップ・発話・ブランドあり/なし・分析失敗 1 本）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from teamagent.skills.search_surface_check import video_structure as vs
from teamagent.skills.search_surface_check.schema import VideoDigest
from teamagent.skills.search_surface_check.video_chapter import (
    FRAME_MAX_CHARS,
    NOTES_MISSING,
    render_tabs,
)
from teamagent.skills.search_surface_check.video_digest import digest_videos
from teamagent.skills.search_surface_check.video_notes import (
    STORYBOARD_STAGES,
    build_notes_prompt,
    conclude_notes,
    ground_notes,
    notes_max_tokens,
    structure_payload,
)
from teamagent.skills.search_surface_check.video_render import render_video_chapter
from teamagent.skills.video_algorithm.evidence import Roster
from teamagent.skills.video_algorithm.frames import MAX_SCENE_FRAMES, scene_timecodes
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    FrameShot,
    VideoMeta,
    VideoVSEOAnalysis,
)
from tests.skills.search_surface_check.structure_fakes import (
    RICH_ANALYSES,
    TINY_JPEG,
    rich_videos,
)
from tests.skills.search_surface_check.video_fakes import FAKE_TOKENS_PER_CHAR

KW = "スパイスカレー"


def _video(
    analysis: dict[str, Any],
    *,
    rank: int = 1,
    frames: list[FrameShot] | None = None,
    desc: str = "",
) -> Any:
    return AnalyzedVideo(
        meta=VideoMeta(rank=rank, author=f"u{rank}", duration_sec=30.0, desc=desc),
        analysis=VideoVSEOAnalysis.model_validate(analysis),
        frames=frames or [],
    )


def _grade(
    analysis: dict[str, Any],
    axis: str,
    *,
    roster: Roster | None = None,
    desc: str = "",
    query: str | None = KW,
) -> vs.Grade:
    grades = vs.grade_video(_video(analysis, desc=desc), query=query, roster=roster)
    return next(g for g in grades if g.axis == axis)


# ── ◎○△ の基準の境界 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("analysis", "mark"),
    [
        (
            {
                "hook_has_caption": True,
                "hook_type": "number",
                "telops": [{"sec": 1.0, "text": "a"}],
            },
            "◎",
        ),
        (
            {
                "hook_has_caption": True,
                "hook_type": "number",
                "telops": [{"sec": 1.01, "text": "a"}],
            },
            "○",
        ),
        (
            {"hook_has_caption": True, "hook_type": "other", "telops": [{"sec": 0.5, "text": "a"}]},
            "○",
        ),
        # 分析 AI が「冒頭にテロップなし」と申告しても、0.5 秒のテロップがあれば冒頭にある
        (
            {
                "hook_has_caption": False,
                "hook_type": "number",
                "telops": [{"sec": 0.5, "text": "a"}],
            },
            "◎",
        ),
        # 申告が「あり」でも、最初のテロップが 3 秒より後なら冒頭には無い
        (
            {
                "hook_has_caption": True,
                "hook_type": "number",
                "telops": [{"sec": 3.5, "text": "a"}],
            },
            "△",
        ),
        (
            {
                "hook_has_caption": False,
                "hook_type": "other",
                "telops": [{"sec": 3.5, "text": "a"}],
            },
            "△",
        ),
        ({"hook_has_caption": False, "hook_type": "other", "telops": []}, "△"),
    ],
)
def test_hook_grade_counts_missing_conditions(analysis: dict[str, Any], mark: str) -> None:
    assert _grade(analysis, "冒頭3秒の掴み").mark == mark


@pytest.mark.parametrize(
    ("duration", "cuts", "mark"),
    [(30, 10, "◎"), (31, 10, "○"), (60, 10, "○"), (61, 10, "△"), (30, None, "—")],
)
def test_tempo_grade_boundaries(duration: float, cuts: int | None, mark: str) -> None:
    g = _grade({"duration_sec": duration, "cut_count": cuts}, "テンポ")
    assert g.mark == mark
    if cuts:
        assert f"{cuts}カット" in g.reason


def _spoken(*secs: float) -> dict[str, Any]:
    return {"keyword": KW, "matched": True, "layer": "narration", "appear_sec": list(secs)}


@pytest.mark.parametrize(
    ("analysis", "mark"),
    [
        ({"telops": [{"sec": 3.0, "text": f"{KW}の基本"}]}, "◎"),
        ({"telops": [{"sec": 3.1, "text": f"{KW}の基本"}]}, "○"),
        ({"spoken_keywords": [_spoken(10.0)]}, "○"),
        ({"spoken_keywords": [_spoken(10.1)]}, "△"),
        ({"spoken_keywords": [_spoken()]}, "○"),  # 出るが秒は不明
        ({"keyword_matches": [{"matched": True, "layer": "caption"}]}, "△"),  # キャプションだけ
        ({}, "△"),
    ],
)
def test_kw_exposure_grade_boundaries(analysis: dict[str, Any], mark: str) -> None:
    assert _grade(analysis, "KWの露出").mark == mark


def test_kw_exposure_takes_the_earliest_layer() -> None:
    g = _grade(
        {
            "telops": [{"sec": 5.0, "text": f"{KW}の基本"}],
            "spoken_keywords": [_spoken(1.5)],
        },
        "KWの露出",
    )
    assert g.mark == "◎" and g.reason.startswith("発話に 1.5秒")


def test_kw_exposure_ignores_unverified_kw_match_and_missing_synonyms() -> None:
    """本番の失敗の形: KW を含まないテロップに kw_match=True、実在しないテロップへの言い換え一致。

    分析 AI の申告を信じると 0.8 秒で ◎ になる。本文に語が無く、言い換え（「工程」）も前後 2 秒の
    テロップに実在しないので、KW はテロップに出ない（△）。
    """
    analysis = {
        "telops": [
            {"sec": 0.8, "text": "#PR 〇〇カレー粉", "kw_match": True},
            {"sec": 20.0, "text": "玉ねぎは軽く塩して", "kw_match": False},
        ],
        "keyword_matches": [
            {
                "keyword": KW,
                "matched": True,
                "match_type": "synonym",
                "layer": "telop",
                "appear_sec": [19.0, 34.0],
                "surface_text": "工程",
            }
        ],
    }
    g = _grade(analysis, "KWの露出")
    assert g.mark == "△" and g.reason.startswith("テロップにも発話にも出ない")


def test_kw_exposure_keeps_a_synonym_found_in_a_telop_within_two_seconds() -> None:
    """言い換え（「作れます、レシピ」）は区切って、前後 2 秒のテロップに実在する片だけ残す。"""
    analysis = {
        "telops": [{"sec": 10.0, "text": "これでカレーは作れます", "kw_match": True}],
        "keyword_matches": [
            {
                "keyword": "作り方",
                "matched": True,
                "match_type": "synonym",
                "layer": "telop",
                "appear_sec": [8.0],
                "surface_text": "作れます、レシピ",
            }
        ],
    }
    assert _grade(analysis, "KWの露出", query="作り方").reason.startswith("テロップに 10秒")
    far = {**analysis, "keyword_matches": [{**analysis["keyword_matches"][0], "appear_sec": [7.9]}]}
    assert _grade(far, "KWの露出", query="作り方").mark == "△"  # 2.1 秒離れていれば数えない


@pytest.mark.parametrize(
    ("analysis", "mark"),
    [
        ({}, "△"),
        # 「見返す理由」（保存・シェアの動機の欄）は 5 本とも埋まり差が出ないので数えない
        ({"save_share_motivation": "見返す"}, "△"),
        ({"telops": [{"sec": 12, "text": "塩 小さじ1"}]}, "○"),  # 分量をテロップに
        ({"telops": [{"sec": 12, "text": "塩 小さじ1"}], "cta_type": ["save"]}, "◎"),
        ({"telops": [{"sec": 12, "text": "トマト大6つ"}]}, "△"),  # 「6つ」は分量に数えない
        ({"telops": [{"sec": 1, "text": "1. a"}, {"sec": 2, "text": "2. b"}]}, "○"),
        # 推定の役割（真ん中の場面は手順になる）だけでは「手順の段」と数えない
        (
            {
                "scenes": [
                    {"start_sec": 0, "end_sec": 1},
                    {"start_sec": 1, "end_sec": 2},
                    {"start_sec": 2, "end_sec": 3},
                    {"start_sec": 3, "end_sec": 4},
                ]
            },
            "△",
        ),
        (
            {
                "scenes": [
                    {"start_sec": 0, "end_sec": 1, "role": "steps"},
                    {"start_sec": 1, "end_sec": 2, "role": "steps"},
                ]
            },
            "○",
        ),
    ],
)
def test_save_hook_grade(analysis: dict[str, Any], mark: str) -> None:
    assert _grade(analysis, "保存の仕掛け").mark == mark


def test_save_hook_counts_quantities_in_the_caption() -> None:
    g = _grade({}, "保存の仕掛け", desc="材料: 鶏もも肉 300g・クミン 小さじ1")
    assert g.mark == "○" and g.reason == "分量をキャプションに載せている"


@pytest.mark.parametrize(
    ("analysis", "mark"),
    [
        ({"cta_type": ["save"], "cta_sec": 28.0}, "◎"),
        ({"cta_type": ["save"]}, "○"),
        ({"cta_text": "フォローしてね"}, "○"),
        ({}, "△"),
    ],
)
def test_cta_grade(analysis: dict[str, Any], mark: str) -> None:
    assert _grade(analysis, "CTA").mark == mark


@pytest.mark.parametrize(
    ("brands", "mark"),
    [
        ([], "—"),
        (
            [
                {
                    "brand_name": "A",
                    "appear_sec": [1],
                    "total_screen_time_sec": 3.0,
                    "prominence": "hero",
                }
            ],
            "◎",
        ),
        (
            [
                {
                    "brand_name": "A",
                    "appear_sec": [1],
                    "total_screen_time_sec": 2.9,
                    "prominence": "hero",
                }
            ],
            "○",
        ),
        (
            [
                {
                    "brand_name": "A",
                    "appear_sec": [1],
                    "total_screen_time_sec": 9.0,
                    "prominence": "incidental",
                }
            ],
            "○",
        ),
    ],
)
def test_brand_grade(brands: list[dict[str, Any]], mark: str) -> None:
    assert _grade({"brand_detections": brands}, "商品の見せ方").mark == mark


def test_brand_grade_names_client_or_competitor() -> None:
    brand = {
        "brand_name": "A",
        "appear_sec": [2.0],
        "total_screen_time_sec": 4.0,
        "prominence": "prominent",
        "brand_relation": "neutral_third_party",
    }
    g = _grade({"brand_detections": [brand]}, "商品の見せ方", roster=Roster.of(None, ["B|A"]))
    assert g.reason == "A（競合）・初出 2秒・合計 4秒・目立つ"


def test_brands_outside_the_roster_are_not_graded_as_products() -> None:
    """名簿があるときは、名簿の外のブランド（背景のビール缶・調理家電）を「商品◎」にしない。

    名簿が無ければ従来どおり（カテゴリが分からないので映るブランドで評価する）。壊し方: 名簿の
    判定を外す → 名簿の外の主役ブランドが ◎ で赤。
    """
    brands = [_brand("ノンアルY", 55.0, 4.0, "prominent", "neutral_third_party")]
    g = _grade({"brand_detections": brands}, "商品の見せ方", roster=Roster.of("SPICIA", ["T&K"]))
    assert g.mark == "—"
    assert g.reason == "クライアント・競合の商品は映らない（映るのはノンアルY）"
    assert _grade({"brand_detections": brands}, "商品の見せ方").mark == "◎"


def test_brand_relation_from_the_ai_is_ignored_without_a_roster() -> None:
    """クライアント名を渡していないのに、分析 AI が client と推測した（本番の #4）。区分は書かない。"""
    brand = {
        "brand_name": "A",
        "appear_sec": [2.0],
        "total_screen_time_sec": 4.0,
        "prominence": "hero",
        "brand_relation": "client",
    }
    g = _grade({"brand_detections": [brand]}, "商品の見せ方")
    assert g.reason == "A・初出 2秒・合計 4秒・主役"
    k = vs.video_keys(_video({"brand_detections": [brand]}))
    assert k is not None and k.brand_relation == ""


def _brand(name: str, sec: float, total: float, prominence: str, relation: str) -> dict[str, Any]:
    return {
        "brand_name": name,
        "appear_sec": [sec],
        "total_screen_time_sec": total,
        "prominence": prominence,
        "brand_relation": relation,
    }


def test_brand_name_relation_and_seconds_come_from_the_same_brand() -> None:
    """他社が主役で 0.5 秒・クライアントが背景で 5 秒。他社を「（クライアント）」と書かず、秒も混ぜない。"""
    brands = [
        _brand("他社X", 1.0, 0.5, "hero", "neutral_third_party"),
        _brand("花王Y", 4.0, 5.0, "background", "client"),
    ]
    video = _video({"brand_detections": brands})
    roster = Roster.of("花王Y")
    k = vs.video_keys(video, roster=roster)
    assert k is not None
    assert (k.brand_name, k.brand_relation, k.brand_prominence) == ("花王Y", "client", "background")
    assert (k.brand_first_sec, k.brand_total_sec, k.brand_others) == (4.0, 5.0, ("他社X",))
    g = _grade({"brand_detections": brands}, "商品の見せ方", roster=roster)
    assert g.mark == "○"  # 背景なので ◎ にしない（他社の「主役」と混ぜない）
    assert g.reason == "花王Y（クライアント）・初出 4秒・合計 5秒・背景・ほかに他社Xも映る"


def test_brand_picks_client_then_competitor_then_most_prominent() -> None:
    comp_vs_neutral = [
        _brand("N", 0.0, 9.0, "hero", "unknown"),
        _brand("C", 2.0, 1.0, "incidental", "unknown"),
    ]
    k = vs.video_keys(_video({"brand_detections": comp_vs_neutral}), roster=Roster.of("Z", ["C"]))
    assert k is not None and (k.brand_name, k.brand_relation, k.brand_others) == (
        "C",
        "competitor",
        ("N",),
    )
    no_relation = [
        _brand("A", 0.0, 2.0, "incidental", "unknown"),
        _brand("B", 3.0, 1.0, "prominent", "competitor"),  # AI の区分は使わない
        _brand("D", 5.0, 4.0, "prominent", "unknown"),
    ]
    k = vs.video_keys(_video({"brand_detections": no_relation}))
    # 目立つ（prominent）が 2 つなら長く映る方。関係の無いブランドは「クライアント」と書かない
    assert k is not None and (k.brand_name, k.brand_relation) == ("D", "")
    assert k.brand_others == ("B", "A")


def test_detections_of_one_brand_are_merged() -> None:
    """同じブランドの看板とパッケージ（2 つの検出）は 1 つにまとめ、秒を足す。"""
    brands = [
        _brand("A", 6.0, 2.0, "hero", "unknown"),
        _brand("a ", 1.0, 2.0, "background", "client"),
    ]
    roster = Roster.of("Ａ")  # 全角・大文字小文字・空白を無視して名簿と当てる
    k = vs.video_keys(_video({"brand_detections": brands}), roster=roster)
    assert k is not None
    assert (k.brand_name, k.brand_relation, k.brand_prominence) == ("A", "client", "hero")
    assert (k.brand_first_sec, k.brand_total_sec, k.brand_others) == (1.0, 4.0, ())
    assert _grade({"brand_detections": brands}, "商品の見せ方", roster=roster).mark == "◎"


def test_brand_others_are_capped_in_the_reason_and_panel() -> None:
    brands = [_brand(n, 1.0, 1.0, "incidental", "unknown") for n in ("A", "B", "C", "D")]
    g = _grade({"brand_detections": brands}, "商品の見せ方")
    assert g.reason.endswith("ほかにB・Cほか1も映る")


@pytest.mark.parametrize(
    ("value", "mark"), [(80, "◎"), (79, "○"), (60, "○"), (59, "△"), (None, "—")]
)
def test_coherence_grade_boundaries(value: int | None, mark: str) -> None:
    assert _grade({"message_coherence": value}, "一致度").mark == mark


@pytest.mark.parametrize(("value", "mark"), [(95, "○"), (70, "△"), (40, "△")])
def test_coherence_drops_one_step_when_the_ai_names_a_divergence(value: int, mark: str) -> None:
    """本番の #2: 自分で「動画は 5 つ・本文は 4 つで乖離」と書きながら 95 点。1 段下げる。"""
    note = "動画内は5つ、キャプションは4つで記載数に乖離がある"
    g = _grade({"message_coherence": value, "divergence_note": note}, "一致度")
    assert g.mark == mark and "食い違いの指摘あり" in g.reason


def test_grade_rules_footnote_states_the_constants() -> None:
    text = json.dumps(vs.GRADE_RULES, ensure_ascii=False)
    for needle in (
        "1秒以内",
        "3秒以下",
        "6秒以下",
        "3秒以内",
        "10秒以内",
        "合計3秒以上",
        "80以上",
        "60以上",
        "分量を載せている",
        "1 段下げる",
    ):
        assert needle in text
    assert "見返す理由" not in text
    assert [axis for axis, _ in vs.GRADE_RULES] == list(vs.AXES)


def test_unwatched_videos_get_no_grades() -> None:
    cover_only = _video({"hook_type": "number"})
    cover_only.error = "動画取得失敗・サムネのみ軽量分析"
    assert vs.grade_video(cover_only) == []
    assert vs.grade_video(AnalyzedVideo(meta=VideoMeta(rank=1), error="x")) == []


# ── 場面の役割の推定 ───────────────────────────────────────────────────


def test_roles_are_inferred_for_old_outputs_without_the_field() -> None:
    a = VideoVSEOAnalysis.model_validate(RICH_ANALYSES[2])
    roles = vs.infer_roles(a)
    assert roles[0] == ("hook", True)
    assert roles[-1] == ("cta", True)  # cta_sec=44 を含む最後の場面
    assert all(r == ("steps", True) for r in roles[1:-1])


def test_given_roles_are_kept_and_only_missing_or_unknown_ones_inferred() -> None:
    a = VideoVSEOAnalysis.model_validate(RICH_ANALYSES[4])
    roles = vs.infer_roles(a)
    assert roles[0] == ("hook", False)
    assert roles[1] == ("steps", True)  # "transition" は語彙の外なので推定
    assert roles[2] == ("steps", False)


def test_cta_scene_includes_its_end_only_for_the_last_scene() -> None:
    scenes = [
        {"start_sec": 0, "end_sec": 5},
        {"start_sec": 5, "end_sec": 10},
        {"start_sec": 10, "end_sec": 15},
    ]
    assert [r for r, _ in vs.infer_roles(VideoVSEOAnalysis(scenes=scenes, cta_sec=10.0))] == [
        "hook",
        "steps",
        "cta",
    ]
    assert [r for r, _ in vs.infer_roles(VideoVSEOAnalysis(scenes=scenes, cta_sec=15.0))] == [
        "hook",
        "steps",
        "cta",
    ]
    assert [r for r, _ in vs.infer_roles(VideoVSEOAnalysis(scenes=scenes))] == [
        "hook",
        "steps",
        "steps",
    ]


def test_role_shares_use_all_scenes_and_sum_to_the_duration() -> None:
    a = VideoVSEOAnalysis.model_validate(RICH_ANALYSES[1])
    shares = vs.role_shares(a)
    assert [s.role for s in shares] == [
        "hook",
        "problem",
        "steps",
        "result",
        "proof",
        "cta",
        "other",
    ]
    assert sum(s.sec for s in shares) == pytest.approx(36.0)
    hook = shares[0]
    assert (hook.sec, hook.pct) == (2.5, 7)


# ── 構成表の行とコマ ──────────────────────────────────────────────────


def test_each_row_gets_the_frame_nearest_to_the_scene_time() -> None:
    a = RICH_ANALYSES[1]
    frames = [FrameShot(sec=s, data_uri=f"{TINY_JPEG}#{s}") for s in (0.0, 3.0, 6.1, 9.4, 35.0)]
    rows = vs.scene_rows(_video(a, frames=frames))
    assert len(rows) == 12
    got = [(r.start, r.frame.sec if r.frame else None) for r in rows[:4]]
    # 場面の中央: 1.25→0.0（1.25 と 1.75 の差）/ 3.25→3.0 / 6.0→6.1 / 9.5→9.4
    assert got == [(0.0, 0.0), (2.5, 3.0), (4.0, 6.1), (8.0, 9.4)]
    assert rows[-1].frame is not None and rows[-1].frame.sec == 35.0


def test_nearest_frame_prefers_the_earlier_one_on_a_tie_and_skips_empty() -> None:
    frames = [
        FrameShot(sec=2.0, data_uri="x"),
        FrameShot(sec=4.0, data_uri="y"),
        FrameShot(sec=3.0),
    ]
    picked = vs.nearest_frame(frames, 3.0)
    assert picked is not None and picked.sec == 2.0
    assert vs.nearest_frame([], 1.0) is None


def test_rows_fall_back_to_timed_telops_and_are_capped_at_twelve() -> None:
    videos = rich_videos()
    rows1 = vs.scene_rows(videos[0])
    assert (
        rows1[0].telop == "スパイスカレーは4つでいい"
        and rows1[0].speech == "これ、4つだけで作れます"
    )
    rows2 = vs.scene_rows(videos[1])
    assert rows2[1].telop == "ルーなしで作る"  # 場面の欄が無ければ、その時間帯のテロップ
    rows4 = vs.scene_rows(videos[3])
    assert len(rows4) == MAX_SCENE_FRAMES and rows4[-1].start == 65.0  # 最後の場面は残す
    assert vs.omitted_scenes(videos[3]) == 2


def test_scene_timecodes_stay_inside_the_real_duration() -> None:
    """Gemini の尺が 0 や実尺より長くても、コマの秒は実尺（検索結果の尺）の内側に収める。"""
    scenes = [{"start_sec": 0, "end_sec": 40}, {"start_sec": 40, "end_sec": 90}]
    no_gemini_duration = VideoVSEOAnalysis.model_validate({"duration_sec": 0, "scenes": scenes})
    assert [s for s, _ in scene_timecodes(no_gemini_duration, duration_sec=58.0)] == [20.0, 57.95]
    longer = VideoVSEOAnalysis.model_validate({"duration_sec": 90, "scenes": scenes})
    assert [s for s, _ in scene_timecodes(longer, duration_sec=58.0)] == [20.0, 57.95]
    assert [s for s, _ in scene_timecodes(longer)] == [20.0, 65.0]  # 実尺が無ければ Gemini の尺
    # どちらの尺も分からなければ出さない（尺の外の秒で media job を丸ごと落とさない）
    assert scene_timecodes(no_gemini_duration) == []
    # 場面が無いとき（pick_timecodes に倒す）も実尺に収める
    no_scenes = VideoVSEOAnalysis.model_validate({"duration_sec": 90, "cta_sec": 80})
    secs = [s for s, _ in scene_timecodes(no_scenes, duration_sec=30.0)]
    assert secs and max(secs) <= 29.95


def test_rows_match_frames_taken_inside_the_real_duration() -> None:
    """構成表のコマ選びも、コマを抜いた秒と同じ収め方（実尺）で探す。"""
    scenes = [{"start_sec": 0, "end_sec": 10}, {"start_sec": 10, "end_sec": 90}]
    frames = [FrameShot(sec=s, data_uri=f"{TINY_JPEG}#{s}") for s in (5.0, 29.95, 50.0)]
    video = AnalyzedVideo(
        meta=VideoMeta(rank=1, duration_sec=30.0),
        analysis=VideoVSEOAnalysis.model_validate({"duration_sec": 0, "scenes": scenes}),
        frames=frames,
    )
    rows = vs.scene_rows(video)
    assert [r.frame.sec if r.frame else None for r in rows] == [5.0, 29.95]


def test_scene_timecodes_follow_the_rows_and_fit_the_media_job() -> None:
    a = VideoVSEOAnalysis.model_validate(RICH_ANALYSES[4])
    tcs = scene_timecodes(a)
    secs = [s for s, _ in tcs]
    assert len(secs) == MAX_SCENE_FRAMES and secs == sorted(set(secs))
    assert secs[0] == 2.5 and secs[-1] == 67.5
    assert scene_timecodes(VideoVSEOAnalysis(duration_sec=10.0, hook_has_caption=True))  # 場面なし


# ── LLM の文の照合 ────────────────────────────────────────────────────


def _notes_prompt() -> Any:
    videos = rich_videos()
    prompt = build_notes_prompt(
        "{keyword}{client_name}{stages_json}{common_json}{videos_json}",
        keyword=KW,
        client_name=None,
        videos=videos,
        common=vs.common_points(videos),
    )
    assert prompt is not None
    return prompt


def test_payload_only_has_watched_videos_and_the_code_grades() -> None:
    videos = rich_videos()
    assert structure_payload(videos[4]) is None  # 分析失敗
    p = structure_payload(videos[0])
    assert p is not None
    assert [g["記号"] for g in p["評価"]] == [g.mark for g in vs.grade_video(videos[0])]
    assert len(p["場面"]) == 12 and p["場面"][0]["役割"] == "フック"


def test_notes_keep_grounded_items_and_drop_numbers_from_other_videos() -> None:
    prompt = _notes_prompt()
    dropped: list[tuple[str, str]] = []
    raw = {
        "videos": [
            {
                "rank": 1,
                "learn": [
                    "0.5秒で「スパイスカレーは4つでいい」と出して止める",
                    "12秒で商品を映す",  # 1 位に 12 は無い（2 位の KW の秒）
                    "33秒で保存を呼びかける",
                ],
                "weak": ["ブランドが映らない"],
            },
            {"rank": 9, "learn": ["存在しない順位"]},
            {"rank": 2, "learn": ["6秒でテロップ"], "weak": ["冒頭3秒にテロップが無い", "x"]},
        ],
        "storyboard": [
            {"show": "完成品の寄り", "telop": "4つでいい"},
            {"show": "手順を71秒で", "telop": "ルーなしで作る"},
        ],
    }
    notes = ground_notes(raw, prompt=prompt, on_drop=lambda f, r: dropped.append((f, r)))
    assert notes.videos[1].learn == [
        "0.5秒で「スパイスカレーは4つでいい」と出して止める",
        "33秒で保存を呼びかける",
    ]
    assert 9 not in notes.videos
    assert notes.videos[2].weak == ["冒頭3秒にテロップが無い"]  # 弱点は 1 つまで
    assert [(s.stage, s.show, s.telop) for s in notes.storyboard] == [
        (STORYBOARD_STAGES[0], "完成品の寄り", "4つでいい"),
        (STORYBOARD_STAGES[1], "", "ルーなしで作る"),
    ]
    assert dropped == [("learn:1", "number:12"), ("storyboard:1:show", "number:71")]


@pytest.mark.parametrize("n", [1, 5, 10])
def test_notes_max_tokens_fit_the_longest_accepted_output(n: int) -> None:
    """受け取る最大の量（本数 × 3 項目 × 120 字＋絵コンテ 4 段 × 80・40 字）の JSON が、1 字 1.5
    トークンで見積もっても出力の上限に収まる（2 段目の本数の上限は 10 本）。"""
    item = "あ" * 120
    longest = {
        "videos": [{"rank": r, "learn": [item, item], "weak": [item]} for r in range(1, n + 1)],
        "storyboard": [{"show": "い" * 80, "telop": "う" * 40} for _ in STORYBOARD_STAGES],
    }
    text = json.dumps(longest, ensure_ascii=False, indent=2)
    assert len(text) * FAKE_TOKENS_PER_CHAR <= notes_max_tokens(n)


def test_notes_llm_failure_or_garbage_returns_none() -> None:
    videos = rich_videos()
    dropped: list[tuple[str, str]] = []
    notes, cost = conclude_notes(
        lambda prompt: ("ごめんなさい", 0.002),
        "{keyword}{client_name}{stages_json}{common_json}{videos_json}",
        keyword=KW,
        client_name=None,
        videos=videos,
        common=[],
        on_drop=lambda f, r: dropped.append((f, r)),
    )
    assert notes is None and cost == 0.002 and dropped == [("all", "unparseable")]
    html = render_tabs(videos)
    assert NOTES_MISSING in html


# ── 比較タブ・章 ──────────────────────────────────────────────────────


def _chapter(**kw: Any) -> str:
    videos = kw.pop("videos", None) or rich_videos()
    digest = digest_videos(videos, keyword=KW, requested=5, reserved=5)
    return render_video_chapter(keyword=KW, digest=digest, conclusion=None, videos=videos, **kw)


def test_compare_tab_has_every_axis_for_every_video() -> None:
    html = _chapter()
    compare = html[html.index("id='vp-compare'") :]
    table = compare[: compare.index("</table>")]
    for axis in vs.AXES:
        assert f"<th scope='row'>{axis}</th>" in table
    head = table[: table.index("</thead>")]
    assert [int(r) for r in re.findall(r"<th scope='col'>(\d)位", head)] == [1, 2, 3, 4, 5]
    body = table[table.index("<tbody>") :]
    rows = re.findall(r"<tr>(.*?)</tr>", body)
    assert all(row.count("<td") == 5 for row in rows)
    assert all(row.rstrip().endswith("<td>—</td>") for row in rows)  # 5 位は分析失敗
    assert "上位に共通すること" in compare and "フックの型は" in compare


def test_common_hook_says_most_only_when_two_or_more() -> None:
    videos = rich_videos()
    assert videos[1].analysis is not None
    videos[1].analysis.hook_type = "number"
    assert vs.common_points(videos)[0] == "フックの型は数字が最多（2/4本）"


def test_common_points_are_counted_by_code() -> None:
    points = vs.common_points(rich_videos())
    assert points[0] == "フックの型はそろっていない（数字・問いかけ・ビジュアル・その他）"
    # 3 位の 0.8 秒のテロップ「#PR 〇〇カレー粉」は kw_match=True だが KW を含まないので数えない
    assert "検索 KW を3秒以内にテロップか発話で出す: 1/4本" in points
    # CTA の型は上位 2 種類で切らず、全部並べる（購入が抜けない）
    assert "CTA あり: 3/4本（保存 1本・フォロー 1本・購入 1本）" in points


def test_most_good_axis_is_named_only_when_it_leads_alone() -> None:
    """◎の数が同数なら、先頭の軸を「いちばん多い」と書かない（LLM の入力にもなる）。"""
    points = vs.common_points(rich_videos())
    assert not any(p.startswith("◎がいちばん多い評価軸") for p in points)
    tie = next(p for p in points if p.startswith("◎が多い評価軸は並んでいる"))
    # KW の露出は 3 位の申告（KW を含まないテロップに kw_match=True）を数えず、保存の仕掛けは
    # 「見返す理由」を数えないので、どちらも ◎ は 1 本だけになる。
    assert tie == "◎が多い評価軸は並んでいる: 冒頭3秒の掴み・テンポ・CTA・一致度（各2/4本）"
    videos = rich_videos()
    assert videos[1].analysis is not None
    videos[1].analysis.message_coherence = 95  # 2 位の一致度も ◎ にして一致度だけ 3 本
    points = vs.common_points(videos)
    assert "◎がいちばん多い評価軸: 一致度（3/4本）" in points


def test_panels_are_all_readable_without_js() -> None:
    """JS が無いときは、タブはページ内リンク・パネルは hidden を持たない（縦に全部並ぶ）。"""
    html = _chapter()
    assert " hidden" not in html.replace("[hidden]", "")
    for rank in (1, 2, 3, 4):
        assert (
            f"<a class='vtab' id='vt-{rank}' href='#vp-{rank}' aria-controls='vp-{rank}'>" in html
        )
    assert "この動画は分析できませんでした" in html  # 5 位
    assert html.count("<table class='dads-table scenes'>") == 4
    assert "場面が多いため、最初の11場面と最後の場面を出しています（2場面を省略）" in html
    assert "（推定）" in html  # 2 位の役割は推定


def test_other_brands_are_noted_in_the_panel_and_the_llm_input() -> None:
    """見出しのブランド（クライアント）以外に映るブランドは、パネルと LLM の入力に注記する。"""
    from teamagent.skills.video_algorithm.schema import BrandDetection

    videos = rich_videos()
    assert videos[2].analysis is not None
    videos[2].analysis.brand_detections.append(
        BrandDetection(
            brand_name="他社X", appear_sec=[1.0], total_screen_time_sec=0.5, prominence="hero"
        )
    )
    html = _chapter(videos=videos, client_name="〇〇カレー粉")
    panel3 = html[html.index("id='vp-3'") : html.index("id='vp-4'")]
    assert "〇〇カレー粉・合計 6秒・主役・クライアント・ほかに他社X" in panel3
    payload = structure_payload(videos[2], keyword=KW, roster=Roster.of("〇〇カレー粉"))
    assert payload is not None
    assert payload["商品"]["名前"] == "〇〇カレー粉"
    assert payload["商品"]["ほかに映るブランド"] == "他社X"


def test_tabs_follow_hash_changes_after_load() -> None:
    """開いた後に #vp-N へ移っても（ページ内リンク・戻る）タブを切り替える。"""
    from teamagent.skills.search_surface_check.video_chapter import TABS_JS

    assert "addEventListener('hashchange'" in TABS_JS
    handler = TABS_JS[TABS_JS.index("addEventListener('hashchange'") :]
    assert "select(k,false)" in handler[: handler.index("});")]


def test_client_brand_and_inferred_roles_show_in_the_panel() -> None:
    html = _chapter(client_name="〇〇カレー粉")
    panel3 = html[html.index("id='vp-3'") : html.index("id='vp-4'")]
    assert "〇〇カレー粉" in panel3 and "クライアント" in panel3
    assert "商品（ブランド）" in panel3


def test_client_label_needs_the_client_name_not_the_ai_guess() -> None:
    """クライアント名が無ければ、分析 AI が client と書いていても「クライアント」と出さない。"""
    html = _chapter()
    panel3 = html[html.index("id='vp-3'") : html.index("id='vp-4'")]
    assert "〇〇カレー粉" in panel3 and "クライアント" not in panel3


def test_unsafe_or_oversized_images_are_not_embedded() -> None:
    videos = rich_videos()
    videos[0].cover_data_uri = "javascript:alert(1)"
    videos[1].cover_data_uri = "data:image/jpeg;base64,AAAA' onerror='alert(1)"
    videos[2].cover_data_uri = "data:image/jpeg;base64," + "A" * FRAME_MAX_CHARS
    html = _chapter(videos=videos)
    assert "javascript:alert" not in html and "onerror" not in html
    assert "A" * 1000 not in html


def test_image_budget_caps_the_file_size() -> None:
    videos = rich_videos()
    big = "data:image/jpeg;base64," + "B" * 14_000  # 幅 180px の JPEG 相当
    for v in videos:
        v.cover_data_uri = big
        v.frames = [FrameShot(sec=f.sec, data_uri=big) for f in v.frames]
    full = _chapter(videos=videos)
    assert len(full) < 1_000_000  # 4 本 × (表紙 + 12 コマ) でも 1MB 未満
    small = _chapter(videos=videos, image_budget=100_000)
    assert small.count(big) <= 100_000 // len(big)
    assert "コマは埋め込んでいません" in small and "コマ省略" in small


def test_slack_text_stays_one_line_per_video() -> None:
    from teamagent.skills.search_surface_check.video_render import build_followup_slack_text

    videos = rich_videos()
    digest = digest_videos(videos, keyword=KW, requested=5, reserved=5)
    text = build_followup_slack_text(
        keyword=KW,
        digest=digest,
        conclusion=None,
        videos=videos,
        report_url="https://x",
        total_cost_usd=0,
    )
    per_video = [ln for ln in text.splitlines() if re.match(r"- \d位 ", ln)]
    assert len(per_video) == 5
    assert "構成表" not in text and "場面" not in text


def test_digest_is_unchanged_by_scene_details() -> None:
    """場面の欄は構成表のため。集計（B5 の Slack・結論）の値は変えない。"""
    videos = rich_videos()
    plain = [v.model_copy(deep=True) for v in videos]
    for v in plain:
        if v.analysis is not None:
            for sc in v.analysis.scenes:
                sc.role = sc.telop = sc.speech = sc.intent = None
    a = digest_videos(videos, keyword=KW, requested=5, reserved=5)
    b = digest_videos(plain, keyword=KW, requested=5, reserved=5)
    assert isinstance(a, VideoDigest) and a == b
