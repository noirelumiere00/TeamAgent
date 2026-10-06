"""meeting_prep の入出力。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MaterialKind = Literal["web", "vault", "mail"]


class MeetingPrepInput(BaseModel):
    target: str = Field(
        default="",
        max_length=80,
        description=(
            "Part of the meeting title or the client company name. Empty = the next external "
            "meeting (today first, else the coming days)."
        ),
    )


class PrepSource(BaseModel):
    """出典 1 件（本文の [n] と同じ番号）。URL は材料から機械的に付ける（LLM の出力は使わない）。"""

    index: int = Field(ge=1)
    kind: MaterialKind
    label: str = Field(max_length=160)
    url: str = Field(default="", max_length=1000)


class MeetingPrepOutput(BaseModel):
    message: str = Field(default="", max_length=4000, description="Reply to the user verbatim.")
    meeting_title: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=120)
    sources: list[PrepSource] = Field(default_factory=list)
    error: str = Field(
        default="",
        max_length=32,
        description=(
            '"" / "dm_only" / "rollout_denied" / "calendar_not_connected" / "no_meeting" / '
            '"no_company" / "compose_failed"'
        ),
    )
    total_cost_usd: float = 0.0
