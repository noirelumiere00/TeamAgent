"""ProposalDraft Skill (過去提案再利用支援) の入出力 Pydantic スキーマ。

新規案件のブリーフから、類似の過去提案を検索してドラフト骨子を生成する。
仕様: 実装計画 §7.1 Skill ⑤、Go/No-Go ゲート② (提案書 20h→8-12h) の中核。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProposalDraftInput(BaseModel):
    """ProposalDraft Skill の入力 (新規案件ブリーフ)。"""

    brief: str = Field(
        min_length=1,
        max_length=2000,
        description="新規案件の概要 (クライアント/業界/ターゲット/予算/目的など自然文)",
    )
    industry: str | None = Field(
        default=None, max_length=100, description="業界フィルタ (任意、検索の絞り込み用)"
    )
    top_k: int = Field(default=8, ge=1, le=20, description="参照する類似過去提案の件数")


class ProposalSource(BaseModel):
    """ドラフトの根拠に使った過去提案/FB の 1 件。"""

    chunk_id: int
    source_type: str | None = None
    title: str | None = None
    client_name: str | None = None
    preview: str = Field(description="本文冒頭の抜粋")


class ProposalDraftOutput(BaseModel):
    """ProposalDraft Skill の出力。"""

    brief: str
    draft: str = Field(description="提案ドラフト骨子 (構成案・訴求・推奨メニュー・想定論点)")
    sources: list[ProposalSource] = Field(
        default_factory=list, description="ドラフトの根拠にした過去提案/FB"
    )
    source_count: int = Field(ge=0, description="参照した過去提案/FB 件数")
    total_cost_usd: float = Field(ge=0.0, description="この実行の概算コスト")
