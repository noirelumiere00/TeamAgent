"""管理画面の期間と、その要求内だけで使う質問テーマ分析。本文・分析結果は保存しない。"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.prompts.loader import load_prompt

JST = timezone(timedelta(hours=9))
PERIOD_LABELS = {"today": "今日", "7d": "直近7日", "30d": "直近30日"}
HAIKU_MODEL = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"


@dataclass(frozen=True)
class AdminPeriod:
    key: str
    label: str
    start: datetime
    end: datetime
    previous_start: datetime

    @property
    def days(self) -> int:
        return {"today": 1, "7d": 7, "30d": 30}[self.key]


def admin_period(key: str, *, now: datetime | None = None) -> AdminPeriod:
    if key not in PERIOD_LABELS:
        raise ValueError("invalid_admin_period")
    end = (now or datetime.now(JST)).astimezone(JST)
    start = (
        end.replace(hour=0, minute=0, second=0, microsecond=0)
        if key == "today"
        else end - timedelta(days={"7d": 7, "30d": 30}[key])
    )
    return AdminPeriod(key, PERIOD_LABELS[key], start, end, start - (end - start))


_PRIVATE_TEXT = re.compile(
    r"<[^<>]*(?:[a-z][a-z0-9+.-]*://|mailto:|@|!subteam\^)[^<>]*>"
    r"|(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>]+"
    r"|[\w.+\-]+@[\w.\-]+"
    r"|(?<![A-Z0-9])[UW][A-Z0-9]{8,}(?![A-Z0-9])"
    r"|[@＠][\w.\-]+"
    r"|[一-龥々ぁ-んァ-ヶーA-Za-z]+(?:[ 　]+[一-龥々A-Za-z]+)?(?:さん|さま|様|氏|先生|くん|君)",
    re.IGNORECASE,
)


def redact_question(text: str) -> str:
    """質問者情報は付加せず、自由文の明示的な識別情報も伏せる。"""
    # shortcut: 敬称のない自由文の人名は判別できない。完全匿名化が必要なら人名辞書/NLPを追加する。
    return _PRIVATE_TEXT.sub("［伏せ字］", html.unescape(text))


class ThemeAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    question_ids: list[int] = Field(min_length=1, max_length=200)


class ThemeAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    themes: list[ThemeAssignment] = Field(min_length=1, max_length=20)
    actions: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        min_length=3, max_length=3
    )


def analyze_questions(
    questions: list[dict[str, Any]],
    downs: list[dict[str, Any]],
    metrics: dict[str, Any],
    *,
    client: Any = None,
    request_id: str,
) -> dict[str, Any]:
    """LLMは分類だけを行い、件数・順位はサーバーのサンプルから算出する。"""
    negative = {(str(row.get("who", "")).lower(), str(row.get("query", ""))) for row in downs}
    groups: dict[str, dict[str, Any]] = {}
    sample = questions[:200]
    for row in sample:
        question = redact_question(str(row.get("query_text") or ""))[:300]
        if not question.strip():
            continue
        if question not in groups:
            groups[question] = {
                "id": len(groups) + 1,
                "text": question,
                "sample_count": 0,
                "blocked_count": 0,
            }
        group = groups[question]
        group["sample_count"] += 1
        blocked = (
            row.get("status") != "ok"
            or row.get("found") is False
            or (str(row.get("who", "")).lower(), str(row.get("query_text", ""))) in negative
        )
        group["blocked_count"] += int(blocked)
    if not groups:
        raise ValueError("no_questions")
    payload = {
        "metrics": {
            key: metrics[key]
            for key in ("requests", "users", "attention_events", "not_found", "unknown_search")
            if key in metrics
        },
        "sample_size": len(sample),
        "questions": list(groups.values()),
    }
    bedrock = client or BedrockClient.from_env(model_id_override=HAIKU_MODEL)
    response = bedrock.converse(
        messages=[{"role": "user", "content": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
        request_id=request_id,
        system=load_prompt("admin_summary", "v1", "system"),
        temperature=0.1,
        max_tokens=2400,
        cache_system=True,
    )
    parsed = ThemeAnalysis.model_validate_json(response.text)
    by_id = {group["id"]: group for group in groups.values()}
    assigned: set[int] = set()
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for theme in parsed.themes:
        title = redact_question(theme.title)
        for question_id in theme.question_ids:
            if question_id not in by_id:
                raise ValueError("unknown_question_id")
            if question_id in assigned:
                continue
            assigned.add(question_id)
            group = by_id[question_id]
            counts[title][0] += group["sample_count"]
            counts[title][1] += group["blocked_count"]
    themes: list[dict[str, Any]] = [
        {"title": title, "count": n[0]} for title, n in counts.items() if n[0]
    ]
    blocked_themes: list[dict[str, Any]] = [
        {"title": title, "count": n[1]} for title, n in counts.items() if n[1]
    ]
    return {
        "themes": sorted(themes, key=lambda row: (-row["count"], row["title"]))[:5],
        "blocked_themes": sorted(blocked_themes, key=lambda row: (-row["count"], row["title"]))[:3],
        "actions": [redact_question(action)[:240] for action in parsed.actions],
        "sample_size": len(sample),
        "classified_size": sum(by_id[i]["sample_count"] for i in assigned),
    }
