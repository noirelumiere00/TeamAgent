"""slack_summary Skill の I/O スキーマ（Pydantic v2）。

read-only（conversations.replies のみ・Slack への書込 API は一切呼ばない）。
message は LLM がそのまま返す決定的日本語文で、要約の言い換え・補完をさせない。

**ターゲット決定の優先規則**（尋問 fix・skill.py の `_resolve_target` が真実源）:
  1. 入力に `thread_ts` の明示があればそれを採用（`channel_id` 省略時は発信元チャンネル）。
  2. 明示が無ければ、MCP gateway が注入する署名済み metadata の channel_id/thread_ts。
ACL は本人 xoxp が物理担保するため、明示入力を優先しても権限は超えられない。
"""

from __future__ import annotations

import re
from typing import Any

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
    """「このスレッド要約して」等の自由文依頼の入力（全項目省略可）。"""

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
            out["channel_id"] = normalize_channel_id(out["channel_id"])
        if "thread_ts" in out:
            out["thread_ts"] = normalize_thread_ts(raw_ts)
        return out

    channel_id: str = Field(
        default="",
        pattern=r"^([CGD][A-Za-z0-9]{1,32})?$",
        description=(
            "対象スレッドのチャンネル ID（<#C…|name> 由来の C…/G…/D…）。"
            "**省略時は依頼が行われたチャンネルを自動採用**する。"
            "チャンネル名（#営業）では指定できない＝ID が要る。"
        ),
    )
    thread_ts: str = Field(
        default="",
        pattern=r"^(\d{6,20}\.\d{1,8})?$",
        description=(
            "対象スレッドの親メッセージ ts（例 1755400000.123456・Slack のスレッドリンク末尾）。"
            "**明示された場合はこちらを優先**し、省略時は依頼が行われた現スレッドを自動採用する。"
        ),
    )
    focus: str = Field(
        default="",
        max_length=200,
        description="要約で特に知りたい観点（例『決定事項だけ』『自分への依頼』）。省略可。",
    )


class SlackSummaryOutput(BaseModel):
    """スレッド要約の結果（読み取りのみ・Slack へは何も書かない）。"""

    summary: str = Field(default="", description="LLM が生成した要約本文（生ログは含めない）")
    message_count: int = Field(default=0, ge=0, description="要約対象にしたメッセージ件数")
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
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="要約に要した Bedrock 費用")
