"""全DM判定用の読み取り専用抽出。本文は返り値のメモリ内だけに置く。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from teamagent.adapters.dm_history import redact_secret_links

READ_METHODS = frozenset(
    {"auth_test", "conversations_list", "conversations_history", "conversations_replies"}
)
_TS = re.compile(r"[0-9]+\.[0-9]{6}")


class DmQualityError(ValueError):
    """本文・ID・APIの例外文を含まない固定コード。"""


def input_digest(*parts: str) -> str:
    """初回測定内の区別・再実行用。外部の評価票とは結び付けない。"""
    raw = json.dumps(parts, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass
class QualityExchange:
    input_id: str
    person_id: str
    question: str
    answers: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    context: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QualityExtraction:
    exchanges: list[QualityExchange] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    dm_count: int = 0


def _timestamp(raw: Any) -> float:
    if not isinstance(raw, str) or _TS.fullmatch(raw) is None:
        raise DmQualityError("invalid_message_timestamp")
    return float(raw)


def _text(raw: dict[str, Any]) -> str:
    text = redact_secret_links(str(raw.get("text") or ""))
    if not text and raw.get("blocks"):
        # Block-only の回答を無回答と取り違えない。構造の欠落は判定側で abstain する。
        text = "[block-only message]"
    text = re.sub(r"<@[A-Z0-9]+>", "[person]", text)
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)


def _feedback(raw: dict[str, Any]) -> bool:
    return any(
        isinstance(block, dict) and block.get("block_id") == "aico_answer_feedback"
        for block in raw.get("blocks") or []
    ) or str(raw.get("text") or "") in {
        "この回答は役に立ちましたか？",
        "ありがとうございます（👍 を記録しました）",
        "ありがとうございます（👎 を記録しました）",
    }


class DmQualityReader:
    def __init__(self, client: Any) -> None:
        self.client = client

    def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        if method not in READ_METHODS:
            raise DmQualityError("slack_write_forbidden")
        for attempt in range(3):
            try:
                response = getattr(self.client, method)(**kwargs)
                data = response.data if hasattr(response, "data") else response
                if not isinstance(data, dict) or data.get("ok") is not True:
                    raise DmQualityError("slack_read_failed")
                return data
            except Exception as error:
                response = getattr(error, "response", None)
                if getattr(response, "status_code", None) == 429 and attempt < 2:
                    try:
                        delay = float(getattr(response, "headers", {}).get("Retry-After", "1"))
                    except (TypeError, ValueError):
                        delay = 1
                    if 0 <= delay <= 60:
                        time.sleep(delay)
                        continue
                raise DmQualityError("slack_read_failed") from None
        raise DmQualityError("slack_read_failed")

    def _pages(self, method: str, field_name: str, **kwargs: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        for _ in range(1000):
            data = self._call(method, limit=200, **kwargs, **({"cursor": cursor} if cursor else {}))
            page = data.get(field_name)
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise DmQualityError("invalid_slack_page")
            rows.extend(page)
            cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                if data.get("has_more"):
                    raise DmQualityError("pagination_incomplete")
                return rows
            if cursor in seen:
                raise DmQualityError("pagination_incomplete")
            seen.add(cursor)
        raise DmQualityError("pagination_incomplete")

    def extract(
        self,
        *,
        start: datetime,
        end: datetime,
        observed_until: datetime,
        expected_team: str,
    ) -> QualityExtraction:
        if any(d.tzinfo is None for d in (start, end, observed_until)):
            raise DmQualityError("timezone_required")
        if not start < end <= observed_until:
            raise DmQualityError("invalid_period")
        auth = self._call("auth_test")
        if not expected_team or auth.get("team_id") != expected_team:
            raise DmQualityError("workspace_mismatch")
        if not auth.get("bot_id") or not auth.get("user_id"):
            raise DmQualityError("bot_token_required")
        result = QualityExtraction()
        try:
            channels = self._pages(
                "conversations_list", "channels", types="im", exclude_archived=False
            )
        except DmQualityError:
            result.failures.append("dm_list_failed")
            return result
        seen: set[str] = set()
        for channel in channels:
            cid, user = channel.get("id"), channel.get("user")
            if not isinstance(cid, str) or not cid.startswith("D") or not isinstance(user, str):
                result.failures.append("invalid_dm")
                continue
            if cid in seen:
                continue
            seen.add(cid)
            result.dm_count += 1
            try:
                # 古い親の週内返信も拾うため oldest で親投稿を落とさない。
                raws = self._pages(
                    "conversations_history",
                    "messages",
                    channel=cid,
                    latest=f"{observed_until.timestamp():.6f}",
                    inclusive=False,
                )
                for parent in list(raws):
                    if not parent.get("reply_count"):
                        continue
                    latest = parent.get("latest_reply")
                    if latest is not None and _timestamp(latest) < start.timestamp():
                        continue
                    raws.extend(
                        self._pages(
                            "conversations_replies",
                            "messages",
                            channel=cid,
                            ts=parent["ts"],
                            latest=f"{observed_until.timestamp():.6f}",
                        )
                    )
                result.exchanges.extend(
                    self._group(raws, user, expected_team, auth, start, end, observed_until)
                )
            except (DmQualityError, KeyError, TypeError, ValueError):
                result.failures.append("dm_read_incomplete")
        return result

    @staticmethod
    def _group(
        raws: list[dict[str, Any]],
        user: str,
        team: str,
        auth: dict[str, Any],
        start: datetime,
        end: datetime,
        observed_until: datetime,
    ) -> list[QualityExchange]:
        unique = {
            raw["ts"]: raw for raw in raws if _timestamp(raw.get("ts")) < observed_until.timestamp()
        }
        out: list[QualityExchange] = []
        lanes: dict[str, QualityExchange | None] = {}
        main: QualityExchange | None = None
        main_history: list[QualityExchange] = []
        thread_history: dict[str, list[QualityExchange]] = {}
        for ts, raw in sorted(unique.items(), key=lambda item: _timestamp(item[0])):
            if raw.get("subtype") in {"message_deleted", "message_changed", "tombstone"}:
                if _timestamp(ts) >= start.timestamp():
                    raise DmQualityError("message_unavailable")
                continue
            thread = str(raw.get("thread_ts") or ts)
            threaded_reply = thread != ts
            if raw.get("user") == user and not raw.get("bot_id"):
                history = thread_history.setdefault(thread, []) if threaded_reply else main_history
                exchange = QualityExchange(
                    input_digest("dm-quality-unit-v1", team, user, ts),
                    input_digest("dm-quality-person-v1", team, user),
                    _text(raw),
                    files=[str(f.get("name") or "attachment") for f in raw.get("files") or []],
                    # shortcut: 文脈は直前2発言。足りなければ欠測とし、必要時に窓を広げる。
                    context=[
                        {"question": x.question, "answers": list(x.answers), "files": x.files}
                        for x in history[-2:]
                    ],
                )
                history.append(exchange)
                if start.timestamp() <= _timestamp(ts) < end.timestamp():
                    out.append(exchange)
                lanes[thread] = exchange
                if not threaded_reply:
                    main = exchange
                    thread_history[ts] = [exchange]
            elif raw.get("bot_id") == auth["bot_id"] or raw.get("user") == auth["user_id"]:
                target = lanes.get(thread) if threaded_reply else main
                if target is not None and not _feedback(raw):
                    files = [str(f.get("name") or "attachment") for f in raw.get("files") or []]
                    target.answers.append(
                        _text(raw) + ("\n[attachments] " + ", ".join(files) if files else "")
                    )
        return out
