"""ProposalDraftOutput → 共通 Report への詰め替え（純粋関数・I/O なし）。"""

from __future__ import annotations

from teamagent.skills._html.report import Chip, Report
from teamagent.skills._shared.report_sources import sources_table
from teamagent.skills.proposal.schema import ProposalDraftOutput

_TITLE_MAX = 40


def build_report(out: ProposalDraftOutput) -> Report:
    """ドラフト本文＋根拠資料を 1 枚に。表題はブリーフ冒頭から起こす。"""
    head = " ".join((out.brief or "").split())
    title = head[:_TITLE_MAX] + "…" if len(head) > _TITLE_MAX else head
    chips = [Chip("参照件数", f"{out.source_count}")]
    if out.total_cost_usd:
        chips.append(Chip("コスト", f"${out.total_cost_usd:.4f}"))
    return Report(
        title=f"提案ドラフト — {title}" if title else "提案ドラフト",
        subtitle=(
            "過去提案・FB を検索して生成した骨子。そのまま提出せず、担当の判断で調整すること。"
        ),
        chips=chips,
        body_md=out.draft or "",
        tables=[sources_table(list(out.sources))],
        source_note="出典: 社内ナレッジ（過去提案・フィードバック）。",
    )


__all__ = ["build_report"]
