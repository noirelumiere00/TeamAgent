"""全ツール共通の入力ルール（既定OFF・本人と会話が確認できる場合だけ）。"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from teamagent.mcp_gateway.allowlist import email_allowed

FieldClass = Literal["required", "nice_to_have", "confirm_side_effect"]
InputState = Literal["unspecified", "given", "delegated", "declined"]
Decision = Literal["proceed", "ask_once", "proceed_partial"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    classification: FieldClass
    label: str
    default: Any = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    values: dict[str, Any]
    states: dict[str, InputState]
    missing: tuple[FieldSpec, ...]
    note: str

    @property
    def message(self) -> str:
        labels = "、".join(spec.label for spec in self.missing)
        if self.decision == "proceed_partial":
            return (
                f"{labels}が無いため実行は保留しました。"
                "代わりの骨子：目的を整理→調査対象を決める→実データを集める→要点をまとめる。"
            )
        lines = ["作成に必要な情報だけ、まとめてお知らせください。"]
        for index, spec in enumerate(self.missing, 1):
            choices = "／".join(
                f"{number}) {choice}"
                for number, choice in enumerate(
                    spec.choices or ("対象を指定する", "骨子で進める"), 1
                )
            )
            lines.append(f"{index}. {spec.label}：{choices}（指定する場合は内容も添えてください）")
        return "\n".join(lines)


def enabled(metadata: Mapping[str, Any]) -> bool:
    allowed = {
        v.strip().lower()
        for v in os.getenv("INPUT_POLICY_ALLOWED_EMAILS", "").split(",")
        if v.strip()
    }
    return (
        os.getenv("USE_INPUT_POLICY_V1") == "1"
        and metadata.get("identity_verified") is True
        and email_allowed(metadata.get("user_email"), allowed)
        and bool(metadata.get("channel_id"))
    )


def state_of(value: Any) -> InputState:
    if value in ("不要", "いらない") or value == ["不要"] or value == ["いらない"]:
        return "declined"
    if value in ("お任せ", "おまかせ") or value == ["お任せ"] or value == ["おまかせ"]:
        return "delegated"
    return "given" if value is not None and value != "" and value != [] else "unspecified"


def format_value(value: Any) -> str:
    """既定値の配列も内部表記を出さず、短い和文にする。"""
    if isinstance(value, (list, tuple)):
        return "・".join(str(item) for item in value)
    return str(value)


def evaluate(
    specs: Sequence[FieldSpec],
    values: Mapping[str, Any],
    *,
    remembered: Mapping[str, InputState] | None = None,
    asked: bool = False,
) -> PolicyResult:
    """確認項目には既定値を入れない。不要は後から任せても同じ依頼では復活させない。"""
    resolved = dict(values)
    states: dict[str, InputState] = {}
    missing: list[FieldSpec] = []
    notes: list[str] = []
    for spec in specs:
        state = state_of(values.get(spec.name))
        previous = (remembered or {}).get(spec.name)
        if previous == "declined" or (state == "unspecified" and previous == "delegated"):
            state = previous
        states[spec.name] = state
        if spec.classification == "confirm_side_effect":
            continue
        if state == "declined":
            resolved[spec.name] = None
        elif state != "given":
            resolved[spec.name] = spec.default
        if spec.classification == "required" and not resolved.get(spec.name):
            missing.append(spec)
        elif spec.classification == "nice_to_have" and state != "given":
            value = resolved.get(spec.name)
            notes.append(
                f"{spec.label}を省きました"
                if not value
                else f"{spec.label}は{format_value(value)}にしました"
            )
    decision: Decision = "proceed"
    if missing:
        declined_required = any(states[spec.name] == "declined" for spec in missing)
        decision = "proceed_partial" if asked or declined_required else "ask_once"
    return PolicyResult(
        decision,
        resolved,
        states,
        tuple(missing),
        "今回は" + "、".join(notes) + "。" if notes else "",
    )


@dataclass
class _Entry:
    expires: float
    asked: bool = False
    states: dict[str, InputState] = field(default_factory=dict)


class PolicyCache:
    """TTL付きプロセス内台帳。再起動・複数プロセス間では共有できない。

    任意項目は最初から質問しないので、消失時に繰り返す可能性があるのは必須の質問だけ。
    鍵だけをハッシュ化して保持し、本文・入力値・メールは保存もログ出力もしない。
    """

    def __init__(self, ttl: float = 3600, max_entries: int = 4096) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: dict[str, _Entry] = {}
        self._asked: dict[str, float] = {}
        self._lock = threading.Lock()

    def apply(
        self,
        metadata: Mapping[str, Any],
        tool: str,
        specs: Sequence[FieldSpec],
        values: Mapping[str, Any],
    ) -> PolicyResult:
        # 本人と会話の無い経路は呼出元で無効化する。DM直下はセッションを併用して別依頼を分ける。
        parts = [
            str(metadata.get(k) or "")
            for k in (
                "user_email",
                "verified_slack_team_id",
                "channel_id",
                "thread_ts",
                "_input_policy_session",
            )
        ]
        parts[0] = parts[0].strip().lower()
        anchor = next(
            (
                str(values[k])
                for k in ("brand", "product_name", "query")
                if state_of(values.get(k)) == "given"
            ),
            "",
        )
        conversation_key = hashlib.sha256("\x1f".join([*parts, tool]).encode()).hexdigest()
        key = hashlib.sha256("\x1f".join([*parts, tool, anchor]).encode()).hexdigest()
        with self._lock:
            now = time.monotonic()
            self._entries = {k: v for k, v in self._entries.items() if v.expires > now}
            self._asked = {k: expiry for k, expiry in self._asked.items() if expiry > now}
            entry = self._entries.get(key, _Entry(now + self.ttl))
            # 一部だけ埋まって対象の錨が変わっても、同じ会話では2回目を聞かない。
            result = evaluate(
                specs,
                values,
                remembered=entry.states,
                asked=entry.asked or conversation_key in self._asked,
            )
            if result.decision == "ask_once":
                if len(self._asked) >= self.max_entries:
                    del self._asked[next(iter(self._asked))]
                self._asked[conversation_key] = now + self.ttl
            entry.asked |= result.decision == "ask_once"
            entry.states.update(result.states)
            if key not in self._entries and len(self._entries) >= self.max_entries:
                del self._entries[next(iter(self._entries))]
            self._entries[key] = entry
            return result


CACHE = PolicyCache()
OMIYAGE_FIELDS = (
    FieldSpec("brand", "required", "対象ブランド"),
    FieldSpec("competitors", "nice_to_have", "競合比較"),
    FieldSpec("keywords", "nice_to_have", "一般キーワード検索"),
    FieldSpec("official_tiktok_account", "nice_to_have", "公式投稿の露出判定"),
)
