"""MCP-only sales25 search recovery; the worker never receives Apify credentials."""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime
from typing import Any

from teamagent.adapters.apify_client import ApifyClient, ApifyRunResult
from teamagent.adapters.cost_guard import CostGuard
from teamagent.adapters.media_job import MediaJobClient
from teamagent.adapters.tiktok_video_fallback import (
    apify_fallback_enabled,
    fallback_deadline_s,
    safe_reason,
)
from teamagent.media.tiktok_capture import _sales25_source_ok

_STATS = ("playCount", "diggCount", "commentCount", "shareCount", "collectCount")
_CACHE = "sales25-apify.json"
_MARKER = "sales25-apify.attempted"


def _video(item: dict[str, Any], rank: int) -> dict[str, Any]:
    author, meta = item.get("authorMeta") or {}, item.get("videoMeta") or {}
    url = item.get("webVideoUrl")
    photo = item.get("isSlideshow") is True or (isinstance(url, str) and "/photo/" in url)
    if photo and isinstance(url, str):
        url = url.replace("/video/", "/photo/", 1)
    values = {
        **{f"stats.{key}": item.get(key) for key in _STATS},
        "author.followerCount": author.get("fans"),
        "createTime": item.get("createTime"),
        "duration": meta.get("duration"),
    }
    # Source numbers stay numbers; absent or invalid numbers never become measured zero.
    values = {
        key: value if type(value) is int and value >= 0 else None for key, value in values.items()
    }
    return {
        "id": item.get("id"),
        "url": url,
        "rank_display": rank,
        "acquired_via": "apify",
        "desc": item.get("text"),
        "author": {
            "uniqueId": author.get("name"),
            "nickname": author.get("nickName"),
            "followerCount": values["author.followerCount"],
        },
        "stats": {key: values[f"stats.{key}"] for key in _STATS},
        "missingFields": [key for key, value in values.items() if value is None],
        "createTime": values["createTime"],
        "duration": values["duration"],
        "coverUrl": meta.get("coverUrl"),
        "mediaType": "photo" if photo else "video",
        "photoCount": len(item["slideshowImageLinks"])
        if isinstance(item.get("slideshowImageLinks"), list)
        else None,
        "isAd": item.get("isAd"),
        "hashtags": [
            tag.get("name") if isinstance(tag, dict) else tag
            for tag in (item.get("hashtags") or [])
        ],
    }


def _evidence(run: ApifyRunResult, original: dict[str, Any]) -> dict[str, Any]:
    target = original["acquisition_status"]["target"]
    # Keep every returned row in order; malformed rows reject the entire fallback.
    videos = [_video(item, rank) for rank, item in enumerate(run.items, 1)]
    raw = {
        "owner_id": original["raw"]["owner_id"],
        "query": original["raw"]["query"],
        "ok": True,
        "status": "ok",
        "error_code": None,
        "fetched_at": datetime.now(UTC).isoformat(),
        "sessions": 1,
        "order_basis": "search_display_order",
        "order_provider": "apify_dataset_order",
        "acquired_via": "apify",
        "unfiltered": True,
        "missingness_preserved": True,
        "stop_reason": "capped" if len(videos) == target else "exhausted",
        "stop_reason_basis": "apify_actor_completed",
        "diagnostics": {},
        "requested": target,
        "actual": len(videos),
        "videos": videos,
    }
    if (
        run.status != "SUCCEEDED"
        or not videos
        or not _sales25_source_ok(raw, query=raw["query"], target=target)
    ):
        raise ValueError("APIFY_SEARCH_EVIDENCE_INVALID")
    return {
        "raw": raw,
        "acquisition_status": {
            "query": raw["query"],
            "accepted": True,
            "status": "complete" if len(videos) == target else "partial",
            "target": target,
            "actual": len(videos),
            "shortfall": target - len(videos),
            "stop_reason": raw["stop_reason"],
            "diagnostics": {},
            "raw_ref": "sales25-raw.json",
            "acquired_via": "apify",
        },
        "apify_evidence": {
            "actor_id": run.actor_id,
            "run_id": run.run_id,
            "estimated_cost_usd": run.estimated_cost_usd,
            "warnings": run.warnings,
            "items": run.items,
        },
    }


def apply_sales25_fallback(
    original: dict[str, Any],
    *,
    media_client: MediaJobClient,
    deadline_s: int,
    request_id: str,
) -> dict[str, Any]:
    """Recover blocked/API-error/empty searches, with at most one shortfall retry per job."""
    if not apify_fallback_enabled():
        return original
    out = copy.deepcopy(original)
    source = "browser_skipped" if original.get("browser_skipped") else "worker"
    out["acquired_via"] = out["raw"]["acquired_via"] = source
    out["acquisition_status"]["acquired_via"] = source
    raw, status = original["raw"], original["acquisition_status"]
    if (
        status["actual"] != 0
        and raw.get("error_code")
        not in {
            "TIKTOK_BOT_WALL",
            "TIKTOK_CDN_DENIED",
            "TIKTOK_API_ERROR",
        }
        and raw.get("stop_reason")
        not in {"refused", "captcha", "cdn_denied", "api_error", "no_api_response"}
    ):
        return out
    started = time.monotonic()
    budget = min(deadline_s, fallback_deadline_s())
    deadline_epoch_s = int(time.time()) + budget
    job_id = original["media_job_id"]
    try:
        cached = media_client.find_staged(
            job_id=job_id, name=_CACHE, deadline_epoch_s=deadline_epoch_s
        )
        if cached is not None:
            recovered = json.loads(media_client.download(cached, deadline_epoch_s=deadline_epoch_s))
            if recovered["raw"]["owner_id"] != raw["owner_id"] or not _sales25_source_ok(
                recovered["raw"], query=raw["query"], target=status["target"]
            ):
                raise ValueError("APIFY_SEARCH_CACHE_INVALID")
        else:
            media_client.stage_marker(
                job_id=job_id,
                name=_MARKER,
                body=b"sales25 Apify search attempted\n",
                content_type="text/plain",
                deadline_epoch_s=deadline_epoch_s,
            )
            apify = ApifyClient.from_env(ledger=CostGuard.from_env())
            remaining = int(budget - (time.monotonic() - started))
            if remaining <= 0:
                raise TimeoutError("APIFY_SEARCH_DEADLINE")
            run = apify.tiktok_search(
                raw["query"],
                max_items=status["target"],
                deadline_s=remaining,
                request_id=request_id,
            )
            recovered = _evidence(run, original)
            remaining = int(budget - (time.monotonic() - started))
            if recovered["acquisition_status"]["shortfall"] and remaining > 0:
                try:
                    retry = _evidence(
                        apify.tiktok_search(
                            raw["query"],
                            max_items=status["target"],
                            deadline_s=remaining,
                            request_id=f"{request_id}:retry",
                        ),
                        original,
                    )
                    attempts = [recovered["apify_evidence"], retry["apify_evidence"]]
                    # Never merge sessions: keep the more complete single-run display order.
                    if (
                        retry["acquisition_status"]["actual"]
                        > recovered["acquisition_status"]["actual"]
                    ):
                        recovered = retry
                    recovered["apify_attempts"] = attempts
                except Exception as exc:
                    recovered["apify_retry_warning"] = f"APIFY_RETRY_FAILED:{safe_reason(exc)}"
            # A retry or cache-write failure must not discard already validated rows.
            out.update(recovered)
            out["acquired_via"] = "apify"
            out["worker_evidence"] = original
            media_client.stage_bytes(
                job_id=job_id,
                name=_CACHE,
                body=json.dumps(recovered, ensure_ascii=False, allow_nan=False).encode(),
                content_type="application/json",
                deadline_epoch_s=deadline_epoch_s,
            )
        out.update(recovered)
        out["acquired_via"] = "apify"
        out["worker_evidence"] = original
    except Exception as exc:
        out["apify_fallback_warning"] = f"APIFY_FALLBACK_FAILED:{safe_reason(exc)}"
    return out
