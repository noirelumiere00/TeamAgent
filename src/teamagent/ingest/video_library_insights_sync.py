"""Merge owner-only TikTok analytics from the nightly analysis sheet."""

from __future__ import annotations

import argparse
import math
import os
import re
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime
from typing import Annotated

import structlog
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from teamagent.adapters.google_oauth_env import expand_google_oauth_json
from teamagent.adapters.gsheets_client import GSheetsClient
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library import Video, video_identity
from teamagent.ingest.video_library_account_repository import OWN_ACCOUNT_OPERATORS
from teamagent.ingest.video_library_url_repository import JST
from teamagent.observability.logging_config import configure_logging

logger = structlog.get_logger(__name__)
DEFAULT_SHEET_ID = "1MC3g5_O7ugeBaccY8ioRadyg1k6xUE04RZCLA2AIAO0"
PUBLIC_COLUMNS = ("視聴数", "いいね数", "コメント数", "シェア数", "保存数")
AGE_COLUMNS = tuple(f"年齢{i}位" for i in range(1, 6))
RETENTION_COLUMNS = tuple(f"{i}秒" for i in range(48))
PRIVATE_COLUMNS = (
    "リーチ",
    "完了率",
    "平均視聴時間(秒)",
    "プロフィール閲覧回数",
    "男性%",
    "女性%",
    *AGE_COLUMNS,
    *RETENTION_COLUMNS,
)
REQUIRED_HEADERS = (
    "データ取得日時",
    "アカウント",
    "URL",
    "投稿日",
    "タイトル",
    "動画尺",
    *PUBLIC_COLUMNS,
    *PRIVATE_COLUMNS,
)


class AgeBand(BaseModel):
    band: str
    ratio: float = Field(ge=0, le=1)


class InsightRow(BaseModel):
    video: Video
    captured_at: datetime
    # Bounds mirror 0043's CHECKs so one bad cell skips its row instead of aborting the night.
    public_metrics: list[Annotated[int, Field(ge=0)] | None]
    reach: Annotated[int, Field(ge=0)] | None
    completion_rate: Annotated[float, Field(ge=0, le=1)] | None
    avg_watch_seconds: Annotated[float, Field(ge=0)] | None
    profile_views: Annotated[int, Field(ge=0)] | None
    male_ratio: Annotated[float, Field(ge=0, le=1)] | None
    female_ratio: Annotated[float, Field(ge=0, le=1)] | None
    age_bands: list[AgeBand]
    retention: list[int]
    has_insights: bool


class SyncResult(BaseModel):
    rows: int = 0
    videos: int = 0
    inserted_videos: int = 0
    insights: int = 0
    public_metrics: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    reason: str | None = None
    cost_usd: float = 0.0


def _skip_reason() -> str | None:
    if os.environ.get("VIDEO_LIBRARY_INSIGHTS_ENABLED", "1").lower() in {"0", "false"}:
        return "disabled"
    if not os.environ.get("VIDEO_LIBRARY_ANALYTICS_SHEET_ID", DEFAULT_SHEET_ID).strip():
        return "sheet_unconfigured"
    return None


def _number(value: str, *, ratio: bool = False) -> float | None:
    text = value.strip().replace(",", "")
    try:
        number = float(text.removesuffix("%") if ratio else text)
    except ValueError:
        return None
    if ratio and text.endswith("%"):
        number /= 100
    if not math.isfinite(number) or number < 0 or (ratio and number > 1):
        return None
    return number


def _count(value: str) -> int | None:
    text = value.strip().replace(",", "")
    if not re.fullmatch(r"[0-9]{1,10}", text):
        return None
    number = int(text)
    return number if number <= 2**31 - 1 else None


def _captured_at(value: str) -> datetime:
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=JST)
        except ValueError:
            pass
    raise ValueError("invalid_captured_at")


def parse_row(row: dict[str, str]) -> InsightRow:
    identity = video_identity(row["URL"].strip())
    if identity is None or identity[0] != "tiktok":
        raise ValueError("invalid_url")
    platform, video_id, handle = identity
    captured_at = _captured_at(row["データ取得日時"])
    try:
        posted_at = datetime.strptime(row["投稿日"].strip(), "%Y-%m-%d").replace(tzinfo=JST)
    except ValueError:
        posted_at = None
    duration = re.fullmatch(r"([0-9]+):([0-5][0-9])", row["動画尺"].strip())
    caption = row["タイトル"].strip() or None
    tags = list(
        dict.fromkeys(
            re.findall(
                r"#([^\s#【】\[\](){}「」『』〈〉《》]+)",
                unicodedata.normalize("NFKC", caption or ""),
            )
        )
    )
    ages = []
    for column in AGE_COLUMNS:
        match = re.fullmatch(r"\s*([0-9]+(?:-[0-9]+|\+))\s*:\s*(.+)", row[column])
        if match and (ratio := _number(match[2], ratio=True)) is not None:
            ages.append(AgeBand(band=match[1], ratio=ratio))
    retention = []
    for column in RETENTION_COLUMNS:
        count = _count(row[column])
        if count is None:
            break
        retention.append(count)
    return InsightRow(
        video=Video(
            platform=platform,
            video_id=video_id,
            url=f"https://www.tiktok.com/@{handle}/video/{video_id}",
            account_handle=handle,
            caption=caption,
            posted_at=posted_at,
            duration_seconds=int(duration[1]) * 60 + int(duration[2]) if duration else None,
            hashtags=tags,
            is_pr=any(tag.casefold() == "pr" for tag in tags),
        ),
        captured_at=captured_at,
        public_metrics=[_count(row[column]) for column in PUBLIC_COLUMNS],
        reach=_count(row["リーチ"]),
        completion_rate=_number(row["完了率"], ratio=True),
        avg_watch_seconds=_number(row["平均視聴時間(秒)"]),
        profile_views=_count(row["プロフィール閲覧回数"]),
        male_ratio=_number(row["男性%"], ratio=True),
        female_ratio=_number(row["女性%"], ratio=True),
        age_bands=ages,
        retention=retention,
        has_insights=any(row[column].strip() for column in PRIVATE_COLUMNS),
    )


def run(pg: PgVectorClient, sheets: GSheetsClient, *, request_id: str) -> SyncResult:
    started = time.perf_counter()
    result = SyncResult(reason=_skip_reason())
    log = logger.bind(request_id=request_id, skill="video_library_insights_sync", token_usage=0)
    if result.reason:
        log.info("video_library_insights_skipped", **result.model_dump(), latency_ms=0)
        return result
    table = sheets.get_tab_rows(
        os.environ.get("VIDEO_LIBRARY_ANALYTICS_SHEET_ID", DEFAULT_SHEET_ID).strip(),
        "シート1",
        request_id,
    )
    headers = tuple(header.strip() for header in table.headers)
    named = [header for header in headers if header]
    # The real sheet has ~70 blank header cells to the right; only named columns must be unique.
    if not set(REQUIRED_HEADERS).issubset(headers) or len(set(named)) != len(named):
        log.error(
            "video_library_insights_columns_missing",
            token_usage=0,
            cost_usd=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise ValueError("analytics_columns_missing")
    result.rows = len(table.rows)
    skipped: Counter[str] = Counter()
    seen: set[str] = set()
    with pg.connection(app_role="teamagent_app", user_email=None, user_role="admin") as conn:
        own_handles = {
            row["handle"]
            for row in conn.execute(
                """SELECT lower(ltrim(handle,'@')) AS handle FROM vl_accounts
                   WHERE platform='tiktok' AND operator_label=ANY(%s)""",
                (list(OWN_ACCOUNT_OPERATORS),),
            ).fetchall()
        }
        existing = {
            row["video_id"]
            for row in conn.execute(
                "SELECT video_id FROM vl_videos WHERE platform='tiktok'"
            ).fetchall()
        }
        for cells in table.rows:
            values = dict(zip(headers, cells, strict=False))
            try:
                row = parse_row({header: values.get(header, "") for header in REQUIRED_HEADERS})
            except ValueError as exc:
                reason = str(exc)
                skipped[
                    reason if reason in {"invalid_url", "invalid_captured_at"} else "invalid_row"
                ] += 1
                continue
            video = row.video
            if video.video_id not in existing:
                if video.account_handle not in own_handles:
                    skipped["unregistered_external_handle"] += 1
                    continue
                cur = conn.execute(
                    """INSERT INTO vl_videos
                       (platform,video_id,url,account_handle,caption,posted_at,duration_seconds,
                        hashtags,is_pr,origin)
                       VALUES ('tiktok',%s,%s,%s,%s,%s,%s,%s,%s,'own')
                       ON CONFLICT (platform,video_id) DO NOTHING""",
                    (
                        video.video_id,
                        video.url,
                        video.account_handle,
                        video.caption,
                        video.posted_at,
                        video.duration_seconds,
                        video.hashtags,
                        video.is_pr,
                    ),
                )
                result.inserted_videos += cur.rowcount
                existing.add(video.video_id)
            seen.add(video.video_id)
            if row.has_insights:
                conn.execute(
                    """INSERT INTO vl_video_insights
                       (platform,video_id,captured_at,views,likes,comments,shares,saves,reach,
                        completion_rate,avg_watch_seconds,profile_views,male_ratio,female_ratio,
                        age_bands,retention)
                       VALUES ('tiktok',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (platform,video_id,captured_at) DO UPDATE SET
                       views=EXCLUDED.views,likes=EXCLUDED.likes,comments=EXCLUDED.comments,
                       shares=EXCLUDED.shares,saves=EXCLUDED.saves,reach=EXCLUDED.reach,
                       completion_rate=EXCLUDED.completion_rate,
                       avg_watch_seconds=EXCLUDED.avg_watch_seconds,
                       profile_views=EXCLUDED.profile_views,male_ratio=EXCLUDED.male_ratio,
                       female_ratio=EXCLUDED.female_ratio,age_bands=EXCLUDED.age_bands,
                       retention=EXCLUDED.retention,source=EXCLUDED.source""",
                    (
                        video.video_id,
                        row.captured_at,
                        *row.public_metrics,
                        row.reach,
                        row.completion_rate,
                        row.avg_watch_seconds,
                        row.profile_views,
                        row.male_ratio,
                        row.female_ratio,
                        Jsonb([age.model_dump() for age in row.age_bands]),
                        row.retention,
                    ),
                )
                result.insights += 1
            else:
                skipped["no_insights"] += 1
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
                    row.captured_at.astimezone(JST).date(),
                    row.captured_at,
                    *row.public_metrics,
                    Jsonb({"source": "analytics_sheet"}),
                ),
            )
            result.public_metrics += cur.rowcount
    result.videos = len(seen)
    result.skipped = dict(skipped)
    log.info(
        "video_library_insights_complete",
        **result.model_dump(),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    configure_logging()
    request_id = f"vl-insights-{uuid.uuid4().hex}"
    started = time.perf_counter()
    pg = None
    with structlog.contextvars.bound_contextvars(
        request_id=request_id, skill="video_library_insights_sync"
    ):
        if reason := _skip_reason():
            logger.info(
                "video_library_insights_skipped",
                **SyncResult(reason=reason).model_dump(),
                token_usage=0,
                latency_ms=0,
            )
            return 0
        try:
            expand_google_oauth_json()
            pg = PgVectorClient.from_env()
            run(pg, GSheetsClient.from_env(), request_id=request_id)
            return 0
        except Exception as exc:
            logger.error(
                "video_library_insights_failed",
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
