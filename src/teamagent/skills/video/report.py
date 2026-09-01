"""VideoAnalysisOutput → 共通 Report への詰め替え（純粋関数・I/O なし）。

出典テーブルは持たない（対象は 1 本の動画）。URL は safe_href の許可ホスト外（YouTube 等）が
大半なのでリンクにせず、chip としてそのまま表示する。
"""

from __future__ import annotations

from teamagent.skills._html.report import Chip, Report
from teamagent.skills.video.schema import VideoAnalysisOutput

_URL_MAX = 80


def build_report(out: VideoAnalysisOutput) -> Report:
    """動画 1 本の構造分析を 1 枚に。"""
    url = (out.url or "").strip()
    chips = [Chip("対象", url[:_URL_MAX] + "…" if len(url) > _URL_MAX else url)]
    if out.model_id:
        chips.append(Chip("分析", out.model_id))
    if out.total_cost_usd:
        chips.append(Chip("コスト", f"${out.total_cost_usd:.4f}"))
    return Report(
        title="動画分析 — 構成・フック・CTA",
        subtitle="競合動画の構造を分解し、提案書へ転記できる粒度で言語化したもの。",
        chips=chips,
        body_md=out.analysis or "",
        source_note="出典: 対象動画の実視聴分析（Gemini）。",
    )


__all__ = ["build_report"]
