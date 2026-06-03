"""5本横断の「アルゴリズム読み解き」（決定的・LLM非依存）。

各動画の観測フラグ（テロップにKW/キャプションにKW/CTA/短尺/ブランド検出 等）を数え、
共通パターン・rank上位帯 vs 下位帯の差分・勝ち筋仮説（確信度つき）を出す。
n は小さい（既定5）ので有意性検定はしない＝相関は仮説生成のみ（設計の正直さ原則）。
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from typing import Literal

from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    ColorSwatch,
    CorrItem,
    CrossAnalysis,
    DistItem,
    FeatureRowOut,
    KwCoverage,
    StatsAnalysis,
    WinFactor,
    WinRange,
)

# フラグ名（日本語）。win_factors / common_patterns / rank差分 で共通利用。
_FLAG_LABELS: dict[str, str] = {
    "kw_in_telop": "テロップ(焼き込み)に検索KWが出る",
    "kw_in_caption": "キャプション本文に検索KWが入っている",
    "strong_hook": "冒頭3秒のフックが強い型(問い/数字/衝撃/POV)",
    "heavy_telop": "テロップ密度が高い(常時字幕＝可読性/保存)",
    "has_cta": "明確なCTA(保存/フォロー/来店等)がある",
    "short_video": "尺が短め(≤20秒)で完了率を取りに行く",
    "brand_recognized": "商品/ブランドが映像内で認識できる",
}
_STRONG_HOOKS = {"question", "number", "shock", "pov", "problem"}


def _query_terms(query: str) -> list[str]:
    return [t for t in re.split(r"[\s　,、]+", query.strip()) if t]


def _flags_for(video: AnalyzedVideo, terms: list[str]) -> dict[str, bool]:
    a = video.analysis
    if a is None:
        return {k: False for k in _FLAG_LABELS}
    desc = video.meta.desc
    kw_in_caption = any(t and t in desc for t in terms) or any(
        m.matched and m.layer == "caption" for m in a.keyword_matches
    )
    dur = a.duration_sec
    return {
        "kw_in_telop": a.kw_in_telop(),
        "kw_in_caption": kw_in_caption,
        "strong_hook": a.hook_type in _STRONG_HOOKS,
        "heavy_telop": a.telop_density in ("medium", "heavy"),
        "has_cta": a.has_cta(),
        "short_video": 0 < dur <= 20,
        "brand_recognized": a.has_brand(),
    }


def _confidence(observed: int, total: int) -> Literal["高", "中", "低"]:
    if total == 0:
        return "低"
    ratio = observed / total
    if observed == total:
        return "高"
    if ratio >= 0.8:
        return "中"
    return "低"


def cross_analyze(videos: list[AnalyzedVideo], query: str) -> CrossAnalysis:
    """5本（以下）の分析から横断の読み解きを生成する。"""
    analyzed = [v for v in videos if v.analysis is not None]
    n = len(analyzed)
    cross = CrossAnalysis(keyword=query, video_count=n)
    if n == 0:
        cross.summary = f"KW「{query}」: 分析できた動画がありませんでした。"
        return cross

    terms = _query_terms(query)
    flags_by_video = [_flags_for(v, terms) for v in analyzed]

    # 指標
    eng = [v.meta.engagement_rate for v in analyzed]
    saves = [v.meta.save_rate() for v in analyzed]
    durs = [v.analysis.duration_sec for v in analyzed if v.analysis and v.analysis.duration_sec > 0]
    cross.avg_engagement_rate = round(statistics.mean(eng), 2) if eng else 0.0
    cross.avg_save_rate = round(statistics.mean(saves), 3) if saves else 0.0
    cross.median_duration_sec = round(statistics.median(durs), 1) if durs else 0.0

    # フラグ集計
    counts = {k: sum(1 for f in flags_by_video if f[k]) for k in _FLAG_LABELS}
    threshold = math.ceil(0.8 * n)  # 共通パターン閾値（n=5→4）
    cross.common_patterns = [
        f"{counts[k]}/{n}: {_FLAG_LABELS[k]}" for k in _FLAG_LABELS if counts[k] >= threshold
    ]

    # 勝ち筋（observed>=0.6n を採用、observed降順）
    win = [
        WinFactor(
            factor=_FLAG_LABELS[k],
            observed_in=counts[k],
            total=n,
            confidence=_confidence(counts[k], n),
            evidence=f"上位{n}本中{counts[k]}本で観測",
        )
        for k in _FLAG_LABELS
        if counts[k] >= math.ceil(0.6 * n)
    ]
    win.sort(key=lambda w: w.observed_in, reverse=True)
    cross.win_factors = win[:5]

    # rank 上位帯 vs 下位帯（rankでソートし半分ずつ。中間は無視）
    by_rank = sorted(analyzed, key=lambda v: v.meta.rank or 99)
    half = max(1, n // 2)
    top, bottom = by_rank[:half], by_rank[-half:]
    top_flags = [_flags_for(v, terms) for v in top]
    bot_flags = [_flags_for(v, terms) for v in bottom]
    drivers: list[str] = []
    for k in _FLAG_LABELS:
        tr = sum(1 for f in top_flags if f[k]) / len(top_flags)
        br = sum(1 for f in bot_flags if f[k]) / len(bot_flags)
        if tr - br >= 0.5:
            drivers.append(f"上位帯ほど『{_FLAG_LABELS[k]}』（上位{tr:.0%} vs 下位{br:.0%}）")
    # 保存率の上位/下位差
    top_save = statistics.mean([v.meta.save_rate() for v in top])
    bot_save = statistics.mean([v.meta.save_rate() for v in bottom])
    if top_save - bot_save > 0.05:
        drivers.append(f"上位帯の保存率が高い（上位{top_save:.2f}% vs 下位{bot_save:.2f}%）")
    cross.rank_diff_drivers = drivers

    # サムネ色の横断集計（ffmpeg+stdlib 算出ベース・動画内色は廃止）
    _aggregate_thumb(cross, analyzed)

    # AI 統計（決定的・stdlib のみ）。失敗しても既存出力は壊さない
    try:
        cross.stats = statistical_analyze(analyzed, query, terms)
    except Exception:  # 統計失敗で横断分析全体を落とさない
        cross.stats = None

    # サマリ（1行）
    top_factor = cross.win_factors[0].factor if cross.win_factors else "（顕著な共通項なし）"
    cross.summary = (
        f"KW「{query}」上位{n}本＝平均ENG {cross.avg_engagement_rate}% / "
        f"保存率 {cross.avg_save_rate}% / 尺中央値 {cross.median_duration_sec}秒。"
        f"最も共通する勝ち筋は『{top_factor}』。"
    )
    return cross


# -----------------------------------------------------------
# サムネ色の横断集計（検索一覧での目立ち方）
# -----------------------------------------------------------
_TEMP_MAP = {"暖色": "warm", "寒色": "cool", "中性": "neutral"}
_BRIGHT_MAP = {"高明度": "bright", "中明度": "medium", "低明度": "dark"}


def _aggregate_thumb(cross: CrossAnalysis, analyzed: list[AnalyzedVideo]) -> None:
    thumbs = [v.thumb for v in analyzed if v.thumb]
    if not thumbs:
        return
    n = len(thumbs)
    tone_mode = Counter(t.tone_jp() for t in thumbs).most_common(1)[0][0]
    bright_mode = Counter(t.bright_jp() for t in thumbs).most_common(1)[0][0]
    cross.dominant_temperature = _TEMP_MAP.get(tone_mode, "neutral")  # type: ignore[assignment]
    cross.dominant_brightness = _BRIGHT_MAP.get(bright_mode, "medium")  # type: ignore[assignment]
    # 主要色を色相ビンでまとめ頻出 top5
    swatches = [s for t in thumbs for s in t.swatches if s]
    seen: dict[str, ColorSwatch] = {}
    freq: Counter[str] = Counter()
    for hexs in swatches:
        key = _hex_bin(hexs)
        if key:
            freq[key] += 1
            seen.setdefault(key, ColorSwatch(hex=hexs, role="dominant"))
    cross.common_palette = [seen[k] for k, _ in freq.most_common(5)]
    # コンセンサス1文（暖寒×明度の joint mode）。過半数一致のときだけ「共通則」と呼ぶ
    joint = Counter((t.tone_jp(), t.bright_jp()) for t in thumbs)
    (jt, jb), jm = joint.most_common(1)[0]
    if jm * 2 > n:  # 厳密過半数（n=2なら2本一致が必要）
        cross.thumb_agree = True
        cross.thumb_consensus = f"上位{n}本中{jm}本が{jt}×{jb}（検索一覧での目立ち方）"
    else:
        cross.thumb_agree = False
        cross.thumb_consensus = f"サムネ色は割れている（{n}本で傾向バラつき）— 色の共通則は弱い"


def _hex_bin(hexstr: str) -> str:
    """#RRGGBB を粗いビン（各色2bit）にまとめて代表キー化。"""
    h = hexstr.lstrip("#")
    if len(h) != 6:
        return ""
    try:
        r, g, b = (int(h[i : i + 2], 16) >> 6 for i in (0, 2, 4))
    except ValueError:
        return ""
    return f"{r}{g}{b}"


# -----------------------------------------------------------
# AI 統計（stdlib のみ・有意性なし）
# -----------------------------------------------------------
_DENSITY_ORD = {"none": 0, "light": 1, "medium": 2, "heavy": 3}


def _kw_layers(v: AnalyzedVideo, terms: list[str]) -> int:
    a = v.analysis
    if a is None:
        return 0
    telop = a.kw_in_telop()
    spoken = any(m.matched for m in a.spoken_keywords)
    caption = any(t and t in v.meta.desc for t in terms) or any(
        m.matched and m.layer == "caption" for m in a.keyword_matches
    )
    hashtag = any(m.matched and m.layer == "hashtag" for m in a.keyword_matches)
    return sum((telop, spoken, caption, hashtag))


def _num_features(v: AnalyzedVideo) -> dict[str, float | None]:
    a = v.analysis
    if a is None:
        return {}
    return {
        "保存率": v.meta.save_rate(),
        "係数": v.meta.engagement_rate,
        "尺(秒)": a.duration_sec if a.duration_sec > 0 else None,
        "テロップ枚数": float(len(a.telops)),
        "テロップ密度": float(_DENSITY_ORD.get(a.telop_density, 0)),
    }


def _rank_avg(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(x: Sequence[float | None], y: Sequence[float | None]) -> tuple[float | None, int]:
    pairs = [(a, b) for a, b in zip(x, y, strict=False) if a is not None and b is not None]
    n = len(pairs)
    if n < 3:
        return None, n
    rx = _rank_avg([p[0] for p in pairs])
    ry = _rank_avg([p[1] for p in pairs])
    try:
        return round(statistics.correlation(rx, ry), 2), n
    except statistics.StatisticsError:
        return None, n


def _monotonic(by_rank_feature: list[float]) -> tuple[int, int]:
    """rank 昇順に並べた特徴列の単調性（同方向の隣接ペア数 / 全隣接ペア数）。"""
    diffs = [b - a for a, b in itertools.pairwise(by_rank_feature)]
    nz = [d for d in diffs if d != 0]
    total = len(diffs)
    if not nz:
        return 0, total
    pos = sum(1 for d in nz if d > 0)
    return max(pos, len(nz) - pos), total


def statistical_analyze(
    analyzed: list[AnalyzedVideo], query: str, terms: list[str]
) -> StatsAnalysis:
    ok = [v for v in analyzed if v.analysis]
    n = len(ok)
    st = StatsAnalysis(sample_size=n)
    if n == 0:
        return st
    ranks = [float(v.meta.rank or 99) for v in ok]
    feats = {k: [_num_features(v).get(k) for v in ok] for k in _num_features(ok[0])}

    # ① 相関（rank と各特徴）
    by_rank = sorted(ok, key=lambda v: v.meta.rank or 99)
    for fname, series in feats.items():
        rho, npair = _spearman(ranks, series)
        mono = _monotonic([(_num_features(v).get(fname) or 0.0) for v in by_rank])
        direction = ""
        if rho is not None:
            direction = "値が大きいほど上位" if rho < 0 else "値が小さいほど上位"
        st.correlations.append(
            CorrItem(
                feature=fname,
                target="rank",
                rho=rho,
                n_pairs=npair,
                direction_label=direction,
                monotonic_hits=mono[0],
                monotonic_total=mono[1],
            )
        )

    # ② 分布＋外れ値
    for fname in ("保存率", "テロップ枚数", "尺(秒)"):
        vals = [x for x in feats.get(fname, []) if x is not None]
        if not vals:
            continue
        med = statistics.median(vals)
        di = DistItem(
            feature=fname, median=round(med, 2), min=round(min(vals), 2), max=round(max(vals), 2)
        )
        out = _outlier(ok, fname, vals, med)
        if out:
            di.outlier_rank, di.outlier_value, di.outlier_note = out
        st.distributions.append(di)

    # ③ KW カバレッジ
    _kw_coverage(st, ok, terms, n)

    # ④ フック分布
    hooks = Counter(v.analysis.hook_type for v in ok if v.analysis)
    st.hook_counts = hooks.most_common()
    strong = sum(1 for v in ok if v.analysis and v.analysis.hook_type in _STRONG_HOOKS)
    st.strong_hook_ratio = f"{strong}/{n}"

    # ⑤ 勝ち筋レンジ（上位帯）
    half = by_rank[: max(1, n // 2)]
    st.win_ranges = _win_ranges(half)

    # ⑥ 特徴量マトリクス
    for v in by_rank:
        a = v.analysis
        assert a is not None
        st.feature_matrix.append(
            FeatureRowOut(
                rank=v.meta.rank,
                save_rate=round(v.meta.save_rate(), 2),
                duration_sec=round(a.duration_sec, 1),
                telop_count=len(a.telops),
                telop_density=a.telop_density,
                hook_type=a.hook_type,
                kw_layers=f"{_kw_layers(v, terms)}/4",
                has_cta=a.has_cta(),
                has_brand=a.has_brand(),
            )
        )

    st.caveats = [
        f"n={n} は統計的に極小。相関は方向の“ヒント”で、有意性検定は行っていません。",
        "相関≠因果。順位はTikTok非公開の内部重みで決まり、ここで測るのは表層特徴の共通性のみ。",
        "上位入賞動画だけを見る生存者バイアスあり（落ちた動画は不可視）。テスト投稿での検証推奨。",
    ]
    return st


def _outlier(
    ok: list[AnalyzedVideo], fname: str, vals: list[float], med: float
) -> tuple[int, float, str] | None:
    if len(vals) < 4 or med <= 0:
        # n小は「中央値の2倍超 or 0.5倍未満」で突出1本を拾う
        for v in ok:
            x = _num_features(v).get(fname)
            if x is not None and med > 0 and (x > med * 2 or x < med * 0.5):
                return v.meta.rank, round(x, 2), f"中央値の{x / med:.1f}倍"
        return None
    q1, q3 = statistics.quantiles(vals, n=4)[0], statistics.quantiles(vals, n=4)[2]
    iqr = q3 - q1
    for v in ok:
        x = _num_features(v).get(fname)
        if x is not None and iqr > 0 and (x > q3 + 1.5 * iqr or x < q1 - 1.5 * iqr):
            return v.meta.rank, round(x, 2), "IQR外れ値"
    return None


def _kw_coverage(st: StatsAnalysis, ok: list[AnalyzedVideo], terms: list[str], n: int) -> None:
    weights = {"テロップ": 0.35, "キャプション": 0.30, "HT": 0.20, "音声": 0.15}
    fill = {k: 0 for k in weights}
    scores: list[float] = []
    layers_sum = 0
    per: list[str] = []
    for v in ok:
        a = v.analysis
        if a is None:
            continue
        lay = {
            "テロップ": a.kw_in_telop(),
            "音声": any(m.matched for m in a.spoken_keywords),
            "キャプション": any(t and t in v.meta.desc for t in terms)
            or any(m.matched and m.layer == "caption" for m in a.keyword_matches),
            "HT": any(m.matched and m.layer == "hashtag" for m in a.keyword_matches),
        }
        cnt = sum(lay.values())
        layers_sum += cnt
        sc = sum(weights[k] for k, ok_ in lay.items() if ok_) * 100
        scores.append(sc)
        for k, ok_ in lay.items():
            fill[k] += int(ok_)
        per.append(f"#{v.meta.rank} {cnt}/4({sc:.0f})")
    st.kw_coverage = KwCoverage(
        avg_score_0_100=round(statistics.mean(scores), 1) if scores else 0.0,
        avg_layers_0_4=round(layers_sum / n, 2) if n else 0.0,
        layer_fill=[(k, f"{fill[k]}/{n}") for k in weights],
        per_video=per,
    )


def _win_ranges(half: list[AnalyzedVideo]) -> list[WinRange]:
    """上位帯のレンジ。単一サンプル（min==max）の「レンジ」は誠実でないので出さない。"""
    out: list[WinRange] = []
    durs = [v.analysis.duration_sec for v in half if v.analysis and v.analysis.duration_sec > 0]
    if len(durs) >= 2 and min(durs) != max(durs):
        out.append(WinRange(label="尺", text=f"{min(durs):.0f}-{max(durs):.0f}秒"))
    saves = [v.meta.save_rate() for v in half]
    if len(saves) >= 2:
        out.append(WinRange(label="保存率", text=f"{min(saves):.1f}% 以上"))
    tels = [len(v.analysis.telops) for v in half if v.analysis]
    if len(tels) >= 2:
        out.append(WinRange(label="テロップ", text=f"{min(tels)}枚以上"))
    return out
