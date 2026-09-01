"""ProposalReviewOutput → 共通 Report への詰め替え（純粋関数・I/O なし）。"""

from __future__ import annotations

from teamagent.skills._html.report import Chip, Report
from teamagent.skills._shared.report_sources import sources_table
from teamagent.skills.proposal_review.schema import ProposalReviewOutput


def build_report(out: ProposalReviewOutput) -> Report:
    """レビュー本文＋照合した過去事例を 1 枚に。"""
    chips = [Chip("照合件数", f"{out.source_count}")]
    if out.total_cost_usd:
        chips.append(Chip("コスト", f"${out.total_cost_usd:.4f}"))
    return Report(
        title="提案レビュー",
        subtitle="過去の勝ちパターンと照合した診断。指摘は仮説であり、案件の文脈で取捨すること。",
        chips=chips,
        body_md=out.review or "",
        tables=[sources_table(list(out.sources), caption="照合した過去提案・FB")],
        source_note="出典: 社内ナレッジ（過去提案・フィードバック）。",
    )


__all__ = ["build_report"]
