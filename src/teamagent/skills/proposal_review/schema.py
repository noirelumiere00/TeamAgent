"""ProposalReview Skill (提案レビュー Bot) の入出力 Pydantic スキーマ。

提案テキストを、過去の勝ちパターンと照合して「コードレビュー的に」診断する。
仕様: 実装計画 §7.3 Skill ⑦。提案ドラフト (Skill ⑤) と「作成→レビュー」で繋がる。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProposalReviewInput(BaseModel):
    """ProposalReview Skill の入力。"""

    proposal_text: str = Field(
        min_length=1,
        max_length=8000,
        description="レビュー対象の提案テキスト (骨子・本文・要約いずれも可)",
    )
    industry: str | None = Field(
        default=None, max_length=100, description="業界 (任意、過去事例の絞り込み用)"
    )


class ReviewSource(BaseModel):
    """レビューの根拠に使った過去提案/FB の 1 件。"""

    chunk_id: int
    source_type: str | None = None
    title: str | None = None
    client_name: str | None = None
    preview: str = Field(description="本文冒頭の抜粋")


class ProposalReviewOutput(BaseModel):
    """ProposalReview Skill の出力。"""

    review: str = Field(description="提案の診断 (強み/抜け/刺さりにくい点/改善案)")
    sources: list[ReviewSource] = Field(default_factory=list, description="照合した過去提案/FB")
    source_count: int = Field(ge=0, description="照合した過去提案/FB 件数")
    total_cost_usd: float = Field(ge=0.0, description="この実行の概算コスト")
