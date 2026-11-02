"""類似提案書の推薦結果。内部スコアは回答モデルへ渡さない。"""

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
        description="検索候補件数（会社・原本の重複を除き他社最大4件・自社最大1件を提示）",
    )


class RecommendItem(BaseModel):
    """類似提案書1件。原本未登録の場合は担当への確認を案内する。"""

    title: str | None = Field(default=None, description="資料/スレッドのタイトル")
    source_uri: str | None = Field(default=None, description="Drive の原本リンク")
    score: float = Field(exclude=True, description="内部順位用。回答には表示しない")
    client_name: str | None = Field(default=None, description="クライアント名")
    cls_project: str | None = Field(default=None, description="自動分類された案件名")
    date: str = Field(default="日付不明", description="資料の年月")
    contact_name: str | None = Field(default=None, description="元データの共有者または案件担当者")
    similarity: str = Field(default="", description="根拠のある類似点")


class RecommendOutput(BaseModel):
    """他社の類似提案書を最大4件。同一社の最新提案は最大1件を別に示す。"""

    brief: str
    similar_proposals: list[RecommendItem] = Field(
        default_factory=list, description="他社の類似提案書（直近→関連度順、最大4件）"
    )
    similar_minutes: list[RecommendItem] = Field(
        default_factory=list, description="互換用。提案書推薦では空"
    )
    similar_sales_fb: list[RecommendItem] = Field(
        default_factory=list, description="互換用。提案書推薦では空"
    )
    same_client_materials: list[RecommendItem] = Field(default_factory=list)
    total_count: int = Field(ge=0, description="類似提案書の件数（同一社の補足は含めない）")
    answer: str = Field(default="", description="利用者へそのまま返す回答")
    model_instructions: str = (
        "answer をそのまま返してください。案件名・年月・似ている点・原本リンクまたは未登録案内を"
        "省略せず、スコア等の内部数値や別の候補を追加しないでください。"
    )
