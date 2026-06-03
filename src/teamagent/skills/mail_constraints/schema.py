"""mail_constraints Skill の I/O スキーマ（Pydantic v2）。

⚠️ 戻り値に生メール本文・件名・PII を含めないこと（DLP マスク後の構造化制約のみ）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# 抽出する制約の種別（LLM 出力はこの集合へ正規化する。未知は 'preference' に丸める）
CONSTRAINT_KINDS = ("NG", "budget", "deadline", "relationship", "preference")


class MailConstraintsInput(BaseModel):
    """本人受信箱から制約を抽出する入力。"""

    client_name: str = Field(
        min_length=1,
        max_length=100,
        description="制約を調べたいクライアント/案件名（検索クエリの核）",
    )
    topic_hint: str | None = Field(
        default=None,
        max_length=200,
        description="施策テーマ（例: '認知 ショート動画 タイアップ'）。NG 判定の文脈に使う",
    )
    lookback_days: int = Field(
        default=180,
        ge=1,
        le=365,
        description="遡る期間（日）。直近の合意/NG を優先する",
    )
    max_messages: int = Field(
        default=20,
        ge=1,
        le=50,
        description="走査する最大メール数（コスト/レイテンシ上限）",
    )


class MailConstraint(BaseModel):
    """抽出された 1 件の制約（**DLP マスク後・本文抜粋は不可**）。"""

    kind: str = Field(
        description="制約種別: 'NG' | 'budget' | 'deadline' | 'relationship' | 'preference'",
    )
    statement: str = Field(
        min_length=1,
        max_length=400,
        description="制約の要約（マスク後・施策判断に使える粒度。生本文の貼り付け禁止）",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="抽出の確信度")
    evidence_ref: str = Field(
        description="根拠メールの参照（messageId のハッシュ等。生件名・本文は入れない）",
    )
    occurred_at: str | None = Field(
        default=None,
        description="制約が言及された日付（ISO 文字列・判明時のみ）",
    )


class MailConstraintsOutput(BaseModel):
    """制約抽出の結果。生本文は一切含まない。"""

    client_name: str
    constraints: list[MailConstraint] = Field(default_factory=list)
    summary: str = Field(
        default="",
        description="制約の統合サマリ（DLP マスク済み・施策判断に使える粒度）",
    )
    scanned_count: int = Field(ge=0, description="走査したメール数")
    inbox_owner_masked: str = Field(
        default="",
        description="参照した受信箱（マスク表示）。本人性監査用",
    )
    total_cost_usd: float = Field(ge=0.0, description="Bedrock 抽出の概算コスト")
