"""digest_settings の I/O スキーマ（Pydantic v2）。

指定の無い項目（None）は「変えない」。範囲外の件数は拒否せず上下限へ丸め、丸めたことを
返答に書く（「20 件にして」で失敗させない）。

description は短く保つ（ツール定義は毎リクエストの固定トークンに載り、日本語は英語の
約 4 倍のトークンになる）。1 フィールド 1 文・英語。
"""

from __future__ import annotations

import datetime as _dt
from typing import Literal

from pydantic import BaseModel, Field

from teamagent.skills.morning_digest.preferences import MAX_PAUSE_DAYS, MAX_SKIP_KEYWORDS

Section = Literal["reply", "unread", "slack", "calendar", "brief"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri"]


class DigestSettingsInput(BaseModel):
    action: Literal["show", "update", "reset"] = Field(default="show")
    delivery: bool | None = Field(default=None, description="false=停止、true=休止解除・再開。")
    pause_until: _dt.date | None = Field(default=None, description="休止最終日。")
    pause_days: int | None = Field(
        default=None,
        ge=1,
        le=MAX_PAUSE_DAYS,
        description="今日からの休止日数。",
    )
    weekdays: list[Weekday] | None = Field(default=None, max_length=5)
    hide_sections: list[Section] | None = Field(
        default=None,
        max_length=5,
        description=("非表示欄（enum順）:返信/他の未読/未返信メンション/今日の予定/商談準備。"),
    )
    show_sections: list[Section] | None = Field(default=None, max_length=5)
    limit_reply: int | None = Field(default=None)
    limit_unread: int | None = Field(default=None)
    limit_slack: int | None = Field(default=None)
    limit_calendar: int | None = Field(default=None)
    auto_drafts: bool | None = Field(default=None)
    reminders: bool | None = Field(default=None)
    reminder_lead_minutes: int | None = Field(default=None, description="1〜60分前（ONとは別）。")
    reminder_personal_blocks: bool | None = Field(
        default=None, description="ゲスト・会議リンクなしの作業枠も通知。"
    )
    reminder_skip_add: list[str] | None = Field(
        default=None,
        max_length=MAX_SKIP_KEYWORDS,
        description="予定名による通知除外語を追加。",
    )
    reminder_skip_remove: list[str] | None = Field(default=None, max_length=MAX_SKIP_KEYWORDS)


class DigestSettingsOutput(BaseModel):
    message: str = Field(default="", max_length=1500, description="Reply to the user verbatim.")
    changed: list[str] = Field(default_factory=list, description="Changed items (for logs).")
    error: str = Field(
        default="",
        max_length=32,
        description=('"" / "dm_only" / "no_change" / "invalid" / "conflict" / "store_failed"'),
    )
