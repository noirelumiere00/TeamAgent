"""TikTok acquisition evidence shared by the isolated worker and core."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit


def _sales25_source_ok(raw: dict[str, Any], *, query: str, target: int) -> bool:
    """Admit only evidence the offline sales25 input contract can consume."""
    videos, diagnostics = raw.get("videos"), raw.get("diagnostics")
    if not isinstance(videos, list) or not isinstance(diagnostics, dict):
        return False
    if (
        raw.get("ok") is not True
        or raw.get("query") != query
        or type(raw.get("sessions")) is not int
        or raw["sessions"] != 1
        or raw.get("order_basis") != "search_display_order"
        or raw.get("unfiltered") is not True
        or raw.get("missingness_preserved") is not True
        or raw.get("stop_reason") not in ("exhausted", "no_new", "capped")
        or any(
            diagnostics.get(key)
            for key in (
                "captchaDetected",
                "cdnDenied",
                "apiNonZeroStatus",
                "apiHttpErrors",
                "apiInvalidResponses",
                "exception",
            )
        )
        or len(videos) > target
        or raw.get("status") != ("ok" if videos else "observed_zero")
        or raw.get("error_code") != (None if videos else "TIKTOK_TRULY_EMPTY")
    ):
        return False
    fetched_at = raw.get("fetched_at")
    if not isinstance(fetched_at, str):
        return False
    try:
        datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    for rank, video in enumerate(videos, 1):
        if not isinstance(video, dict):
            return False
        pid, url = video.get("id"), video.get("url")
        if (
            not isinstance(pid, str)
            or not re.fullmatch(r"\d{6,25}", pid)
            or not isinstance(url, str)
        ):
            return False
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.tiktok.com", "tiktok.com"}
            or parsed.username
            or parsed.password
            or not re.fullmatch(r"/@[^/]+/(video|photo)/" + re.escape(pid), parsed.path)
            or type(video.get("rank_display")) is not int
            or video["rank_display"] != rank
        ):
            return False
        missing, stats = video.get("missingFields"), video.get("stats")
        if (
            not isinstance(missing, list)
            or any(not isinstance(key, str) for key in missing)
            or not isinstance(stats, dict)
        ):
            return False
        for key in ("playCount", "diggCount", "commentCount", "shareCount", "collectCount"):
            value = stats.get(key)
            if value is None:
                if f"stats.{key}" not in missing:
                    return False
            elif type(value) is not int or value < 0:
                return False
    return True


def _normalized_posts(payload: dict[str, Any], query: str) -> list[dict[str, Any]]:
    rows = payload.get("posts")
    normalized = isinstance(rows, list)
    if not isinstance(rows, list):
        rows = payload.get("videos") or []
    posts = []
    numbers = {
        "plays": "stats.playCount",
        "likes": "stats.diggCount",
        "comments": "stats.commentCount",
        "shares": "stats.shareCount",
        "saves": "stats.collectCount",
        "followers": "author.followerCount",
        "create_time": "createTime",
        "duration": "duration",
    }
    for rank, source in enumerate(rows, 1):
        if not isinstance(source, dict):
            continue
        if normalized:
            row = dict(source)
        else:
            stats, author = source.get("stats") or {}, source.get("author") or {}
            row = {
                "provider_id": source.get("id"),
                "url": source.get("url"),
                "title": source.get("desc"),
                "hashtags": source.get("hashtags"),
                "account_id": author.get("uniqueId"),
                "account_name": author.get("nickname"),
                "cover_url": source.get("coverUrl"),
                **{
                    field: stats.get(path.split(".")[1])
                    for field, path in numbers.items()
                    if path.startswith("stats.")
                },
                "followers": author.get("followerCount"),
                "create_time": source.get("createTime"),
                "duration": source.get("duration"),
            }
        row.setdefault("kw", query)
        row.setdefault("rank_display", rank)
        # Normalized zero cannot distinguish an observed zero from a missing source value.
        row["unknownFields"] = [
            path for field, path in numbers.items() if row.get(field) in (0, None)
        ]
        row["missingFields"] = []
        posts.append(row)
    return posts


def _capture_record(
    payload: dict[str, Any],
    *,
    query: str,
    search_type: str,
    requested: int,
    sessions: int = 1,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Prefer source evidence; retain normalized values with explicit unknowns otherwise."""
    raw_details = payload.get("firstVisitCapture")
    details = raw_details if isinstance(raw_details, dict) else {}
    posts = details.get("posts")
    if not isinstance(posts, list):
        posts = _normalized_posts(payload, query)
    code = error_code or payload.get("errorCode")
    shortfalls = details.get("shortfalls") or payload.get("shortfalls") or []
    if not code:
        code = next(
            (
                row["reason"]
                for row in shortfalls
                if row.get("kw") == query
                and row.get("reason")
                and row["reason"] != "MEDIA_TIKTOK_RESULT_SHORTFALL"
            ),
            None,
        )
    status = (
        "observed_zero"
        if code in {"TIKTOK_TRULY_EMPTY", "MEDIA_TIKTOK_TRULY_EMPTY"}
        else ("failed" if code or payload.get("ok") is not True else "ok")
    )
    source_stop_reason = details.get("stop_reason") or code or "unknown"
    if not isinstance(source_stop_reason, str):
        source_stop_reason = "unknown"
    stop_reason = {
        "target_reached": "capped",
        "page_limit": "capped",
        "scroll_limit": "capped",
        "has_more_false": "exhausted",
        "no_new_data": "no_new",
        "TIKTOK_TRULY_EMPTY": "exhausted",
        "MEDIA_TIKTOK_TRULY_EMPTY": "exhausted",
    }.get(source_stop_reason, "unknown")
    return {
        "query": query,
        "search_type": details.get("executed_search_type") or payload.get("type") or search_type,
        "requested_search_type": search_type,
        "requested": requested,
        "status": status,
        "acquired_on": details.get("acquired_on"),
        "attempted_on": datetime.now(UTC).isoformat(),
        "order_basis": details.get("order_basis")
        or ("search_display_order" if sessions == 1 else "frequency_rank"),
        "sessions": details.get("sessions", sessions),
        "stop_reason": stop_reason,
        "source_stop_reason": source_stop_reason,
        "unfiltered": True,
        "missingness_preserved": details.get("missingness_preserved") is True,
        "posts": posts,
        "shortfalls": shortfalls
        or (
            [
                {
                    "kw": query,
                    "requested": requested,
                    "actual": len(posts),
                    "reason": (
                        f"MEDIA_{code}"
                        if str(code).startswith("TIKTOK_")
                        else code or "MEDIA_TIKTOK_RESULT_SHORTFALL"
                    ),
                }
            ]
            if code or len(posts) < requested
            else []
        ),
        "media_job_id": None,
        "artifacts": [],
        "raw_response": payload,
        "diag": payload.get("diag") or {},
        "error_code": code,
    }
