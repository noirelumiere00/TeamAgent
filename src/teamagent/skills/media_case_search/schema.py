"""Bounded inputs and evidence-only distillation; no inferred client engagements."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)


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


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for name, field in cls.model_fields.items():
            if value.get(name) is None and not field.is_required():
                value.pop(name, None)
            text = value.get(name)
            for constraint in field.metadata:
                limit = getattr(constraint, "max_length", None)
                if isinstance(text, str) and limit is not None and len(text) > limit:
                    value[name] = text[:limit]
                    if info.context is not None:
                        info.context.setdefault("truncated_fields", []).append(name)
                        info.context.setdefault("truncated_texts", []).append(text)
        return value


class Evidence(ExtractionModel):
    source: Literal["caption", "hashtag", "thumbnail", "subtitle", "slide"]
    excerpt: str = Field(min_length=1, max_length=200)
    image_index: int | None = Field(default=None, ge=0, le=10)
    _original_excerpt: str = PrivateAttr(default="")

    @model_validator(mode="wrap")
    @classmethod
    def preserve_excerpt(cls, value: Any, handler: ModelWrapValidatorHandler[Evidence]) -> Evidence:
        parsed = handler(value)
        # Check the entire quote before truncation, including any invented suffix.
        if isinstance(value, dict):
            parsed._original_excerpt = value.get("excerpt", "")
        return parsed

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip().lower()
            return {"hashtags": "hashtag", "subtitles": "subtitle", "cover": "thumbnail"}.get(
                value, value
            )
        return value


class CaseItem(ExtractionModel):
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
    summary: str = Field(default="", max_length=200)
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)

    @field_validator("geography", mode="before")
    @classmethod
    def normalize_geography(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip().lower()
            return {"国内": "domestic", "海外": "overseas", "不明": "unknown"}.get(value, value)
        return value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip().lower()
            return {
                "travel": "旅行",
                "food": "食品",
                "beauty": "美容",
                "other": "その他",
                "unknown": "不明",
            }.get(value, value)
        return value

    @field_validator("campaign_types", mode="before")
    @classmethod
    def normalize_campaign_types(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        aliases = {
            "cp": "campaign",
            "キャンペーン": "campaign",
            "新商品": "new_product",
            "イベント": "event",
            "ブランド紹介": "brand_intro",
            "その他": "other",
            "不明": "unknown",
        }
        return [
            aliases.get(v.strip().lower(), v.strip().lower().replace("-", "_").replace(" ", "_"))
            if isinstance(v, str)
            else v
            for v in value
        ]


class Distillation(ExtractionModel):
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
