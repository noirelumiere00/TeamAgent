"""ProposalDraft Skill 本体 (過去提案再利用支援、仕様: 実装計画 §7.1 Skill ⑤)。

新規案件ブリーフから類似の過去提案/FB を検索し、提案ドラフトの骨子を生成する。
検索は SearchSkill.retrieve_hits を再利用 (gold set top-1 88% パイプライン)。
生成は Bedrock Sonnet 4.6。営業がレビュー・肉付けする前提のたたき台を返す。

3 層分離: Skill 層。retrieval は SearchSkill、生成は BedrockClient 経由。
"""

from __future__ import annotations

from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.report_html import publish_report
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.proposal.report import build_report
from teamagent.skills.proposal.schema import (
    ProposalDraftInput,
    ProposalDraftOutput,
    ProposalSource,
)
from teamagent.skills.search.skill import SearchSkill

logger = structlog.get_logger(__name__)


@register
class ProposalDraftSkill(BaseSkill[ProposalDraftInput, ProposalDraftOutput]):
    """過去の類似提案を再利用して提案ドラフト骨子を生成する Skill。"""

    name: ClassVar[str] = "proposal_draft"
    description: ClassVar[str] = "新規案件ブリーフから類似過去提案を検索し提案ドラフト骨子を生成"
    input_schema: ClassVar[type[BaseModel]] = ProposalDraftInput
    output_schema: ClassVar[type[BaseModel]] = ProposalDraftOutput

    def __init__(
        self,
        search: SearchSkill | None = None,
        bedrock: BedrockClient | None = None,
        *,
        prompt_version: str = "v1",
        summary_max_tokens: int = 1400,
    ) -> None:
        # search は検索基盤の再利用。None なら自前生成 (本番は bot が注入)。
        self._search = search or SearchSkill()
        self._bedrock = bedrock or BedrockClient.from_env()
        self._prompt_version = prompt_version
        self._summary_max_tokens = summary_max_tokens

    def run(self, input: ProposalDraftInput, ctx: SkillContext) -> ProposalDraftOutput:
        log = ctx.bind_logger(self.name)
        log.info("proposal_draft_start", brief_len=len(input.brief), top_k=input.top_k)

        # 1. 類似の過去提案/FB を取得 (検索基盤を再利用)
        hits = self._search.retrieve_hits(
            input.brief,
            ctx,
            top_k=input.top_k,
            filter_industry=input.industry,
        )

        # 2. ドラフト骨子を生成
        draft, cost_usd = self._generate(input.brief, hits, ctx.request_id)

        sources = [self._to_source(h) for h in hits]
        out = ProposalDraftOutput(
            brief=input.brief,
            draft=draft,
            sources=sources,
            source_count=len(sources),
            total_cost_usd=cost_usd,
        )
        out.report_url = publish_report(
            build_report(out),
            tool=self.name,
            request_id=ctx.request_id,
            query=input.brief,
            rls_derived=True,
        )
        log.info(
            "proposal_draft_done",
            source_count=len(sources),
            cost_usd=cost_usd,
            has_report=bool(out.report_url),
        )
        return out

    @staticmethod
    def _to_source(hit: SearchHit) -> ProposalSource:
        meta = hit.metadata or {}
        return ProposalSource(
            chunk_id=hit.chunk_id,
            source_type=meta.get("source_type"),
            title=meta.get("title"),
            client_name=meta.get("client_name"),
            preview=hit.content[:160],
        )

    def _generate(self, brief: str, hits: list[SearchHit], request_id: str) -> tuple[str, float]:
        """過去提案を踏まえてドラフト骨子を生成する。ヒット 0 件なら Bedrock を呼ばない。"""
        if not hits:
            return (
                "参照できる類似の過去提案が見つかりませんでした。"
                "ブリーフの業界・ターゲットを変えて再検索するか、新規に作成してください。",
                0.0,
            )

        system = load_prompt("proposal", self._prompt_version, "system")

        ref_block = "\n\n".join(
            f"[chunk_id: {h.chunk_id}] "
            f"{(h.metadata or {}).get('title', '') or (h.metadata or {}).get('client_name', '')}\n"
            f"{h.content[:400]}"
            for h in hits
        )
        user_message = (
            f"# 新規案件ブリーフ\n{brief}\n\n"
            f"# 参照: 過去の類似提案 / 営業 FB（{len(hits)} 件）\n{ref_block}\n\n"
            "上記の過去提案の勝ち筋を再利用して、この新規案件の提案ドラフト骨子を"
            "フォーマットに従って作成してください。"
        )

        resp = self._bedrock.converse(
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            request_id=request_id,
            system=system,
            cache_system=True,
            max_tokens=self._summary_max_tokens,
        )
        return resp.text, resp.usage.cost_usd
