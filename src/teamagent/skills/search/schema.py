"""検索 Skill の入出力 Pydantic スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchInput(BaseModel):
    """検索 Skill の入力。"""

    query: str = Field(min_length=1, max_length=1000, description="自然文クエリ")
    top_k: int = Field(default=5, ge=1, le=50, description="返す上位件数")
    filter_industry: str | None = Field(
        default=None,
        max_length=100,
        description="業界フィルタ（メタデータ JSONB 経由）",
    )


class SearchHitOut(BaseModel):
    """検索結果の1ヒット。"""

    chunk_id: int
    content: str
    score: float = Field(ge=0.0, le=1.0)
    source: str | None = None


class SearchOutput(BaseModel):
    """検索 Skill の出力。"""

    answer: str = Field(description="Claude による要約（引用付き）")
    hits: list[SearchHitOut] = Field(default_factory=list, description="検索ヒット一覧")
    total_cost_usd: float = Field(ge=0.0, description="この検索実行の概算コスト")
