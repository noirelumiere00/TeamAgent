"""横断シンセシスの数字の照合（synthesis.ground_synthesis）のテスト。

偽の Gemini に「入力に無い数字・実在しない順位・ρ 入りの見出し」を返させ、
- shadow（既定）: 出力は照合前と同一（確信度を含む）で、ログ grounding_dropped だけが出る
- enforce: 欄ごとに捨てられ、report の既存の代替（仮説 1 本目・次の一手）に代わる
を確かめる。正しい文を落とさない回帰として、実物レポート（新宿 20260617・n=2）の
シンセシス文と、統計ブロックの値を丸めた文で落ちる件数 0 を固定する。
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.skills._shared import grounding
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CrossSynthesis,
    DistItem,
    StatsAnalysis,
    TelopItem,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
    WinHypothesis,
)
from teamagent.skills.video_algorithm.synthesis import (
    COUNTER_EXAMPLE_MARK,
    _enforce_confidence,
    build_grounder,
    build_prompt,
    parse_synthesis,
    synthesize,
)

QUERY = "新宿 ランチ"
_REASON_RE = re.compile(
    r"^(?:number:[\d.,]+|rank:[\d,]+|deny:[^;]+|rank_dup|no_valid_rank|prevalence_rebuilt"
    r"|confidence_capped)"
    r"(?:;(?:number:[\d.,]+|rank:[\d,]+|deny:[^;]+|rank_dup))*$"
)


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROUNDING_MODE_VIDEO_ALGORITHM", raising=False)


def _resp(text: str) -> GeminiResponse:
    return GeminiResponse(
        text=text,
        input_tokens=3000,
        output_tokens=800,
        cost_usd=0.002,
        model_id="gemini-3.5-flash",
        latency_ms=9000,
    )


def _gemini(payload: dict[str, Any]) -> MagicMock:
    gem = MagicMock()
    body = json.dumps(payload, ensure_ascii=False)
    gem.generate_text.return_value = _resp(f"所見です。\n```json\n{body}\n```")
    return gem


def _video(rank: int, *, dur: float, saves: int, plays: int = 100_000) -> AnalyzedVideo:
    return AnalyzedVideo(
        meta=VideoMeta(
            rank=rank,
            url=f"https://t/{rank}",
            desc="新宿 ランチ 名店",
            play_count=plays,
            collect_count=saves,
            engagement_rate=8.0,
        ),
        analysis=VideoVSEOAnalysis(
            duration_sec=dur,
            hook_type="question",
            hook_summary="冒頭で価格を見せる",
            main_message="新宿で安くて多い",
            telop_density="heavy",
            telops=[TelopItem(sec=1, text="新宿 ランチ", kw_match=True)],
            cta_type=["save"],
        ),
    )


def _videos() -> list[AnalyzedVideo]:
    return [
        _video(1, dur=18, saves=2000),
        _video(2, dur=16, saves=1500),
        _video(3, dur=20, saves=1000),
        _video(4, dur=22, saves=1200),
        _video(5, dur=24, saves=900),
    ]


# 入力に無い数字: 0.5・2.5（v1 例文）・73・47・88・12・33、「7倍」。実在しない順位: 7・8・9。
_FABRICATED: dict[str, Any] = {
    "headline": "『新宿 ランチ』面は冒頭0.5秒の価格テロップで勝つ（ρで裏付け）",
    "strategy": "上位は価格テロップを冒頭に置く。保存率が73%上がる型。",
    "creative_brief": [
        "冒頭で価格を大テロップ（rank1で観測）。テスト仮説として初期値に置く",
        "断面アップを2.5秒",
        "#7の行列カットを入れる",
    ],
    "posting_design": "キャプション1行目にKWを入れる。",
    "client_pitch": "御社も同じ面を狙える可能性があります。再生が7倍になる見込みです。",
    "common_concepts": [
        {
            "concept": "価格の即提示",
            "gist": "冒頭で価格",
            "videos": [1, 2, 2, 9],
            "prevalence": "5/5",
        },
        {"concept": "量の実演", "gist": "保存率47%の型", "videos": [3], "prevalence": "1/5"},
    ],
    "angle_clusters": [
        {"angle": "price_volume", "label_jp": "安さ実感", "videos": [8, 9], "why_works": "安い"}
    ],
    "shared_funnel": {"pattern": "保存を促す。", "cta_consensus": ["save"], "save_logic": "再訪用"},
    "differentiators": [{"rank": 7, "edge": "唯一の俯瞰"}, {"rank": 2, "edge": "唯一の断面"}],
    "win_hypotheses": [
        {
            "hypothesis": "冒頭の価格テロップが上位の型",
            "supported_by": [1, 1, 1, 1, 1],
            "confidence": "高",
            "counter_example": "#3は保存率88%で例外",
            "so_what": "次は12本で検証する",
        },
        {"hypothesis": "行列の型", "supported_by": [8, 9], "confidence": "中"},
        {"hypothesis": "尺は33秒が最適", "supported_by": [1, 2], "confidence": "中"},
    ],
    "caveat": "n=5・観測仮説",
}


def _run(
    payload: dict[str, Any], **kw: Any
) -> tuple[CrossSynthesis | None, list[tuple[str, str]], MagicMock]:
    vids = _videos()
    cross = cross_analyze(vids, QUERY)
    gem = _gemini(payload)
    dropped: list[tuple[str, str]] = []
    syn, _ = synthesize(
        gem,
        vids,
        QUERY,
        request_id="r-test",
        stats=cross.stats,
        on_drop=lambda f, r: dropped.append((f, r)),
        **kw,
    )
    return syn, dropped, gem


def test_fixture_numbers_are_really_absent_from_the_input() -> None:
    """前提: 作り話として使う数字が、実際に渡す入力に無いこと（あるとテストが空振りする）。"""
    vids = _videos()
    cross = cross_analyze(vids, QUERY)
    g = build_grounder(build_prompt(vids, QUERY, cross.stats), [1, 2, 3, 4, 5])
    for value in ("0.5", "2.5", "73", "47", "88", "12", "33", "7"):
        assert value not in g.allowed, value


def test_shadow_keeps_everything_and_logs_only(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class _Log:
        def info(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

    monkeypatch.setattr(grounding, "logger", _Log())
    syn, dropped, _ = _run(_FABRICATED)
    assert syn is not None
    # shadow では記録欄を書かない（件数はログだけ）
    assert syn.grounding_mode == "" and syn.grounding_dropped == 0
    # 文は 1 つも捨てていない
    assert syn.headline == _FABRICATED["headline"]
    assert syn.strategy == _FABRICATED["strategy"]
    assert syn.creative_brief == _FABRICATED["creative_brief"]
    assert syn.client_pitch == _FABRICATED["client_pitch"]
    assert len(syn.common_concepts) == 2 and syn.common_concepts[0].prevalence == "5/5"
    assert [d.rank for d in syn.differentiators] == [7, 2]
    assert len(syn.win_hypotheses) == 3
    assert syn.win_hypotheses[0].supported_by == [1, 1, 1, 1, 1]
    assert syn.win_hypotheses[0].counter_example == "#3は保存率88%で例外"
    # ログは出る（欄名と理由だけ）
    logged = [kw for ev, kw in events if ev == "grounding_dropped"]
    assert len(logged) == len(dropped)
    assert len(dropped) >= 12
    fields = {kw["field"] for kw in logged}
    assert {"headline", "strategy", "creative_brief", "client_pitch"} <= fields
    assert {"win_hypotheses", "differentiators", "angle_clusters"} <= fields
    for kw in logged:
        assert kw["mode"] == "shadow" and kw["skill"] == "video_algorithm"
        assert _REASON_RE.match(kw["reason"]), kw["reason"]  # 本文を含めない
    assert ("headline", "number:0.5;deny:ρ") in dropped


def test_enforce_drops_per_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", "enforce")
    syn, dropped, _ = _run(_FABRICATED)
    assert syn is not None
    assert syn.grounding_mode == "enforce" and syn.grounding_dropped == len(dropped)
    assert syn.headline == ""  # 見出しは空にして report の代替に任せる
    assert syn.strategy == "上位は価格テロップを冒頭に置く。"  # 文単位
    assert syn.creative_brief == [_FABRICATED["creative_brief"][0]]  # 項目単位
    assert syn.posting_design == _FABRICATED["posting_design"]
    assert syn.client_pitch == "御社も同じ面を狙える可能性があります。"  # 「7倍」の文だけ落ちる
    assert [(c.videos, c.prevalence) for c in syn.common_concepts] == [([1, 2], "2/5")]
    assert syn.angle_clusters == []
    assert [d.rank for d in syn.differentiators] == [2]
    assert len(syn.win_hypotheses) == 1
    h = syn.win_hypotheses[0]
    assert h.supported_by == [1]
    assert h.so_what == ""
    assert h.counter_example == COUNTER_EXAMPLE_MARK  # 反例の印は残す
    # 実在順位は 1 本・反例あり → 「高」は中へ（len(supported_by)=5 では数えない）
    assert h.confidence == "中"


def test_enforce_report_falls_back_and_hides_fabrications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", "enforce")
    syn, _, _ = _run(_FABRICATED)
    vids = _videos()
    cross = cross_analyze(vids, QUERY)
    cross.synthesis = syn
    html = render_report(VideoAlgorithmOutput(query=QUERY, videos=vids, cross=cross))
    assert '<div class="vbig">冒頭の価格テロップが上位の型</div>' in html  # 仮説 1 本目に代わる
    for fabricated in (
        "0.5秒",
        "2.5秒",
        "保存率が73%",
        "7倍になる",
        "保存率88%",
        "12本で検証",
        "33秒",
        "保存率47%",
    ):
        assert fabricated not in html, fabricated
    assert '<span class="rk">#7</span>' not in html and '<span class="rk">#9</span>' not in html


def test_shadow_report_still_shows_the_llm_text() -> None:
    syn, _, _ = _run(_FABRICATED)
    vids = _videos()
    cross = cross_analyze(vids, QUERY)
    cross.synthesis = syn
    html = render_report(VideoAlgorithmOutput(query=QUERY, videos=vids, cross=cross))
    assert "冒頭0.5秒の価格テロップで勝つ" in html


def test_grounding_input_excludes_the_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 の system 例文（冒頭0.5秒）どおりに書いても、system は照合の入力に入らないので落ちる。"""
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", "enforce")
    payload = dict(_FABRICATED, creative_brief=["冒頭0.5秒で価格を大テロップ"])
    syn, _, gem = _run(payload, prompt_version="v1")
    assert syn is not None and syn.creative_brief == []
    assert "冒頭0.5秒" in gem.generate_text.call_args.kwargs["system"]


def test_default_prompt_is_v2() -> None:
    _, _, gem = _run(_FABRICATED)
    system = gem.generate_text.call_args.kwargs["system"]
    assert "system prompt v2" in system
    assert "秒や % は横断統計の値だけを書く。丸めてよい" in system
    assert "0.5秒" not in system and "2.5秒" not in system


def test_skill_default_prompt_version_is_v2() -> None:
    from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

    assert VideoAlgorithmSkill(gemini=MagicMock())._prompt_version == "v2"


_CONFIDENCE_PAYLOAD: dict[str, Any] = {
    "win_hypotheses": [
        {"hypothesis": "型A", "supported_by": [1, 1, 1, 1, 1], "confidence": "高"},
        {"hypothesis": "型B", "supported_by": [1, 2, 3, 4, 9], "confidence": "高"},
        {"hypothesis": "型C", "supported_by": [5, 4, 3, 2, 1], "confidence": "高"},
    ]
}


def test_enforce_confidence_counts_existing_unique_ranks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enforce: [1,1,1,1,1] や実在しない順位で「全数支持」に見せかけても「高」にしない。"""
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", "enforce")
    syn, _, _ = _run(_CONFIDENCE_PAYLOAD)
    assert syn is not None
    assert [h.confidence for h in syn.win_hypotheses] == ["中", "中", "高"]


def test_shadow_confidence_keeps_the_legacy_count_and_logs_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shadow: 確信度は従来の len(supported_by) で決め（表示を変えない）、差はログにだけ出す。"""
    events: list[tuple[str, dict[str, Any]]] = []

    class _Log:
        def info(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

    monkeypatch.setattr(grounding, "logger", _Log())
    syn, dropped, _ = _run(_CONFIDENCE_PAYLOAD)
    assert syn is not None
    assert [h.confidence for h in syn.win_hypotheses] == ["高", "高", "高"]
    assert [h.supported_by for h in syn.win_hypotheses] == [
        [1, 1, 1, 1, 1],
        [1, 2, 3, 4, 9],
        [5, 4, 3, 2, 1],
    ]
    capped = [d for d in dropped if d[0] == "win_hypotheses.confidence"]
    assert capped == [("win_hypotheses.confidence", "confidence_capped")] * 2  # 型A と型B
    logged = [
        kw
        for ev, kw in events
        if ev == "grounding_dropped" and kw["field"] == "win_hypotheses.confidence"
    ]
    assert [(kw["mode"], kw["reason"]) for kw in logged] == [("shadow", "confidence_capped")] * 2


def test_enforce_confidence_without_valid_ranks_is_the_legacy_count() -> None:
    """valid_ranks を渡さないときは従来どおり len(supported_by) で数える（shadow の表示用）。"""
    s = CrossSynthesis(
        win_hypotheses=[WinHypothesis(hypothesis="h", confidence="高", supported_by=[1] * 5)]
    )
    _enforce_confidence(s, 5)
    assert s.win_hypotheses[0].confidence == "高"
    _enforce_confidence(s, 5, valid_ranks=[1, 2, 3, 4, 5])
    assert s.win_hypotheses[0].confidence == "中"


def _pre_grounding_confidence(syn: CrossSynthesis, n: int) -> None:
    """照合を入れる前（3b8eff21）の _enforce_confidence の写し。shadow の比較の基準。"""
    order = {"高": 2, "中": 1, "低": 0}
    for h in syn.win_hypotheses:
        full = len(h.supported_by) >= n
        lvl = order.get(h.confidence, 1)
        if h.counter_example and lvl > 0:
            lvl -= 1
        if n < 3:
            lvl = 0
        else:
            if not full and lvl > 1:
                lvl = 1
            if lvl == 2 and not (full and n >= 5):
                lvl = 1
        h.confidence = "高" if lvl == 2 else "中" if lvl == 1 else "低"


# 照合前との同一性を見る入力: 作り話の数字・順位に加え、「実在順位の重複なし」で数えると
# 「高」から下がる仮説（[1,2,3,4,4]）と、LLM が勝手に書いた記録欄を入れる。
_SHADOW_PAYLOAD: dict[str, Any] = dict(
    _FABRICATED,
    win_hypotheses=[
        *_FABRICATED["win_hypotheses"],
        {"hypothesis": "型D", "supported_by": [1, 2, 3, 4, 4], "confidence": "高"},
    ],
    grounding_mode="enforce",
    grounding_dropped=99,
)


def test_shadow_output_is_identical_to_pre_grounding() -> None:
    """shadow の出力オブジェクト全体（確信度を含む）が、照合を入れる前と同じ。"""
    syn, dropped, gem = _run(_SHADOW_PAYLOAD)
    assert syn is not None and dropped  # 照合は走っている（ログは出ている）
    expected = parse_synthesis(gem.generate_text.return_value.text)
    assert expected is not None
    _pre_grounding_confidence(expected, 5)
    assert syn.model_dump() == expected.model_dump()
    assert syn.model_dump(mode="json") == expected.model_dump(mode="json")
    assert [h.confidence for h in syn.win_hypotheses][-1] == "高"
    # 記録欄は LLM の値を使わず、shadow では書かない
    assert (syn.grounding_mode, syn.grounding_dropped) == ("", 0)


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_grounding_fields_never_reach_the_returned_json(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """記録欄は MCP の返却 JSON（model_dump）に載せない。Aico が件数を言い換えないように。"""
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", mode)
    syn, dropped, _ = _run(_SHADOW_PAYLOAD)
    assert syn is not None and dropped
    for dumped in (
        syn.model_dump(),
        syn.model_dump(mode="json"),
        json.loads(syn.model_dump_json()),
    ):
        assert "grounding_mode" not in dumped and "grounding_dropped" not in dumped
    if mode == "enforce":  # enforce はプロセス内（テスト・計測）でだけ件数が読める
        assert syn.grounding_mode == "enforce" and syn.grounding_dropped == len(dropped)


# ρ・相関係数を書いた文（数字は入れない＝deny だけで落ちることを確かめる）。
_RHO_PAYLOAD: dict[str, Any] = {
    "headline": "『新宿 ランチ』面は価格テロップで勝つ",
    "strategy": "上位は価格テロップを冒頭に置く。ρで見ても尺が短いほど上位。",
    "creative_brief": ["冒頭で価格を大テロップ", "相関係数が高い尺で撮る"],
    "posting_design": "キャプション1行目にKWを入れる。ρが負なので短尺で出す。",
    "client_pitch": "御社も同じ面を狙える可能性があります。相関係数でも裏付けがあります。",
    "shared_funnel": {
        "pattern": "保存を促す。ρの向きどおり短尺で締める。",
        "cta_consensus": ["save"],
        "save_logic": "再訪用。",
    },
}


def test_enforce_drops_only_the_rho_sentences(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", "enforce")
    syn, dropped, _ = _run(_RHO_PAYLOAD)
    assert syn is not None
    assert syn.headline == _RHO_PAYLOAD["headline"]
    assert syn.strategy == "上位は価格テロップを冒頭に置く。"
    assert syn.creative_brief == ["冒頭で価格を大テロップ"]
    assert syn.posting_design == "キャプション1行目にKWを入れる。"
    assert syn.client_pitch == "御社も同じ面を狙える可能性があります。"
    assert syn.shared_funnel is not None
    assert syn.shared_funnel.pattern == "保存を促す。"
    assert syn.shared_funnel.save_logic == "再訪用。"
    assert sorted(dropped) == sorted(
        [
            ("strategy", "deny:ρ"),
            ("creative_brief", "deny:相関係数"),
            ("posting_design", "deny:ρ"),
            ("client_pitch", "deny:相関係数"),
            ("shared_funnel", "deny:ρ"),
        ]
    )


def test_shadow_keeps_the_rho_sentences_and_logs_them(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class _Log:
        def info(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

    monkeypatch.setattr(grounding, "logger", _Log())
    syn, dropped, _ = _run(_RHO_PAYLOAD)
    assert syn is not None
    assert syn.strategy == _RHO_PAYLOAD["strategy"]
    assert syn.creative_brief == _RHO_PAYLOAD["creative_brief"]
    assert syn.posting_design == _RHO_PAYLOAD["posting_design"]
    assert syn.client_pitch == _RHO_PAYLOAD["client_pitch"]
    assert syn.shared_funnel is not None
    assert syn.shared_funnel.pattern == _RHO_PAYLOAD["shared_funnel"]["pattern"]
    logged = {(kw["field"], kw["reason"]) for ev, kw in events if ev == "grounding_dropped"}
    assert (
        logged
        == set(dropped)
        == {
            ("strategy", "deny:ρ"),
            ("creative_brief", "deny:相関係数"),
            ("posting_design", "deny:ρ"),
            ("client_pitch", "deny:相関係数"),
            ("shared_funnel", "deny:ρ"),
        }
    )


# ── 正しい文を落とさない回帰 ─────────────────────────────────────────────────


def _shinjuku_inputs() -> tuple[list[AnalyzedVideo], StatsAnalysis]:
    """実物（VSEO動画アルゴリズム分析_新宿_20260617.html・n=2・#2 と #3）の入力を再構成。"""
    vids = [
        _video(2, dur=20.3, saves=36, plays=10_000),
        _video(3, dur=5.2, saves=21, plays=10_000),
    ]
    stats = StatsAnalysis(
        sample_size=2,
        distributions=[
            DistItem(feature="保存率", median=0.29, min=0.21, max=0.36),
            DistItem(
                feature="テロップ枚数",
                median=5.5,
                min=0.0,
                max=11.0,
                outlier_rank=3,
                outlier_value=0.0,
                outlier_note="中央値の0.0倍",
            ),
            DistItem(
                feature="尺(秒)",
                median=12.75,
                min=5.2,
                max=20.3,
                outlier_rank=3,
                outlier_value=5.2,
                outlier_note="中央値の0.4倍",
            ),
        ],
    )
    return vids, stats


# 実物レポートに出ていたシンセシスの文（表示されていたものをそのまま写した）。
_SHINJUKU: dict[str, Any] = {
    "headline": "「新宿」面は「場所のギャップが生む非日常体験」でユーザーの興味を惹きつけろ",
    "strategy": (
        "上位動画は「新宿」というキーワードが持つ都市のイメージを逆手に取り、"
        "「非日常的な体験」をフックにしています 〔上位 2/2〕。クライアントの商材が提供する"
        "“非日常”の性質（癒し、衝撃など）に合わせて表現手法を選び、冒頭でユーザーを"
        "惹きつけるクリエイティブが有効と考えられます。"
    ),
    "creative_brief": [
        "冒頭3秒以内に「新宿にあるとは思えない〇〇」といった、場所と内容のギャップを明示する"
        "強いフックを配置する 〔#2 フック〕。",
        "動画の主題となる場所の名前や核心的なメッセージは、テロップで明確に提示する"
        " 〔#2 テロップ要旨〕。",
        "キャプションの1行目とハッシュタグには「#新宿」を必ず含める 〔上位 2/2〕。",
        "動画尺は5秒から20秒の範囲で、コンテンツ密度に応じて調整する 〔尺(秒) 範囲5.2–20.3〕。",
        "「保存して次のデートプランに！」「新宿の秘密スポットをチェック」など、具体的な行動に"
        "つながるCTAを動画内に設置する 〔#2 CTA save〕。",
    ],
    "posting_design": (
        "キャプションの1行目にKW「新宿」を含め、ハッシュタグにも「#新宿」を最優先で設定して"
        "ください 〔上位 2/2〕。CTAは次回の訪問や情報収集につながる「保存」誘導を強く推奨します"
        " 〔#2 CTA save〕。"
    ),
    "client_pitch": (
        "御社の新宿での商品/サービスも、「新宿の知られざる一面」や「都会の喧騒を忘れさせる"
        "体験」といった“非日常的な新宿”という切り口で訴求することで、同検索面での注目を"
        "集める可能性があります。"
    ),
    "common_concepts": [
        {
            "concept": "新宿の非日常体験",
            "gist": "意外な一面",
            "videos": [2, 3],
            "prevalence": "2/2",
        },
        {
            "concept": "具体的なロケーション描写",
            "gist": "場所",
            "videos": [2, 3],
            "prevalence": "2/2",
        },
    ],
    "angle_clusters": [
        {
            "angle": "novelty",
            "label_jp": "非日常性・意外性",
            "videos": [2, 3],
            "why_works": "広範な「新宿」キーワードの中で、ユーザーが期待するイメージを裏切る"
            "意外な情報や光景で興味を惹きつけるため。",
        }
    ],
    "shared_funnel": {
        "pattern": "新宿という場所のイメージに対するギャップを冒頭で提示し、その詳細を見せる"
        "ことで興味喚起を狙う。",
        "cta_consensus": ["save"],
        "save_logic": "「新宿の知らなかった一面」や「次に行ってみたい場所」といった発見が、"
        "ユーザーの再訪や計画に役立つと判断されるため 〔#2 save〕。",
    },
    "differentiators": [
        {"rank": 2, "edge": "「ここ知ってたらモテる」のような問いかけで価値を冒頭で示している。"},
        {
            "rank": 3,
            "edge": "テロップを一切使わず、短い尺で衝撃的な光景のみを提示する 〔テロップ枚数 "
            "外れ値#3(中央値の0.0倍), 尺(秒) 外れ値#3(中央値の0.4倍)〕。",
        },
    ],
    "win_hypotheses": [
        {
            "hypothesis": "「新宿の意外な一面」を冒頭で提示し、ユーザーの好奇心を刺激する"
            "非日常的な映像は、再生数を伸ばす可能性が高い。",
            "supported_by": [2, 3],
            "confidence": "低",
            "so_what": "クライアントの商材が提供する「新宿の意外な価値」を明確に言語化し、"
            "冒頭フックで表現できるかを検証するテスト投稿を企画しましょう。",
        },
        {
            "hypothesis": "テロップを最小限に抑え、短尺でインパクトのある映像を見せるスタイル"
            "も、一部のユーザーには効果的である可能性がある。",
            "supported_by": [3],
            "confidence": "低",
        },
    ],
}


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_real_shinjuku_synthesis_drops_nothing(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", mode)
    vids, stats = _shinjuku_inputs()
    dropped: list[tuple[str, str]] = []
    syn, _ = synthesize(
        _gemini(_SHINJUKU),
        vids,
        "新宿",
        request_id="r-shinjuku",
        stats=stats,
        on_drop=lambda f, r: dropped.append((f, r)),
    )
    assert syn is not None
    assert dropped == []
    assert syn.grounding_dropped == 0
    assert syn.headline == _SHINJUKU["headline"]
    assert syn.creative_brief == _SHINJUKU["creative_brief"]
    assert len(syn.win_hypotheses) == 2 and len(syn.differentiators) == 2


@pytest.mark.parametrize(
    "sentence",
    [
        "尺は13秒前後に収める〔尺(秒) 中央値12.75〕。",  # 中央値の四捨五入
        "尺は5〜21秒の幅で試す。",  # 範囲の端の切り捨て・切り上げ
        "保存率0.3%前後が基準〔保存率 中央値0.29, n=2〕。",  # 小数 1 桁の四捨五入
        "テロップは5〜6枚から試す〔テロップ枚数 中央値5.5〕。",
        "#3は尺が中央値の0.4倍と短い。",
        "冒頭0:03までにKWを出す。",  # タイムコード
        "縦型9:16の全画面で撮る。",  # 画面比
    ],
)
def test_rounded_stat_values_are_kept(sentence: str) -> None:
    vids, stats = _shinjuku_inputs()
    g = build_grounder(build_prompt(vids, "新宿", stats), [2, 3])
    assert g.reason(sentence) is None
