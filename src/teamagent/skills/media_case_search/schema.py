"""Bounded inputs and evidence-only distillation; no inferred client engagements."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MediaCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    accounts: list[str] = Field(default_factory=list, max_length=10)
    posted_from: date | None = None
    posted_to: date | None = None
    pr_only: bool = False
    limit: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def valid_range(self) -> MediaCaseInput:
        if not self.query.strip():
            raise ValueError("query must not be blank")
        self.accounts = list(dict.fromkeys(self.accounts))
        if self.posted_from and self.posted_to and self.posted_from > self.posted_to:
            raise ValueError("invalid date range")
        return self


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["caption", "hashtag", "thumbnail", "subtitle", "slide"]
    excerpt: str = Field(min_length=1, max_length=200)
    image_index: int | None = Field(default=None, ge=0, le=10)


class CaseItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brand: str | None = Field(default=None, max_length=100)
    product: str | None = Field(default=None, max_length=100)
    campaign: str | None = Field(default=None, max_length=100)
    campaign_tags: list[str] = Field(default_factory=list, max_length=5)
    facility: str | None = Field(default=None, max_length=100)
    category: Literal[
        "旅行", "宿泊", "交通", "飲食", "食品", "飲料", "美容", "家電", "娯楽", "その他", "不明"
    ] = "不明"
    campaign_types: list[
        Literal[
            "campaign", "new_product", "event", "store_opening", "brand_intro", "other", "unknown"
        ]
    ] = Field(default_factory=list, max_length=6)
    geography: Literal["domestic", "overseas", "mixed", "unknown"] = "unknown"
    summary: str = Field(max_length=200)
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)


class Distillation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_roundup: bool
    contains_personal_names: bool
    items: list[CaseItem] = Field(max_length=10)
    pr_evidence: list[Evidence] = Field(default_factory=list, max_length=5)


class MediaCaseOutput(BaseModel):
    message: str
    status: Literal["ready", "queued", "running", "done", "failed"] = "ready"
    job_id: str = ""
    deduplicated: bool = False
    total_cost_usd: float = 0.0
