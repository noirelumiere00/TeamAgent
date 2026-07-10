"""schedule_propose Skill の I/O スキーマ（Pydantic v2）。

⚠️ schedule_token は draft_token と同一形式の HMAC 署名トークン（生 thread_id 非露出）。
生の候補日時は message（本人向け返答）にのみ載せる（G3）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScheduleProposeInput(BaseModel):
    """『🗓 日程候補を提案』ボタン押下の入力。value(署名トークン)を渡す。"""

    schedule_token: str = Field(
        min_length=1,
        max_length=400,
        description="『🗓 日程候補を提案』ボタンの value（HMAC署名トークン・生thread_id非露出）",
    )


class ScheduleProposeOutput(BaseModel):
    """日程候補の返信下書き＋カレンダー仮予定の作成結果（送信はしない）。"""

    created: bool = Field(default=False, description="候補入り返信下書きを作成できたか")
    already: bool = Field(default=False, description="既に下書きがあった（冪等スキップ）")
    error: str = Field(
        default="", description="失敗種別（expired/not_connected/no_slots 等・無ければ空）"
    )
    holds_created: int = Field(default=0, ge=0, description="作成した仮予定（透明ホールド）の数")
    open_url: str = Field(default="", description="Gmail でそのスレッドを開くリンク（本人確認用）")
    message: str = Field(default="", description="本人へ返す案内文（成功/失敗）")
