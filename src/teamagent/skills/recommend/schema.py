"""Recommend Skill (類似案件レコメンド) の入出力 Pydantic スキーマ。

新規案件の概要テキストから、過去の類似「提案書 / 議事録 / 営業 FB」をベクトル近傍で
3 バケットに振り分けて提示する (Phase1)。検索は SearchSkill.retrieve_hits を再利用。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RecommendInput(BaseModel):
    """Recommend Skill の入力 (新規案件の概要)。"""

    brief: str = Field(
        min_length=1,
        max_length=2000,
        description="新規案件の概要 (クライアント/業界/ターゲット/予算/目的など自然文)",
    )
    industry: str | None = Field(
        default=None, max_length=100, description="業界フィルタ (任意、検索の絞り込み用)"
    )
    top_k: int = Field(
        default=12,
        ge=1,
        le=30,
        description="ベクトル近傍で取得する候補件数 (各バケット上位3件に振り分け前の母数)",
    )


class RecommendItem(BaseModel):
    """レコメンド 1 件 (各バケットの上位エントリ)。"""

    title: str | None = Field(default=None, description="資料/スレッドのタイトル")
    source_uri: str | None = Field(default=None, description="参照元 URI (Drive/Slack 等)")
    score: float = Field(description="類似度スコア (rerank 時は relevance_score)")
    client_name: str | None = Field(default=None, description="クライアント名")
    cls_project: str | None = Field(default=None, description="自動分類された案件名")


class RecommendOutput(BaseModel):
    """Recommend Skill の出力 (3 バケット × 各上位3件)。"""

    brief: str
    similar_proposals: list[RecommendItem] = Field(
        default_factory=list, description="類似提案書 (上位3件)"
    )
    similar_minutes: list[RecommendItem] = Field(
        default_factory=list, description="類似議事録 (上位3件)"
    )
    similar_sales_fb: list[RecommendItem] = Field(
        default_factory=list, description="類似営業 FB (上位3件)"
    )
    total_count: int = Field(ge=0, description="3 バケット合計のレコメンド件数")
