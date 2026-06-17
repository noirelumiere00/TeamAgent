"""VideoAlgorithm Skill のテスト（tiktok検索/Gemini/DLをモック、実API無し）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.skills.base import SkillContext
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    BrandDetection,
    TelopItem,
    VideoAlgorithmInput,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill, parse_analysis


def _json_block(*, kw_telop: bool, cta: bool, brand: bool, dur: float) -> str:
    telops = '[{"sec":1.0,"text":"新宿の名店","position":"center","kw_match":%s}]' % (
        "true" if kw_telop else "false"
    )
    brands = (
        '[{"brand_name":"ユニクロ","detection_source":"signboard","appear_sec":[12.0],'
        '"total_screen_time_sec":2.0,"prominence":"prominent","is_intentional":"incidental",'
        '"brand_relation":"neutral_third_party"}]'
        if brand
        else "[]"
    )
    cta_block = '["save"]' if cta else "[]"
    return (
        "### 所見\nこの動画は…\n\n```json\n"
        f'{{"duration_sec":{dur},"hook_type":"question","hook_summary":"問いかけ",'
        f'"telop_density":"heavy","telops":{telops},"main_objects":["寿司"],'
        f'"brand_detections":{brands},"scenes":[{{"start_sec":0,"end_sec":3,"desc":"導入"}}],'
        f'"pacing":"fast","main_message":"新宿の名店","cta_type":{cta_block},"cta_sec":18.0,'
        f'"keyword_matches":[{{"keyword":"新宿","matched":true,"match_type":"exact","layer":"telop"}}],'
        f'"caption_relevance":"キャプションと一致","win_factors":["冒頭フック強"]}}\n```'
    )


def _resp(text: str) -> GeminiResponse:
    return GeminiResponse(
        text=text,
        input_tokens=6000,
        output_tokens=400,
        cost_usd=0.0014,
        model_id="gemini-2.5-flash",
        latency_ms=18000,
    )


# -----------------------------------------------------------
# parse_analysis
# -----------------------------------------------------------
def test_parse_analysis_extracts_json() -> None:
    a = parse_analysis(_json_block(kw_telop=True, cta=True, brand=True, dur=20))
    assert a is not None
    assert a.duration_sec == 20
    assert a.kw_in_telop() is True
    assert a.has_cta() is True
    assert a.brand_detections[0].brand_name == "ユニクロ"
    assert a.brand_detections[0].appear_sec == [12.0]


def test_parse_analysis_no_block_returns_none() -> None:
    assert parse_analysis("所見のみでJSONなし") is None


def test_parse_analysis_bad_json_returns_none() -> None:
    assert parse_analysis("```json\n{壊れた json,,}\n```") is None


# -----------------------------------------------------------
# cross_analyze
# -----------------------------------------------------------
def _av(
    rank: int, *, kw_telop: bool, cta: bool, dur: float, saves: int, plays: int = 100000
) -> AnalyzedVideo:
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
            telop_density="heavy" if kw_telop else "none",
            telops=[TelopItem(sec=1, text="新宿", kw_match=kw_telop)],
            cta_type=["save"] if cta else [],
        ),
    )


def test_cross_analyze_finds_common_winfactors() -> None:
    videos = [
        _av(1, kw_telop=True, cta=True, dur=18, saves=2000),
        _av(2, kw_telop=True, cta=True, dur=16, saves=1500),
        _av(3, kw_telop=True, cta=True, dur=20, saves=1000),
    ]
    cross = cross_analyze(videos, "新宿 ランチ")
    assert cross.video_count == 3
    factors = {w.factor for w in cross.win_factors}
    assert any("テロップ" in f for f in factors)  # 3/3でテロップにKW
    assert any("CTA" in f for f in factors)
    # 3/3 は確信度「高」
    telop_wf = next(w for w in cross.win_factors if "テロップ" in w.factor)
    assert telop_wf.observed_in == 3 and telop_wf.confidence == "高"
    assert "新宿 ランチ" in cross.summary


def test_cross_analyze_empty_is_safe() -> None:
    cross = cross_analyze([], "x")
    assert cross.video_count == 0 and "分析できた動画がありません" in cross.summary


# -----------------------------------------------------------
# render_report（HTMLスモーク）
# -----------------------------------------------------------
def test_render_report_contains_timeline_and_brand() -> None:
    a = VideoVSEOAnalysis(
        duration_sec=20,
        hook_type="question",
        hook_summary="問いかけ",
        telops=[TelopItem(sec=2, text="新宿の名店", position="center", kw_match=True)],
        brand_detections=[
            BrandDetection(brand_name="ユニクロ", appear_sec=[12.0], prominence="hero")
        ],
        caption_relevance="一致",
    )
    out = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[
            AnalyzedVideo(meta=VideoMeta(rank=1, url="https://t/1", play_count=100000), analysis=a)
        ],
    )
    out.cross = cross_analyze(out.videos, "新宿 ランチ")
    html = render_report(out)
    assert "<!doctype html>" in html
    assert "新宿 ランチ" in html
    assert 'class="nle"' in html  # Premiere風NLEタイムライン
    assert "nscrub" in html  # スクラブ(再生ヘッド)操作面
    assert "新宿の名店" in html  # テロップ(クリップ)
    assert "ユニクロ" in html  # ブランド
    assert "相関≠因果" in html  # 正直な注記


def test_render_report_shows_consistency_matrix_and_competitor() -> None:
    """v2: 一貫性マトリクス（テロップ↔キャプ↔映像）と競合ブランド映り込み警告を出す。"""
    from teamagent.skills.video_algorithm.schema import KeywordMatch

    a = VideoVSEOAnalysis(
        duration_sec=20,
        telops=[TelopItem(sec=1, text="新宿", kw_match=True)],
        spoken_keywords=[KeywordMatch(keyword="新宿", matched=True, layer="narration")],
        keyword_matches=[KeywordMatch(keyword="新宿", matched=True, layer="caption")],
        message_coherence=82,
        brand_detections=[
            BrandDetection(
                brand_name="しまむら",
                appear_sec=[8.0],
                prominence="background",
                brand_relation="competitor",
                detection_source="signboard",
            )
        ],
    )
    out = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[
            AnalyzedVideo(meta=VideoMeta(rank=1, url="https://t/1", play_count=1000), analysis=a)
        ],
    )
    out.cross = cross_analyze(out.videos, "新宿 ランチ")
    html = render_report(out)
    assert "一貫性マトリクス" in html
    assert "テロップ↔KW" in html  # 4経路の整合性ヘッダ
    assert "メッセージ一貫性" in html  # coherence band 列
    assert "競合ブランドの映り込み" in html and "しまむら" in html  # 競合警告(ブランドタブ内)


# -----------------------------------------------------------
# skill.run（フルパイプライン・モック）
# -----------------------------------------------------------
def test_skill_run_full_pipeline(tmp_path: object) -> None:
    metas = [
        VideoMeta(
            rank=i, url=f"https://t/{i}", desc="新宿 ランチ", play_count=100000, collect_count=1500
        )
        for i in (1, 2, 3)
    ]
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(
        _json_block(kw_telop=True, cta=True, brand=True, dur=18)
    )
    skill = VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda q, n, r: metas,
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
    )
    out = skill.run(VideoAlgorithmInput(query="新宿 ランチ", max_videos=3), ctx=SkillContext())
    assert len(out.videos) == 3
    assert all(v.analysis is not None for v in out.videos)
    assert out.cross.video_count == 3
    assert out.total_cost_usd == pytest.approx(0.0042, abs=1e-4)  # 3 * 0.0014
    assert out.report_html_path is not None
    assert "VSEO動画アルゴリズム分析" in out.slack_summary
    # HTML が実際に書き出されている
    with open(out.report_html_path, encoding="utf-8") as f:
        html = f.read()
    assert "ユニクロ" in html and 'class="nle"' in html


def test_board_decoupled_from_deep_analysis(tmp_path: object) -> None:
    """取得（board_size 本のメタ）と 深掘り分析（max_videos 本）は独立。

    30本取得・上位3本だけ深掘り → out.board=全12メタ / out.videos=3分析、
    レポートに「取得ボード」全12行＋★（深掘り対象）が出ることを検証。
    """
    metas = [
        VideoMeta(
            rank=i,
            url=f"https://t/{i}",
            author=f"acc{i}",
            follower_count=10000 * i,
            desc="新宿 ランチ",
            play_count=500000,
            collect_count=8000,
            cover_url=f"https://cdn/{i}.jpg",
        )
        for i in range(1, 13)  # 12 本取得
    ]
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(
        _json_block(kw_telop=True, cta=True, brand=True, dur=18)
    )
    skill = VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda q, n, r: metas[:n],
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
    )
    out = skill.run(
        VideoAlgorithmInput(query="新宿 ランチ", max_videos=3, board_size=12),
        ctx=SkillContext(),
    )
    # 取得は12本（ボード）、深掘り分析は3本だけ（分離）
    assert len(out.board) == 12
    assert len(out.videos) == 3
    assert all(v.analysis is not None for v in out.videos)
    # ボードはフォロワー数も保持（投稿者規模）
    assert out.board[0].follower_count == 10000
    # 深掘りされたのは Gemini を3回呼んだ分だけ（30本DLしていない＝コスト分離）
    assert gemini.analyze_video_bytes.call_count == 3
    # レポートに取得ボードが出る（12行＋★深掘りマーカー）
    with open(out.report_html_path, encoding="utf-8") as f:  # type: ignore[arg-type]
        html = f.read()
    assert "検索上位 取得ボード" in html
    assert html.count('class="sbr"') == 12  # 取得12本ぶんの行
    assert "sbdeep" in html  # ★深掘り対象マーカー


# -----------------------------------------------------------
# フレーム抽出 / 統計 / 色味（新機能）
# -----------------------------------------------------------
def test_pick_timecodes_collects_key_moments() -> None:
    from teamagent.skills.video_algorithm.frames import pick_timecodes
    from teamagent.skills.video_algorithm.schema import Scene

    a = VideoVSEOAnalysis(
        duration_sec=20,
        telops=[TelopItem(sec=6.0, text="KW", kw_match=True), TelopItem(sec=6.2, text="x")],
        brand_detections=[
            BrandDetection(brand_name="ユニクロ", appear_sec=[12.0], prominence="hero")
        ],
        scenes=[
            Scene(start_sec=0, end_sec=4, desc="導入"),
            Scene(start_sec=4, end_sec=16, desc="本編"),
        ],
        cta_sec=18.0,
    )
    tcs = pick_timecodes(a, max_frames=6)
    secs = [round(s, 1) for s, _ in tcs]
    assert 0.0 <= secs[0] <= 1.5  # フック
    assert any(abs(s - 12.0) < 0.5 for s in secs)  # ブランド秒
    assert any(abs(s - 18.0) < 0.5 for s in secs)  # CTA
    assert secs == sorted(secs)  # 時系列
    # 近接(6.0,6.2)は間引かれ1つに
    assert sum(1 for s in secs if 5.5 <= s <= 6.5) == 1


def test_extract_frames_graceful_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.skills.video_algorithm import frames

    monkeypatch.setattr(frames.shutil, "which", lambda _n: None)
    assert frames.extract_frames(b"x" * 100, "video/mp4", [1.0, 2.0]) == []


def test_statistical_analyze_via_cross() -> None:
    videos = [
        _av(1, kw_telop=True, cta=True, dur=14, saves=3000),
        _av(2, kw_telop=True, cta=True, dur=16, saves=2000),
        _av(3, kw_telop=False, cta=False, dur=34, saves=400),
    ]
    cross = cross_analyze(videos, "新宿 ランチ")
    assert cross.stats is not None
    s = cross.stats
    assert s.sample_size == 3
    assert len(s.feature_matrix) == 3  # 5本×特徴の行
    assert s.kw_coverage.layer_fill  # 4層充足率
    assert s.hook_counts  # フック分布
    assert len(s.caveats) == 3  # n小の限界を必ず明記
    # 相関は rank ターゲットで生成される
    assert any(c.target == "rank" for c in s.correlations)


def test_render_report_shows_frames_thumb_and_stats() -> None:
    """v2: 実フレーム埋込 + サムネ色比較ボード + 統計付録（動画内色は廃止）。"""
    from teamagent.skills.video_algorithm.schema import FrameShot, ThumbColor

    a = VideoVSEOAnalysis(
        duration_sec=18,
        telops=[TelopItem(sec=1, text="新宿", kw_match=True)],
    )
    out = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[
            AnalyzedVideo(
                meta=VideoMeta(rank=1, url="https://t/1", play_count=100000, collect_count=1500),
                analysis=a,
                frames=[
                    FrameShot(sec=1.0, caption="フック", data_uri="data:image/jpeg;base64,AAAA")
                ],
                cover_data_uri="data:image/jpeg;base64,BBBB",
                thumb=ThumbColor(swatches=["#e8c8a0"], brightness01=0.72, warmth=0.3),
            )
        ],
    )
    out.cross = cross_analyze(out.videos, "新宿 ランチ")
    html = render_report(out)
    assert "data:image/jpeg;base64,AAAA" in html  # フレーム埋込(タイムライン)
    assert "サムネ色の比較" in html and "data:image/jpeg;base64,BBBB" in html  # サムネ色ボード
    assert "#e8c8a0" in html  # サムネ支配色スウォッチ
    assert "統計付録" in html and "KWカバレッジ" in html  # 統計（折りたたみ付録）
    assert "相関分析には n≥3 が必要" in html  # n=1で空の相関表を見せない（誠実さ）
    assert "tldata" in html  # スクラブ用データ


def test_render_report_video_player_vs_frame_fallback() -> None:
    """v3.2: video_data_uri があれば<video>プレーヤー、無ければ静止フレームにフォールバック。"""
    from teamagent.skills.video_algorithm.schema import FrameShot

    a = VideoVSEOAnalysis(duration_sec=18, telops=[TelopItem(sec=1, text="新宿", kw_match=True)])
    fr = [FrameShot(sec=1.0, caption="フック", data_uri="data:image/jpeg;base64,AAAA")]
    # 動画あり → <video> プレーヤー + 再生ボタン + 双方向同期JS
    out_v = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[
            AnalyzedVideo(
                meta=VideoMeta(rank=1, url="https://t/1", play_count=1000),
                analysis=a,
                frames=fr,
                video_data_uri="data:video/mp4;base64,BBBB",
            )
        ],
    )
    out_v.cross = cross_analyze(out_v.videos, "新宿 ランチ")
    h = render_report(out_v)
    assert '<video class="nvid"' in h and "data:video/mp4;base64,BBBB" in h
    assert 'class="nplay"' in h and "timeupdate" in h  # 再生ボタン + 同期JS
    # 動画なし → 静止フレーム(<video>無し)
    out_f = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[AnalyzedVideo(meta=VideoMeta(rank=1, url="https://t/1"), analysis=a, frames=fr)],
    )
    out_f.cross = cross_analyze(out_f.videos, "新宿 ランチ")
    h2 = render_report(out_f)
    assert "<video" not in h2 and 'class="nimg"' in h2


def test_render_report_top_tabs_overview_and_per_video() -> None:
    """v3.3: トップタブで統計(overview)⇄各動画の個別レポートを切り替える構造。"""

    def mk(r: int) -> AnalyzedVideo:
        a = VideoVSEOAnalysis(
            duration_sec=18,
            main_message="安いランチ",
            telops=[TelopItem(sec=1, text="新宿", kw_match=True)],
            win_factors=["KWテロップ"],
        )
        return AnalyzedVideo(
            meta=VideoMeta(rank=r, url=f"https://t/{r}", author=f"u{r}", play_count=100000),
            analysis=a,
        )

    out = VideoAlgorithmOutput(query="新宿 ランチ", videos=[mk(1), mk(2)])
    out.cross = cross_analyze(out.videos, "新宿 ランチ")
    h = render_report(out)
    assert 'class="toptabs"' in h and "📊 統計レポート" in h  # トップタブ
    assert 'data-tt="ov"' in h and 'data-ttp="ov"' in h  # 統計pane
    assert 'data-tt="v0"' in h and 'data-tt="v1"' in h  # 各動画タブ
    assert 'class="ttpane show"' in h  # 既定=統計が表示
    assert h.count('class="vpane"') == 2  # 個別レポート2本
    assert "setupTopTabs" in h  # 切替JS


def test_render_report_planner_strategy_summary() -> None:
    """v3.5: synthesis のプランナー戦略フィールドが結論バンドに出る（PRプランナー視点）。"""
    from teamagent.skills.video_algorithm.schema import CrossSynthesis, WinHypothesis

    a = VideoVSEOAnalysis(
        duration_sec=18,
        main_message="安いランチ",
        telops=[TelopItem(sec=1, text="新宿", kw_match=True)],
    )
    out = VideoAlgorithmOutput(
        query="新宿 ランチ",
        videos=[
            AnalyzedVideo(meta=VideoMeta(rank=1, url="https://t/1", play_count=1000), analysis=a),
            AnalyzedVideo(meta=VideoMeta(rank=2, url="https://t/2", play_count=900), analysis=a),
        ],
    )
    out.cross = cross_analyze(out.videos, "新宿 ランチ")
    out.cross.synthesis = CrossSynthesis(
        headline="価格×ボリュームで勝つ",
        strategy="冒頭で価格提示",
        creative_brief=["冒頭0.5秒で価格テロップ", "断面アップ3秒"],
        client_pitch="値ごろ感の実演で攻めましょう",
        posting_design="保存誘導CTA",
        win_hypotheses=[
            WinHypothesis(hypothesis="価格大テロップ型", supported_by=[1, 2], confidence="中")
        ],
    )
    h = render_report(out)
    assert "プランナーの戦略サマリ" in h and "価格×ボリュームで勝つ" in h  # headline
    assert "クリエイティブ指示" in h and "断面アップ3秒" in h  # creative_brief
    assert "クライアント提案" in h and "値ごろ感の実演" in h  # client_pitch
    assert "投稿設計" in h and "保存誘導CTA" in h  # posting_design
    assert "勝ちパターン仮説" in h and "価格大テロップ型" in h  # headline版でも仮説は表示


def test_synthesis_prompt_injects_computed_stats() -> None:
    """v3.6: 計算済み統計(StatsAnalysis)が synthesis プロンプトに注入され、根拠にできる。"""
    from teamagent.skills.video_algorithm.schema import CorrItem, KwCoverage, StatsAnalysis
    from teamagent.skills.video_algorithm.synthesis import build_prompt

    st = StatsAnalysis(
        sample_size=3,
        kw_coverage=KwCoverage(
            avg_score_0_100=35, layer_fill=[("テロップ", "3/3"), ("音声", "0/3")]
        ),
        correlations=[CorrItem(feature="保存率", rho=None, n_pairs=2)],
        caveats=["n=3 は統計的に極小"],
    )
    vids = [
        AnalyzedVideo(meta=VideoMeta(rank=1), analysis=VideoVSEOAnalysis(main_message="x")),
        AnalyzedVideo(meta=VideoMeta(rank=2), analysis=VideoVSEOAnalysis(main_message="y")),
    ]
    p = build_prompt(vids, "新宿 ランチ", st)
    assert "## 横断統計" in p and "KWカバレッジ層別" in p and "テロップ3/3" in p
    assert "判定不能" in p  # ρ=None は数字でなく判定不能と渡す
    assert "## 横断統計" not in build_prompt(vids, "新宿 ランチ", None)  # stats無しなら出さない


def test_enforce_confidence_ceiling_by_n_and_support() -> None:
    """v3.6: 確信度の天井が n・支持本数・反例に機械連動（敵対レビュー反映）。"""
    from teamagent.skills.video_algorithm.schema import CrossSynthesis, WinHypothesis
    from teamagent.skills.video_algorithm.synthesis import _enforce_confidence

    # n<3 → 低
    s = CrossSynthesis(
        win_hypotheses=[WinHypothesis(hypothesis="h", confidence="高", supported_by=[1, 2])]
    )
    _enforce_confidence(s, 2)
    assert s.win_hypotheses[0].confidence == "低"
    # n=5・全数支持・反例なし → 高 維持
    s2 = CrossSynthesis(
        win_hypotheses=[
            WinHypothesis(hypothesis="h", confidence="高", supported_by=[1, 2, 3, 4, 5])
        ]
    )
    _enforce_confidence(s2, 5)
    assert s2.win_hypotheses[0].confidence == "高"
    # n=5・全数支持・反例あり → 1段下げて中
    s3 = CrossSynthesis(
        win_hypotheses=[
            WinHypothesis(
                hypothesis="h", confidence="高", supported_by=[1, 2, 3, 4, 5], counter_example="#3"
            )
        ]
    )
    _enforce_confidence(s3, 5)
    assert s3.win_hypotheses[0].confidence == "中"
    # n=4・過半数止まり(2/4) → 中止まり
    s4 = CrossSynthesis(
        win_hypotheses=[WinHypothesis(hypothesis="h", confidence="高", supported_by=[1, 2])]
    )
    _enforce_confidence(s4, 4)
    assert s4.win_hypotheses[0].confidence == "中"


def test_palette_quantizes_dominant_colors() -> None:
    """thumbnails._palette: 画素を量子化して頻出色を hex で返す。"""
    from teamagent.skills.video_algorithm.thumbnails import _palette

    px = [(230, 200, 160)] * 40 + [(20, 20, 20)] * 20 + [(250, 250, 250)] * 4
    sw = _palette(px)
    assert sw and all(s.startswith("#") and len(s) == 7 for s in sw)
    assert sw[0].startswith("#e")  # 最頻ビン=暖色寄り


def test_parse_synthesis_extracts_and_clamps() -> None:
    """synthesis: JSONブロック抽出 + 確信度クランプ（全数未満の高→中）。"""
    from teamagent.skills.video_algorithm.synthesis import _enforce_confidence, parse_synthesis

    text = (
        "所見\n```json\n"
        '{"common_concepts":[{"concept":"安さ","videos":[1,2],"prevalence":"2/3"}],'
        '"win_hypotheses":[{"hypothesis":"型","supported_by":[1,2],"confidence":"高"}],'
        '"caveat":"n=3 観測仮説"}\n```'
    )
    syn = parse_synthesis(text)
    assert syn is not None
    assert syn.common_concepts[0].concept == "安さ"
    _enforce_confidence(syn, 3)  # 3本中2本支持の「高」は「中」に下がる
    assert syn.win_hypotheses[0].confidence == "中"


def test_synthesize_skips_under_two_videos() -> None:
    """synthesis: 1本では横断にならずGeminiを呼ばずに (None,0.0)。"""
    from unittest.mock import MagicMock

    from teamagent.skills.video_algorithm.synthesis import synthesize

    gem = MagicMock()
    one = [AnalyzedVideo(meta=VideoMeta(rank=1), analysis=VideoVSEOAnalysis())]
    syn, cost = synthesize(gem, one, "x", request_id="t")
    assert syn is None and cost == 0.0
    gem.generate_text.assert_not_called()


def test_skill_run_no_results_returns_message() -> None:
    skill = VideoAlgorithmSkill(gemini=MagicMock(), searcher=lambda q, n, r: [])
    out = skill.run(VideoAlgorithmInput(query="存在しない"), ctx=SkillContext())
    assert out.videos == []
    assert "取得できませんでした" in out.slack_summary


def test_skill_run_download_failure_is_isolated(tmp_path: object) -> None:
    """1本のDL失敗が全体を止めず、その動画は error 付きで残る。"""
    metas = [VideoMeta(rank=1, url="https://t/1", play_count=1000)]

    def _boom(url: str) -> tuple[bytes, str]:
        raise RuntimeError("VIDEO_DOWNLOAD_FAILED")

    skill = VideoAlgorithmSkill(
        gemini=MagicMock(),
        searcher=lambda q, n, r: metas,
        downloader=_boom,
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
    )
    out = skill.run(VideoAlgorithmInput(query="x", max_videos=1), ctx=SkillContext())
    assert out.videos[0].analysis is None
    assert "取得失敗" in (out.videos[0].error or "")


# -----------------------------------------------------------
# 寛容パース（1フィールドの enum ズレで動画を丸ごと失わない）
# -----------------------------------------------------------
def test_parse_analysis_recovers_bad_enum() -> None:
    """pacing="medium"（許容外）でも棄却せず default に戻して動画を救済する。"""
    bad = _json_block(kw_telop=True, cta=True, brand=True, dur=18).replace(
        '"pacing":"fast"', '"pacing":"medium"'
    )
    a = parse_analysis(bad)
    assert a is not None  # 丸ごと棄却しない
    assert a.pacing == "unknown"  # 不正フィールドだけ schema default へ
    assert a.duration_sec == 18  # 他フィールドは無傷
    assert a.hook_type == "question"


def test_parse_analysis_recovers_nested_list_enum() -> None:
    """ネストした list 要素の enum ズレ（brand_relation）も要素を保ったまま救済。"""
    bad = _json_block(kw_telop=True, cta=True, brand=True, dur=18).replace(
        '"brand_relation":"neutral_third_party"', '"brand_relation":"frenemy"'
    )
    a = parse_analysis(bad)
    assert a is not None
    assert a.brand_detections[0].brand_name == "ユニクロ"  # 要素は生存
    assert a.brand_detections[0].brand_relation == "unknown"  # 不正値のみ default


def test_parse_analysis_recovery_is_logged() -> None:
    """救済は『サイレント補正』にしない：診断ログと救済ログが必ず出る。"""
    from structlog.testing import capture_logs

    bad = _json_block(kw_telop=True, cta=True, brand=True, dur=18).replace(
        '"pacing":"fast"', '"pacing":"medium"'
    )
    with capture_logs() as logs:
        a = parse_analysis(bad)
    assert a is not None
    events = {log["event"] for log in logs}
    assert "video_algorithm_parse_validation_failed" in events  # 初回診断
    assert "video_algorithm_parse_recovered" in events  # 救済を明示
    rec = next(log for log in logs if log["event"] == "video_algorithm_parse_recovered")
    assert any("pacing" in f for f in rec["reset_fields"])


def test_parse_analysis_recovers_out_of_range_coherence() -> None:
    """message_coherence の範囲外(150>100)は棄却せず default(None) に戻して救済。"""
    bad = _json_block(kw_telop=True, cta=True, brand=True, dur=18).replace(
        '"caption_relevance":"キャプションと一致"',
        '"caption_relevance":"キャプションと一致","message_coherence":150',
    )
    a = parse_analysis(bad)
    assert a is not None
    assert a.message_coherence is None  # 範囲外→default に戻る（bogus値を表示しない）
    assert a.duration_sec == 18  # 他フィールドは無傷


# -----------------------------------------------------------
# over-fetch バックフィル（DL失敗を後続候補で埋めて狙った本数を揃える）
# -----------------------------------------------------------
def test_overfetch_backfills_to_target(tmp_path: object) -> None:
    """上位のDL失敗を後続候補で埋め、狙った本数を揃える（下位繰上げを正直に明示）。"""
    pool = [
        VideoMeta(
            rank=i, url=f"https://t/{i}", desc="新宿 ランチ", play_count=100000, collect_count=1500
        )
        for i in range(1, 7)  # rank 1..6
    ]

    def _dl(url: str) -> tuple[bytes, str]:
        if url == "https://t/1":
            raise RuntimeError("VIDEO_DOWNLOAD_FAILED")  # rank1 を失敗させる
        return (b"vid", "video/mp4")

    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(
        _json_block(kw_telop=True, cta=True, brand=True, dur=18)
    )
    skill = VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda q, n, r: pool[:n],
        downloader=_dl,
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
    )
    out = skill.run(VideoAlgorithmInput(query="新宿 ランチ", max_videos=3), ctx=SkillContext())
    assert sum(1 for v in out.videos if v.analysis) == 3  # 狙った3本が揃う
    assert all(v.meta.rank != 1 for v in out.videos)  # 失敗した rank1 は除外
    assert "下位繰上げ1本" in out.slack_summary  # no silent caps（正直に明示）


def test_overfetch_exhausted_shows_failures(tmp_path: object) -> None:
    """候補が全滅したら失敗を隠さず見せる（N本を捏造しない）。"""
    pool = [VideoMeta(rank=i, url=f"https://t/{i}", play_count=1000) for i in range(1, 4)]

    def _boom(url: str) -> tuple[bytes, str]:
        raise RuntimeError("VIDEO_DOWNLOAD_FAILED")

    skill = VideoAlgorithmSkill(
        gemini=MagicMock(),
        searcher=lambda q, n, r: pool[:n],
        downloader=_boom,
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
    )
    out = skill.run(VideoAlgorithmInput(query="x", max_videos=3), ctx=SkillContext())
    assert out.videos  # 空にしない
    assert all(v.analysis is None for v in out.videos)  # 全部 error 表示
