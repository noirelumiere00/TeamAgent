"""URL validation and one-response observations for URL import (no network or DB)."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from teamagent.adapters.video_library_page import item_struct
from teamagent.skills.ad_label_check.skill import find_disclosures

Source = Literal["url_manual", "p3b"]
METRIC_KEYS = ("playCount", "diggCount", "commentCount", "shareCount", "collectCount")


class URLImportError(ValueError):
    """Only fixed, non-sensitive codes may leave the transport/parser boundary."""


class NormalizedURL(BaseModel):
    url: str
    video_id: str | None = None


def normalize_url(value: str, *, redirect: bool = False) -> NormalizedURL:
    if len(value) > 4096 or re.search(r"[\s\x00-\x1f\x7f\\]", value):
        raise URLImportError("invalid_url")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ({"https"} if redirect else {"http", "https"})
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise URLImportError("invalid_url")
        host = (parsed.hostname or "").lower()
        if host not in {
            "tiktok.com",
            "www.tiktok.com",
            "m.tiktok.com",
            "vt.tiktok.com",
            "vm.tiktok.com",
        }:
            raise URLImportError("unsupported_host")
        path = parsed.path.rstrip("/")
        if host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
            match = re.fullmatch(r"/@([A-Za-z0-9_.-]+)/video/([0-9]{1,30})", path)
            if match:
                return NormalizedURL(url=f"https://www.tiktok.com{path}", video_id=match[2])
        if (
            host in {"vt.tiktok.com", "vm.tiktok.com"} and re.fullmatch(r"/[A-Za-z0-9_-]+", path)
        ) or (host == "www.tiktok.com" and re.fullmatch(r"/t/[A-Za-z0-9_-]+", path)):
            return NormalizedURL(url=f"https://{host}{path}/")
        raise URLImportError("unsupported_path")
    except ValueError as exc:
        if isinstance(exc, URLImportError):
            raise
        raise URLImportError("invalid_url") from None


class InputRow(BaseModel):
    line_no: int
    input_url: str
    canonical_url: str | None = None
    video_id: str | None = None
    status: str = "queued"
    error_code: str | None = None
    duplicate_of: int | None = None


def inspect_input(text: str) -> list[InputRow]:
    lines = [(i, line.strip()) for i, line in enumerate(text.splitlines(), 1) if line.strip()]
    if not lines or len(lines) > 100:
        raise URLImportError("empty_input" if not lines else "too_many_lines")
    rows = []
    seen: dict[str, int] = {}
    for number, value in lines:
        row = InputRow(line_no=number, input_url=value)
        try:
            normalized = normalize_url(value)
            row.canonical_url, row.video_id = normalized.url, normalized.video_id
            # Exact short links are also deduplicated before any network access.
            identity = normalized.video_id or normalized.url
            if identity in seen:
                row.status, row.duplicate_of = "duplicate", seen[identity]
            else:
                seen[identity] = number
        except URLImportError as exc:
            row.error_code = str(exc)
            row.status = "unsupported" if str(exc).startswith("unsupported") else "invalid"
        rows.append(row)
    return rows


class Observation(BaseModel):
    video_id: str
    url: str
    captured_at: datetime
    caption: str | None
    account_handle: str | None
    posted_at: datetime | None
    hashtags: list[str] = Field(default_factory=list)
    is_pr: bool = False
    page_is_ad: bool | None = None
    page_ad_label_version: Any = None
    disclosures: list[str] = Field(default_factory=list)
    metrics: list[int | None]
    warnings: list[str] = Field(default_factory=list)


def parse_observation(page: str, target: NormalizedURL, now: datetime) -> Observation:
    try:
        item = item_struct(page)
    except ValueError:
        if any(
            marker in page.lower()
            for marker in ("captcha", "verify to continue", "security challenge")
        ):
            raise URLImportError("challenge") from None
        raise URLImportError("page_data_missing") from None
    if not isinstance(item.get("id"), str) or item["id"] != target.video_id:
        raise URLImportError("video_id_mismatch")
    warnings: list[str] = []
    stats = item.get("stats")
    if not isinstance(stats, dict) or not stats:
        stats = item.get("statsV2")
    stats = stats if isinstance(stats, dict) else {}
    metrics: list[int | None] = []
    for key in METRIC_KEYS:
        value = stats.get(key)
        number = None
        if type(value) is int or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value)):
            parsed = int(value)
            if 0 <= parsed <= 2**63 - 1:
                number = parsed
        if number is None:
            warnings.append(f"metric_missing:{key}")
        metrics.append(number)
    caption = item.get("desc")
    if not isinstance(caption, str):
        caption = None
        warnings.append("caption_missing")
    author = item.get("author")
    handle = author.get("uniqueId") if isinstance(author, dict) else None
    if not isinstance(handle, str) or not handle:
        handle = None
        warnings.append("author_missing")
    posted_at = None
    timestamp = item.get("createTime")
    if type(timestamp) is int or (
        isinstance(timestamp, str) and re.fullmatch(r"[0-9]{1,12}", timestamp)
    ):
        try:
            if int(timestamp) > 0:
                posted_at = datetime.fromtimestamp(int(timestamp), tz=UTC)
        except (ValueError, OverflowError, OSError):
            pass
    if posted_at is None:
        warnings.append("posted_at_missing")
    tags = list(
        dict.fromkeys(
            re.findall(
                r"#([^\s#【】\[\](){}「」『』〈〉《》]+)",
                unicodedata.normalize("NFKC", caption or ""),
            )
        )
    )
    return Observation(
        video_id=item["id"],
        url=target.url,
        captured_at=now,
        caption=caption,
        account_handle=handle,
        posted_at=posted_at,
        hashtags=tags,
        is_pr=any(tag.casefold() == "pr" for tag in tags),
        page_is_ad=item.get("isAd") if type(item.get("isAd")) is bool else None,
        page_ad_label_version=item.get("adLabelVersion"),
        disclosures=find_disclosures(caption or "", tags),
        metrics=metrics,
        warnings=warnings,
    )


class ImportResult(BaseModel):
    status: str
    video_id: str | None = None
    registration_result: str | None = None
    error_code: str | None = None
    warnings: list[str] = Field(default_factory=list)
    duplicate_of: int | None = None
