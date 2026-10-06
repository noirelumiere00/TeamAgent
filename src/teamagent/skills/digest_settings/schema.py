"""digest_settings の I/O スキーマ（Pydantic v2）。

指定の無い項目（None）は「変えない」。範囲外の件数・分は拒否せず上下限へ丸め、丸めたことを
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
    action: Literal["show", "update", "reset"] = Field(
        default="show",
        description="show=current settings, update=apply the fields below, reset=back to defaults.",
    )
    delivery: bool | None = Field(
        default=None, description="false=stop the morning summary; true=resume (also ends a pause)."
    )
    pause_until: _dt.date | None = Field(
        default=None, description="Skip deliveries through this date (YYYY-MM-DD, inclusive)."
    )
    pause_days: int | None = Field(
        default=None,
        ge=1,
        le=MAX_PAUSE_DAYS,
        description="Alternative to pause_until: skip for N days starting today.",
    )
    weekdays: list[Weekday] | None = Field(
        default=None, max_length=5, description="Weekdays to deliver on (weekdays only)."
    )
    hide_sections: list[Section] | None = Field(
        default=None,
        max_length=5,
        description=(
            "Sections to remove. reply=mail needing reply, unread=other unread mail, "
            "slack=Slack mentions left unanswered, calendar=today's events, "
            "brief=client meeting brief."
        ),
    )
    show_sections: list[Section] | None = Field(
        default=None, max_length=5, description="Sections to show again."
    )
    limit_reply: int | None = Field(default=None, description="Max items in reply (1-8).")
    limit_unread: int | None = Field(default=None, description="Max items in unread (1-10).")
    limit_slack: int | None = Field(default=None, description="Max items in slack (1-8).")
    limit_calendar: int | None = Field(default=None, description="Max items in calendar (1-15).")
    auto_drafts: bool | None = Field(
        default=None, description="Whether Aico auto-creates Gmail reply drafts each morning."
    )
    reminders: bool | None = Field(
        default=None, description="Whether to send a DM just before each calendar event."
    )
    reminder_lead_minutes: int | None = Field(
        default=None, description="Minutes before the event to remind (1-60)."
    )
    reminder_personal_blocks: bool | None = Field(
        default=None,
        description=(
            "Remind for events with no guests and no meeting link (tasks the user put on "
            "the calendar). false for タスクの通知を止めて."
        ),
    )
    reminder_skip_add: list[str] | None = Field(
        default=None,
        max_length=MAX_SKIP_KEYWORDS,
        description="Words: events whose title contains one get no reminder (e.g. タスク).",
    )
    reminder_skip_remove: list[str] | None = Field(
        default=None, max_length=MAX_SKIP_KEYWORDS, description="Words to remove from that list."
    )


class DigestSettingsOutput(BaseModel):
    message: str = Field(default="", max_length=1500, description="Reply to the user verbatim.")
    changed: list[str] = Field(default_factory=list, description="Changed items (for logs).")
    error: str = Field(
        default="",
        max_length=32,
        description='"" / "dm_only" / "no_change" / "invalid" / "conflict" / "store_failed"',
    )
