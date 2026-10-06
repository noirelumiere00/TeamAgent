"""slack_search Skill の I/O スキーマ（Pydantic v2）。

read-only（search.messages / users.info のみ・Slack への書込 API は一切呼ばない）。
message は LLM がそのまま返す決定的日本語文で、一覧の言い換え・並べ替えをさせない。

description は短く保つ（ツール定義は毎リクエストの固定トークンに載り、日本語は
英語の約 4 倍のトークンになる）。説明は 1 フィールド 1 文。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# 1 回に返す件数の既定と上限（Slack 本文が長くなりすぎない・users.info を叩き過ぎない）。
DEFAULT_COUNT = 10
MAX_COUNT = 20


class SlackSearchInput(BaseModel):
    """Slack 検索依頼の入力。"""

    query: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "検索語。in:#ch / from:@名前 / after:YYYY-MM-DD 等の Slack 検索構文もそのまま使える。"
        ),
    )
    count: int = Field(
        default=DEFAULT_COUNT,
        ge=1,
        le=MAX_COUNT,
        description=f"返す件数（既定 {DEFAULT_COUNT}・最大 {MAX_COUNT}）。",
    )
    focus: str = Field(
        default="",
        max_length=200,
        description="結果を短くまとめてほしい観点。指定したときだけ要約する。",
    )

    @field_validator("query", mode="before")
    @classmethod
    def _one_line(cls, value: Any) -> Any:
        """改行・前後空白を潰す（検索語は 1 行。空白だけなら min_length で落ちる）。"""
        if isinstance(value, str):
            return " ".join(value.split())
        return value

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, value: Any) -> Any:
        """範囲外の件数は拒否せず上下限へ丸める（「50 件」と頼まれても 20 件で答える）。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, int | float):
            return max(1, min(MAX_COUNT, int(value)))
        return value


Visibility = Literal["public", "private", "dm", "group_dm", "unknown"]


class SlackSearchHit(BaseModel):
    """検索で一致した 1 メッセージ（表示用に整形済み）。"""

    channel_id: str = Field(default="", description="一致したメッセージの会話 ID")
    channel_label: str = Field(default="", description="表示用の場所（#ch / 🔒#ch / DM 等）")
    visibility: Visibility = Field(default="unknown", description="公開範囲の判定結果")
    sender: str = Field(default="", description="差出人の表示名（引けなければハンドル）")
    posted_at: str = Field(default="", description="投稿日時（JST・YYYY-MM-DD HH:MM）")
    excerpt: str = Field(default="", description="本文の抜粋（通知記法は無害化済み）")
    permalink: str = Field(default="", description="メッセージへのリンク")


class SlackSearchOutput(BaseModel):
    """Slack 検索の結果（読み取りのみ・Slack へは何も書かない）。"""

    matches: list[SlackSearchHit] = Field(default_factory=list, description="表示した一致")
    match_count: int = Field(default=0, ge=0, description="表示した件数")
    hidden_count: int = Field(
        default=0, ge=0, description="公開範囲の都合で出さなかった件数（中身は出さない）"
    )
    total_hits: int = Field(
        default=0,
        ge=0,
        description="Slack が申告した総ヒット数（DM での依頼のときだけ。チャンネルでは 0）",
    )
    summary: str = Field(default="", description="focus 指定時だけの要約（一覧の範囲だけ）")
    error: str = Field(
        default="",
        description=(
            "失敗種別（not_connected / reconnect_required / search_failed・無ければ空）。"
            "空なら検索は成功で、0 件は本当に 0 件"
        ),
    )
    message: str = Field(
        default="",
        description="LLM がそのまま返す決定的日本語文（言い換え・並べ替えをしないこと）",
    )
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="要約に要した Bedrock 費用")
