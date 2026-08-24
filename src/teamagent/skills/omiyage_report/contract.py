"""お土産資料デッキの計測JSON契約（deck content contract）。

正本は deck_spec_fmt_v1.json の ``input_contract``（repo 同梱コピー:
``assets/deck_specs/omiyage_fmt_v1.json``）。エンジンは本契約の
``DeckPlan``（deck_meta + slide_plan）だけを出力し、レンダラはそれ**だけ**を
入力に描く（レンダラ無作文原則: heading/lead/tag/summary_rows の文言は
エンジン側が実測数値から決定論で作文して渡す）。

2026-08-24 ユーザー裁定の反映:
- U1: Q1〜Q5 目次維持。A → B(PART1) → 露出シェア導入(Q無し) → Q1(D) → Q2(C)
  → Q3(C・動画解析由来) → Q4(D) → Q5(E) → H(CTA固定文をdark結論バンド内)。
- 計測経路: caption/hashtag/telop の3経路。voice のみ未計測を制約行に明記。
- U2: FMT語彙（EG率/UGC等）は初出スライドの脚注で1回だけ定義注記。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SPEC_NAME = "fmt_tiktok_search_deck"
SPEC_VERSION = "1.1.0"

# CTA固定文（SKILL/CANON 正本・C3裁定: H類型 dark結論バンド内に配置）
CTA_TEXT = "上位10本の冒頭・価格・商品説明まで詳しく比較した事例が必要な方はご連絡ください。"

# 便1制約行（U裁定 2026-08-24: telop は視覚AI読取で計測済み・voice のみ未計測）。
# レンダラの誠実性ゲートはこの文字列との完全一致で判定する（エンジン・レンダラ共用定数）。
VOICE_UNMEASURED_NOTE = (
    "音声内の言及は本便では未計測（キャプション・ハッシュタグ・テロップの3経路で計測。"
    "テロップは視覚AI読取で、解析できた動画のみを分母とする）"
)

# EG率の初出脚注（U2裁定: 24px muted で1回だけ）
EG_RATE_FOOTNOTE = "EG率＝(いいね+コメント+保存+シェア)÷再生"

SlideType = Literal["A", "B", "C", "D", "E", "H"]
TagVariant = Literal["発見", "結論", "所見"]
ImageKind = Literal["real_frame", "real_screen_browser", "provided_thumbnail"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeckBrand(_StrictModel):
    name: str = Field(min_length=1, max_length=200)
    accent_color: str = Field(default="", pattern=r"^(#[0-9A-Fa-f]{6})?$")


class DeckMeta(_StrictModel):
    addressee: str = Field(min_length=1)
    cover_title: str = Field(min_length=1)
    abstract: str = Field(min_length=1)
    category_en: str = Field(min_length=1)
    running_head: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    brand_a: DeckBrand
    brand_b: DeckBrand
    part_titles: list[str] = Field(min_length=1)
    method_target_constraints: list[str] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def _require_ben1_constraint_line(self) -> DeckMeta:
        # 便1は制約行（3行目）が必須で、voice 未計測の固定文と完全一致していること。
        if len(self.method_target_constraints) < 3:
            raise ValueError("便1では手法/対象/制約の3行が必須です")
        if self.method_target_constraints[2] != VOICE_UNMEASURED_NOTE:
            raise ValueError("制約行は VOICE_UNMEASURED_NOTE と完全一致が必要です")
        return self


class CardImage(_StrictModel):
    """カード画像（data URI 埋め込みのみ）。

    spec input_contract は「data_uri または参照」を許すが、media worker の
    slides オペは network 参照を ``MEDIA_HTML_NETWORK_REFERENCE`` で拒否する
    （operations.py の ``_EXTERNAL_HTML_REF`` ゲート）ため、便1の実装契約は
    data URI 埋め込み一本とする。実体は解析済み動画の1フレーム目
    （image_kind="real_frame"）で、embed_budget は組成側が守る。
    """

    data_uri: str = Field(min_length=32)
    image_kind: ImageKind

    @model_validator(mode="after")
    def _require_embedded_image(self) -> CardImage:
        if not self.data_uri.startswith(("data:image/jpeg;base64,", "data:image/png;base64,")):
            raise ValueError("image は data:image/jpeg|png;base64 の data URI が必須です")
        return self


class Card(_StrictModel):
    source_url: str = Field(min_length=1)  # 検証済み投稿URLのみ・推測URL禁止
    image: CardImage
    caption: str = ""


class RankingMetrics(_StrictModel):
    plays: int = Field(ge=0)
    eg_rate_pct: float = Field(ge=0)
    followers: int = Field(ge=0)


class RankingCard(Card):
    """E類型カード（metrics=再生数/EG率/フォロワー + account_name + content_summary）。"""

    metrics: RankingMetrics
    account_name: str = Field(min_length=1)
    content_summary: str = ""


class SlideDataA(_StrictModel):
    """A（表紙）ペイロード。段差サムネ2枚（未入手なら A は data 省略可）。"""

    thumbnail_pair: list[Card] = Field(min_length=2, max_length=2)


class SlideTag(_StrictModel):
    variant: TagVariant
    text: str = Field(min_length=1)


class QListItem(_StrictModel):
    q_number: str = Field(min_length=1)
    question: str = Field(min_length=1)


class SlideDataB(_StrictModel):
    part: int = Field(ge=1)
    title: str = Field(min_length=1)
    abstract: str = Field(min_length=1)
    q_list: list[QListItem] = Field(min_length=1)


class ComparisonGroup(_StrictModel):
    label: str = Field(min_length=1)
    value_a: float
    value_b: float
    unit: str = ""


class SlideDataC(_StrictModel):
    groups: list[ComparisonGroup] = Field(min_length=1)
    example: Card | None = None


class SlideDataD(_StrictModel):
    columns: list[str] = Field(min_length=2)
    rows: list[list[str]] = Field(min_length=1)
    highlight: str = "列/群の最大値のみ accent"

    @model_validator(mode="after")
    def _rows_match_columns(self) -> SlideDataD:
        width = len(self.columns)
        for row in self.rows:
            if len(row) != width:
                raise ValueError("D類型の行はカラム数と一致が必要です")
        return self


class SlideDataE(_StrictModel):
    cards: list[RankingCard] = Field(min_length=1, max_length=5)


class SummaryRow(_StrictModel):
    number: int = Field(ge=1)
    pattern: str = Field(min_length=1)
    description: str = Field(min_length=1)


class SlideDataH(_StrictModel):
    summary_rows: list[SummaryRow] = Field(min_length=1)
    cta: bool
    conclusion: str = ""


SlideData = Annotated[
    SlideDataA | SlideDataB | SlideDataC | SlideDataD | SlideDataE | SlideDataH,
    Field(),
]

_DATA_TYPE_BY_SLIDE: dict[str, type[BaseModel] | None] = {
    "A": SlideDataA,
    "B": SlideDataB,
    "C": SlideDataC,
    "D": SlideDataD,
    "E": SlideDataE,
    "H": SlideDataH,
}


class Slide(_StrictModel):
    type: SlideType
    part: int | None = None
    q_number: str = ""  # 「現状」導入枠は空（U1裁定・Q番号なし）
    heading: str = Field(min_length=1)
    lead: str = ""
    footnote: str = ""  # U2裁定: 初出スライドの用語定義注記（24px muted・レンダラ描画）
    tag: SlideTag | None = None
    data: SlideDataA | SlideDataB | SlideDataC | SlideDataD | SlideDataE | SlideDataH | None = None

    @model_validator(mode="after")
    def _data_matches_type(self) -> Slide:
        expected = _DATA_TYPE_BY_SLIDE[self.type]
        if self.type == "A":
            # 表紙のみ data 省略可（サムネ未入手時）
            if self.data is not None and not isinstance(self.data, SlideDataA):
                raise ValueError("A 類型の data は SlideDataA が必要です")
            return self
        if expected is None or not isinstance(self.data, expected):
            name = expected.__name__ if expected is not None else "None"
            raise ValueError(f"{self.type} 類型の data は {name} が必要です")
        return self


class DeckPlan(_StrictModel):
    """エンジンの最終出力（計測JSON）。レンダラはこれだけを入力に描く。"""

    spec_name: str = SPEC_NAME
    spec_version: str = SPEC_VERSION
    generated_on: str = Field(min_length=1)  # YYYY-MM-DD
    deck_meta: DeckMeta
    slide_plan: list[Slide] = Field(min_length=3)

    @model_validator(mode="after")
    def _ben1_composition(self) -> DeckPlan:
        types = [slide.type for slide in self.slide_plan]
        if types[0] != "A":
            raise ValueError("1枚目は A（表紙）が必須です")
        if types[-1] != "H":
            raise ValueError("最終枚は H（総括）が必須です")
        if "G" in types:  # 型の上でも来ないが二重防御
            raise ValueError("便1では G（構成解剖）を生成しません")
        last_h = self.slide_plan[-1]
        if isinstance(last_h.data, SlideDataH) and not last_h.data.cta:
            raise ValueError("便1の H は CTA 必須です（C3裁定）")
        return self


__all__ = [
    "CTA_TEXT",
    "EG_RATE_FOOTNOTE",
    "SPEC_NAME",
    "SPEC_VERSION",
    "VOICE_UNMEASURED_NOTE",
    "Card",
    "CardImage",
    "ComparisonGroup",
    "DeckBrand",
    "DeckMeta",
    "DeckPlan",
    "QListItem",
    "RankingCard",
    "RankingMetrics",
    "Slide",
    "SlideData",
    "SlideDataA",
    "SlideDataB",
    "SlideDataC",
    "SlideDataD",
    "SlideDataE",
    "SlideDataH",
    "SlideTag",
    "SummaryRow",
]
