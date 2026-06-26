"""mail_reply Skill の I/O スキーマ（Pydantic v2）。

⚠️ 生メール本文・生 messageId は戻り値/ログに出さない（G3）。draft_body は AI 生成文なので返す。
to_display（返信先）は本人の取引相手＝本人にだけ Slack 表示する（ログではマスク）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class MailReplyInput(BaseModel):
    """返信ドラフト生成の入力。

    G5: 対象メールを必ず絞る＝ client_name / target_message_id / target_thread_id の
    いずれか必須。Slack ボタン等から呼ぶ場合は、生 messageId を出さないよう
    target_thread_id（スレッド単位の不透明ID）で指定する。
    """

    client_name: str = Field(
        default="",
        max_length=100,
        description="返信対象を探すクライアント/案件名（target_* 未指定時は必須）",
    )
    instructions: str | None = Field(
        default=None, max_length=500, description="返信の方針・盛り込みたい点（任意・トーン等）"
    )
    lookback_days: int = Field(default=30, ge=1, le=90, description="対象メールを探す期間（日）")
    target_message_id: str | None = Field(
        default=None, description="返信対象メールを明示指定する場合の messageId（任意）"
    )
    target_thread_id: str | None = Field(
        default=None,
        description="返信対象スレッドを thread_id で指定（Slackボタン用・生messageId非露出）",
    )

    @model_validator(mode="after")
    def _require_target(self) -> MailReplyInput:
        if not (self.client_name.strip() or self.target_message_id or self.target_thread_id):
            raise ValueError(
                "client_name / target_message_id / target_thread_id のいずれかが必須です"
            )
        return self


class MailReplyOutput(BaseModel):
    """返信ドラフト結果。送信はしない（Gmail 下書き保存のみ）。"""

    client_name: str
    created: bool = Field(description="Gmail 下書きを作成できたか")
    to_display: str = Field(
        default="",
        description="返信先アドレス（本人確認用。結果は本人へ ephemeral 配信され他者に出さない）",
    )
    draft_subject: str = Field(default="", description="生成した下書きの件名")
    draft_body: str = Field(
        default="", description="生成した下書き本文（AI 生成・本人がGmailで確認→送信）"
    )
    gmail_draft_id: str = Field(default="", description="作成された Gmail 下書きの ID")
    thread_id: str = Field(
        default="", description="下書きを入れたスレッドの thread_id（不透明ID・Gmail deep link 用）"
    )
    note: str = Field(default="", description="但し書き（送信していない 等）")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この生成の概算コスト")
