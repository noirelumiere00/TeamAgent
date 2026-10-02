"""事例レコード v1 の契約（pydantic・strict）。

tag 語彙は青木 事例DB.json（``_meta.tag_vocab``）を **そのまま定数として写す**。語彙外の値は
抽出層（extract.py）が ``その他`` へ倒す。この schema は「語彙 ∪ {その他}」以外を拒む
（ここを緩めると LLM の自由記述が similar_keys に混ざり、類似事例が引けなくなる）。

数値（``metrics[].value``）は本文に現れる **文字列そのまま** を持つ（LLM に計算させない）。
出典（``metrics[].source_url`` / ``sources[]``）は元文書に固定する。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CASE_RECORD_VERSION: Final = 1

#: 語彙外の値の受け皿。全 kind で共通。
OTHER: Final = "その他"

# --- 青木 事例DB.json ``_meta.tag_vocab`` の写し（順序も維持・2026-09-16 時点） ---
PURPOSE_VOCAB: Final[tuple[str, ...]] = (
    "認知拡大",
    "売上・POS",
    "指名検索・VSEO",
    "来店集客",
    "EC・CV",
    "採用",
    "話題化・動員",
)
SECTOR_VOCAB: Final[tuple[str, ...]] = (
    "飲料食品",
    "日用品美容",
    "高単価D2C",
    "飲食店",
    "金融サービス",
    "エンタメIP",
    "BtoB",
    "教育",
    "小売アパレル",
    "レジャー観光",
    "不動産住宅",
)
PRODUCT_STATE_VOCAB: Final[tuple[str, ...]] = (
    "新商品",
    "既存",
    "リニューアル",
    "イベント",
)
CHANNEL_VOCAB: Final[tuple[str, ...]] = (
    "EC",
    "店頭小売",
    "実店舗来店",
    "オンライン申込",
    "予約",
    "クラファン",
)
TRAITS_VOCAB: Final[tuple[str, ...]] = (
    "薬機・景表規制",
    "発表会連動",
    "セール連動",
    "検証型",
    "切り抜き型",
    "INFタイアップ",
    "コラボコース",
    "記者発表会",
    "ポップアップ連動",
)

TAG_VOCAB: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "purpose": PURPOSE_VOCAB,
        "sector": SECTOR_VOCAB,
        "product_state": PRODUCT_STATE_VOCAB,
        "channel": CHANNEL_VOCAB,
        "traits": TRAITS_VOCAB,
    }
)

ExternalUse = Literal["ok", "ng", "unknown"]


def allowed_values(kind: str) -> frozenset[str]:
    """``kind`` の語彙 ∪ {その他}。未知の kind は KeyError。"""
    return frozenset(TAG_VOCAB[kind]) | {OTHER}


def _check_one(kind: str, value: str) -> str:
    if value not in allowed_values(kind):
        raise ValueError(f"{kind} は語彙外です: {value!r}")
    return value


def _check_many(kind: str, values: list[str]) -> list[str]:
    seen: set[str] = set()
    for value in values:
        _check_one(kind, value)
        if value in seen:
            raise ValueError(f"{kind} に重複があります: {value!r}")
        seen.add(value)
    return values


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CaseMetric(_StrictModel):
    """本文中の数値 1 つ。``value`` は本文の文字列そのまま（換算・丸め禁止）。"""

    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100)
    unit: str = Field(default="", max_length=40)
    source_url: str = Field(min_length=1, max_length=2000)


class CaseSource(_StrictModel):
    """元 document への参照（引用・検証用）。"""

    external_id: str = Field(min_length=1, max_length=500)
    url: str = Field(default="", max_length=2000)
    excerpt: str = Field(default="", max_length=1000)


class CasePeriod(_StrictModel):
    """施策期間（本文表記の文字列。空なら不明）。"""

    start: str = Field(default="", max_length=40)
    end: str = Field(default="", max_length=40)


def build_similar_keys(
    *,
    sector: str,
    purpose: list[str],
    product_state: str,
    traits: list[str],
) -> list[str]:
    """類似事例の第一鍵（sector＋purpose＋product_state＋traits の組・決定論）。"""
    keys = [f"sector:{sector}"]
    keys.extend(f"purpose:{value}" for value in purpose)
    keys.append(f"product_state:{product_state}")
    keys.extend(f"traits:{value}" for value in traits)
    return keys


class CaseRecord(_StrictModel):
    """事例レコード v1。"""

    case_id: str = Field(min_length=1, max_length=600)
    case_group: str = Field(min_length=1, max_length=600)
    client_internal: str = Field(default="", max_length=200)
    client_masked: str = Field(min_length=1, max_length=200)
    sector: str
    purpose: list[str] = Field(min_length=1, max_length=len(PURPOSE_VOCAB) + 1)
    product_state: str
    channel: list[str] = Field(default_factory=list, max_length=len(CHANNEL_VOCAB) + 1)
    traits: list[str] = Field(default_factory=list, max_length=len(TRAITS_VOCAB) + 1)
    product: str = Field(default="", max_length=300)
    scale: str = Field(default="", max_length=300)
    period: CasePeriod = Field(default_factory=CasePeriod)
    metrics: list[CaseMetric] = Field(default_factory=list, max_length=50)
    result_masked: str = Field(default="", max_length=2000)
    winpattern: str = Field(default="", max_length=2000)
    similar_keys: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list, max_length=30)
    external_use: ExternalUse = "unknown"
    sources: list[CaseSource] = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)
    reviewed: bool = False

    @field_validator("sector")
    @classmethod
    def _sector_in_vocab(cls, value: str) -> str:
        return _check_one("sector", value)

    @field_validator("product_state")
    @classmethod
    def _product_state_in_vocab(cls, value: str) -> str:
        return _check_one("product_state", value)

    @field_validator("purpose")
    @classmethod
    def _purpose_in_vocab(cls, value: list[str]) -> list[str]:
        return _check_many("purpose", value)

    @field_validator("channel")
    @classmethod
    def _channel_in_vocab(cls, value: list[str]) -> list[str]:
        return _check_many("channel", value)

    @field_validator("traits")
    @classmethod
    def _traits_in_vocab(cls, value: list[str]) -> list[str]:
        return _check_many("traits", value)

    @field_validator("competitors")
    @classmethod
    def _competitors_not_blank(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 200 for item in value):
            raise ValueError("competitors は空白でない 200 文字以内の文字列")
        return value

    @model_validator(mode="after")
    def _derive_similar_keys(self) -> CaseRecord:
        # similar_keys は入力ではなく導出値。常に構造 4 項目から組み直す（不整合を残さない）。
        derived = build_similar_keys(
            sector=self.sector,
            purpose=self.purpose,
            product_state=self.product_state,
            traits=self.traits,
        )
        if self.similar_keys != derived:
            object.__setattr__(self, "similar_keys", derived)
        return self


__all__ = [
    "CASE_RECORD_VERSION",
    "CHANNEL_VOCAB",
    "OTHER",
    "PRODUCT_STATE_VOCAB",
    "PURPOSE_VOCAB",
    "SECTOR_VOCAB",
    "TAG_VOCAB",
    "TRAITS_VOCAB",
    "CaseMetric",
    "CasePeriod",
    "CaseRecord",
    "CaseSource",
    "ExternalUse",
    "allowed_values",
    "build_similar_keys",
]
