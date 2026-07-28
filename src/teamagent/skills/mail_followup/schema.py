"""mail_followup Skill の I/O スキーマ（Pydantic v2）。

⚠️ 戻り値に生メール本文・生件名・生 From・生 messageId を含めないこと（G3）。
件名は DLP マスク後・短縮、相手アドレスはマスク、messageId はハッシュのみ。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MailFollowupInput(BaseModel):
    """要返信トリアージの入力。G5: client_name 必須（無差別走査を構造的に禁止）。"""

    client_name: str = Field(
        min_length=1,
        max_length=100,
        description="対象クライアント/案件名（検索クエリの核・必須）",
    )
    lookback_days: int = Field(
        default=14,
        ge=1,
        le=90,
        description="遡る期間（日）。直近の放置メールに絞る",
    )
    idle_days: int | None = Field(
        default=None,
        ge=1,
        le=90,
        description="この日数以上『相手から来たまま動いていない』ものだけに絞る（任意）",
    )
    max_messages: int = Field(
        default=30,
        ge=1,
        le=50,
        description="走査する最大メール数（コスト/レイテンシ上限）",
    )


class FollowupItem(BaseModel):
    """放置気味の受信スレッド 1 件（**DLP マスク後・生データ不可**）。"""

    counterpart_masked: str = Field(
        description="相手アドレスのマスク表示（先頭1文字＋ドメイン）",
    )
    subject_scrubbed: str = Field(
        default="",
        max_length=80,
        description="件名（DLP マスク後・短縮。生件名の貼り付け禁止）",
    )
    idle_days: int = Field(ge=0, description="相手から最後に来てからの経過日数")
    occurred_at: str | None = Field(
        default=None,
        description="相手から最後に来た日時（ISO 文字列・判明時のみ）",
    )
    evidence_ref: str = Field(
        description="根拠メールの参照（messageId のハッシュ。生 ID は入れない）",
    )


class MailFollowupOutput(BaseModel):
    """トリアージ結果。生本文・生件名・生 From は一切含まない。"""

    client_name: str
    items: list[FollowupItem] = Field(default_factory=list)
    scanned_count: int = Field(ge=0, description="走査したメール数")
    inbox_owner_masked: str = Field(
        default="",
        description="参照した受信箱（マスク表示）。本人性監査用",
    )
    note: str = Field(
        default="",
        description="ラベルの正直な但し書き（本人返信が末尾のスレッドは除外 等）",
    )
    total_cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="LLM 不使用のため常に 0.0",
    )
