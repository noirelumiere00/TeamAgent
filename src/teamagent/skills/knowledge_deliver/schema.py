"""knowledge_deliver Skill の入出力スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeRef(BaseModel):
    """検索でヒットした資料の参照（DM 配信したかも持つ）。"""

    title: str | None = Field(default=None, description="資料タイトル")
    url: str | None = Field(default=None, description="正本 URL（Drive リンク等）")
    doc_type: str | None = Field(default=None, description="資料種別（提案書/議事録 等・自動分類）")
    industry: str | None = Field(default=None, description="業界（自動分類）")
    score: float = Field(default=0.0, description="関連度スコア")
    delivered: bool = Field(default=False, description="実ファイルを DM に添付したか")


class KnowledgeDeliverInput(BaseModel):
    """knowledge_deliver の入力。"""

    query: str = Field(
        min_length=1,
        max_length=1000,
        description="探したい資料の自然文（例: アース製薬の提案資料 / 食品業界の成功事例）",
    )
    top_k: int = Field(default=3, ge=1, le=5, description="DM に届ける最大ファイル数")
    filter_industry: str | None = Field(
        default=None, max_length=100, description="業界フィルタ（任意）"
    )


class KnowledgeDeliverOutput(BaseModel):
    """knowledge_deliver の出力（実ファイルは依頼者 DM に直接添付済）。"""

    answer: str = Field(description="該当資料の要約（引用付き）")
    references: list[KnowledgeRef] = Field(default_factory=list, description="ヒットした資料一覧")
    delivered_count: int = Field(default=0, description="DM に添付できたファイル数")
    note: str = Field(default="", description="配信ステータス（依頼者へ伝える 1 行）")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この実行の概算コスト")
