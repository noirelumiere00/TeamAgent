"""横断シンセシスの数字の照合（synthesis.ground_synthesis）のテスト。

偽の Gemini に「入力に無い数字・実在しない順位・ρ 入りの見出し」を返させ、
- shadow（既定）: 文はそのまま残り、ログ grounding_dropped だけが出る
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
    synthesize,
)

QUERY = "新宿 ランチ"
_REASON_RE = re.compile(
    r"^(?:number:[\d.,]+|rank:[\d,]+|deny:[^;]+|rank_dup|no_valid_rank|prevalence_rebuilt)"
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
    assert syn.grounding_mode == "shadow"
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
    assert len(logged) == len(dropped) == syn.grounding_dropped
    assert syn.grounding_dropped >= 12
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


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_enforce_confidence_counts_existing_unique_ranks(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """[1,1,1,1,1] や実在しない順位で「全数支持」に見せかけても「高」にしない（mode に依らない）。"""
    monkeypatch.setenv("GROUNDING_MODE_VIDEO_ALGORITHM", mode)
    payload = {
        "win_hypotheses": [
            {"hypothesis": "型A", "supported_by": [1, 1, 1, 1, 1], "confidence": "高"},
            {"hypothesis": "型B", "supported_by": [1, 2, 3, 4, 9], "confidence": "高"},
            {"hypothesis": "型C", "supported_by": [5, 4, 3, 2, 1], "confidence": "高"},
        ]
    }
    syn, _, _ = _run(payload)
    assert syn is not None
    assert [h.confidence for h in syn.win_hypotheses] == ["中", "中", "高"]


def test_enforce_confidence_without_valid_ranks_still_dedupes() -> None:
    s = CrossSynthesis(
        win_hypotheses=[WinHypothesis(hypothesis="h", confidence="高", supported_by=[1] * 5)]
    )
    _enforce_confidence(s, 5)
    assert s.win_hypotheses[0].confidence == "中"


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
