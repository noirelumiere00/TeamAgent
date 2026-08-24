"""FMT deck spec（deck_spec_fmt_v1）の読み込みとスキーマ検証。

資料の見た目（トークン・8類型・画像規律・固定文言）は
``teamagent/assets/deck_specs/omiyage_fmt_v1.json`` が正で、レンダラは
この spec を解釈して描くだけにする（コードへの焼き込み禁止）。
spec 差し替え時はスキーマ検証とゴールデンテストが仕様書の役割を果たす。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

_ASSET_ROOT = Path(__file__).resolve().parents[3] / "assets" / "deck_specs"

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]
RgbaColor = Annotated[str, StringConstraints(pattern=r"^rgba\([0-9., ]+\)$")]

# 便1で描画対象になる類型（spec の ben1_deck_composition と一致していることを検証する）
BEN1_SLIDE_TYPES = ("A", "B", "C", "D", "E", "H")


class FmtSpecError(ValueError):
    """spec JSON がレンダラ契約を満たさない（fail-fast）。"""


class _Lenient(BaseModel):
    """spec には _note 等の説明キーが多いので、未モデル化キーは許容する。"""

    model_config = ConfigDict(extra="allow", frozen=True)


class _ColorToken(_Lenient):
    value: HexColor


class _BrandAccent(_Lenient):
    value: HexColor


class _BrandAccents(_Lenient):
    brand_a: _BrandAccent
    brand_b: _BrandAccent


class _ChapterDarkWhites(_Lenient):
    text_dim: RgbaColor
    text_mid: RgbaColor
    text_strong: RgbaColor
    rule: RgbaColor


class SpecColors(_Lenient):
    paper: _ColorToken
    ink: _ColorToken
    dark: _ColorToken
    panel: _ColorToken
    body: _ColorToken
    body_sub: _ColorToken
    lead: _ColorToken
    muted: _ColorToken
    rule_strong: _ColorToken
    rule_row: _ColorToken
    rule_card: _ColorToken
    hairline: _ColorToken
    track: _ColorToken
    neutral_bar: _ColorToken
    brand_accents: _BrandAccents
    chapter_dark_whites: _ChapterDarkWhites


class _FontDef(_Lenient):
    family: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    weights: tuple[int, ...] = Field(min_length=1)


class _Fonts(_Lenient):
    mincho: _FontDef
    gothic: _FontDef
    latin: _FontDef


class SpecTypography(_Lenient):
    fonts: _Fonts


class SpecTokens(_Lenient):
    colors: SpecColors
    typography: SpecTypography


class _FooterStrip(_Lenient):
    left: str
    right: str


class SpecLayout(_Lenient):
    side_margin_px: int = Field(ge=0, le=400)
    footer_strip: _FooterStrip


class _ImageSize(_Lenient):
    w: int = Field(ge=16, le=1920)
    h: int = Field(ge=16, le=1080)


class _ImageSizes(_Lenient):
    cover_pair: _ImageSize
    ranking_card: _ImageSize
    example_panel_s: _ImageSize
    example_panel_m: _ImageSize


class _EmbedBudget(_Lenient):
    per_image_max_kb: int = Field(ge=1, le=2048)
    deck_total_max_kb: int = Field(ge=1, le=2048)


class _ImageKind(_Lenient):
    enum: tuple[str, ...] = Field(min_length=1)


class _SourceUrlRule(_Lenient):
    required: bool


class SpecImageRules(_Lenient):
    aspect: str
    sizes_px: _ImageSizes
    embed_budget: _EmbedBudget
    image_kind: _ImageKind
    source_url: _SourceUrlRule

    @model_validator(mode="after")
    def _contain_only(self) -> SpecImageRules:
        fit = getattr(self, "fit_policy", None)
        mode = fit.get("mode") if isinstance(fit, dict) else None
        if mode != "contain":
            raise ValueError("image_rules.fit_policy.mode must be 'contain'")
        if "provided_thumbnail" not in self.image_kind.enum:
            raise ValueError("image_rules.image_kind.enum must include 'provided_thumbnail'")
        if not self.source_url.required:
            raise ValueError("image_rules.source_url.required must be true")
        return self


class _Cta(_Lenient):
    text: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    required_when: str


class _SlideTypeH(_Lenient):
    cta: _Cta


class SpecSlideTypes(_Lenient):
    # A〜G は自由構造（テンプレートは layout_canon 由来でコード側にある）だが、
    # H の CTA 固定文はレンダラが埋める固定文言なので必須で検証する。
    H: _SlideTypeH


class _MethodConstraints(_Lenient):
    ben1_required_text: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class _PrLabels(_Lenient):
    canonical: tuple[str, ...] = Field(min_length=2)
    alternative: str


class SpecTextRules(_Lenient):
    method_and_constraints: _MethodConstraints
    pr_labels: _PrLabels


class _Ben1Composition(_Lenient):
    slide_types_used: tuple[str, ...] = Field(min_length=1)


class SpecDataRequirements(_Lenient):
    ben1_deck_composition: _Ben1Composition


class _Canvas(_Lenient):
    width_px: int
    height_px: int
    min_font_size_px: int = Field(ge=1)


class SpecMeta(_Lenient):
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    canvas: _Canvas


class FmtDeckSpec(_Lenient):
    meta: SpecMeta
    tokens: SpecTokens
    layout: SpecLayout
    image_rules: SpecImageRules
    slide_types: SpecSlideTypes
    text_rules: SpecTextRules
    prohibitions: dict[str, object]
    data_requirements: SpecDataRequirements

    @model_validator(mode="after")
    def _validate_contract(self) -> FmtDeckSpec:
        if (self.meta.canvas.width_px, self.meta.canvas.height_px) != (1920, 1080):
            raise ValueError("canvas must be 1920x1080")
        if self.meta.canvas.min_font_size_px != 24:
            raise ValueError("min_font_size_px must be 24")
        if tuple(self.data_requirements.ben1_deck_composition.slide_types_used) != tuple(
            BEN1_SLIDE_TYPES
        ):
            raise ValueError("ben1_deck_composition.slide_types_used must be A/B/C/D/E/H")
        budget = self.image_rules.embed_budget
        if budget.per_image_max_kb > budget.deck_total_max_kb:
            raise ValueError("embed_budget per-image exceeds deck total")
        return self

    # ------------------------------------------------------------------
    # レンダラが頻用する導出値
    # ------------------------------------------------------------------

    @property
    def cta_text(self) -> str:
        return self.slide_types.H.cta.text

    @property
    def ben1_required_text(self) -> str:
        return self.text_rules.method_and_constraints.ben1_required_text

    @property
    def min_font_px(self) -> int:
        return self.meta.canvas.min_font_size_px

    def color(self, name: str) -> str:
        token = getattr(self.tokens.colors, name)
        value = token.value
        if not isinstance(value, str):  # pragma: no cover - 型ガード
            raise FmtSpecError(f"color token {name!r} is invalid")
        return value


def fmt_spec_path(name: str = "omiyage_fmt_v1") -> Path:
    if not name.replace("_", "").replace("-", "").isalnum():
        raise FmtSpecError(f"invalid fmt spec name: {name!r}")
    return _ASSET_ROOT / f"{name}.json"


@lru_cache(maxsize=4)
def load_fmt_spec(name: str = "omiyage_fmt_v1") -> FmtDeckSpec:
    """同梱 spec JSON を読み込み・スキーマ検証して返す（プロセス内キャッシュ）。"""
    path = fmt_spec_path(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FmtSpecError(f"fmt deck spec not found: {path}") from exc
    except ValueError as exc:
        raise FmtSpecError(f"fmt deck spec is not valid JSON: {path}") from exc
    try:
        return FmtDeckSpec.model_validate(data)
    except ValueError as exc:
        raise FmtSpecError(f"fmt deck spec failed schema validation: {exc}") from exc
