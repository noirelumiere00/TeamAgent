"""横断シンセシス v3（仕様 v3 §3）の入力・出力の検査のテスト。

フェイクは本番の失敗の形を再現する（prod_shape: ρ タグ・〔上位 2/5〕・御社・cta_consensus
visit/save・angle problem_solving・4〜5 種の食い違い・横長コマ・#PR・実在しないテロップへの
言い換え）。v3 の検査は「常時」の型なので、env GROUNDING_MODE_VIDEO_ALGORITHM が shadow（既定）
でも効くことを確かめる（数字の照合だけは enforce のとき落とす）。

変異テスト（修正を戻したら赤）の対応は各テストの docstring の「壊し方」。
"""

from __future__ import annotations

import functools
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.adapters.video_algorithm_cache import VideoAlgorithmResultCache
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.evidence import TIER_CASE, TIER_MAJORITY, Roster
from teamagent.skills.video_algorithm.facts import video_facts
from teamagent.skills.video_algorithm.rebuild import rebuild_cross
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CrossSynthesis,
    StatsAnalysis,
    TelopItem,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.slides import render_slides
from teamagent.skills.video_algorithm.synthesis import (
    SYNTHESIS_VERSION_ENV,
    build_prompt,
    parse_synthesis_v3,
    synthesis_version_from_env,
    synthesize,
)
from teamagent.skills.video_algorithm.synthesis_checks import (
    CheckLog,
    extract_claims,
    finalize,
    find_conflicts,
    has_avoid,
    strip_count_tags,
)
from teamagent.skills.video_algorithm.synthesis_input import SynthesisContext, cut_plan
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    prod_board,
    prod_synthesis,
    prod_videos,
)

AVOID = ["ルー卒業"]


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROUNDING_MODE_VIDEO_ALGORITHM", raising=False)
    monkeypatch.delenv(SYNTHESIS_VERSION_ENV, raising=False)


def _ctx(
    roster: Roster | None = None, avoid: list[str] | None = None, **kw: Any
) -> SynthesisContext:
    return SynthesisContext.build(
        prod_videos(), QUERY, board=prod_board(), roster=roster, avoid_terms=avoid, **kw
    )


def _final(payload: dict[str, Any], ctx: SynthesisContext | None = None) -> CrossSynthesis:
    return finalize(CrossSynthesis.model_validate(payload), ctx or _ctx(avoid=AVOID))


def _resp(payload: dict[str, Any] | str, cost: float = 0.002) -> GeminiResponse:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return GeminiResponse(
        text=f"所見です。\n```json\n{body}\n```",
        input_tokens=9000,
        output_tokens=3000,
        cost_usd=cost,
        model_id="gemini-3.5-flash",
        latency_ms=20000,
    )


def _gemini(*payloads: dict[str, Any] | str) -> MagicMock:
    gem = MagicMock()
    gem.generate_text.side_effect = [_resp(p) for p in payloads]
    return gem


def _screen(syn: CrossSynthesis) -> str:
    """画面用のデータ（描画が読む欄すべて）。"""
    return json.dumps(syn.model_dump(mode="json"), ensure_ascii=False)


def _ref(rank: int, sec: float | None, quote: str) -> dict[str, Any]:
    return {"rank": rank, "sec": sec, "quote": quote}


# v3 の形の出力（本番で起きた誤りの形を v3 の欄に入れたもの）。
_V3: dict[str, Any] = {
    "summary_lines": {
        "type_line": "スパイス4選と30分調理で勝つ",
        "feature_ids": ["first_telop_0s", "qty_caption"],
        "best_reason": "痩せたい動機を0秒に出し、分量をテロップで全部見せている。",
        "client_move": "御社のスパイスで分量を全部出す1本を試す。",
    },
    "per_video": [
        {
            "rank": 4,
            "win_line": "痩せたい動機から入る",
            "why_fact": "0秒に「とにかく痩せたいから」。",
            "why_guess": "減量中の層に届いた可能性",
            "steal": ["分量をテロップで出す", "悩む表情のアップで始める"],
            "not_to_copy": "タイアップ商品の名前",
        }
    ],
    "directives": [
        {
            "text": "分量をテロップで全部出す〔上位 2/5〕",
            "kind": "テロップ",
            "refs": [_ref(4, 25.0, "大さじ8杯"), _ref(5, 13.0, "塩 小さじ1/2")],
        },
        {
            "text": "スプーンで引き上げる完成映像から始める",
            "kind": "フック",
            "refs": [_ref(1, 0.0, "スプーンで引き上げる")],
        },
        {
            "text": "冒頭でカレールーは卒業と宣言する",
            "kind": "フック",
            "refs": [_ref(3, 0.0, "カレールーはもう卒業！"), _ref(2, 0.0, "作ってみたい")],
        },
    ],
    "avoid": [
        {"text": "カレールーは卒業の宣言で始める", "refs": [_ref(3, 0.0, "カレールーはもう卒業")]}
    ],
    "storyboards": [
        {
            "name": "動機から分量へ",
            "basis_ranks": [4, 5],
            "cuts": [
                {
                    "cut": 1,
                    "show": "食べる画に動機のテロップ",
                    "telop": "痩せたいから（案）",
                    "aim": "動機で止める",
                    "refs": [_ref(4, 0.0, "とにかく痩せたいから")],
                    "start_sec": 30,
                },
                {
                    "cut": 4,
                    "show": "分量を1つずつ出す",
                    "telop": "大さじ8杯（案）",
                    "aim": "保存したくなる",
                    "refs": [_ref(4, 25.0, "大さじ8杯")],
                },
            ],
        },
        {
            "name": "ルー卒業の宣言",
            "cuts": [
                {
                    "cut": 1,
                    "show": "完成品",
                    "telop": "ルー卒業（案）",
                    "aim": "驚かせる",
                    "refs": [_ref(3, 0.0, "カレールーはもう卒業！")],
                }
            ],
        },
    ],
    "board_angles": [
        {"label": "無水", "match_terms": ["無水"]},
        {"label": "カレー", "match_terms": ["カレー"]},
    ],
    "hypotheses": [
        {
            "text": "紹介するスパイスを4種類から5種類に絞ると保存されやすい〔ρ=−0.30, n=5〕",
            "match_terms": ["4種類"],
            "test": "主役4種に絞った版を投稿して比べる",
        },
        {
            "text": "30分で作れると伝えると見られやすい。ρが負なので短尺ほど上位。",
            "match_terms": ["30分"],
            "test": "30分の表記の有無で比べる",
        },
        {
            "text": "離脱を防ぐため冒頭で完成品を見せる",
            "match_terms": ["完成"],
            "test": "",
        },
    ],
    "posting": {"caption_plan": "キャプションに分量を全部書く", "ab_plan": "翌日の順位で比べる"},
}


# ── T2 ρ を書かせない ─────────────────────────────────────────────────────


def test_rho_never_reaches_the_screen_data() -> None:
    """壊し方: 常時の deny から「ρ」を外す（STAT_WORDS）→ 仮説 2 の「ρが負なので」が残って赤。"""
    syn = _final(_V3)
    screen = _screen(syn)
    assert "ρ" not in screen and "相関" not in screen and "n=" not in screen
    h = next(h for h in syn.hypotheses if "30分" in h.text)
    assert h.text == "30分で作れると伝えると見られやすい。（タイアップ投稿 #1を含む）"
    # 本番の形（v2 の欄・〔ρ=−0.30, n=5〕）でも出ない
    assert "ρ" not in _screen(finalize(prod_synthesis(), _ctx()))


def test_correlations_are_not_passed_to_the_llm_below_eight_videos() -> None:
    """C1: n<8 では相関を渡さない。壊し方: CORR_MIN_N を 3 にする → 相関の行が入って赤。"""
    videos = prod_videos()
    cross = cross_analyze(videos, QUERY, board=prod_board())
    assert cross.stats is not None and cross.stats.correlations  # 計算はしている（付録用）
    prompt = build_prompt(videos, QUERY, cross.stats, board=prod_board())
    assert "ρ" not in prompt and "特徴×表示順位" not in prompt


def _small_video(rank: int) -> AnalyzedVideo:
    return AnalyzedVideo(
        meta=VideoMeta(
            rank=rank,
            url=f"https://t/{rank}",
            desc="新宿 ランチ",
            play_count=10_000 * (10 - rank),
            collect_count=100 + rank,
            duration_sec=20.0 + rank,
        ),
        analysis=VideoVSEOAnalysis(
            duration_sec=20.0 + rank,
            telops=[TelopItem(sec=float(i), text=f"テロップ{i}") for i in range(rank)],
        ),
    )


def test_eight_videos_pass_correlations_and_code_writes_the_tag() -> None:
    """R2: n≥8 だけ渡し、タグは文に特徴名があるときだけコードが付ける。"""
    videos = [_small_video(r) for r in range(1, 9)]
    cross = cross_analyze(videos, "新宿 ランチ")
    assert cross.stats is not None
    prompt = build_prompt(videos, "新宿 ランチ", cross.stats)
    assert "特徴×表示順位の相関" in prompt and "テロップ枚数 ρ" in prompt
    ctx = SynthesisContext.build(videos, "新宿 ランチ", stats=cross.stats)
    syn = finalize(
        CrossSynthesis.model_validate(
            {
                "hypotheses": [
                    {
                        "text": "テロップ枚数が少ないほど上位の可能性",
                        "match_terms": ["テロップ"],
                        "stat_feature": "テロップ枚数",
                    },
                    {"text": "新宿の語を出す", "match_terms": ["新宿"], "stat_feature": "保存率"},
                ]
            }
        ),
        ctx,
    )
    tags = [h.stat_tag for h in syn.hypotheses]
    assert tags[0].startswith("〔テロップ枚数×順位 ρ=") and "n=8" in tags[0]
    assert tags[1] == ""  # 文に「保存率」が無い
    assert all("ρ" not in h.hypothesis for h in syn.win_hypotheses)  # 描画の本文には出さない


# ── T3 本数タグはコードだけ ─────────────────────────────────────────────────


def test_llm_count_tags_are_stripped_and_code_tags_are_added() -> None:
    """壊し方: strip_count_tags を外す（そのまま返す）→ 〔上位 2/5〕が残って赤。"""
    syn = _final(_V3)
    d = next(d for d in syn.directives if d.origin == "llm")
    assert d.text.startswith("分量をテロップで全部出す") and "〔" not in d.text
    assert (d.tier, d.ranks) == (TIER_CASE, [4, 5])
    line = next(x for x in syn.creative_brief if x.startswith("分量をテロップで全部出す"))
    assert "上位 2/5" not in line
    assert line.endswith("〔事例 2/5（#4・#5）｜根拠 #4 25秒「大さじ8杯」〕")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("冒頭で出す〔上位 2/5〕", "冒頭で出す"),
        ("尺は46-59秒〔尺46-59秒, n=5〕", "尺は46-59秒"),
        ("保存される〔保存率0.8%以上, n=5〕", "保存される"),
        ("上位2/5本で観測", "で観測"),
        ("塩 小さじ1/2 を入れる", "塩 小さじ1/2 を入れる"),  # 分量は本数ではない
        ("4/30本がタイアップ", "がタイアップ"),
        ("ρ=−0.80 の特徴", " の特徴"),
    ],
)
def test_strip_count_tags(text: str, expected: str) -> None:
    assert strip_count_tags(text, 5, 30) == expected


# ── T4/R4/R5 見出し・段階名 ───────────────────────────────────────────────


def test_headline_with_a_case_feature_or_assertive_word_becomes_the_code_line() -> None:
    """壊し方: feature_ids の段階の照合を外す → 「で勝つ」の見出しが残って赤。"""
    syn = _final(_V3)
    sl = syn.summary_lines
    assert sl is not None and sl.type_line_by_code
    assert sl.type_line == syn.headline
    assert "勝" not in syn.headline and "4選" not in syn.headline
    assert syn.headline.startswith("上位5本の共通点（仮説）: 最初のテロップが0秒台")
    assert sl.feature_ids == ["first_telop_0s", "kw_telop:スパイスカレー"]
    assert sl.best_rank == 4


def test_headline_from_majority_features_is_kept() -> None:
    syn = _final(
        {
            "summary_lines": {
                "type_line": "0秒のテロップと分量の明示が上位に多い",
                "feature_ids": ["first_telop_0s", "qty_anywhere"],
            }
        }
    )
    assert syn.summary_lines is not None and not syn.summary_lines.type_line_by_code
    assert syn.headline == "0秒のテロップと分量の明示が上位に多い"


def test_headline_from_a_case_feature_becomes_the_code_line() -> None:
    """壊し方: feature_ids の段階（事例）の照合を外す → 事例の見出しが残って赤。"""
    syn = _final(
        {"summary_lines": {"type_line": "見た目で始める型が上位", "feature_ids": ["hook:visual"]}}
    )
    assert syn.summary_lines is not None and syn.summary_lines.type_line_by_code
    assert syn.headline.startswith("上位5本の共通点（仮説）")


@pytest.mark.parametrize(
    "line",
    [
        "4種のスパイスで上位を狙う",  # 数の主張が多数派でない（4 種は #1・#2 だけ）
        "尺46〜59秒で作るのが上位に多い",  # 上位 2 本の幅
        "分量の明示で確実に上位",  # 断定語
    ],
)
def test_headline_claims_must_be_majority(line: str) -> None:
    syn = _final({"summary_lines": {"type_line": line, "feature_ids": ["qty_anywhere"]}})
    assert syn.summary_lines is not None and syn.summary_lines.type_line_by_code


def test_winning_words_and_assertions_are_rephrased() -> None:
    syn = _final(
        {
            "summary_lines": {
                "type_line": "x",
                "best_reason": "勝ち筋は分量で、必ず上位を取れる。",
            }
        }
    )
    assert syn.summary_lines is not None
    assert syn.summary_lines.best_reason == "共通点は分量で、上位を狙える。"


# ── T5/R7 match_terms の数え直し ─────────────────────────────────────────────


def test_hypothesis_ranks_are_recounted_from_match_terms() -> None:
    """壊し方: term_ranks を外して LLM の順位を使う → #2 が残って赤（v2 の概念 [2,3] で確認）。

    「30分」は #3 のテロップ「調理時間は30分で」と #1 のテロップ「弱火で30分」にある
    （#1 は煮込み時間で意味は違うが、語で数える規則なので数える。キャプションの先頭を人が見る）。
    #2 には無い。
    """
    syn = _final(_V3)
    h = next(h for h in syn.hypotheses if h.match_terms == ["30分"])
    assert h.ranks == [1, 3] and h.tier == TIER_CASE
    concepts = finalize(prod_synthesis(), _ctx()).common_concepts
    thirty = next(c for c in concepts if "30分" in c.concept)
    assert thirty.videos == [1, 3] and thirty.prevalence == "2/5"


def test_hypothesis_with_one_or_zero_videos_or_no_terms_is_dropped() -> None:
    syn = _final(
        {
            "hypotheses": [
                {"text": "30分でと言う", "match_terms": ["30分で"]},  # #3 だけ
                {"text": "語が無い仮説", "match_terms": []},
                {"text": "分量を載せる", "match_terms": ["大さじ"]},
            ]
        }
    )
    assert [h.text for h in syn.hypotheses] == ["分量を載せる（タイアップ投稿 #4を含む）"]
    assert syn.hypotheses[0].ranks == [3, 4, 5] and syn.hypotheses[0].tier == TIER_MAJORITY


# ── T6/R8 refs の照合 ──────────────────────────────────────────────────────


def test_refs_are_verified_against_the_video() -> None:
    """壊し方: evidence.SEC_TOLERANCE を 10 にする → ±2.1 秒の ref が通って赤。"""
    syn = _final(
        {
            "directives": [
                {"text": "完成映像から入る", "refs": [_ref(1, 0.0, "スプーンで引き上げる")]},
                {"text": "分量を大きく出す", "refs": [_ref(4, 25.0, "大さじ8杯")]},
                {"text": "分量を秒で出す", "refs": [_ref(4, 27.0, "大さじ8杯")]},
                {"text": "分量を遅れて出す", "refs": [_ref(4, 27.1, "大さじ8杯")]},
            ]
        }
    )
    llm = [d for d in syn.directives if d.origin == "llm"]
    assert [d.text for d in llm] == [
        "分量を大きく出す（タイアップ投稿 #4）",
        "分量を秒で出す（タイアップ投稿 #4）",
    ]
    assert llm[0].refs[0].source == "telop" and llm[0].refs[0].found_sec == 25.0


def test_llm_cannot_write_code_only_fields() -> None:
    """段階・順位・カットの秒・照合の結果は LLM の JSON から捨てる（コードが決め直す）。"""
    raw = {
        "directives": [
            {
                "text": "分量を出す",
                "tier": "必須条件",
                "ranks": [1, 2, 3, 4, 5],
                "origin": "code",
                "refs": [{**_ref(4, 25.0, "大さじ8杯"), "source": "telop", "found_sec": 99}],
            }
        ],
        "storyboards": [{"name": "案", "cuts": [{"cut": 1, "start_sec": 30, "stage": "x"}]}],
        "version": "v3",
    }
    syn = parse_synthesis_v3(f"```json\n{json.dumps(raw, ensure_ascii=False)}\n```")
    assert syn is not None and syn.version == ""
    d = syn.directives[0]
    assert (d.tier, d.ranks, d.origin) == ("", [], "llm")
    assert (d.refs[0].source, d.refs[0].found_sec) == ("", None)
    assert syn.storyboards[0].cuts[0].start_sec is None


def test_storyboard_cut_seconds_come_from_the_code_plan() -> None:
    syn = _final(_V3)
    assert len(syn.storyboards) == 1  # 2 案目は避けたい訴求で落ちる
    sb = syn.storyboards[0]
    assert [(c.cut, c.start_sec, c.end_sec, c.stage) for c in sb.cuts] == [
        (1, 0.0, 3.0, "0〜3秒"),
        (4, 23.0, 37.0, "10秒〜残り10秒"),
    ]
    assert sb.target_sec == 60.0 and sb.basis_ranks == [4]
    assert sb.basis_note == "事例1本（#4）にもとづく案（タイアップ投稿 #4を含む）"
    assert [c.cut for c in cut_plan(60.0)] == [1, 2, 3, 4, 5, 6]
    assert [(c.start, c.end) for c in cut_plan(15.0)] == [(0, 3), (3, 10), (10, 15)]


# ── T7/R6 数の食い違い ─────────────────────────────────────────────────────


def _conflict_payload(test: str) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "text": "紹介するスパイスを4種類から5種類に厳選する",
                "match_terms": ["スパイス"],
                "test": test,
            }
        ]
    }


def test_number_conflict_is_detected() -> None:
    fields = [("a", "紹介するスパイスを4種類から5種類に厳選する"), ("b", "主役4種に絞る")]
    assert [(c.field, c.group) for c in find_conflicts(fields)] == [("b", "種")]
    assert find_conflicts([("a", "4種に絞る"), ("b", "主役4種")]) == []
    # A/B の比較（4種と5種）は主張ではない
    assert find_conflicts([("a", "4種に絞る"), ("b", "4種版と5種版で比べる")]) == []
    # 対象の違う秒は比べない（冒頭3秒と尺60秒）
    assert find_conflicts([("a", "冒頭3秒で出す"), ("b", "尺60秒にする")]) == []
    assert [c.values for c in extract_claims("4〜5種")] == [frozenset({"4", "5"})]


def test_conflict_asks_once_then_drops_the_later_sentence() -> None:
    """壊し方: 検出器を外す（find_conflicts が空）→ 作り直しが呼ばれず・後の文が残って赤。"""
    bad = _conflict_payload("主役4種に絞った版を投稿して比べる")
    gem = _gemini(bad, bad)
    syn, cost = synthesize(gem, prod_videos(), QUERY, request_id="r-t7", board=prod_board())
    assert syn is not None
    assert gem.generate_text.call_count == 2
    retry_prompt = gem.generate_text.call_args_list[1].args[0]
    assert "前回の出力の食い違い" in retry_prompt and "4・5と4" in retry_prompt
    h = syn.hypotheses[0]
    assert h.text.startswith("紹介するスパイスを4種類から5種類") and h.test == ""
    assert cost == pytest.approx(0.004)


def test_conflict_fixed_by_the_retry_keeps_the_new_output() -> None:
    gem = _gemini(
        _conflict_payload("主役4種に絞った版を投稿して比べる"),
        _conflict_payload("スパイスの数を変えずに投稿時間だけ変えて比べる"),
    )
    syn, _ = synthesize(gem, prod_videos(), QUERY, request_id="r-t7b", board=prod_board())
    assert syn is not None and gem.generate_text.call_count == 2
    assert syn.hypotheses[0].test == "スパイスの数を変えずに投稿時間だけ変えて比べる"


def test_no_conflict_calls_the_llm_once() -> None:
    gem = _gemini(_conflict_payload("投稿時間だけ変えて比べる"))
    synthesize(gem, prod_videos(), QUERY, request_id="r-t7c", board=prod_board())
    assert gem.generate_text.call_count == 1


# ── T8/T9/R11 クライアント ─────────────────────────────────────────────────


def test_unspecified_client_words_become_the_placeholder() -> None:
    """壊し方: 置き換えを外す → 「御社」が残って赤。"""
    syn = _final(_V3)
    assert syn.client_pitch == "（クライアント商品）のスパイスで分量を全部出す1本を試す。"
    assert "御社" not in _screen(syn) and "貴社" not in _screen(syn)
    assert "御社" not in _screen(finalize(prod_synthesis(), _ctx()))


def test_specified_client_name_is_used() -> None:
    ctx = _ctx(Roster.of(CLIENT, COMPETITORS), AVOID)
    syn = finalize(CrossSynthesis.model_validate(_V3), ctx)
    assert syn.client_pitch == "SPICIAのスパイスで分量を全部出す1本を試す。"
    prompt = build_prompt(prod_videos(), QUERY, roster=Roster.of(CLIENT, COMPETITORS))
    assert "- クライアント: SPICIA（提案文では「SPICIA」と書く）" in prompt
    assert "- 競合: T&K（別名: ティーケー食品）、ハーブ専科" in prompt


# ── T10/R11 避けたい訴求 ───────────────────────────────────────────────────


def test_avoid_terms_drop_recommendations_but_not_quoted_facts() -> None:
    """壊し方: 避けたい訴求の検査を外す → 「ルー卒業」の指示と絵コンテが残って赤。

    事実の引用欄（refs）ややらないこと・#3 の冒頭 3 秒の原文には残す（そこに掛けたら赤）。
    """
    syn = _final(_V3)
    texts = [d.text for d in syn.directives]
    assert not any(has_avoid(t, AVOID) for t in texts)
    assert all("ルー卒業" not in sb.name for sb in syn.storyboards)
    assert not any(has_avoid(t, AVOID) for t in syn.creative_brief)
    assert [a.text for a in syn.avoid] == ["カレールーは卒業の宣言で始める"]
    kept = _final(
        {
            "directives": [
                {
                    "text": "冒頭に強い宣言のテロップを置く",
                    "refs": [
                        _ref(3, 0.0, "カレールーはもう卒業！"),
                        _ref(2, 0.0, "スパイスカレーを作ってみたい！"),
                    ],
                }
            ],
            "summary_lines": {"client_move": "ルー卒業を打ち出す"},
        }
    )
    d = next(d for d in kept.directives if d.origin == "llm")
    assert d.refs[0].quote == "カレールーはもう卒業！"
    assert kept.client_pitch == ""
    f3 = video_facts(prod_videos()[2], QUERY)
    assert f3.opening_telops[0] == (0.0, "カレールーはもう卒業！")


# ── R9・R12・R13・R14 ────────────────────────────────────────────────────


def test_single_low_performer_directive_moves_to_avoid() -> None:
    """壊し方: 移す処理を外す → #3（再生 2.05 万）だけの指示が指示に残って赤。"""
    syn = _final(
        {
            "directives": [
                {"text": "調理時間を冒頭で言う", "refs": [_ref(3, 2.0, "調理時間は30分で")]}
            ]
        }
    )
    assert not any(d.text.startswith("調理時間") for d in syn.directives)
    moved = syn.avoid[0]
    assert moved.origin == "moved" and moved.ranks == [3]
    assert moved.reason == "実績が伴わない事例（#3・再生が5本の中央値の2割未満）"


def test_unmeasured_metrics_and_framing_without_data_are_dropped() -> None:
    """壊し方: 未計測指標の語（R12）や画角の語（R14）の検査を外す → 残って赤。"""
    syn = _final(_V3)
    screen = _screen(syn)
    assert "離脱" not in screen and "表情" not in screen
    assert syn.per_video[0].steal == ["分量をテロップで出す"]
    only = _final(
        {
            "summary_lines": {"best_reason": "視聴維持率が高い。分量をテロップで出している。"},
            "hypotheses": [{"text": "分量を出すと離脱が減る", "match_terms": ["大さじ"]}],
            "directives": [
                {"text": "完了率を上げるため分量を出す", "refs": [_ref(4, 25.0, "大さじ8杯")]}
            ],
        }
    )
    assert only.summary_lines is not None
    assert only.summary_lines.best_reason == "分量をテロップで出している。"
    assert only.hypotheses == [] and [d.origin for d in only.directives] == ["code"] * 3


def test_pr_videos_get_the_tieup_note() -> None:
    syn = _final(_V3)
    d = next(d for d in syn.directives if d.origin == "llm")
    assert d.text.endswith("（タイアップ投稿 #4を含む）")


# ── T29/R16 角度 ──────────────────────────────────────────────────────────


def test_angles_outside_the_vocabulary_or_too_wide_are_dropped() -> None:
    """壊し方: 許可値・広さの検査を外す → problem_solving と 4/5 のクラスタが残って赤。"""
    syn = _final(
        {
            "angle_clusters": [
                {"angle": "problem_solving", "label_jp": "悩み解決", "videos": [1, 2]},
                {"angle": "convenience", "label_jp": "手軽", "videos": [1, 2, 3, 4]},
                {"angle": "novelty", "label_jp": "意外性", "videos": [2, 3, 9]},
            ],
        }
    )
    assert [(a.angle, a.videos) for a in syn.angle_clusters] == [("novelty", [2, 3])]
    board = _final(_V3).board_angles
    assert [(b.label, b.ranks) for b in board] == [("無水", [4, 14, 16, 20])]  # カレーは全部


# ── 本番の形（prod_output_slim.json の cross.synthesis と同じ誤り）───────────────────


_PROD_ERRORS = (
    "ρ",
    "〔上位",
    "n=5",
    "御社",
    "勝つ",
    "勝ち筋",
    "離脱",
    "visit",
    "problem_solving",
    "46-59",
    "4種類から5種類",
    "表情",
    "アップ",
    "スプーンで引き上げる",
)


def test_prod_shaped_synthesis_errors_disappear_from_the_screen_data() -> None:
    """本番の JSON の形（v2 の欄）をそのまま v3 の経路に入れても、誤りが画面用のデータに残らない。"""
    raw = prod_synthesis().model_dump(mode="json")
    gem = _gemini(raw)
    syn, _ = synthesize(
        gem, prod_videos(), QUERY, request_id="r-prod", board=prod_board(), avoid_terms=AVOID
    )
    assert syn is not None and syn.version == "v3"
    screen = _screen(syn)
    for word in _PROD_ERRORS:
        assert word not in screen, word
    assert syn.shared_funnel is None  # 来店・保存の多数派（誤り）は出さない
    assert syn.summary_lines is not None and syn.summary_lines.type_line_by_code
    assert syn.creative_brief[0] == (
        "最初のテロップを0秒台に出す〔必須条件 5/5｜根拠 #4 0秒「とにかく痩せたいから」〕"
    )
    out = VideoAlgorithmOutput(
        query=QUERY,
        videos=prod_videos(),
        board=prod_board(),
        cross=cross_analyze(prod_videos(), QUERY, board=prod_board()),
    )
    out.cross.synthesis = syn
    slides = render_slides(out)
    for word in ("ρ", "御社", "勝つ", "〔上位", "離脱"):
        assert word not in slides, word
    report = render_report(out)
    for word in ("御社", "〔上位", "離脱", "来店"):
        assert word not in report, word


def test_finalize_is_idempotent() -> None:
    ctx = _ctx(Roster.of(CLIENT, COMPETITORS), AVOID)
    once = finalize(CrossSynthesis.model_validate(_V3), ctx)
    assert finalize(once, ctx).model_dump() == once.model_dump()
    once_prod = finalize(prod_synthesis(), ctx)
    assert finalize(once_prod, ctx).model_dump() == once_prod.model_dump()


def test_code_writes_the_fact_directives_first() -> None:
    """M7: 0 秒のテロップ（5/5）など事実の指示はコードが作る（LLM が書かなくても出る）。"""
    syn = _final({})
    assert [(d.text, d.tier, d.origin) for d in syn.directives] == [
        ("最初のテロップを0秒台に出す", "必須条件", "code"),
        ("「スパイスカレー」を3秒以内にテロップで出す", "多数派", "code"),
        ("材料と分量をテロップかキャプションに載せる", "多数派", "code"),
    ]
    assert syn.directives[2].refs[0].quote == "ナス大3本"


# ── R15 per_video の数字（enforce）───────────────────────────────────────────


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_per_video_numbers_come_from_that_video_only(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """#4 の文に #3 の数字（2.05万）を混ぜたら enforce で落ちる（shadow はログだけ）。"""
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", mode)
    payload = {
        "per_video": [
            {
                "rank": 4,
                "why_fact": "再生86万で保存率1.07%。",
                "win_line": "再生2.05万から伸びた",
            }
        ]
    }
    dropped: list[tuple[str, str]] = []
    syn, _ = synthesize(
        _gemini(payload),
        prod_videos(),
        QUERY,
        request_id="r-r15",
        board=prod_board(),
        on_drop=lambda f, r: dropped.append((f, r)),
    )
    assert syn is not None
    p = syn.per_video[0]
    assert p.why_fact == "再生86万で保存率1.07%。"
    assert ("per_video", "number:20500") in dropped
    assert p.win_line == ("" if mode == "enforce" else "再生2.05万から伸びた")


# ── 版・キャッシュ・再描画 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"), [("", "v3"), ("v2", "v2"), (" V2 ", "v2"), ("x", "v3")]
)
def test_synthesis_version_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setenv(SYNTHESIS_VERSION_ENV, raw)
    assert synthesis_version_from_env() == expected
    from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

    assert VideoAlgorithmSkill(gemini=MagicMock())._synthesis_version == expected


def test_cache_key_separates_synthesis_versions() -> None:
    """壊し方: キャッシュのキーに版を入れない → v2 と v3 のキーが同じで赤。"""
    args: dict[str, Any] = {
        "query": QUERY,
        "max_videos": 5,
        "prompt_version": "v2",
        "model_id": "gemini-3.5-flash",
        "board_size": 30,
        "outputs": ["report"],
        "kw_set": None,
    }
    old = VideoAlgorithmResultCache.cache_key(**args)
    v3 = VideoAlgorithmResultCache.cache_key(**args, synthesis_version="v3")
    v2 = VideoAlgorithmResultCache.cache_key(**args, synthesis_version="v2")
    assert len({old, v3, v2}) == 3


def test_legacy_version_skips_the_v3_checks() -> None:
    """env で v2 に戻したときは旧版（旧 build_prompt・数字の照合だけ）。

    v3 の欄は v3 の検査を通さないと出せないので、旧版の経路で LLM が書いても捨てる。
    """
    gem = _gemini({**prod_synthesis().model_dump(mode="json"), **_V3, "version": "v3"})
    syn, _ = synthesize(gem, prod_videos(), QUERY, request_id="r-v2", prompt_version="v2")
    assert syn is not None and syn.version == ""
    assert syn.directives == [] and syn.summary_lines is None and syn.storyboards == []
    assert "# 上位 5 本の構造分析" in gem.generate_text.call_args.args[0]
    assert "system prompt v2" in gem.generate_text.call_args.kwargs["system"]


def _cached_output() -> VideoAlgorithmOutput:
    cross = cross_analyze(prod_videos(), QUERY, board=prod_board())
    cross.synthesis = prod_synthesis()  # 旧版のまま残ったキャッシュ
    return VideoAlgorithmOutput(
        query=QUERY,
        videos=prod_videos(),
        board=prod_board(),
        cross=cross,
        report_url="https://example.invalid/old",
        total_cost_usd=0.5,
        generated_at="2026-09-28T10:00:00+09:00",
    )


def test_rebuild_without_llm_rechecks_the_cached_synthesis() -> None:
    out = rebuild_cross(
        _cached_output(), client_name=CLIENT, competitors=COMPETITORS, avoid_terms=AVOID
    )
    syn = out.cross.synthesis
    assert syn is not None and syn.version == "v3"
    for word in _PROD_ERRORS:
        assert word not in _screen(syn), word
    assert (out.client_name, out.competitors, out.avoid_terms) == (CLIENT, COMPETITORS, AVOID)
    assert out.report_url is None and out.total_cost_usd == 0.5
    assert out.generated_at == "2026-09-28T10:00:00+09:00"
    stats = out.cross.stats
    assert isinstance(stats, StatsAnalysis)
    # 区分は名簿で決め直す（ブランドの特徴が出る）
    assert any(
        "brand_category_prominent" in line
        for line in build_prompt(
            out.videos, QUERY, stats, Roster.of(CLIENT, COMPETITORS)
        ).splitlines()
    )


def test_rebuild_with_a_synthesizer_calls_the_llm_with_the_new_roster() -> None:
    gem = _gemini(_V3)
    out = rebuild_cross(
        _cached_output(),
        client_name=CLIENT,
        competitors=COMPETITORS,
        synthesize=functools.partial(synthesize, gem, request_id="r-rebuild"),
    )
    prompt = gem.generate_text.call_args.args[0]
    assert "- クライアント: SPICIA" in prompt
    assert '"名前": "SPICIA", "区分": "クライアント"' in prompt
    assert out.cross.synthesis is not None and out.cross.synthesis.version == "v3"
    assert out.total_cost_usd == pytest.approx(0.502)


def test_v2_output_shape_is_unchanged_without_v3_fields() -> None:
    """v3 の欄は空なら出力（MCP の返却 JSON・結果キャッシュ）に出さない＝v2 の形のまま。"""
    dumped = prod_synthesis().model_dump(mode="json")
    for key in ("version", "summary_lines", "directives", "per_video", "posting"):
        assert key not in dumped


def test_skill_passes_board_and_avoid_terms_to_the_v3_prompt(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")
    from teamagent.skills.base import SkillContext
    from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput
    from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

    metas = prod_board()[:5]
    gem = MagicMock()
    gem.analyze_video_bytes.side_effect = [
        GeminiResponse(
            text=f"```json\n{v.analysis.model_dump_json() if v.analysis else '{}'}\n```",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.001,
            model_id="gemini-3.5-flash",
            latency_ms=1,
        )
        for v in prod_videos()
    ]
    gem.generate_text.return_value = _resp({})
    skill = VideoAlgorithmSkill(
        gemini=gem,
        searcher=lambda q, n, r: metas,
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
        max_workers=1,
    )
    out = skill.run(
        VideoAlgorithmInput(query=QUERY, max_videos=5, outputs=["report"], avoid_terms=AVOID),
        ctx=SkillContext(),
    )
    prompt = gem.generate_text.call_args.args[0]
    assert "# 上位5本の一覧" in prompt and "- 避けたい訴求: ルー卒業" in prompt
    assert "system prompt v3" in gem.generate_text.call_args.kwargs["system"]
    assert out.cross.synthesis is not None and out.cross.synthesis.version == "v3"


def test_check_log_reports_field_and_reason_only() -> None:
    seen: list[tuple[str, str]] = []
    finalize(
        CrossSynthesis.model_validate(_V3),
        _ctx(avoid=AVOID),
        log=CheckLog(sink=lambda f, r: seen.append((f, r))),
    )
    assert ("directives", "avoid_term") in seen
    assert all("ルー" not in r and "スプーン" not in r for _f, r in seen)


def test_hedges_are_not_mangled_by_the_rephrasing() -> None:
    syn = _final(
        {
            "summary_lines": {
                "type_line": "必ずしも決め手ではないが分量の明示が上位に多い",
                "feature_ids": ["qty_anywhere"],
                "best_reason": "必ずしも伸びるとは限らない。確実性は低い。",
            }
        }
    )
    assert syn.summary_lines is not None
    assert syn.summary_lines.best_reason == "必ずしも伸びるとは限らない。確実性は低い。"
    assert not syn.summary_lines.type_line_by_code  # 「必ずしも」は断定語ではない


def test_timing_facts_are_not_number_conflicts() -> None:
    """「テロップが0秒台」と「テロップを3秒以内」は別の事実（秒は尺だけ比べる）。"""
    assert find_conflicts([("a", "最初のテロップが0秒台"), ("b", "テロップを3秒以内に出す")]) == []
    assert [c.group for c in find_conflicts([("a", "尺は60秒前後"), ("b", "尺45秒で作る")])] == [
        "秒"
    ]


def test_conflict_with_a_replaced_headline_does_not_ask_again() -> None:
    """見出しがコードの代わりの文になるなら、LLM の見出しとの食い違いで作り直させない。

    壊し方: 判定（conflict_probe）で見出しの検査を外す → 「4種」と「5種」で作り直して赤。
    """
    payload = {
        "summary_lines": {
            "type_line": "スパイス4種が上位に多い",  # 4 種は #1・#2 だけ＝多数派でない
            "feature_ids": ["first_telop_0s"],
        },
        "hypotheses": [
            {"text": "スパイスを5種にする", "match_terms": ["スパイス"], "test": "投稿時間で比べる"}
        ],
    }
    gem = _gemini(payload)
    syn, _ = synthesize(gem, prod_videos(), QUERY, request_id="r-hl", board=prod_board())
    assert gem.generate_text.call_count == 1
    assert syn is not None and syn.hypotheses[0].text.startswith("スパイスを5種にする")


def test_fewer_than_two_watched_videos_skip_the_llm() -> None:
    videos = prod_videos()
    for v in videos[1:]:
        v.error = "動画取得失敗・サムネのみ軽量分析"
    gem = _gemini({})
    assert synthesize(gem, videos, QUERY, request_id="r-one") == (None, 0.0)
    gem.generate_text.assert_not_called()


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("冒頭でカレールーは卒業と宣言する", True),
        ("カレールーはもう卒業！と出す", True),
        ("ルー卒業をうたう", True),
        ("ルーを使わない。卒業式の話", False),  # 文をまたがない
        ("ルーの香りを残して、後で卒業", False),  # 間が 4 字より長い
    ],
)
def test_avoid_terms_match_across_short_particles(text: str, hit: bool) -> None:
    assert has_avoid(text, AVOID) is hit
