"""mail_summary Skill の I/O スキーマ（Pydantic v2）。

⚠️ 戻り値に生メール本文・生 From・生 messageId を含めないこと（G3）。要約は LLM 生成文、
件名は DLP マスク後・短縮、相手はマスク。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MailSummaryInput(BaseModel):
    """本人受信箱の要約入力。G5: client_name 必須（無差別走査を構造的に禁止）。"""

    client_name: str = Field(
        min_length=1,
        max_length=100,
        description="要約したいクライアント/案件名（検索クエリの核・必須）",
    )
    lookback_days: int = Field(default=14, ge=1, le=90, description="遡る期間（日）")
    max_messages: int = Field(
        default=15, ge=1, le=40, description="走査する最大メール数（コスト/レイテンシ上限）"
    )


class MailHighlight(BaseModel):
    """要約に含める受信メール 1 件のメタ（DLP マスク後・本文は含まない）。"""

    counterpart_masked: str = Field(description="相手アドレスのマスク表示")
    subject_scrubbed: str = Field(default="", max_length=80, description="件名（マスク後・短縮）")
    occurred_at: str | None = Field(default=None, description="受信日時（ISO・判明時）")


class MailSummaryOutput(BaseModel):
    """要約結果。生本文・生件名・生 From は一切含まない。"""

    client_name: str
    summary: str = Field(default="", description="LLM による横断要約（DLP マスク後の本文に基づく）")
    highlights: list[MailHighlight] = Field(default_factory=list)
    scanned_count: int = Field(ge=0, description="走査したメール数")
    inbox_owner_masked: str = Field(default="", description="参照した受信箱（マスク）")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この要約の概算コスト")
