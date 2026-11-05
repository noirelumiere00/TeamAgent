"""knowledge_deliver Skill の入出力スキーマ。"""

from __future__ import annotations

from typing import Literal

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

    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=3, ge=1, le=5)
    filter_industry: str | None = Field(default=None, max_length=100)
    filter_client: str | None = Field(default=None, max_length=200)
    filter_doc_type: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "資料種別（提案資料・提案事例→提案書、レポート→報告書、打合せメモ→議事録、"
            "料金表→価格表、契約書→契約、投稿実績→施策実績）。"
        ),
    )
    filter_solution: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "依頼にある施策名（SNS運用/動画広告/インフルエンサー/SEO/Web制作/広告運用/"
            "イベント等）。"
        ),
    )


class KnowledgeDeliveryReceipt(BaseModel):
    """全添付の完了を確認した場合だけ、gateway が重複返信を抑止するために使う。"""

    kind: Literal["knowledge_deliver"] = "knowledge_deliver"
    status: Literal["posted"] = "posted"
    delivered_count: int = Field(ge=1)


class KnowledgeDeliverOutput(BaseModel):
    """knowledge_deliver の出力（実ファイルは依頼者 DM に直接添付済）。"""

    error: str = Field(
        default="", exclude_if=lambda value: not value, description="連携失敗の共通コード"
    )
    message: str = Field(
        default="", exclude_if=lambda value: not value, description="本人へそのまま返す連携の案内"
    )
    answer: str = Field(description="該当資料の要約（引用付き）")
    references: list[KnowledgeRef] = Field(default_factory=list, description="ヒットした資料一覧")
    delivered_count: int = Field(default=0, description="DM に添付できたファイル数")
    note: str = Field(default="", description="配信ステータス（依頼者へ伝える 1 行）")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この実行の概算コスト")
    delivery: KnowledgeDeliveryReceipt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="本文と全添付を 1 投稿に配信済みの場合だけ付く機械確認情報",
    )
