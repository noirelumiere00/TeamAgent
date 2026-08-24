"""レンダラ入力（spec ``input_contract``）の検証。

エンジン（計測・作文側）は deck_meta + slide_plan の計測JSONを出力し、
レンダラはそれ **だけ** を入力に描く。ここでの検証は「描く前の門番」で、
誠実性ゲート（便1制約行の完全一致・画像バジェット・image_kind・source_url・
空章扉禁止・PR語彙）を機械チェックする。違反は ``FmtContractError`` で fail-fast。
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from teamagent.skills.omiyage_report.contract import VOICE_UNMEASURED_NOTE
from teamagent.skills.omiyage_report.fmt.spec import BEN1_SLIDE_TYPES, FmtDeckSpec

SlideType = Literal["A", "B", "C", "D", "E", "F", "G", "H"]
TagVariant = Literal["発見", "結論", "所見"]
ImageKind = Literal["real_frame", "real_screen_browser", "provided_thumbnail"]

_DATA_URI = re.compile(r"^data:image/(?:jpeg|png);base64,(?P<body>[A-Za-z0-9+/=\s]+)$")
_ORGANIC_WORD = "オーガニック"
# 「オーガニック」使用時に必須の初出定義注記（spec text_rules.pr_labels.alternative）
_ORGANIC_NOTE_FRAGMENT = "#PR等の表記が確認できない投稿"

Text = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=400)]


class FmtContractError(ValueError):
    """入力JSONが input_contract / 誠実性ゲートを満たさない。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _decoded_image_bytes(data_uri: str) -> int:
    match = _DATA_URI.match(data_uri)
    if match is None:
        raise ValueError("image must be a data:image/jpeg|png;base64 URI")
    body = re.sub(r"\s+", "", match.group("body"))
    try:
        return len(base64.b64decode(body, validate=True))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("image data URI is not valid base64") from exc


class CardImage(_Strict):
    data_uri: str = Field(min_length=32)
    image_kind: ImageKind

    @model_validator(mode="after")
    def _valid_data_uri(self) -> CardImage:
        _decoded_image_bytes(self.data_uri)
        return self

    @property
    def byte_size(self) -> int:
        return _decoded_image_bytes(self.data_uri)


class Card(_Strict):
    """サムネ1枚ぶんの素材。source_url は検証済み投稿URLのみ（推測URL禁止）。"""

    source_url: str = Field(min_length=12, max_length=2048)
    image: CardImage
    caption: ShortText | None = None

    @field_validator("caption", mode="before")
    @classmethod
    def _empty_caption_is_none(cls, value: object) -> object:
        # エンジン契約（DeckPlan）は「無し」を空文字で表現する
        return None if value == "" else value

    @model_validator(mode="after")
    def _valid_source_url(self) -> Card:
        parsed = urlsplit(self.source_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("card source_url must be canonical HTTPS")
        if any(ch.isspace() for ch in self.source_url):
            raise ValueError("card source_url must not contain whitespace")
        return self


class RankingCard(Card):
    """E類型のランキングカード（指標セット = 再生数 / EG率 / フォロワー）。

    エンジン契約（DeckPlan）は指標を ``metrics: {plays, eg_rate_pct, followers}`` の
    入れ子で渡す。ここで flat 形へ正規化して両形を受ける（plays = views）。
    """

    account_name: ShortText
    content_summary: str = Field(default="", max_length=400)
    views: int = Field(ge=0)
    eg_rate_pct: float = Field(ge=0)
    followers: int = Field(ge=0)
    brand: Literal["a", "b"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_engine_metrics(cls, values: Any) -> Any:
        if not isinstance(values, dict) or not isinstance(values.get("metrics"), dict):
            return values
        flat = {key: value for key, value in values.items() if key != "metrics"}
        metrics = values["metrics"]
        flat.setdefault("views", metrics.get("plays", metrics.get("views")))
        for key in ("eg_rate_pct", "followers"):
            flat.setdefault(key, metrics.get(key))
        return flat


class Tag(_Strict):
    variant: TagVariant
    text: Text


class QListItem(_Strict):
    q_number: ShortText
    question: ShortText


class BData(_Strict):
    part: int = Field(ge=1, le=9)
    title: ShortText
    abstract: Text
    q_list: tuple[QListItem, ...] = Field(min_length=1, max_length=8)


class CGroup(_Strict):
    label: ShortText
    value_a: float = Field(ge=0)
    value_b: float = Field(ge=0)
    unit: Annotated[str, StringConstraints(max_length=16)] = ""
    count_a: int | None = Field(default=None, ge=0)
    count_b: int | None = Field(default=None, ge=0)


class CData(_Strict):
    groups: tuple[CGroup, ...] = Field(min_length=1, max_length=8)
    note_a: ShortText | None = None
    note_b: ShortText | None = None
    example: Card | None = None


class DData(_Strict):
    columns: tuple[ShortText, ...] = Field(min_length=2, max_length=10)
    rows: tuple[tuple[str, ...], ...] = Field(min_length=1, max_length=24)
    highlight: str = Field(default="", max_length=100)  # エンジン契約の注記（表示はしない）
    example: Card | None = None

    @model_validator(mode="after")
    def _rows_match_columns(self) -> DData:
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("D row width must match columns")
        return self


class EData(_Strict):
    # TOP5 が正・部分結果裁定（2026-08-24）により取得できた枚数(1..5)を許容し、
    # 何枚中何枚かは lead 側の文言（TOP{n}）で開示する
    cards: tuple[RankingCard, ...] = Field(min_length=1, max_length=5)


class HRow(_Strict):
    number: int = Field(ge=1, le=9)
    pattern: ShortText
    description: Text


class HData(_Strict):
    # U1構成の再掲行は最大6（「現状」+ Q1〜Q5）
    summary_rows: tuple[HRow, ...] = Field(min_length=1, max_length=6)
    cta: bool
    conclusion: Text | None = None

    @field_validator("conclusion", mode="before")
    @classmethod
    def _empty_conclusion_is_none(cls, value: object) -> object:
        return None if value == "" else value


class AData(_Strict):
    thumbnail_pair: tuple[Card, Card] | None = None


class Slide(_Strict):
    type: SlideType
    part: int | None = Field(default=None, ge=1, le=9)
    q_number: ShortText | None = None
    heading: str = Field(default="", max_length=400)
    lead: str = Field(default="", max_length=400)
    footnote: str = Field(default="", max_length=400)  # U2裁定: 初出の用語定義（24px muted）
    tag: Tag | None = None
    data: AData | BData | CData | DData | EData | HData

    @field_validator("q_number", mode="before")
    @classmethod
    def _empty_q_number_is_none(cls, value: object) -> object:
        # エンジン契約（DeckPlan）は「Q番号なし」を空文字で表現する（U1「現状」枠等）
        return None if value == "" else value

    @model_validator(mode="before")
    @classmethod
    def _cover_without_payload(cls, values: Any) -> Any:
        # エンジン契約では A（表紙）の data は省略可（サムネ未入手時は None）
        if isinstance(values, dict) and values.get("type") == "A" and values.get("data") is None:
            return {**values, "data": {}}
        return values

    @model_validator(mode="after")
    def _data_matches_type(self) -> Slide:
        expected: dict[str, type] = {
            "A": AData,
            "B": BData,
            "C": CData,
            "D": DData,
            "E": EData,
            "H": HData,
        }
        model = expected.get(self.type)
        if model is None:
            raise ValueError(f"slide type {self.type!r} is not renderable in 便1")
        if type(self.data) is not model:
            raise ValueError(f"slide type {self.type!r} requires {model.__name__} payload")
        if self.type in ("C", "D", "E", "H") and not self.heading.strip():
            raise ValueError(f"slide type {self.type!r} requires heading")
        return self


class BrandMeta(_Strict):
    name: ShortText
    accent_color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")] | None = None

    @field_validator("accent_color", mode="before")
    @classmethod
    def _empty_accent_is_none(cls, value: object) -> object:
        return None if value == "" else value


class DeckMeta(_Strict):
    addressee: ShortText
    cover_title: ShortText
    abstract: Text
    category_en: ShortText
    running_head: ShortText
    issuer: ShortText
    brand_a: BrandMeta
    brand_b: BrandMeta
    part_titles: tuple[ShortText, ...] = Field(min_length=1, max_length=4)
    method_target_constraints: tuple[ShortText, ...] = Field(min_length=2, max_length=3)


class DeckContent(_Strict):
    """レンダラ入力の全体。エンジン契約 ``DeckPlan`` の JSON をそのまま受ける。

    トップレベルは spec input_contract の ``deck_meta`` + ``slide_plan``。
    エンジンが付ける ``spec_name`` / ``spec_version`` / ``generated_on`` は
    受理し、spec との一致は ``validate_deck_content`` で照合する。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    spec_name: str = Field(default="", max_length=64)
    spec_version: str = Field(default="", max_length=32)
    generated_on: str = Field(default="", max_length=10)
    deck_meta: DeckMeta
    slides: tuple[Slide, ...] = Field(
        min_length=3,
        max_length=20,
        validation_alias=AliasChoices("slide_plan", "slides"),
    )

    @model_validator(mode="after")
    def _ben1_composition(self) -> DeckContent:
        types = [slide.type for slide in self.slides]
        unsupported = sorted(set(types) - set(BEN1_SLIDE_TYPES))
        if unsupported:
            raise ValueError(f"slide types not allowed in 便1: {unsupported}")
        if types[0] != "A" or types.count("A") != 1:
            raise ValueError("deck must start with exactly one cover (A)")
        if types[-1] != "H" or types.count("H") != 1:
            raise ValueError("deck must end with exactly one summary (H)")
        # 空のPART章扉禁止: B の直後に C/D/E が1枚も無い章扉を残さない
        for index, slide in enumerate(self.slides):
            if slide.type != "B":
                continue
            following = self.slides[index + 1 :]
            has_content = False
            for later in following:
                if later.type == "B":
                    break
                if later.type in ("C", "D", "E"):
                    has_content = True
                    break
            if not has_content:
                raise ValueError("empty PART chapter (B) is prohibited")
            if slide.part is not None and slide.part > len(self.deck_meta.part_titles):
                raise ValueError("B part number exceeds part_titles")
        return self

    def all_cards(self) -> tuple[Card, ...]:
        cards: list[Card] = []
        for slide in self.slides:
            data = slide.data
            if isinstance(data, AData) and data.thumbnail_pair is not None:
                cards.extend(data.thumbnail_pair)
            elif isinstance(data, CData | DData):
                if data.example is not None:
                    cards.append(data.example)
            elif isinstance(data, EData):
                cards.extend(data.cards)
        return tuple(cards)

    def display_texts(self) -> tuple[str, ...]:
        """表示され得る全テキスト（PR語彙ゲート・フォント文字集合の母集合）。"""
        texts: list[str] = [
            self.deck_meta.addressee,
            self.deck_meta.cover_title,
            self.deck_meta.abstract,
            self.deck_meta.category_en,
            self.deck_meta.running_head,
            self.deck_meta.issuer,
            self.deck_meta.brand_a.name,
            self.deck_meta.brand_b.name,
            *self.deck_meta.part_titles,
            *self.deck_meta.method_target_constraints,
        ]
        for slide in self.slides:
            texts.extend(filter(None, (slide.q_number, slide.heading, slide.lead, slide.footnote)))
            if slide.tag is not None:
                texts.extend((slide.tag.variant, slide.tag.text))
            data = slide.data
            if isinstance(data, BData):
                texts.extend((data.title, data.abstract))
                for item in data.q_list:
                    texts.extend((item.q_number, item.question))
            elif isinstance(data, CData):
                texts.extend(filter(None, (data.note_a, data.note_b)))
                texts.extend(group.label for group in data.groups)
                texts.extend(group.unit for group in data.groups)
            elif isinstance(data, DData):
                texts.extend(data.columns)
                for row in data.rows:
                    texts.extend(row)
            elif isinstance(data, EData):
                for ranking_card in data.cards:
                    texts.extend((ranking_card.account_name, ranking_card.content_summary))
            elif isinstance(data, HData):
                for h_row in data.summary_rows:
                    texts.extend((h_row.pattern, h_row.description))
                if data.conclusion is not None:
                    texts.append(data.conclusion)
        for card in self.all_cards():
            if card.caption is not None:
                texts.append(card.caption)
        return tuple(texts)


def validate_deck_content(raw: object, spec: FmtDeckSpec) -> DeckContent:
    """入力JSON → DeckContent（構造検証 + spec依存の誠実性ゲート）。"""
    try:
        content = DeckContent.model_validate(raw)
    except ValueError as exc:
        raise FmtContractError(f"deck content violates input_contract: {exc}") from exc

    if content.spec_name and content.spec_name != spec.meta.name:
        raise FmtContractError(
            f"spec_name mismatch: engine={content.spec_name!r} spec={spec.meta.name!r}"
        )
    if content.spec_version and content.spec_version != spec.meta.version:
        raise FmtContractError(
            f"spec_version mismatch: engine={content.spec_version!r} spec={spec.meta.version!r}"
        )

    # 便1制約行: 2026-08-24 計測経路裁定（telop計測済み・voiceのみ未計測）が
    # spec JSON の旧文言を上書きする。正はエンジン・レンダラ共用の定数。
    constraints = content.deck_meta.method_target_constraints
    if len(constraints) != 3 or constraints[2] != VOICE_UNMEASURED_NOTE:
        raise FmtContractError(
            "便1の表紙制約行が必須: method_target_constraints[2] は "
            f"{VOICE_UNMEASURED_NOTE!r} と完全一致しなければならない"
        )

    allowed_kinds = set(spec.image_rules.image_kind.enum)
    per_image_max = spec.image_rules.embed_budget.per_image_max_kb * 1024
    total_max = spec.image_rules.embed_budget.deck_total_max_kb * 1024
    total = 0
    for card in content.all_cards():
        if card.image.image_kind not in allowed_kinds:
            raise FmtContractError(f"image_kind {card.image.image_kind!r} is not in spec enum")
        size = card.image.byte_size
        if size > per_image_max:
            raise FmtContractError(
                f"image exceeds per-image budget ({size} > {per_image_max} bytes): "
                f"{card.source_url}"
            )
        total += size
    if total > total_max:
        raise FmtContractError(f"images exceed deck budget ({total} > {total_max} bytes)")

    texts = content.display_texts()
    if any(_ORGANIC_WORD in text for text in texts) and not any(
        _ORGANIC_NOTE_FRAGMENT in text for text in texts
    ):
        raise FmtContractError(
            "「オーガニック」を使う場合は初出の定義注記"
            f"（{_ORGANIC_NOTE_FRAGMENT} …）が必須（pr_labels ゲート）"
        )
    return content
