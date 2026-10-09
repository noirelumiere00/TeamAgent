"""提案書 FMT v2 の placeholder 契約（Composer 出力スキーマ）。

teamagent_consulting で凍結した「FMT v2 / 95 placeholder（{1}–{103}、欠番 {48}–{55}）」契約を
本番 proposal_deck Skill 用に移植。`ComposerOutput` が renderer の入力契約。

- {48}〜{55} は独立セルでなく {47} に PR ワード 9 案をまとめて投入する設計のため欠番。
- LENGTH_RULES は Bedrock Sonnet 4.6 実走で字数下振れが判明したため運用緩和済み（運用で調整可）。
- 文字数違反は全件を集約して 1 つの ValueError にする
  （self-repair が 1 ラウンドで全修正できるよう）。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Final

from pydantic import BaseModel, Field, field_validator, model_validator

_MISSING_IDS: Final[frozenset[int]] = frozenset({48, 49, 50, 51, 52, 53, 54, 55})
VALID_IDS: Final[frozenset[int]] = frozenset(range(1, 104)) - _MISSING_IDS
_AUXILIARY_KEY: Final[re.Pattern[str]] = re.compile(r"^PB-[A-Z0-9_-]{1,60}$")
PROPOSAL_BUILDER_TEMPLATE_PROFILE: Final[str] = "proposal-builder-v1"
PROPOSAL_BUILDER_REQUIRED_AUXILIARY: Final[frozenset[str]] = frozenset(
    {
        "PB-ACCOUNTS",
        "PB-CASES",
        "PB-CLIENT-NAME",
        "PB-DATETIME",
        "PB-EXPERIENCE",
        "PB-KEY-MESSAGE",
        "PB-MONTH",
        "PB-PRODUCT-NAME",
    }
)

LENGTH_RULES: Final[dict[int, tuple[int, int]]] = {
    # 「程度」表記の下限は運用緩和（日本語はモデルが字数を下振れしやすい）.
    2: (50, 140),  # 目的（100 文字程度）
    3: (50, 140),  # 課題（100 文字程度）
    13: (50, 140),  # SNS 戦略概要（100 文字程度）
    18: (180, 420),  # 強みまとめ（300 文字程度）
    28: (15, 45),  # 社会的潮流サマリ（30 文字程度）
    56: (300, 700),  # PR ワード背景（500 文字程度）
    57: (50, 140),  # 戦略方針（100 文字程度）
    # 統合 FMT 53枚目（sha256
    # 62d39a2b76e0dedc6cdaae8de5cd224b80c5b93ddbb3beb2f01add67c27f0490）。
    # 手元で deck_qa._text_height と renderer の置換を使い全角連続文字で計測。
    # 全角1/半角0.55、行高=字の1.2倍、高さ許容1.1倍。全枠6pt、
    # 余白は左右各7.1988pt・上下各3.5984pt。固定ラベル・空段落も残す。
    # 幅×高さ(pt) → 全角容量（placeholder部分のみ）:
    # 61:55.6275×54.6316→30、63:58.2687×54.6316→35、
    # 66/68:58.1040×54.6316→35、73/78/85/90:54.2658×54.6316→30、
    # 75/80/87/92:57.3224×54.6316→35。
    # 25文字枠は全て56.4060×65.3362→56（79のみ末尾空段落で49）。
    # 上限=min(ceil(指示字数×1.2), 容量)で15→18、25→30。
    # 下限の強制は短い訴求の self-repair が収束しないため非空(1)のみ。
    61: (1, 18),
    62: (1, 30),
    63: (1, 18),
    64: (1, 30),
    66: (1, 18),
    67: (1, 30),
    68: (1, 18),
    69: (1, 30),
    73: (1, 18),
    74: (1, 30),
    75: (1, 18),
    76: (1, 30),
    78: (1, 18),
    79: (1, 30),
    80: (1, 18),
    81: (1, 30),
    85: (1, 18),
    86: (1, 30),
    87: (1, 18),
    88: (1, 30),
    90: (1, 18),
    91: (1, 30),
    92: (1, 18),
}

# 旧訴求リストのうち53枚目の「N文字程度」以外は従来の上限80を維持。
# テンプレ実文では60/77/84/89=具体的シーン、70=界隈B、71=界隈言語、
# 82=ターゲット属性。65/72も具体的シーン、83は興味関心だが従来規則なし。
# 字数指定の23枠は上の明示規則に分離（旧Slide 40/30〜50文字の前提は不適用）。
_PITCH_MESSAGE_IDS: Final[frozenset[int]] = frozenset({60, 70, 71, 77, 82, 84, 89})
for _pid in _PITCH_MESSAGE_IDS:
    LENGTH_RULES.setdefault(_pid, (1, 80))


class SkippedPlaceholder(BaseModel):
    """データ未検出時の placeholder スキップ記録。"""

    id: int
    reason: str = Field(..., description="必ず『要確認（データ未検出）』を含むこと")

    @field_validator("id")
    @classmethod
    def _id_must_be_valid(cls, v: int) -> int:
        if v not in VALID_IDS:
            raise ValueError(f"invalid placeholder id: {v}")
        return v

    @field_validator("reason")
    @classmethod
    def _reason_must_signal_missing(cls, v: str) -> str:
        if "要確認" not in v:
            raise ValueError("reason must contain 『要確認』")
        return v


class EvidenceImage(BaseModel):
    """証拠画像メタ（実物サムネ等）。Composer は生成せず、フィーダ（TikTok 等）が後付けする。

    画像バイトは載せず参照型（URL/パス）のみ保持し JSON 安全に保つ
    （raw bytes を model_dump_json すると Pydantic v2 が UTF-8 で落ちるため）。
    renderer（Phase3）が source_url/image_path を解決して add_picture する。
    placeholder_id は 95 枠の text id と直交（被覆 validator は不参照）。
    """

    placeholder_id: int = Field(..., description="貼り先 placeholder/枠ヒント（VALID_IDS）")
    rank: int = Field(..., ge=1, description="同一枠内での優先順位（1 始まり）")
    keyword: str = Field(..., min_length=1, description="由来キーワード（例: ラーメン）")
    source_url: str | None = Field(default=None, description="元画像 URL（例: TikTok cover_url）")
    image_path: str | None = Field(default=None, description="ローカル取得済み画像パス")
    video_url: str | None = Field(default=None, description="由来動画 URL（任意・トレース用）")

    @field_validator("placeholder_id")
    @classmethod
    def _valid_placeholder_id(cls, v: int) -> int:
        if v not in VALID_IDS:
            raise ValueError(f"invalid placeholder id: {v}")
        return v

    @model_validator(mode="after")
    def _at_least_one_source(self) -> EvidenceImage:
        if not (self.source_url or self.image_path):
            raise ValueError("EvidenceImage requires source_url or image_path")
        return self


class ComposerOutput(BaseModel):
    """Composer 出力（FMT v2 / 95 placeholder 完全網羅契約）。renderer の入力。

    `placeholders` には埋めた ID のみ、未検出は `skipped_placeholders` に積む。
    両者の和集合が VALID_IDS（95 個）と一致しないと validation fail。
    """

    placeholders: dict[int, str] = Field(default_factory=dict)
    citations_per_placeholder: dict[int, list[str]] = Field(default_factory=dict)
    skipped_placeholders: list[SkippedPlaceholder] = Field(default_factory=list)
    # フィーダ（TikTok 等）が後付けする証拠画像メタ。95 枠 text 被覆とは直交（既定空＝後方互換）。
    evidence_images: dict[int, list[EvidenceImage]] = Field(default_factory=dict)
    # proposal-builder 統合FMTの非数値枠。95枠の被覆契約とは直交し、renderer が
    # `{{PB-ACCOUNTS}}` / `{{PB-CASES}}` のような明示トークンだけを置換する。
    auxiliary_placeholders: dict[str, str] = Field(default_factory=dict)
    # 投稿開始日 D。統合FMT内の `{{PB-DATE:<offset>:<format>}}` を決定論的に解決する。
    posting_start_date: date | None = None
    # 既定値は従来テンプレとの後方互換。統合経路だけが厳格なtemplate inventory検証を有効化する。
    template_profile: str = Field(default="base", max_length=80)

    @field_validator("placeholders")
    @classmethod
    def _ids_in_valid_set(cls, v: dict[int, str]) -> dict[int, str]:
        bad = set(v) - VALID_IDS
        if bad:
            raise ValueError(f"invalid placeholder ids: {sorted(bad)}")
        for pid, text in v.items():
            if not text or not text.strip():
                raise ValueError(f"placeholder {{{pid}}} is empty")
        return v

    @field_validator("citations_per_placeholder")
    @classmethod
    def _citation_ids_valid(cls, v: dict[int, list[str]]) -> dict[int, list[str]]:
        bad = set(v) - VALID_IDS
        if bad:
            raise ValueError(f"citations reference invalid ids: {sorted(bad)}")
        return v

    @field_validator("evidence_images")
    @classmethod
    def _evidence_ids_valid(
        cls, v: dict[int, list[EvidenceImage]]
    ) -> dict[int, list[EvidenceImage]]:
        bad = set(v) - VALID_IDS
        if bad:
            raise ValueError(f"evidence_images reference invalid ids: {sorted(bad)}")
        for pid, imgs in v.items():
            for img in imgs:
                if img.placeholder_id != pid:
                    raise ValueError(
                        f"evidence_images key {pid} does not match "
                        f"image.placeholder_id {img.placeholder_id}"
                    )
        return v

    @field_validator("auxiliary_placeholders")
    @classmethod
    def _auxiliary_placeholders_valid(cls, value: dict[str, str]) -> dict[str, str]:
        total = 0
        for key, text in value.items():
            if not _AUXILIARY_KEY.fullmatch(key):
                raise ValueError(f"invalid auxiliary placeholder key: {key!r}")
            if not text or not text.strip():
                raise ValueError(f"auxiliary placeholder {key!r} is empty")
            total += len(text)
        if total > 20_000:
            raise ValueError("auxiliary placeholder text exceeds 20000 characters")
        return value

    @field_validator("template_profile")
    @classmethod
    def _template_profile_valid(cls, value: str) -> str:
        if value not in {"base", PROPOSAL_BUILDER_TEMPLATE_PROFILE}:
            raise ValueError(f"unsupported template profile: {value!r}")
        return value

    @model_validator(mode="after")
    def _coverage_and_lengths(self) -> ComposerOutput:
        filled_ids = set(self.placeholders)
        skipped_ids = {s.id for s in self.skipped_placeholders}
        overlap = filled_ids & skipped_ids
        if overlap:
            raise ValueError(
                f"placeholder must be either filled or skipped, not both: {sorted(overlap)}"
            )
        missing = VALID_IDS - filled_ids - skipped_ids
        if missing:
            raise ValueError(
                f"uncovered placeholders (must fill or skip all 95 ids): {sorted(missing)}"
            )
        length_errors: list[str] = []
        for pid, (lo, hi) in LENGTH_RULES.items():
            if pid in self.placeholders:
                n = len(self.placeholders[pid])
                if not (lo <= n <= hi):
                    length_errors.append(f"{{{pid}}} length {n} out of [{lo}, {hi}]")
        if length_errors:
            raise ValueError("; ".join(length_errors))
        return self

    @property
    def coverage_ratio(self) -> float:
        return len(self.placeholders) / len(VALID_IDS)


__all__ = [
    "LENGTH_RULES",
    "PROPOSAL_BUILDER_REQUIRED_AUXILIARY",
    "PROPOSAL_BUILDER_TEMPLATE_PROFILE",
    "VALID_IDS",
    "ComposerOutput",
    "EvidenceImage",
    "SkippedPlaceholder",
]
