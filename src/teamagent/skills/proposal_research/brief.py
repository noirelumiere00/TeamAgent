"""既存の提案入力から独立した調査の依頼。"""

from __future__ import annotations

import unicodedata
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    product_name: str = Field(min_length=1, max_length=200, description="調査する商材名")
    official_url: str | None = Field(
        default=None, description="公式URL。無ければ省略または「なし」"
    )
    brief: str = Field(
        default="", max_length=4000, description="ターゲット・目的・訴求・制約の与件"
    )
    unreleased: bool = Field(default=False, description="未発表商材は商品名で検索しない")
    category_term: str | None = Field(
        default=None, max_length=200, description="未発表時の検索カテゴリ語"
    )

    @field_validator("product_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip()
        if not value:
            raise ValueError("product_name must not be blank")
        return value

    @field_validator("official_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None or value in ("", "なし"):
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or any(char.isspace() for char in value)
        ):
            raise ValueError("official_url must use HTTP(S)")
        return value

    @model_validator(mode="after")
    def require_category_for_unreleased(self) -> Self:
        if self.unreleased and (not self.category_term or not self.category_term.strip()):
            raise ValueError("未発表商材の調査には category_term が必要です")
        return self
