"""Claude が道具で取得した会話を読む。送信・認証・ネットワーク処理は行わない。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from scripts.user_eval.core import Attachment, Citation, Message

CRITICAL_VIOLATIONS = {
    "fabricated_numbers": "作り話の数字",
    "other_company_numbers": "他社の数字",
    "completed_as_ongoing": "終了済み施策を進行中と回答",
    "own_answer_as_source": "自分の過去の答えを根拠にした",
}


def timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("ts は有限の秒数（Slack ts 文字列も可）が必要")
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError("ts は有限の秒数が必要") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError("ts は非負・有限の秒数が必要")
    return number


def _string(row: dict[str, Any], key: str) -> None:
    if not isinstance(row.get(key), str):
        raise ValueError(f"{key} は文字列が必要")


def validate_record(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("会話はオブジェクトが必要")
    _string(row, "scenario_id")
    if not row["scenario_id"].strip():
        raise ValueError("scenario_id は空にできない")
    for key in ("sent", "received", "request_ids"):
        if not isinstance(row.get(key), list):
            raise ValueError(f"{key} は配列が必要")
    if not all(isinstance(v, str) and v.strip() for v in row["request_ids"]):
        raise ValueError("request_ids は空でない文字列の配列が必要")
    if not isinstance(row.get("logs", []), list):
        raise ValueError("logs は配列が必要")
    for key in ("sent", "received"):
        for message in row[key]:
            if not isinstance(message, dict):
                raise ValueError("発話はオブジェクトが必要")
            timestamp(message.get("ts"))
            _string(message, "text")
            if key == "sent":
                continue
            if message.get("author") not in ("aico", "user"):
                raise ValueError("author は aico または user が必要")
            if not isinstance(message.get("is_feedback_buttons"), bool):
                raise ValueError("is_feedback_buttons は bool が必要")
            if not isinstance(message.get("files"), list):
                raise ValueError("files は配列が必要")
            for attachment in message["files"]:
                if not isinstance(attachment, dict):
                    raise ValueError("file はオブジェクトが必要")
                _string(attachment, "name")
                _string(attachment, "mimetype")
                if "url" in attachment:
                    _string(attachment, "url")
            if not isinstance(message.get("citations", []), list):
                raise ValueError("citations は配列が必要")
            for citation in message.get("citations", []):
                if not isinstance(citation, dict):
                    raise ValueError("citation はオブジェクトが必要")
                for field in ("url", "author_id"):
                    _string(citation, field)
                if not isinstance(citation.get("is_aico_dm"), bool):
                    raise ValueError("is_aico_dm は bool が必要")
    judge = row.get("judge")
    if judge is not None:
        if not isinstance(judge, dict) or judge.get("verdict") not in ("good", "partial", "bad"):
            raise ValueError("judge.verdict は good/partial/bad が必要")
        _string(judge, "reason")
        if not judge["reason"].strip():
            raise ValueError("judge.reason は空にできない")
    for source in (row, judge or {}):
        violations = source.get("critical_violations", [])
        if not isinstance(violations, list) or any(
            not isinstance(v, str) or v not in CRITICAL_VIOLATIONS for v in violations
        ):
            raise ValueError("critical_violations は既知の違反コードの配列が必要")
    return row


def load_recorded(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("記録は空でない JSON 配列が必要")
    rows = [validate_record(row) for row in data]
    ids = [row["scenario_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("scenario_id が重複")
    return rows


def answers(record: dict[str, Any]) -> list[Message]:
    """本人・評価ボタン・最初の送信前の履歴を採点対象から除く。"""
    start = min((timestamp(m["ts"]) for m in record["sent"]), default=math.inf)
    return [
        Message(
            str(index),
            m["text"],
            timestamp(m["ts"]),
            attachments=[Attachment(a["name"], a.get("url", "")) for a in m["files"]],
            citations=[Citation(**c) for c in m.get("citations", [])],
        )
        for index, m in sorted(
            enumerate(record["received"]), key=lambda item: timestamp(item[1]["ts"])
        )
        if m["author"] == "aico" and not m["is_feedback_buttons"] and timestamp(m["ts"]) >= start
    ]
