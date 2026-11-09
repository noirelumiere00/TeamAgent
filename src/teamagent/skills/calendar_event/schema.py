"""calendar_event Skill の I/O スキーマ（Pydantic v2）。

⚠️ event_token は HMAC 署名トークン（日時/タイトルは署名済み・所有者照合付き）。
生の予定詳細は戻り値の message/event_url（本人向け）以外に出さない（G3）。

入口は 2 つ:
  1. **ボタン経路**（従来）: ``event_token`` だけを渡す。日時・タイトルは署名済み。
  2. **自由文経路**（freeform）: ``title`` + ``start`` (+ ``end`` / ``location``)。
     朝ダイジェストを経ない「カレンダーに追加して」に応えるための入口。

⚠️ **どちらの入口にも attendees / 招待 / カレンダー ID は存在しない**。入力に無い＝
LLM がどう指示されても他人へ招待を飛ばせない（adapter 側も sendUpdates="none" 強制
＋ primary 固定で二重に封鎖）。この「引数が無い」こと自体が設計上のガードなので、
attendees 相当のフィールドを足してはいけない。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CalendarEventInput(BaseModel):
    """カレンダー登録の入力（ボタンの署名トークン、または自由文の予定内容）。"""

    event_token: str = Field(
        default="",
        max_length=500,
        description=("ボタン時は署名valueのみ。"),
    )
    title: str = Field(default="", max_length=200)
    start: str = Field(
        default="",
        max_length=40,
        description=("開始ISO 8601（TZなし=JST）。"),
    )
    end: str = Field(
        default="",
        max_length=40,
        description="終了ISO 8601（既定60分後、上限8時間）。",
    )
    location: str = Field(default="", max_length=200)
    bulk_text: str = Field(
        default="", max_length=12000, description="一括原文（共通時刻・日付・件名）"
    )
    confirm_bulk: bool = Field(default=False)


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
