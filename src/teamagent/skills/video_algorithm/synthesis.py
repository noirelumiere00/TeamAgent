"""複数動画の横断シンセシス（Gemini 2nd pass・概念の関連性/勝ちパターン仮説）。

1本ずつ構造分析済みの結果を要約してテキストプロンプト化し、generate_text で
CrossSynthesis を JSON 生成させる。決定的統計(stats)とは別の「解釈層」。
1本では横断概念にならないため n<2 はスキップ。失敗は graceful（None）でレポート続行。
"""

from __future__ import annotations

import json
import re

import structlog
from pydantic import ValidationError

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CrossSynthesis,
    StatsAnalysis,
)

logger = structlog.get_logger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_DESC_MAX = 220


def _video_brief(v: AnalyzedVideo) -> str:
    a = v.analysis
    if a is None:
        return ""
    lm = a.layer_messages
    telop_gist = lm.telop if lm and lm.telop else " / ".join(t.text for t in a.telops[:4])
    brands = (
        "、".join(f"{b.brand_name}({b.brand_relation})" for b in a.brand_detections if b.brand_name)
        or "なし"
    )
    thumb = f"{v.thumb.tone_jp()}/{v.thumb.bright_jp()}" if v.thumb else "—"
    desc = (v.meta.desc or "")[:_DESC_MAX]
    coh = a.message_coherence if a.message_coherence is not None else "—"
    return (
        f"#{v.meta.rank}（保存率{v.meta.save_rate():.2f}% / 尺{a.duration_sec:.0f}s）\n"
        f"  主訴求: {a.main_message or '—'}\n"
        f"  訴求軸: {', '.join(a.value_propositions) or '—'}\n"
        f"  フック: {a.hook_type} / {a.hook_summary}\n"
        f"  テロップ要旨: {telop_gist or '—'}\n"
        f"  キャプション: {desc or '—'}\n"
        f"  CTA: {', '.join(a.cta_type) or 'なし'}\n"
        f"  ブランド: {brands}\n"
        f"  サムネ色: {thumb}\n"
        f"  メッセージ一貫性: {coh}"
    )


def _stats_block_text(stats: StatsAnalysis | None) -> str:
    """計算済み統計を「## 横断統計」テキスト化（プランナーが根拠に書くため・読む順に並べる）。"""
    if stats is None or stats.sample_size == 0:
        return ""
    lines: list[str] = [
        f"## 横断統計（n={stats.sample_size}・有意性検定なし。各主張はこの数字を根拠に書け）"
    ]
    kc = stats.kw_coverage
    if kc.layer_fill:  # ① 検索面の穴（最優先）
        lines.append(
            "・KWカバレッジ層別: "
            + " / ".join(f"{k}{v}" for k, v in kc.layer_fill)
            + f"（重み付き平均{kc.avg_score_0_100:.0f}/100）※全員充足の層=前提条件、0の層=不要かも"
        )
        if kc.per_video:
            lines.append("    動画別: " + " / ".join(kc.per_video))
    if stats.hook_counts:  # ② フック
        lines.append(
            "・フック分布: "
            + " ".join(f"{h}×{c}" for h, c in stats.hook_counts)
            + f"（強フック {stats.strong_hook_ratio}）※過半数未満の型は第一指定にしない"
        )
    lines.append(  # ③ レンジ
        "・勝ち筋レンジ(上位帯の実測幅): "
        + (
            " / ".join(f"{r.label}{r.text}" for r in stats.win_ranges)
            or "なし=割れている/サンプル不足"
        )
    )
    cr: list[str] = []  # ④ 相関（方向の裏取り専用）
    for c in stats.correlations:
        if c.rho is None:
            cr.append(f"{c.feature}=判定不能(n<3 or 全員同値)")
        else:
            mono = f"{c.monotonic_hits}/{c.monotonic_total}"
            cr.append(f"{c.feature} ρ{c.rho:+.2f}({c.direction_label}・単調{mono})")
    if cr:
        lines.append("・相関(特徴×順位／方向のヒントのみ・結論や指示に使わない): " + " / ".join(cr))
    for d in stats.distributions:  # ⑤ 分布・外れ値
        ol = f"／外れ値#{d.outlier_rank}({d.outlier_note})" if d.outlier_rank else ""
        lines.append(f"・分布[{d.feature}]: 中央値{d.median} 範囲{d.min}–{d.max}{ol}")
    if stats.caveats:
        lines.append("・前提(必ず caveat に反映): " + " ".join(stats.caveats))
    return "\n".join(lines) + "\n\n"


def build_prompt(
    analyzed: list[AnalyzedVideo], query: str, stats: StatsAnalysis | None = None
) -> str:
    briefs = "\n".join(b for v in analyzed if (b := _video_brief(v)))
    n = sum(1 for v in analyzed if v.analysis)
    return (
        f"# 検索KW: {query}\n"
        f"{_stats_block_text(stats)}"
        f"# 上位 {n} 本の構造分析（個票・rank紐付けのエビデンス源）:\n\n"
        f"{briefs}\n\n"
        "まず上の『横断統計』を読み、システム指示の統計ガードレールに従って、"
        "各主張に統計の裏付けを角括弧で併記しながら JSON を出力してください。"
    )


def parse_synthesis(text: str) -> CrossSynthesis | None:
    """所見＋JSONブロックを CrossSynthesis にパース（防御的）。"""
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return CrossSynthesis.model_validate(data)
    except ValidationError:
        logger.warning("video_synthesis_validation_failed")
        return None


def _enforce_confidence(syn: CrossSynthesis, n: int) -> None:
    """確信度の天井を n・支持本数・反例に機械的に連動（n小の誠実さ・敵対レビュー反映）。

    - n<3: 全仮説『低』（相関すら出ない領域＝共通点メモ扱い）
    - 『高』は「全数支持 ∧ 反例なし ∧ n≥5」を全て満たす時のみ。それ以外の高→中
    - 全数支持でない（過半数止まり）は中止まり
    - 反例ありは更に1段下げる
    """
    order = {"高": 2, "中": 1, "低": 0}
    for h in syn.win_hypotheses:
        full = len(h.supported_by) >= n
        lvl = order.get(h.confidence, 1)
        if h.counter_example and lvl > 0:
            lvl -= 1  # 反例ありは1段下げる（先に適用）
        if n < 3:
            lvl = 0  # 相関すら出ない領域＝全仮説「低」
        else:
            if not full and lvl > 1:
                lvl = 1  # 部分支持(過半数止まり)は中止まり
            if lvl == 2 and not (full and n >= 5):
                lvl = 1  # 高は全数支持かつn≥5のときのみ
        h.confidence = "高" if lvl == 2 else "中" if lvl == 1 else "低"


def synthesize(
    gemini: GeminiClient,
    analyzed: list[AnalyzedVideo],
    query: str,
    *,
    request_id: str,
    prompt_version: str = "v1",
    stats: StatsAnalysis | None = None,
) -> tuple[CrossSynthesis | None, float]:
    """横断シンセシスを生成。stats を渡すと統計を根拠に推論させる。失敗で (None, 0.0)。"""
    ok = [v for v in analyzed if v.analysis]
    if len(ok) < 2:  # 1本では「横断」概念にならない
        return None, 0.0
    try:
        system = load_prompt("video_algorithm", prompt_version, "synthesis")
        resp = gemini.generate_text(build_prompt(ok, query, stats), request_id, system=system)
        syn = parse_synthesis(resp.text)
        cost = float(resp.cost_usd)
    except Exception as e:  # load/生成/パース/型 どこで失敗してもレポートは続行
        logger.warning("video_synthesis_failed", request_id=request_id, error=type(e).__name__)
        return None, 0.0
    if syn is not None:
        _enforce_confidence(syn, len(ok))
    return syn, cost
