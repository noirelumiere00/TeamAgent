"""morning_digest Skill の I/O スキーマ（Pydantic v2）。

⚠️ 戻り値に生メール本文・生 From・生 messageId・生件名を含めないこと（G3）。
要約は LLM 生成文、件名・相手は DLP マスク後・短縮、日時は ISO のみ。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MorningDigestInput(BaseModel):
    """1 ユーザー分の朝ダイジェスト入力。EventBridge Scheduled Task が `user_email` ごとに呼ぶ。

    G5: user_email は SkillContext.metadata.user_email から渡される（本人受信箱限定・fail-closed）。
    本 input は「何を集めるか」のスコープを絞るための任意パラメータのみ。
    """

    lookback_days: int = Field(
        default=3,
        ge=1,
        le=14,
        description="メール走査の遡り日数（既定 3 日・週末挟みを考慮）",
    )
    max_messages: int = Field(
        default=30,
        ge=1,
        le=60,
        description="走査する最大メール数（コスト/レイテンシ上限）",
    )
    calendar_horizon_hours: int = Field(
        default=24,
        ge=1,
        le=72,
        description="カレンダー予定の取得範囲（時間・既定 24h）",
    )
    slack_unread_horizon_days: int = Field(
        default=7,
        ge=1,
        le=14,
        description="Slack 未返信メンションの遡り日数（既定 7 日）",
    )
    max_drafts: int = Field(
        default=3,
        ge=0,
        le=10,
        description="要返信メールに対する下書き生成の上限（コスト抑制・既定 3 件）",
    )


class MailDigestItem(BaseModel):
    """要返信/重要メール 1 件のメタ（DLP マスク後・本文なし）。"""

    counterpart_masked: str = Field(description="相手アドレスのマスク表示")
    subject_scrubbed: str = Field(default="", max_length=80, description="件名（マスク後・短縮）")
    importance: str = Field(default="medium", description="優先度: high / medium / low")
    occurred_at: str | None = Field(default=None, description="受信日時（ISO・判明時）")
    summary: str = Field(default="", max_length=200, description="1 行サマリ（LLM 生成）")
    has_draft: bool = Field(default=False, description="この件で下書きを生成したか")


class CalendarEventItem(BaseModel):
    """当日の予定 1 件（DLP マスク後）。"""

    summary_scrubbed: str = Field(default="", max_length=80, description="件名（マスク後）")
    start_at: str | None = Field(default=None, description="開始時刻（ISO）")
    end_at: str | None = Field(default=None, description="終了時刻（ISO）")
    location_scrubbed: str = Field(default="", max_length=80, description="場所（マスク後）")


class SlackUnreadItem(BaseModel):
    """Slack 未返信メンション 1 件（DLP マスク後）。"""

    channel_name_masked: str = Field(default="", description="チャンネル名のマスク表示")
    excerpt_scrubbed: str = Field(default="", max_length=120, description="抜粋（マスク後）")
    permalink: str | None = Field(default=None, description="Slack の permalink")
    occurred_at: str | None = Field(default=None, description="メンション日時（ISO）")


class MorningDigestOutput(BaseModel):
    """1 ユーザー分の朝ダイジェスト結果。生本文・生件名・生 From は含まない。"""

    user_email_masked: str = Field(description="参照した受信箱（マスク）")
    mail_digest: list[MailDigestItem] = Field(default_factory=list)
    calendar_events: list[CalendarEventItem] = Field(default_factory=list)
    slack_unread: list[SlackUnreadItem] = Field(default_factory=list)
    drafts_created: int = Field(default=0, ge=0, description="Gmail draft として作成した下書き数")
    delivered: bool = Field(default=False, description="Slack DM 配信に成功したか")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この digest の概算コスト")
    errors: list[str] = Field(
        default_factory=list,
        description="部分失敗の構造化メッセージ（mail/calendar/slack のどれが落ちたか）",
    )
