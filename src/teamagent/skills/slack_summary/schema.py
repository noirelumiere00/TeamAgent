"""slack_summary Skill の I/O スキーマ（Pydantic v2）。

read-only（conversations.replies / history のみ・Slack への書込 API は一切呼ばない）。
message は LLM がそのまま返す決定的日本語文で、要約の言い換え・補完をさせない。

**thread / auto のターゲット決定規則**（skill.py の `_resolve_target` が真実源）:
  1. 入力に `thread_ts` の明示があればそれを採用（`channel_id` 省略時は発信元チャンネル）。
  2. 明示が無ければ、MCP gateway が注入する署名済み metadata の channel_id/thread_ts。
ACL は本人 xoxp が物理担保するため、明示入力を優先しても権限は超えられない。
channel は thread_ts を使わず、入力 channel_id、次に署名済み metadata の channel_id を使う。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# エージェントが実際に渡してくる表記を ID へ正規化する（pattern で門前払いしない）。
_CHANNEL_REF = re.compile(r"<#([CGD][A-Za-z0-9]{1,32})(?:\|[^>]*)?>")
_PERMALINK = re.compile(r"/archives/([CGD][A-Za-z0-9]{1,32})/p(\d{13,20})")


def normalize_channel_id(raw: Any) -> str:
    """`<#C123|general>` / `https://…/archives/C123/p…` / 素の ID → ID。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    for pattern in (_CHANNEL_REF, _PERMALINK):
        m = pattern.search(s)
        if m:
            return m.group(1)
    return s.lstrip("#")


def normalize_thread_ts(raw: Any) -> str:
    """スレッドリンク（`…/p1755400000100100`）や `p…` 表記 → `1755400000.100100`。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    m = _PERMALINK.search(s)
    if m:
        s = m.group(2)
    elif s.startswith("p") and s[1:].isdigit():
        s = s[1:]
    if s.isdigit() and len(s) > 6:
        return f"{s[:-6]}.{s[-6:]}"
    return s


class SlackSummaryInput(BaseModel):
    """Slack のスレッドまたはチャンネル要約依頼の入力（全項目省略可）。"""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        """`<#C…|name>` やスレッド URL をそのまま渡されても ID/ts へ直してから検証する。

        URL だけ渡された場合は channel_id も URL から補う（別スレッドのリンク要約の主経路）。
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)
        raw_ts = out.get("thread_ts", "")
        if not str(out.get("channel_id", "") or "").strip():
            from_link = normalize_channel_id(raw_ts)
            if _PERMALINK.search(str(raw_ts or "")):
                out["channel_id"] = from_link
        if "channel_id" in out:
            normalized = normalize_channel_id(out["channel_id"])
            if normalized and not re.fullmatch(r"[CGD][A-Za-z0-9]{1,32}", normalized):
                from teamagent.adapters.slack_user_reader import normalize_channel_name

                if not re.fullmatch(r"[\w\-]+", normalize_channel_name(str(out["channel_id"]))):
                    raise ValueError("invalid channel name")
                if not str(out.get("channel_name") or "").strip():
                    out["channel_name"] = str(out["channel_id"])
                normalized = ""
            out["channel_id"] = normalized
        if "thread_ts" in out:
            out["thread_ts"] = normalize_thread_ts(raw_ts)
        return out

    scope: Literal["auto", "thread", "channel"] = Field(
        default="auto",
        description=(
            "「このチャンネルの要約」「チャンネルの決定事項」「ここ最近の流れ」はchannel"
            "、単一スレッドはthread、autoは1件以下でchannelに切替。"
        ),
    )
    channel_id: str = Field(default="", pattern="^([CGD][A-Za-z0-9]{1,32})?$")
    channel_name: str = Field(default="", max_length=100)
    period: str = Field(
        default="",
        max_length=80,
        description=(
            "期間の原文（今日/昨日/一昨日/今週/先週/先々週/今月/先月/直近N日/M月D日〜D日/"
            "YYYY-MM-DD等）。"
        ),
    )
    thread_ts: str = Field(
        default="",
        pattern=r"^(\d{6,20}\.\d{1,8})?$",
        description=("明示された親ts（省略時は現スレッド、channelでは不要）。"),
    )
    focus: str = Field(
        default="",
        max_length=200,
        description="依頼の原文・観点（全部/一覧/遡って/誰が/URLは一覧）。",
    )


class SlackSummaryOutput(BaseModel):
    """Slack 要約の結果（読み取りのみ・Slack へは何も書かない）。"""

    summary: str = Field(default="", description="LLM が生成した要約本文（生ログは含めない）")
    message_count: int = Field(default=0, ge=0, description="要約対象にしたメッセージ件数")
    scope: str = Field(
        default="", description="実際に要約した対象範囲（thread または channel。未実行時は空）"
    )
    error: str = Field(
        default="",
        description=(
            "失敗種別（not_connected / no_target / cross_channel_blocked / "
            "not_found / read_failed / empty_thread / summary_failed・無ければ空）"
        ),
    )
    message: str = Field(
        default="",
        description="LLM がそのまま返す決定的日本語文（言い換え・要約のやり直しをしないこと）",
    )
    truncated: bool = Field(default=False, description="取得・要約の上限や返信の取得失敗で一部のみ")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="要約に要した Bedrock 費用")
