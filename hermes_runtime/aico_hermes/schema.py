"""/v1/learn の入力スキーマと、本人メモ項目の共通規則。

MCP から届くのは「不透明な job_id」「本人メモのスナップショット」
「本人の 1 対 1 DM 発話 5 件まで」だけ。email・Slack user ID・名前など本人を特定するキーは
受け付けない（未知のキーはすべて拒否する）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

JOB_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{8,64}")
MAX_UTTERANCES: Final = 5
MAX_UTTERANCE_CHARS: Final = 800
MAX_ENTRY_CHARS: Final = 200
MAX_SNAPSHOT_ENTRIES: Final = 40
# Hermes の MEMORY.md / USER.md の項目区切り
# （Hermes の tools/memory_tool_store.py の ENTRY_DELIMITER と同じ）
ENTRY_DELIMITER: Final = "\n§\n"
# 項目の中に § があると区切りが崩れる（本家は許すが、次回の入力で拒否されて学習が止まる）
ENTRY_SEPARATOR_CHAR: Final = "§"
# Hermes の上限（config の memory_char_limit / user_char_limit と同じ値。区切りを含む合計字数）
USER_CHAR_LIMIT: Final = 1375
MEMORY_CHAR_LIMIT: Final = 2200

_TOP_KEYS: Final = frozenset({"job_id", "snapshot", "utterances"})
_SNAPSHOT_KEYS: Final = frozenset({"user", "memory"})


class RequestError(ValueError):
    """入力がスキーマに合わない。

    code は応答に載せてよい固定の短い識別子で、入力の中身を含めない。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LearnRequest:
    job_id: str
    user_entries: tuple[str, ...]
    memory_entries: tuple[str, ...]
    utterances: tuple[str, ...]


def valid_entry(item: object) -> bool:
    """本人メモ 1 項目として受け渡してよいか（入力と出力の両方で同じ規則を使う）。"""
    return (
        isinstance(item, str)
        and bool(item.strip())
        and len(item.strip()) <= MAX_ENTRY_CHARS
        and ENTRY_SEPARATOR_CHAR not in item
    )


def joined_length(entries: Iterable[str]) -> int:
    return len(ENTRY_DELIMITER.join(entries))


def _entries(value: Any, *, field: str, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_SNAPSHOT_ENTRIES:
        raise RequestError(f"bad_{field}")
    if not all(valid_entry(item) for item in value):
        raise RequestError(f"bad_{field}_entry")
    entries = tuple(item.strip() for item in value)
    if joined_length(entries) > limit:
        # 上限を超えたスナップショットでは Hermes が add を拒否し、学習ゼロが成功扱いになる
        raise RequestError(f"{field}_over_limit")
    return entries


def parse_learn_request(payload: Any) -> LearnRequest:
    """JSON を読み込んだ値を検査して LearnRequest にする。合わなければ RequestError。"""
    if not isinstance(payload, Mapping):
        raise RequestError("not_object")
    keys = set(payload)
    if keys != _TOP_KEYS:
        # 本人を特定するキー（email・user_id・name など）が混ざったときもここで落ちる
        raise RequestError("unexpected_keys" if keys - _TOP_KEYS else "missing_keys")

    job_id = payload["job_id"]
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise RequestError("bad_job_id")

    snapshot = payload["snapshot"]
    if not isinstance(snapshot, Mapping) or set(snapshot) != _SNAPSHOT_KEYS:
        raise RequestError("bad_snapshot")

    utterances = payload["utterances"]
    if not isinstance(utterances, list) or not 1 <= len(utterances) <= MAX_UTTERANCES:
        raise RequestError("bad_utterances")
    for text in utterances:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_UTTERANCE_CHARS:
            raise RequestError("bad_utterance")

    return LearnRequest(
        job_id=job_id,
        user_entries=_entries(snapshot["user"], field="user", limit=USER_CHAR_LIMIT),
        memory_entries=_entries(snapshot["memory"], field="memory", limit=MEMORY_CHAR_LIMIT),
        utterances=tuple(text.strip() for text in utterances),
    )
