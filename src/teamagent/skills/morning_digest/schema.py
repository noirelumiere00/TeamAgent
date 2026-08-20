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
    max_threads: int = Field(
        default=25,
        ge=1,
        le=60,
        description="triage 対象スレッド上限（重複排除後・バッチ規模/コスト上限）",
    )


class MailDigestItem(BaseModel):
    """要返信/重要メール（スレッド）1 件のメタ。

    ⚠️ マスク版（`*_masked` / `*_scrubbed`）はログ/監査安全。表示版（`*_display`）は
    本人宛 DM のレンダリング専用の PII で、ログ/print/CloudWatch には絶対に出さない（G3/G7）。
    """

    counterpart_masked: str = Field(description="相手アドレスのマスク表示（ログ安全）")
    subject_scrubbed: str = Field(default="", max_length=80, description="件名（マスク後・短縮）")
    importance: str = Field(default="medium", description="優先度: high / medium / low")
    is_unread: bool = Field(default=False, description="未読(UNREAD)か＝未開封セクション用")
    to_self: bool = Field(default=False, description="本人が To に直接いるか＝要返信(下書き)の条件")
    occurred_at: str | None = Field(default=None, description="受信日時（ISO・JST +09:00・判明時）")
    occurred_at_display: str | None = Field(
        default=None,
        description=(
            "受信日時のJST表示（例 08/13(木) 19:00）。"
            "表示にはこの文字列をそのまま使い、ISO から時刻・曜日を再計算しないこと"
        ),
    )
    summary: str = Field(default="", max_length=200, description="1 行サマリ（LLM 生成）")
    has_draft: bool = Field(default=False, description="この件で下書きを生成したか")
    # --- 構造化抽出（LLM triage）---
    deadline: str | None = Field(default=None, description="抽出した期限（自由文・LLM抽出）")
    ask: str = Field(default="", max_length=120, description="相手の依頼/要求（1行・LLM抽出）")
    next_step: str = Field(default="", max_length=120, description="次アクション（1行・LLM抽出）")
    thread_count: int = Field(default=1, ge=1, description="このスレッドのメッセージ数")
    sender_label: str = Field(default="", description="差出人区分の表示ラベル（社内/社外/重要）")
    # --- 表示専用（本人宛 DM のみ・未マスク・PII・ログ厳禁）---
    counterpart_display: str = Field(
        default="", description="相手の表示名/会社（本人DM限定・未マスク・ログ厳禁）"
    )
    subject_display: str = Field(
        default="", max_length=160, description="件名（本人DM限定・未マスク・ログ厳禁）"
    )
    # ボタン用 HMAC 署名トークン。生 thread_id は value/ログに出さない（G3）。
    draft_token: str = Field(default="", max_length=400, description="下書きボタン用の署名トークン")
    thread_gmail_url: str = Field(
        default="", max_length=300, description="そのスレッドの Gmail 直リンク（確認するボタン用）"
    )
    # --- v0.3 Task3/4: 確定MTG・日程打診（LLM triage 抽出・フラットキー） ---
    meeting_start: str | None = Field(
        default=None, description="本文で確定しているMTGの開始（ISO・offset付き・検証済み）"
    )
    meeting_end: str | None = Field(default=None, description="同・終了（不明時は開始+1h を補完）")
    meeting_title: str = Field(
        default="", max_length=60, description="MTGの呼び名（本人DM表示・📅ボタン文言用）"
    )
    scheduling_request: bool = Field(
        default=False, description="相手が日程提示を求めているか（Task4 🗓ボタンの条件）"
    )
    event_token: str = Field(
        default="",
        max_length=500,
        description="📅カレンダー登録ボタン用の署名トークン（To本人かつ日時確定時のみ発行）",
    )


class CalendarEventItem(BaseModel):
    """当日の予定 1 件（DLP マスク後）。"""

    summary_scrubbed: str = Field(default="", max_length=80, description="件名（マスク後・ログ用）")
    summary_display: str = Field(
        default="", max_length=120, description="件名（本人DM表示用・未マスク）"
    )
    start_at: str | None = Field(default=None, description="開始時刻（ISO）")
    end_at: str | None = Field(default=None, description="終了時刻（ISO）")
    all_day: bool = Field(
        default=False,
        description=(
            "終日予定か（Google の start.date 由来）。"
            "⚠️ end_at（＝end.date）は排他的（8/21 のみの終日は end=8/22）"
        ),
    )
    location_scrubbed: str = Field(
        default="", max_length=80, description="場所（マスク後・ログ用）"
    )
    location_display: str = Field(
        default="", max_length=120, description="場所（本人DM表示用・未マスク）"
    )
    meeting_url: str = Field(
        default="", max_length=600, description="会議リンク（Meet/Zoom等・本人DM表示用）"
    )


class SlackUnreadItem(BaseModel):
    """Slack 未返信メンション 1 件（DLP マスク後＋本人 DM 表示用の display）。"""

    channel_name_masked: str = Field(default="", description="チャンネル名のマスク表示")
    excerpt_scrubbed: str = Field(default="", max_length=120, description="抜粋（マスク後）")
    channel_name_display: str = Field(
        default="", max_length=80, description="チャンネル名（本人DM表示用・未マスク・ログ厳禁）"
    )
    excerpt_display: str = Field(
        default="",
        max_length=1500,
        description=(
            "本文（本人DM表示用・未マスク・ログ厳禁）。"
            "⚠️ 上限を変えるときは skill 側の切り詰め長と必ず同時に直すこと"
            "（片方だけ伸ばすと pydantic ValidationError で digest ごと落ちる）"
        ),
    )
    permalink: str | None = Field(default=None, description="Slack の permalink")
    occurred_at: str | None = Field(default=None, description="メンション日時（ISO）")
    # --- 会話の素性（描画の分岐材料・読み取れなかったものは空/None/unknown のまま）---
    channel_id: str = Field(
        default="", max_length=32, description="会話 ID（D=DM / G=グループDM / C=チャンネル）"
    )
    channel_kind: str = Field(
        default="unknown",
        max_length=16,
        description=(
            '会話種別: "dm" / "group_dm" / "channel" / "unknown"。'
            "unknown は「判定できなかった」＝空欄と同義（推測で埋めない）"
        ),
    )
    # --- 差出人（誰の返事を止めているのかを言うための最小材料）---
    from_user_id: str | None = Field(
        default=None, max_length=32, description="差出人の Slack user_id"
    )
    from_display_name: str | None = Field(
        default=None,
        max_length=80,
        description=(
            "差出人の表示名（本人DM表示用・未マスク・ログ厳禁）。"
            "users.info で解決できなかったら None のまま＝架空の名前を作らない"
        ),
    )
    # --- スレッド由来の文脈（追加 API 呼び出し 0 回で拾えたぶん）---
    thread_message_count: int = Field(
        default=0, ge=0, description="スレッド内メッセージ総数（親含む）。0=取得できなかった"
    )
    thread_participant_ids: list[str] = Field(
        default_factory=list, description="スレッド参加者の user_id（登場順）"
    )
    thread_last_user_id: str | None = Field(
        default=None, max_length=32, description="スレッド最終発言者の user_id"
    )
    thread_last_at: str | None = Field(
        default=None, description="スレッド最終発言の日時（ISO・JST）"
    )
    answered_by_other: bool = Field(
        default=False,
        description=(
            "メンション後に「自分でも差出人でもない」誰かが発言したか"
            "（＝他人が代わりに答えた可能性）。差出人の追撃は sender_followed_up 側"
        ),
    )
    sender_followed_up: bool = Field(
        default=False, description="差出人自身がメンション後に再度発言したか（催促 or 自己解決）"
    )
    mentioned_user_ids: list[str] = Field(
        default_factory=list, description="本文で名指しされた user_id（自分含む・登場順）"
    )


class MorningDigestOutput(BaseModel):
    """1 ユーザー分の朝ダイジェスト結果。生本文・生件名・生 From は含まない。"""

    user_email_masked: str = Field(description="参照した受信箱（マスク）")
    mail_digest: list[MailDigestItem] = Field(default_factory=list)
    calendar_events: list[CalendarEventItem] = Field(default_factory=list)
    calendar_date: str = Field(
        default="",
        description=(
            "予定セクションの対象日（JST・YYYY-MM-DD）。"
            "見出しの日付明示に使う（空なら描画側が JST の今日にフォールバック）"
        ),
    )
    slack_unread: list[SlackUnreadItem] = Field(default_factory=list)
    slack_unread_total: int = Field(
        default=0,
        ge=0,
        description=(
            "走査範囲で見つかった未返信メンションの総件数（母数）。"
            "slack_unread は表示上限で切った先頭ぶんなので len() とは一致しない"
        ),
    )
    slack_unread_truncated: bool = Field(
        default=False,
        description=(
            "走査が上限で打ち切られたか。True のとき slack_unread_total は下限値"
            "（＝「少なくともこれだけ」）であり、確定値として表示してはいけない"
        ),
    )
    slack_unread_scanned: bool = Field(
        default=False,
        description=(
            "Slack を実際に走査できたか。False は「見ていない」（機能OFF・未連携・"
            "scope 不足・取得失敗）であって「0 件だった」ではない。"
            "⚠️ 描画側はこれを見ずに空リストを『返信漏れなし』と書いてはいけない"
        ),
    )
    drafts_created: int = Field(default=0, ge=0, description="Gmail draft として作成した下書き数")
    delivered: bool = Field(default=False, description="Slack DM 配信に成功したか")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この digest の概算コスト")
    errors: list[str] = Field(
        default_factory=list,
        description="部分失敗の構造化メッセージ（mail/calendar/slack のどれが落ちたか）",
    )
