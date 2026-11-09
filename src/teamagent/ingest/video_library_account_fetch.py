"""Fetch new own-account TikTok posts during nightly ingest."""

from __future__ import annotations

import argparse
import math
import os
import re
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import structlog
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

from teamagent.adapters.apify_client import ACTOR_TIKTOK_VIDEO, ApifyClient
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.adapters.video_library_page import page_client, save_cover
from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.ingest.video_library import Video, video_identity
from teamagent.ingest.video_library_account_repository import OWN_ACCOUNT_OPERATORS
from teamagent.ingest.video_library_url_models import METRIC_KEYS
from teamagent.ingest.video_library_url_repository import JST
from teamagent.observability.logging_config import configure_logging

logger = structlog.get_logger(__name__)


# First fetch only looks back this far; afterwards the window starts the day before the last fetch.
FIRST_FETCH_DAYS = 30
POSTS_PER_ACCOUNT = 10


class FetchResult(BaseModel):
    accounts: int = 0
    batches: int = 0
    items: int = 0
    inserted: int = 0
    metrics: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    covers_saved: int = 0
    covers_skipped: dict[str, int] = Field(default_factory=dict)
    failed_batches: int = 0
    cost_usd: float = 0.0
    reason: str | None = None


def _skip_reason() -> str | None:
    if os.environ.get("VIDEO_LIBRARY_ACCOUNT_FETCH_ENABLED", "1").lower() in {"0", "false"}:
        return "disabled"
    if not os.environ.get("APIFY_API_TOKEN", "").strip():
        return "apify_unconfigured"
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            number = float(value)
            if math.isfinite(number) and number >= 0:
                return number
        except (ValueError, OverflowError):
            pass
    return None


def _metric(value: object) -> int | None:
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value)):
        integer = int(value)
        return integer if 0 <= integer <= 2**63 - 1 else None
    number = _number(value)
    return int(number) if number is not None and number.is_integer() and number < 2**63 else None


def _video(item: dict[str, Any], handles: set[str]) -> Video:
    vid = str(item.get("id") or "")
    author = item.get("authorMeta")
    handle = author.get("name") if isinstance(author, dict) else None
    if not re.fullmatch(r"[0-9]+", vid) or not isinstance(handle, str) or not handle:
        raise ValueError("missing_required")
    assert isinstance(author, dict)
    handle = handle.lstrip("@").lower()
    if handle not in handles:
        raise ValueError("unrequested_handle")
    url = item.get("webVideoUrl")
    if (
        not isinstance(url, str)
        or not url.startswith("https://www.tiktok.com/@")
        or video_identity(url) != ("tiktok", vid, handle)
    ):
        raise ValueError("invalid_url")
    caption = item.get("text") or None
    if caption is not None and not isinstance(caption, str):
        raise ValueError("invalid_item")
    tags = list(
        dict.fromkeys(
            re.findall(
                r"#([^\s#【】\[\](){}「」『』〈〉《》]+)",
                unicodedata.normalize("NFKC", caption or ""),
            )
        )
    )
    meta, music = item.get("videoMeta"), item.get("musicMeta")
    meta = meta if isinstance(meta, dict) else {}
    music = music if isinstance(music, dict) else {}
    return Video(
        platform="tiktok",
        video_id=vid,
        url=url.split("?")[0],
        account_name=author.get("nickName") or handle,
        account_handle=handle,
        posted_at=item.get("createTimeISO"),
        duration_seconds=_number(meta.get("duration")),
        music_name=music.get("musicName"),
        music_author=music.get("musicAuthor"),
        music_original=music.get("musicOriginal")
        if isinstance(music.get("musicOriginal"), bool)
        else None,
        caption=caption,
        hashtags=tags,
        is_pr=any(tag.casefold() == "pr" for tag in tags),
    )


def run(
    pg: PgVectorClient,
    client: ApifyClient,
    *,
    request_id: str,
    storage: VideoLibraryStorage | None = None,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
) -> FetchResult:
    started = time.perf_counter()
    result = FetchResult(reason=_skip_reason())
    log = logger.bind(request_id=request_id, skill="video_library_account_fetch", token_usage=0)
    if result.reason:
        log.info("video_library_account_fetch_skipped", **result.model_dump(), latency_ms=0)
        return result
    if storage is not None and http_client is None:
        raise ValueError("cover_client_required")
    now = (now or datetime.now(JST)).astimezone(JST)
    today = now.date()
    # 11/09 小俣さん「毎日全部取ったら金がかかる・Apify 制限して」: $1/晩（月にして最大 $30）。
    max_usd = Decimal(os.environ.get("VIDEO_LIBRARY_ACCOUNT_FETCH_MAX_USD", "1.0"))
    if not max_usd.is_finite() or max_usd < 0:
        raise ValueError("invalid_account_fetch_budget")
    session: dict[str, Any] = dict(app_role="teamagent_app", user_email=None, user_role="admin")
    with pg.connection(**session) as conn:
        rows = conn.execute(
            """SELECT lower(ltrim(handle,'@')) AS handle,
                bool_or(last_fetched_on IS NULL) AS initial,
                min(coalesce(last_fetched_on - 1, %s)) AS oldest
                FROM vl_accounts WHERE platform='tiktok' AND operator_label=ANY(%s)
                AND coalesce(classification,'') NOT LIKE %s
                AND lower(ltrim(handle,'@')) ~ '^[a-z0-9_.]{2,24}$'
                GROUP BY lower(ltrim(handle,'@'))
                ORDER BY handle""",
            (today - timedelta(days=FIRST_FETCH_DAYS), list(OWN_ACCOUNT_OPERATORS), "%未稼働%"),
        ).fetchall()
    result.accounts = len(rows)
    groups: dict[tuple[bool, date], list[str]] = defaultdict(list)
    for row in rows:
        groups[row["initial"], row["oldest"]].append(row["handle"])
    spent = Decimal(0)
    skipped: Counter[str] = Counter()
    covers_skipped: Counter[str] = Counter()
    # Already-fetched accounts sort first (False < True), oldest checkpoint first.
    for (_initial, oldest), handles in sorted(groups.items()):
        per_account = POSTS_PER_ACCOUNT
        for offset in range(0, len(handles), 20):
            part = handles[offset : offset + 20]
            reservation = len(part) * per_account * Decimal("0.004")
            if spent + reservation > max_usd:
                result.reason = "cost_limit"
                break
            result.batches += 1
            try:
                response = client.run_actor_sync(
                    ACTOR_TIKTOK_VIDEO,
                    {
                        "profiles": part,
                        "resultsPerPage": per_account,
                        "profileSorting": "latest",
                        "profileScrapeSections": ["videos"],
                        "excludePinnedPosts": True,
                        "oldestPostDateUnified": oldest.isoformat(),
                        "shouldDownloadCovers": False,
                        "shouldDownloadSlideshowImages": False,
                        "shouldDownloadVideos": False,
                    },
                    max_items=per_account * len(part),
                    deadline_s=900,
                    request_id=request_id,
                    usage_mode="tiktok_profiles_date",
                )
                spent += Decimal(str(response.estimated_cost_usd))
                result.cost_usd = float(spent)
                result.items += len(response.items)
                counts: Counter[str] = Counter()
                covers: list[tuple[str, object]] = []
                with pg.connection(**session) as conn:
                    for item in response.items:
                        try:
                            video = _video(item, set(part))
                        except ValidationError:
                            skipped["invalid_item"] += 1
                            continue
                        except ValueError as exc:
                            skipped[str(exc)] += 1
                            continue
                        cur = conn.execute(
                            """INSERT INTO vl_videos
                               (platform,video_id,url,account_name,account_handle,
                               posted_at,duration_seconds,music_name,music_author,music_original,
                               caption,hashtags,origin,is_pr,page_is_ad)
                               VALUES ('tiktok',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'own',%s,%s)
                               ON CONFLICT (platform,video_id) DO NOTHING""",
                            (
                                video.video_id,
                                video.url,
                                video.account_name,
                                video.account_handle,
                                video.posted_at,
                                video.duration_seconds,
                                video.music_name,
                                video.music_author,
                                video.music_original,
                                video.caption,
                                video.hashtags,
                                video.is_pr,
                                item.get("isAd") if isinstance(item.get("isAd"), bool) else None,
                            ),
                        )
                        counts["inserted"] += int(cur.rowcount == 1)
                        metrics = [_metric(item.get(key)) for key in METRIC_KEYS]
                        cur = conn.execute(
                            """INSERT INTO vl_public_metrics
                               (platform,video_id,captured_on,captured_at,total_plays,total_likes,
                                total_comments,total_shares,total_saves,metric_observation)
                               VALUES ('tiktok',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (platform,video_id,captured_on) DO UPDATE SET
                               captured_at=EXCLUDED.captured_at,total_plays=EXCLUDED.total_plays,
                               total_likes=EXCLUDED.total_likes,total_comments=EXCLUDED.total_comments,
                               total_shares=EXCLUDED.total_shares,total_saves=EXCLUDED.total_saves,
                               metric_observation=EXCLUDED.metric_observation
                               WHERE vl_public_metrics.captured_at<=EXCLUDED.captured_at""",
                            (
                                video.video_id,
                                today,
                                now,
                                *metrics,
                                Jsonb({"source": "public_page"}),
                            ),
                        )
                        counts["metrics"] += cur.rowcount
                        meta = item.get("videoMeta")
                        url = (
                            (meta.get("coverUrl") or meta.get("originalCoverUrl"))
                            if isinstance(meta, dict)
                            else None
                        )
                        covers.append((video.video_id, url))
                    conn.execute(
                        """UPDATE vl_accounts SET last_fetched_on=%s WHERE platform='tiktok'
                            AND operator_label=ANY(%s) AND coalesce(classification,'') NOT LIKE %s
                            AND lower(ltrim(handle,'@')) ~ '^[a-z0-9_.]{2,24}$'
                            AND lower(ltrim(handle,'@'))=ANY(%s)""",
                        (today, list(OWN_ACCOUNT_OPERATORS), "%未稼働%", part),
                    )
                result.inserted += counts["inserted"]
                result.metrics += counts["metrics"]
                # Finish the batch transaction before any image download or S3 upload.
                for video_id, url in covers:
                    if storage is None:
                        covers_skipped["storage_unconfigured"] += 1
                        continue
                    assert http_client is not None
                    try:
                        with pg.connection(**session) as conn:
                            cover_row = conn.execute(
                                """SELECT thumbnail_s3_key FROM vl_videos
                                   WHERE platform=%s AND video_id=%s""",
                                ("tiktok", video_id),
                            ).fetchone()
                        if cover_row is None or cover_row["thumbnail_s3_key"] is not None:
                            covers_skipped["already_present"] += 1
                            continue
                        if not url:
                            covers_skipped["cover_missing"] += 1
                            continue
                        if not isinstance(url, str):
                            raise ValueError("invalid_cover_url")
                        key = save_cover(http_client, storage, "tiktok", video_id, url)
                        with pg.connection(**session) as conn:
                            updated = conn.execute(
                                """UPDATE vl_videos SET thumbnail_s3_key=%s
                                   WHERE platform=%s AND video_id=%s
                                   AND thumbnail_s3_key IS NULL""",
                                (key, "tiktok", video_id),
                            ).rowcount
                        if updated == 1:
                            result.covers_saved += 1
                        else:
                            covers_skipped["already_present"] += 1
                    except httpx.HTTPStatusError as exc:
                        covers_skipped[f"http_{exc.response.status_code}"] += 1
                    except Exception as exc:
                        reason = type(exc).__name__
                        if isinstance(exc, ValueError) and str(exc) in {
                            "invalid_cover_url",
                            "invalid_cover_image",
                            "cover_too_large",
                        }:
                            reason = str(exc)
                        covers_skipped[reason] += 1
            except Exception as exc:
                result.failed_batches += 1
                log.warning(
                    "video_library_account_fetch_batch_failed",
                    batch=result.batches,
                    error=type(exc).__name__,
                    cost_usd=result.cost_usd,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
        if result.reason == "cost_limit":
            break
    result.skipped = dict(skipped)
    result.covers_skipped = dict(covers_skipped)
    log.info(
        "video_library_account_fetch_complete",
        **result.model_dump(),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    configure_logging()
    pg = None
    request_id = f"vl-account-fetch-{uuid.uuid4().hex}"
    with structlog.contextvars.bound_contextvars(
        request_id=request_id, skill="video_library_account_fetch"
    ):
        reason = _skip_reason()
        if reason:
            logger.info(
                "video_library_account_fetch_skipped",
                **FetchResult(reason=reason).model_dump(),
                token_usage=0,
                latency_ms=0,
            )
            return 0
        try:
            pg = PgVectorClient.from_env()
            storage = VideoLibraryStorage() if os.environ.get("VIDEO_LIBRARY_S3_BUCKET") else None
            with page_client() as http_client:
                result = run(
                    pg,
                    ApifyClient.from_env(),
                    request_id=request_id,
                    storage=storage,
                    http_client=http_client,
                )
            return int(result.failed_batches > 0)
        except Exception as exc:
            logger.error(
                "video_library_account_fetch_failed",
                error=type(exc).__name__,
                token_usage=0,
                cost_usd=0,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return 1
        finally:
            if pg:
                pg.close()


if __name__ == "__main__":
    raise SystemExit(main())
