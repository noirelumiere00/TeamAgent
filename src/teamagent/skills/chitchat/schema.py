"""Chitchat Skill（会話応答）の入出力 Pydantic スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChitchatInput(BaseModel):
    """ユーザーの会話メッセージ（メンション除去済みの素テキスト）。"""

    message: str = Field(max_length=2000, description="ユーザーの発話")


class ChitchatOutput(BaseModel):
    """会話応答。"""

    reply: str = Field(description="自然言語の会話応答（2〜4文）")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この実行の概算コスト")
