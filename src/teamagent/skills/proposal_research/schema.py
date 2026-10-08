"""URL の代わりに出典番号を受け取る中間型と調査結果。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from teamagent.skills.proposal_builder.schema import InsightEvidence, ProductMeta
from teamagent.skills.proposal_research.brief import ResearchBrief


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IntermediateProductMeta(ProductMeta):
    kaiwai_keywords: list[str] = Field(min_length=8, max_length=12)


class IntermediateAlternative(StrictModel):
    headline: str
    analysis: str
    url: str


class IntermediateMarket(StrictModel):
    theme: str
    headline: str
    analysis: str
    url: str
    source_name: str
    alt_data: list[IntermediateAlternative] = Field(min_length=3, max_length=3)


class IntermediateSocial(IntermediateMarket):
    alt_data: list[IntermediateAlternative] = Field(max_length=3)


class IntermediateTikTok(StrictModel):
    related_tag: str
    representative_post_url: str
    search_demand_note: str
    total_count: str


class IntermediatePublicity(StrictModel):
    trend_word: str
    article_count_500days: str
    evidence_url: str
    recommended_media: list[str]


class IntermediateTag(StrictModel):
    tag: str
    representative_post_url: str


class IntermediateCommunity(StrictModel):
    name: str
    estimated_population: str
    calculation: str
    data_url: str
    tiktok_tags: list[IntermediateTag]


class IntermediateCompetitor(StrictModel):
    name: str
    target: str
    core_concept: str
    features: str
    positioning: str
    url: str


class IntermediateEvent(StrictModel):
    overview: str
    scale: str
    sns_reality: str
    benchmark_case: str
    url: str


class IntermediateResearch(StrictModel):
    research_date: str
    brand: str
    product_meta: IntermediateProductMeta
    a_market_data: list[IntermediateMarket] = Field(
        alias="A_market_data", min_length=3, max_length=3
    )
    b_social_trend: list[IntermediateSocial] = Field(
        alias="B_social_trend", min_length=3, max_length=3
    )
    c_tiktok: list[IntermediateTikTok] = Field(alias="C_tiktok", default_factory=list)
    d_publicity: list[IntermediatePublicity] = Field(
        alias="D_publicity", min_length=6, max_length=8
    )
    e_community: list[IntermediateCommunity] = Field(
        alias="E_community", min_length=3, max_length=3
    )
    f_competitor: list[IntermediateCompetitor] = Field(alias="F_competitor", max_length=3)
    # v3 の G は URL 欄が無いため、各文字列の [S番号] をコードが引用へ置換する。
    g_insight: InsightEvidence = Field(alias="G_insight")
    h_event: IntermediateEvent = Field(alias="H_event")


class ResearchSummary(StrictModel):
    source_count: int = Field(ge=0)
    discarded_count: int = Field(ge=0)
    discarded_by_section: dict[str, int] = Field(default_factory=dict)
    elapsed_seconds: float = Field(ge=0)
    gemini_cost_usd: float = Field(ge=0)
    tiktok_search_count: int = Field(ge=0)


class ProposalResearchOutput(StrictModel):
    research_json: dict[str, Any]
    summary: ResearchSummary


__all__ = ["IntermediateResearch", "ProposalResearchOutput", "ResearchBrief", "ResearchSummary"]
