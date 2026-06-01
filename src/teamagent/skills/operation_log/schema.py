"""OperationLog Skill (営業活動ログ自動生成) の入出力 Pydantic スキーマ。

Slack スレッドの営業会話 → CRM に転記できる構造化ログ (deal_phase / action /
next_step / BANT) に変換する。仕様: 実装計画 §7.3 Skill ⑥。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OperationLogInput(BaseModel):
    """OperationLog Skill の入力。

    Slack スレッド (channel_id + thread_ts) を指定する経路と、テキストを直接渡す
    経路の両対応。Slack 経由なら adapter でスレッドを取得して整形する。
    """

    channel_id: str | None = Field(default=None, description="Slack チャネル ID (スレッド取得経路)")
    thread_ts: str | None = Field(
        default=None, description="スレッドの親タイムスタンプ (スレッド取得経路)"
    )
    conversation_text: str | None = Field(
        default=None,
        max_length=20000,
        description="会話テキストを直接渡す経路 (channel/thread の代替)",
    )


class BantAssessment(BaseModel):
    """BANT 4 軸の評価。会話から読み取れた部分のみ埋める (不明は None)。"""

    budget: str | None = Field(default=None, description="予算")
    authority: str | None = Field(default=None, description="決裁権")
    need: str | None = Field(default=None, description="ニーズ")
    timeline: str | None = Field(default=None, description="導入時期")


class OperationLogOutput(BaseModel):
    """OperationLog Skill の出力 (CRM 転記用)。"""

    log_entry: str = Field(description="そのまま CRM に貼れる営業活動ログ本文")
    deal_phase: str | None = Field(
        default=None, description="案件フェーズ (ヒアリング/提案/検討/受注/失注 等)"
    )
    action: str | None = Field(default=None, description="このやり取りで実施した営業アクション")
    next_step: str | None = Field(default=None, description="次にやるべきこと")
    bant: BantAssessment = Field(default_factory=BantAssessment, description="BANT 4 軸の評価")
    source_message_count: int = Field(default=0, ge=0, description="ログ化した元メッセージ数")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この実行の概算コスト")
