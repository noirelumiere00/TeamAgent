"""Gemini v3 research payload schemas for proposal-builder."""

from __future__ import annotations

import json
import unicodedata
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    """Reject schema drift instead of silently inventing or discarding research data."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class _GeminiEvidenceModel(_StrictModel):
    """Gemini research object whose URL fields must be concrete HTTP(S) evidence."""

    @field_validator(
        "url",
        "representative_post_url",
        "evidence_url",
        "data_url",
        check_fields=False,
    )
    @classmethod
    def _evidence_url_is_http(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            value != value.strip()
            or any(char.isspace() for char in value)
            or parsed.scheme.lower() not in {"http", "https"}
            or "." not in host
            or ".." in host
            or "..." in value
            or "…" in value
        ):
            raise ValueError("evidence URL must be a concrete HTTP(S) URL")
        return value


class ProductMeta(_StrictModel):
    """Selector metadata emitted by the Gemini v3 prompt."""

    sector: str
    purpose: list[str] = Field(min_length=1)
    product_state: str
    channel: list[str] = Field(min_length=1)
    regulation: bool
    moment: str
    target_categories: list[str] = Field(min_length=1)
    kaiwai_keywords: list[str] = Field(min_length=1)

    @field_validator(
        "sector",
        "product_state",
        "moment",
    )
    @classmethod
    def _required_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("product_meta required text must not be blank")
        return value

    @field_validator("purpose", "channel", "target_categories", "kaiwai_keywords")
    @classmethod
    def _vocabulary_is_not_blank(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("product_meta vocabulary entries must not be blank")
        return value


class AlternativeEvidence(_GeminiEvidenceModel):
    """Alternative evidence attached to a market/social finding."""

    headline: str
    analysis: str
    url: str


class MarketEvidence(_GeminiEvidenceModel):
    """A_market_data entry."""

    theme: str
    headline: str
    analysis: str
    url: str
    source_name: str
    alt_data: list[AlternativeEvidence]


class SocialTrendEvidence(_GeminiEvidenceModel):
    """B_social_trend entry."""

    theme: str
    headline: str
    analysis: str
    url: str
    source_name: str
    alt_data: list[AlternativeEvidence]


class TikTokEvidence(_GeminiEvidenceModel):
    """C_tiktok entry; total_count normally states that the UI does not expose a total."""

    related_tag: str
    representative_post_url: str
    search_demand_note: str
    total_count: str

    @field_validator("total_count")
    @classmethod
    def _total_count_is_unavailable_marker(cls, value: str) -> str:
        if value != "取得不可（UI非表示）":
            raise ValueError("total_count must be 取得不可（UI非表示）")
        return value


class PublicityEvidence(_GeminiEvidenceModel):
    """D_publicity entry."""

    trend_word: str
    article_count_500days: str
    evidence_url: str
    recommended_media: list[str]


class CommunityTagEvidence(_GeminiEvidenceModel):
    """Representative TikTok tag nested in E_community."""

    tag: str
    representative_post_url: str


class CommunityEvidence(_GeminiEvidenceModel):
    """E_community entry."""

    name: str
    estimated_population: str
    calculation: str
    data_url: str
    tiktok_tags: list[CommunityTagEvidence]


class CompetitorEvidence(_GeminiEvidenceModel):
    """F_competitor entry."""

    name: str
    target: str
    core_concept: str
    features: str
    positioning: str
    url: str


class InsightEvidence(_StrictModel):
    """G_insight entry."""

    complaint_pattern: str
    complaint_example: str
    desire_pattern: str
    desire_example: str


class EventEvidence(_GeminiEvidenceModel):
    """H_event entry."""

    overview: str
    scale: str
    sns_reality: str
    benchmark_case: str
    url: str


class GeminiResearch(_StrictModel):
    """Complete Gemini v3 payload.

    Alias-backed lower-case attributes avoid leaking prompt-specific capitalisation
    into Python callers while preserving the exact A-H wire schema.
    """

    research_date: str
    brand: str = Field(min_length=1, max_length=200)
    product_meta: ProductMeta
    a_market_data: list[MarketEvidence] = Field(alias="A_market_data", min_length=1)
    b_social_trend: list[SocialTrendEvidence] = Field(alias="B_social_trend", min_length=1)
    c_tiktok: list[TikTokEvidence] = Field(alias="C_tiktok", min_length=1)
    d_publicity: list[PublicityEvidence] = Field(alias="D_publicity", min_length=1)
    e_community: list[CommunityEvidence] = Field(alias="E_community", min_length=1)
    f_competitor: list[CompetitorEvidence] = Field(alias="F_competitor")
    g_insight: InsightEvidence = Field(alias="G_insight")
    h_event: EventEvidence = Field(alias="H_event")

    @field_validator("research_date")
    @classmethod
    def _research_date_is_iso(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("research_date must use YYYY-MM-DD") from None
        return value

    @field_validator("brand")
    @classmethod
    def _brand_is_not_blank(cls, value: str) -> str:
        if (
            not value.strip()
            or value != value.strip()
            or value != unicodedata.normalize("NFKC", value)
        ):
            raise ValueError(
                "brand must be non-blank, whitespace-trimmed NFKC text"
            )
        return value


class EvidenceReference(_GeminiEvidenceModel):
    """One syntactically valid HTTP(S) evidence URL found in the payload."""

    object_path: str
    path: str
    url: str


class EvidenceRegistry(_StrictModel):
    """Evidence URLs keyed by their nearest containing JSON object."""

    references: list[EvidenceReference] = Field(default_factory=list)

    def urls_for_object(self, object_path: str) -> tuple[str, ...]:
        """Return unique URLs owned by one JSON object, preserving source order."""

        return tuple(
            dict.fromkeys(ref.url for ref in self.references if ref.object_path == object_path)
        )


class ResearchIssue(_StrictModel):
    """A proposal-safety issue without echoing the raw research value."""

    path: str
    code: str
    message: str


class SanitizationResult(_StrictModel):
    """Non-mutating provenance sanitization output."""

    sanitized: dict[str, object]
    evidence_registry: EvidenceRegistry
    issues: list[ResearchIssue] = Field(default_factory=list)

    @property
    def is_draft(self) -> bool:
        """Any unresolved numeric claim keeps the proposal in draft state."""

        return bool(self.issues)


class ProposalBuilderInput(_StrictModel):
    """Slack/MCPから一度で提案書を生成する統合入力。"""

    # MCP JSONのISO日付文字列をdateへ変換するため、この外部入力境界だけstrict=False。
    # Gemini本文は parse_gemini_research() が改めてstrict検証する。
    # NOTE: pydantic の model_config は親 (_StrictModel: strict=True) とキー単位で
    # マージされるため、ここで strict=False を「明示」しないと親の strict=True が
    # 残り、MCP 経由の全呼び出しが date 文字列で必ず ValidationError になる（実測）。
    model_config = ConfigDict(extra="forbid", strict=False)

    gemini_json: dict[str, Any] | str = Field(
        description=(
            "Gemini v3の統合JSONオブジェクト、またはそのJSON文字列。"
            "文字列は構文のみ限定修復し、内容は補完しない。"
        )
    )
    posting_start_date: date = Field(description="投稿開始日D（YYYY-MM-DD）")
    client_name: str = Field(
        default="クライアント",
        min_length=1,
        max_length=200,
        description="統合FMTの宛名。未指定時は汎用表記「クライアント」。",
    )
    category_term: str = Field(
        default="",
        max_length=200,
        description="守秘案件で商品名の代わりに使うカテゴリ語",
    )
    target_persona: str = Field(
        default="",
        max_length=1000,
        description="Gemini target_categoriesを補足する主・拡張ターゲット",
    )
    proposal_brief: str = Field(
        default="",
        max_length=4000,
        description="訴求の核、キーロジック、案件固有の与件",
    )
    constraints: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="守秘、薬機・景表、同時展開禁止等の制約",
    )
    official_urls: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="公式サイト/LP等、Composerが証拠として利用可能なHTTP(S) URL",
    )
    confidential_product_name: bool = Field(
        default=False,
        description="Trueなら本文中の商品名をカテゴリ語/本商品へ置き換える",
    )
    case_limit: int = Field(default=3, ge=1, le=3, description="RAGから採用する事例数")
    max_repair: int = Field(
        default=4,
        ge=0,
        le=4,
        description="Composer自己修復回数（同期runtime attestationの最大5回呼出しに固定）",
    )

    @field_validator("gemini_json")
    @classmethod
    def _gemini_payload_bounded(cls, value: dict[str, Any] | str) -> dict[str, Any] | str:
        encoded = (
            value.encode("utf-8")
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if not encoded or len(encoded) > 256 * 1024:
            raise ValueError("gemini_json must be between 1 byte and 256 KiB")
        return value

    @field_validator("constraints")
    @classmethod
    def _constraints_bounded(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 1000 for item in value):
            raise ValueError("each constraint must contain 1-1000 characters")
        return value

    @field_validator("client_name")
    @classmethod
    def _client_name_is_trimmed(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("client_name must be non-blank and whitespace-trimmed")
        return value

    @field_validator("official_urls")
    @classmethod
    def _official_urls_are_http(cls, value: list[str]) -> list[str]:
        for raw in value:
            if raw != raw.strip() or any(char.isspace() for char in raw):
                raise ValueError("official_urls must be exact whitespace-free URLs")
            parsed = urlsplit(raw)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                raise ValueError("official_urls must use HTTP(S)")
        return value


class ProposalBuilderCaseReference(_StrictModel):
    """生成物へ採用したRAG事例のトレース情報。"""

    source: Literal["report_rag", "general_news-tv"]
    title: str
    url: str | None = None


class ProposalBuilderOutput(_StrictModel):
    """統合生成結果。生のGemini JSON/アカウントDBは返さない。"""

    status: Literal["ready", "draft"]
    message: str
    pptx_url: str | None = None
    version_id: str
    filled_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0.0, le=1.0)
    skipped_ids: list[int] = Field(default_factory=list)
    selected_account_names: list[str] = Field(default_factory=list)
    case_references: list[ProposalBuilderCaseReference] = Field(default_factory=list)
    verification_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    slack_delivered: bool = False
    delivery_target: Literal["thread", "dm", "none"] = "none"
    total_cost_usd: float = Field(ge=0.0)


__all__ = [
    "AlternativeEvidence",
    "CommunityEvidence",
    "CommunityTagEvidence",
    "CompetitorEvidence",
    "EventEvidence",
    "EvidenceReference",
    "EvidenceRegistry",
    "GeminiResearch",
    "InsightEvidence",
    "MarketEvidence",
    "ProductMeta",
    "ProposalBuilderCaseReference",
    "ProposalBuilderInput",
    "ProposalBuilderOutput",
    "PublicityEvidence",
    "ResearchIssue",
    "SanitizationResult",
    "SocialTrendEvidence",
    "TikTokEvidence",
]
