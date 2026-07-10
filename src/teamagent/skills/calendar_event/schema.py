"""calendar_event Skill の I/O スキーマ（Pydantic v2）。

⚠️ event_token は HMAC 署名トークン（日時/タイトルは署名済み・所有者照合付き）。
生の予定詳細は戻り値の message/event_url（本人向け）以外に出さない（G3）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CalendarEventInput(BaseModel):
    """『📅 カレンダーに登録』ボタン押下の入力。value(署名トークン)を渡す。"""

    event_token: str = Field(
        min_length=1,
        max_length=500,
        description="『📅 カレンダーに登録』ボタンの value（HMAC署名トークン）",
    )


class CalendarEventOutput(BaseModel):
    """カレンダー登録結果（本人カレンダーのみ・招待は送信しない）。"""

    created: bool = Field(default=False, description="新規に予定を登録できたか")
    already: bool = Field(default=False, description="既に登録済みだった（冪等・連打）")
    error: str = Field(
        default="", description="失敗種別（expired/not_connected/reauth_needed 等・無ければ空）"
    )
    event_url: str = Field(
        default="", description="Google カレンダーでその予定を開くリンク（本人確認用）"
    )
    message: str = Field(default="", description="本人へ返す案内文（成功/失敗）")
