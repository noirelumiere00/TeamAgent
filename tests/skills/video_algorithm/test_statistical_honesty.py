"""統計的誠実性のリグレッション（審査所見R7）。

n小の横断分析が「板全体でも同率の特徴」「帯あたり数本のノイズ差」「弱い相関の方向断定」を
勝ち筋/傾向として断定しないことを固定する:
(a) メタ判定フラグの win_factor は board 基準率とのリフト>=1.5 でゲート
(b) rank_diff_drivers は n>=8 のみ・保存率差は pooled 比率の 2SE 超のみ
(c) Spearman 方向ラベルは |ρ|>=0.5 かつ n>=5 のみ
(d) レポートの小サンプル警告は n<6 まで段階化・caveats を統計付録に描画
"""

from __future__ import annotations

import re

from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    TelopItem,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)


def _av(
    rank: int,
    *,
    kw_telop: bool = True,
    dur: float = 18,
    saves: int = 1500,
    plays: int = 100000,
    desc: str = "新宿 ランチ 名店",
) -> AnalyzedVideo:
    return AnalyzedVideo(
        meta=VideoMeta(
            rank=rank,
            url=f"https://t/{rank}",
            desc=desc,
            play_count=plays,
            collect_count=saves,
            engagement_rate=8.0,
        ),
        analysis=VideoVSEOAnalysis(
            duration_sec=dur,
            hook_type="question",
            telops=[TelopItem(sec=1, text="新宿", kw_match=kw_telop)],
        ),
    )


# -----------------------------------------------------------
# (a) win_factor の board リフトゲート
# -----------------------------------------------------------
def test_kw_caption_winfactor_gated_by_board_base_rate() -> None:
    """板全体でもKWキャプションがほぼ100%なら、上位共通でも勝ち筋と呼ばない（リフト<1.5）。"""
    videos = [_av(r) for r in (1, 2, 3, 4, 5)]
    board = [VideoMeta(rank=i, url=f"https://t/{i}", desc="新宿 ランチ") for i in range(1, 21)]
    cross = cross_analyze(videos, "新宿 ランチ", board=board)
    assert not any("キャプション本文" in w.factor for w in cross.win_factors)
    # board 未指定（従来呼び出し）は後方互換でゲートなし＝採用される
    cross_nb = cross_analyze(videos, "新宿 ランチ")
    assert any("キャプション本文" in w.factor for w in cross_nb.win_factors)


def test_kw_caption_winfactor_adopted_when_lift_high() -> None:
    """板全体では2割しかKWキャプションが無い→上位で全員なら lift=5 で勝ち筋に採用。"""
    videos = [_av(r) for r in (1, 2, 3, 4, 5)]
    board = [
        VideoMeta(rank=i, url=f"https://t/{i}", desc="新宿 ランチ" if i <= 4 else "無関係な動画")
        for i in range(1, 21)
    ]
    cross = cross_analyze(videos, "新宿 ランチ", board=board)
    assert any("キャプション本文" in w.factor for w in cross.win_factors)


# -----------------------------------------------------------
# (b) rank_diff_drivers の n>=8 ゲートと保存率差の 2SE 採用
# -----------------------------------------------------------
def test_rank_diff_drivers_silent_under_n8() -> None:
    """n=5（既定運用）では帯あたり2-3本＝ノイズ差になるためドライバーを出さない。"""
    videos = [_av(r, kw_telop=(r <= 2), saves=3000 - r * 500) for r in (1, 2, 3, 4, 5)]
    cross = cross_analyze(videos, "新宿 ランチ")
    assert cross.rank_diff_drivers == []


def test_rank_diff_drivers_with_n8_and_pooled_se() -> None:
    """n=8: フラグ差（上位帯のみKWテロップ）と 2SE 超の保存率差はドライバーに載る。"""
    videos = [_av(r, kw_telop=(r <= 4), saves=5000 if r <= 4 else 1000) for r in range(1, 9)]
    cross = cross_analyze(videos, "新宿 ランチ")
    assert any("テロップ" in d for d in cross.rank_diff_drivers)
    assert any("保存率" in d for d in cross.rank_diff_drivers)


def test_save_rate_driver_rejected_when_within_2se() -> None:
    """保存率差が pooled 比率の 2SE 以内（低再生のノイズ差）なら採用しない。"""
    videos = [
        _av(r, saves=2 if r <= 4 else 1, plays=1000) for r in range(1, 9)
    ]  # 0.2% vs 0.1%・試行4000ずつ→2SE内
    cross = cross_analyze(videos, "新宿 ランチ")
    assert not any("保存率" in d for d in cross.rank_diff_drivers)


# -----------------------------------------------------------
# (c) Spearman 方向ラベルの制限
# -----------------------------------------------------------
def test_direction_label_suppressed_for_small_n() -> None:
    """n=3（npair<5）では ρ が出ても方向ラベルは断定しない。"""
    videos = [_av(r, dur=10 + r * 4) for r in (1, 2, 3)]
    cross = cross_analyze(videos, "新宿 ランチ")
    assert cross.stats is not None
    assert all(c.direction_label == "" for c in cross.stats.correlations)


def test_direction_label_shown_for_strong_rho_n5() -> None:
    """n=5 かつ |ρ|>=0.5（強い単調傾向）のときだけ方向ラベルを出す。"""
    videos = [_av(r, dur=8 + r * 4) for r in (1, 2, 3, 4, 5)]  # rank↑=尺↑（ρ=+1）
    cross = cross_analyze(videos, "新宿 ランチ")
    assert cross.stats is not None
    dur_corr = next(c for c in cross.stats.correlations if c.feature == "尺(秒)")
    assert dur_corr.rho is not None and abs(dur_corr.rho) >= 0.5
    assert dur_corr.direction_label == "値が小さいほど上位"


# -----------------------------------------------------------
# (d) レポート: 段階化バナーと caveats 描画
# -----------------------------------------------------------
def _report_html(n: int) -> str:
    out = VideoAlgorithmOutput(query="新宿 ランチ", videos=[_av(r) for r in range(1, n + 1)])
    out.cross = cross_analyze(out.videos, "新宿 ランチ")
    return render_report(out)


def test_report_banner_staged_by_sample_size() -> None:
    """警告バナー: n<3=強（極小）・3<=n<6=中（小）・n>=6=なし（審査所見R7）。"""
    assert "極小サンプル" in _report_html(2)
    h5 = _report_html(5)
    assert "小サンプル" in h5 and "極小サンプル" not in h5
    assert '<div class="nbanner">' not in _report_html(6)


def test_report_renders_stats_caveats() -> None:
    """生成済み caveats（免責）が統計付録に箇条書きで描画される（審査所見R7）。"""
    html = _report_html(5)
    assert '<ul class="caveats">' in html
    assert "生存者バイアス" in html  # caveats 定型文の1つ


# -----------------------------------------------------------
# (e) レビュー回帰: 保存数>再生数の破損帯で落ちない/106%を出さない
# -----------------------------------------------------------
def test_pooled_save_rate_no_crash_when_saves_exceed_plays() -> None:
    """帯合計で保存数>再生数（play_count=0 や整合ズレ）でも math.sqrt(負値)で落ちず、
    100%超の非現実的な保存率をドライバーに出さない（破損帯は判定不能＝不採用）。"""
    # 上位帯(rank1-4)を保存数>再生数の破損データにする。旧コードは pooled>1 で ValueError。
    videos = [
        _av(1, saves=200, plays=0),  # play_count=0（分母に寄与せず保存だけ分子へ）
        _av(2, saves=200, plays=50),  # saves>plays（比率>1）
        _av(3, saves=200, plays=50),
        _av(4, saves=200, plays=50),
        _av(5, saves=5, plays=100000),
        _av(6, saves=5, plays=100000),
        _av(7, saves=5, plays=100000),
        _av(8, saves=5, plays=100000),
    ]
    cross = cross_analyze(videos, "新宿 ランチ")  # 例外を投げないこと（回帰の主眼）
    # 破損帯はドライバーに採用しない。万一出しても 100% 超の値は絶対に出さない。
    assert not any("保存率" in d for d in cross.rank_diff_drivers)
    for d in cross.rank_diff_drivers:
        for token in re.findall(r"([0-9]+(?:\.[0-9]+)?)%", d):
            assert float(token) <= 100.0, f"保存率が100%超: {d}"
