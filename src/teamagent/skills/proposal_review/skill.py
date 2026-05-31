"""ProposalReview Skill 本体 (提案レビュー Bot、仕様: 実装計画 §7.3 Skill ⑦)。

提案テキストを過去の勝ちパターン・失注理由と照合し、コードレビュー的に診断する。
照合素材の検索は SearchSkill.retrieve_hits を再利用 (gold set top-1 88%)。
生成は Bedrock Sonnet 4.6。提案ドラフト (Skill ⑤) と「作成→レビュー」で繋がる。
"""

from __future__ import annotations

from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.proposal_review.schema import (
    ProposalReviewInput,
    ProposalReviewOutput,
    ReviewSource,
)
from teamagent.skills.search.skill import SearchSkill

logger = structlog.get_logger(__name__)


@register
class ProposalReviewSkill(BaseSkill[ProposalReviewInput, ProposalReviewOutput]):
    """提案を過去の勝ち筋と照合して診断する Skill。"""

    name: ClassVar[str] = "proposal_review"
    description: ClassVar[str] = "提案テキストを過去の勝ちパターン・失注理由と照合して診断する"
    input_schema: ClassVar[type[BaseModel]] = ProposalReviewInput
    output_schema: ClassVar[type[BaseModel]] = ProposalReviewOutput

    def __init__(
        self,
        search: SearchSkill | None = None,
        bedrock: BedrockClient | None = None,
        *,
        prompt_version: str = "v1",
        summary_max_tokens: int = 1200,
        retrieve_top_k: int = 6,
    ) -> None:
        self._search = search or SearchSkill()
        self._bedrock = bedrock or BedrockClient.from_env()
        self._prompt_version = prompt_version
        self._summary_max_tokens = summary_max_tokens
        self._retrieve_top_k = retrieve_top_k

    def run(self, input: ProposalReviewInput, ctx: SkillContext) -> ProposalReviewOutput:
        log = ctx.bind_logger(self.name)
        log.info("proposal_review_start", text_len=len(input.proposal_text))

        # 1. 提案文をクエリに、照合用の類似過去提案/FB を取得 (検索基盤を再利用)。
        #    クエリは長すぎると埋め込みが鈍るため先頭を使う。
        query = input.proposal_text[:500]
        hits = self._search.retrieve_hits(
            query,
            ctx,
            top_k=self._retrieve_top_k,
            filter_industry=input.industry,
        )

        # 2. レビューを生成 (過去事例が 0 件でもレビュー自体は行う)
        review, cost_usd = self._review(input.proposal_text, hits, ctx.request_id)

        sources = [self._to_source(h) for h in hits]
        log.info("proposal_review_done", source_count=len(sources), cost_usd=cost_usd)
        return ProposalReviewOutput(
            review=review,
            sources=sources,
            source_count=len(sources),
            total_cost_usd=cost_usd,
        )

    @staticmethod
    def _format_ref(hit: SearchHit) -> str:
        meta = hit.metadata or {}
        label = meta.get("title") or meta.get("client_name") or ""
        return f"[chunk_id: {hit.chunk_id}] {label}\n{hit.content[:300]}"

    @staticmethod
    def _to_source(hit: SearchHit) -> ReviewSource:
        meta = hit.metadata or {}
        return ReviewSource(
            chunk_id=hit.chunk_id,
            source_type=meta.get("source_type"),
            title=meta.get("title"),
            client_name=meta.get("client_name"),
            preview=hit.content[:160],
        )

    def _review(
        self, proposal_text: str, hits: list[SearchHit], request_id: str
    ) -> tuple[str, float]:
        system = load_prompt("proposal_review", self._prompt_version, "system")

        if hits:
            ref_block = "\n\n".join(self._format_ref(h) for h in hits)
            ref_section = f"# 照合用: 過去の類似提案 / 営業 FB（{len(hits)} 件）\n{ref_block}\n\n"
        else:
            ref_section = (
                "# 照合用の過去提案\n"
                "（類似事例が見つかりませんでした。一般原則で診断してください）\n\n"
            )

        user_message = (
            f"# レビュー対象の提案\n{proposal_text}\n\n"
            f"{ref_section}"
            "上記の提案を、過去の勝ち筋・失注理由と照合して、"
            "フォーマットに従って診断してください。"
        )

        resp = self._bedrock.converse(
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            request_id=request_id,
            system=system,
            cache_system=True,
            max_tokens=self._summary_max_tokens,
        )
        return resp.text, resp.usage.cost_usd
