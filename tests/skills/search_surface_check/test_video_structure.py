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
    structure_payload,
)
from teamagent.skills.search_surface_check.video_render import render_video_chapter
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

KW = "スパイスカレー"


def _video(
    analysis: dict[str, Any], *, rank: int = 1, frames: list[FrameShot] | None = None
) -> Any:
    return AnalyzedVideo(
        meta=VideoMeta(rank=rank, author=f"u{rank}", duration_sec=30.0),
        analysis=VideoVSEOAnalysis.model_validate(analysis),
        frames=frames or [],
    )


def _grade(analysis: dict[str, Any], axis: str) -> vs.Grade:
    return next(g for g in vs.grade_video(_video(analysis)) if g.axis == axis)


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
        (
            {
                "hook_has_caption": False,
                "hook_type": "number",
                "telops": [{"sec": 0.5, "text": "a"}],
            },
            "○",
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


@pytest.mark.parametrize(
    ("analysis", "mark"),
    [
        ({"telops": [{"sec": 3.0, "text": "kw", "kw_match": True}]}, "◎"),
        ({"telops": [{"sec": 3.1, "text": "kw", "kw_match": True}]}, "○"),
        ({"spoken_keywords": [{"matched": True, "appear_sec": [10.0]}]}, "○"),
        ({"spoken_keywords": [{"matched": True, "appear_sec": [10.1]}]}, "△"),
        ({"spoken_keywords": [{"matched": True}]}, "○"),  # 出るが秒は不明
        ({"keyword_matches": [{"matched": True, "layer": "caption"}]}, "△"),  # キャプションだけ
        ({}, "△"),
    ],
)
def test_kw_exposure_grade_boundaries(analysis: dict[str, Any], mark: str) -> None:
    assert _grade(analysis, "KWの露出").mark == mark


def test_kw_exposure_takes_the_earliest_layer() -> None:
    g = _grade(
        {
            "telops": [{"sec": 5.0, "text": "kw", "kw_match": True}],
            "spoken_keywords": [{"matched": True, "appear_sec": [1.5]}],
        },
        "KWの露出",
    )
    assert g.mark == "◎" and g.reason.startswith("発話に 1.5秒")


@pytest.mark.parametrize(
    ("analysis", "mark"),
    [
        ({}, "△"),
        ({"save_share_motivation": "見返す"}, "○"),
        ({"save_share_motivation": "見返す", "cta_type": ["save"]}, "◎"),
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
        "brand_relation": "competitor",
    }
    g = _grade({"brand_detections": [brand]}, "商品の見せ方")
    assert g.reason == "A（競合）・初出 2秒・合計 4秒・目立つ"


@pytest.mark.parametrize(
    ("value", "mark"), [(80, "◎"), (79, "○"), (60, "○"), (59, "△"), (None, "—")]
)
def test_coherence_grade_boundaries(value: int | None, mark: str) -> None:
    assert _grade({"message_coherence": value}, "一致度").mark == mark


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
    ):
        assert needle in text
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
    assert "検索 KW を3秒以内にテロップか発話で出す: 2/4本" in points
    assert any(p.startswith("CTA あり: 3/4本") for p in points)


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


def test_client_brand_and_inferred_roles_show_in_the_panel() -> None:
    html = _chapter()
    panel3 = html[html.index("id='vp-3'") : html.index("id='vp-4'")]
    assert "〇〇カレー粉" in panel3 and "クライアント" in panel3
    assert "商品（ブランド）" in panel3


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
