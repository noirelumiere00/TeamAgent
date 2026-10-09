"""既存の提案入力から独立した調査の依頼。"""

from __future__ import annotations

import unicodedata
from itertools import groupby
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
        default="",
        max_length=4000,
        description="ターゲット・目的・訴求・制約の与件（未発表のときは調査に使わない）",
    )
    unreleased: bool = Field(
        default=False,
        description="未発表商材は商品名も与件も調査に渡さず、category_term だけで調べる",
    )
    category_term: str | None = Field(
        default=None, max_length=200, description="未発表時の検索カテゴリ語"
    )

    @field_validator("product_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = "".join(
            "".join(chars) if is_symbol else unicodedata.normalize("NFKC", "".join(chars))
            for is_symbol, chars in groupby(
                value, key=lambda char: unicodedata.category(char)[0] == "S"
            )
        ).strip()
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
        if self.unreleased and _folded(self.product_name) in _folded(self.category_term or ""):
            # 未発表のときに調査へ渡るのは category_term だけ。
            # ここに名前が入っていたら受付で断る。
            raise ValueError("未発表商材の category_term に商品名が含まれています")
        return self


def _folded(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())
