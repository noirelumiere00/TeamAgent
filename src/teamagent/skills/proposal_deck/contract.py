"""提案書 FMT v2 の placeholder 契約（Composer 出力スキーマ）。

teamagent_consulting で凍結した「FMT v2 / 95 placeholder（{1}–{103}、欠番 {48}–{55}）」契約を
本番 proposal_deck Skill 用に移植。`ComposerOutput` が renderer の入力契約。

- {48}〜{55} は独立セルでなく {47} に PR ワード 9 案をまとめて投入する設計のため欠番。
- LENGTH_RULES は Bedrock Sonnet 4.6 実走で字数下振れが判明したため運用緩和済み（運用で調整可）。
- 文字数違反は全件を集約して 1 つの ValueError にする
  （self-repair が 1 ラウンドで全修正できるよう）。
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field, field_validator, model_validator

_MISSING_IDS: Final[frozenset[int]] = frozenset({48, 49, 50, 51, 52, 53, 54, 55})
VALID_IDS: Final[frozenset[int]] = frozenset(range(1, 104)) - _MISSING_IDS

LENGTH_RULES: Final[dict[int, tuple[int, int]]] = {
    # 「程度」表記の下限は運用緩和（日本語はモデルが字数を下振れしやすい）.
    2: (50, 140),  # 目的（100 文字程度）
    3: (50, 140),  # 課題（100 文字程度）
    13: (50, 140),  # SNS 戦略概要（100 文字程度）
    18: (180, 420),  # 強みまとめ（300 文字程度）
    28: (15, 45),  # 社会的潮流サマリ（30 文字程度）
    56: (300, 700),  # PR ワード背景（500 文字程度）
    57: (50, 140),  # 戦略方針（100 文字程度）
}

# Slide 40 訴求メッセージ群。FMT は「30〜50文字」だが、Bedrock Sonnet 4.6 実走で
# 4〜14 字の極端に短い punchy 訴求を多数出すことが判明。下限を強制すると self-repair が
# 収束しない（モデルが下限を満たせない）ため、下限なし（非空チェックのみ）+ 上限 80 に運用緩和。
_PITCH_MESSAGE_IDS: Final[frozenset[int]] = frozenset(
    {60, 61, 63, 64, 67, 68, 70, 71, 74, 75, 77, 78, 81, 82, 84, 85, 88, 89, 91, 92}
)
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


class ComposerOutput(BaseModel):
    """Composer 出力（FMT v2 / 95 placeholder 完全網羅契約）。renderer の入力。

    `placeholders` には埋めた ID のみ、未検出は `skipped_placeholders` に積む。
    両者の和集合が VALID_IDS（95 個）と一致しないと validation fail。
    """

    placeholders: dict[int, str] = Field(default_factory=dict)
    citations_per_placeholder: dict[int, list[str]] = Field(default_factory=dict)
    skipped_placeholders: list[SkippedPlaceholder] = Field(default_factory=list)

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


__all__ = ["LENGTH_RULES", "VALID_IDS", "ComposerOutput", "SkippedPlaceholder"]
