"""取得原文を保持し、引用と分類件数をコードで確定する。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

VoiceStatus = Literal["ok_nonempty", "ok_empty", "partial", "failed", "skipped", "not_requested"]
CATEGORIES = ("好意", "不満・障壁", "質問・購入意向", "利用場面", "その他・判定不能")


class VoiceEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    platform: Literal["tiktok", "x"]
    url: str
    text: str = Field(min_length=1)
    fetched_at: str
    source: str
    text_sha256: str = ""
    run_id: str = ""
    likes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _source(self) -> VoiceEvidence:
        parsed = urlsplit(self.url)
        hosts = ("tiktok.com",) if self.platform == "tiktok" else ("x.com", "twitter.com")
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not any(host == h or host.endswith("." + h) for h in hosts)
        ):
            raise ValueError("voice source URL is not allowed")
        if datetime.fromisoformat(self.fetched_at).tzinfo is None:
            raise ValueError("fetched_at requires an explicit timezone")
        if not self.text.strip():
            raise ValueError("voice body is empty")
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.text_sha256 and self.text_sha256 != digest:
            raise ValueError("voice body hash mismatch")
        object.__setattr__(self, "text_sha256", digest)
        return self

    def quote(self, start: int = 0, end: int | None = None) -> str:
        stop = len(self.text) if end is None else end
        if not 0 <= start < stop <= len(self.text):
            raise ValueError("quote must be a nonempty raw-text span")
        return self.text[start:stop]


class VoiceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    platform: Literal["tiktok", "x"]
    url: str = ""
    status: VoiceStatus
    items: list[VoiceEvidence] = Field(default_factory=list)
    groups: dict[str, list[str]] = Field(default_factory=dict)
    classified: int = 0
    cost_usd: float = Field(default=0, ge=0)
    reserved_usd: float = Field(default=0, ge=0)
    reason: str = ""

    @property
    def unclassified(self) -> int:
        return len(self.items) - self.classified


def aggregate_assignments(
    items: Sequence[VoiceEvidence],
    groups: object,
    *,
    categories: Sequence[str] = CATEGORIES,
) -> tuple[dict[str, list[str]], int]:
    """既知IDを一度だけ数える。モデルのcountや未知分類は採用しない。"""
    known = {item.id for item in items}
    used: set[str] = set()
    result: dict[str, list[str]] = {}
    if not isinstance(groups, list):
        return result, 0
    for group in groups:
        if not isinstance(group, dict) or group.get("category") not in categories:
            continue
        ids: Any = group.get("ids", [])
        if not isinstance(ids, list):
            continue
        for key in ids:
            if isinstance(key, str) and key in known and key not in used:
                result.setdefault(group["category"], []).append(key)
                used.add(key)
    return result, len(used)
