"""Discover own TikTok cases, register grounded names and infer only their target videos."""

from __future__ import annotations

import argparse
import os
import re
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import structlog
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from teamagent.adapters.apify_client import ACTOR_TIKTOK_VIDEO, ApifyClient
from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library import video_identity
from teamagent.ingest.video_library_case_inference import (
    BudgetExceededError,
    Inference,
    default_policy_path,
)
from teamagent.ingest.video_library_case_inference import (
    run as infer_cases,
)
from teamagent.ingest.video_library_case_models import (
    _FENCE,
    DEFAULT_MODEL,
    Case,
    Policy,
    Score,
    StrictModel,
    _ground,
)
from teamagent.ingest.video_library_case_repository import CaseRepository
from teamagent.observability.logging_config import configure_logging
from teamagent.prompts.loader import load_prompt

logger = structlog.get_logger(__name__)
GENERIC_TAGS = frozenset(
    {
        "pr",
        "fyp",
        "foryou",
        "foryoupage",
        "おすすめ",
        "おすすめにのりたい",
        "おすすめに載りたい",
        "tiktok",
        "広告",
        "提供",
        "プロモーション",
        "ad",
        "ads",
        "sponsored",
        "sponsor",
        "タイアップ",
        "promotion",
        "promoted",
        "宣伝",
    }
)
PRICE_PER_VIDEO = Decimal("0.004")
MAX_SUBTITLE_BYTES = 1024 * 1024
Status = Literal["sampled", "registered", "existing", "candidate", "unidentifiable", "failed"]
TARGET_VIDEOS_SQL = """SELECT video_id,url,caption,hashtags,posted_at FROM vl_videos_effective
    WHERE platform='tiktok' AND origin='own' AND is_pr AND campaign IS NULL"""


class DiscoveryConfig(BaseModel):
    min_videos: int = Field(default=10, ge=1)
    max_tags: int = Field(default=5, ge=0)
    max_usd: Decimal = Field(default=Decimal("0.25"), ge=0, allow_inf_nan=False)
    haiku_max_usd: Decimal = Field(default=Decimal("0.2"), ge=0, allow_inf_nan=False)

    @classmethod
    def from_env(cls) -> DiscoveryConfig:
        prefix = "VIDEO_LIBRARY_CASE_DISCOVERY_"
        return cls.model_validate(
            {
                key: os.environ[prefix + key.upper()]
                for key in cls.model_fields
                if prefix + key.upper() in os.environ
            }
        )


class DiscoveryVideo(BaseModel):
    video_id: str
    url: str
    caption: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    posted_at: datetime | None = None

    @field_validator("posted_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("naive_posted_at")
        return value


class DiscoveryHistory(BaseModel):
    tag: str
    status: Status
    attempted_at: datetime


class Cluster(BaseModel):
    tag: str
    video_ids: set[str]
    videos: list[DiscoveryVideo]


class Sample(BaseModel):
    video_id: str
    caption: str = ""
    subtitle_text: str = ""
    no_subtitle: bool = True


class CollectResult(BaseModel):
    tags: int = 0
    videos: int = 0
    items: int = 0
    sampled: int = 0
    failed: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0
    reserved_usd: float = 0


class DiscoveryInput(StrictModel):
    tag: str
    samples: list[Sample]
    existing_cases: list[Case]


class DiscoveryDecision(StrictModel):
    decision: Literal["new", "existing", "unknown"]
    advertiser: str | None
    campaign: str | None
    advertiser_source: Literal["text", "knowledge"] | None
    confidence: Score
    evidence: list[str] = Field(max_length=10)


class DecisionResult(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    video_ids: set[str] = Field(default_factory=set, exclude=True)


class DiscoveryInference(Inference):
    def predict_cluster(self, data: DiscoveryInput, *, request_id: str) -> DiscoveryDecision:
        text = self._response(data, load_prompt("case_discovery", "v1", "system"), request_id)
        fenced = _FENCE.match(text)
        return DiscoveryDecision.model_validate_json(fenced.group(1) if fenced else text)


def decision_status(prediction: DiscoveryDecision, data: DiscoveryInput) -> Status:
    if prediction.decision == "unknown":
        return "unidentifiable"
    if prediction.advertiser_source == "knowledge" and prediction.confidence < 0.85:
        prediction.advertiser = prediction.campaign
    for field in ("advertiser", "campaign"):
        value = getattr(prediction, field)
        if (
            not value
            or any(unicodedata.category(char).startswith("C") for char in value)
            or not 1 <= len(value.strip()) <= 80
        ):
            return "candidate"
        setattr(prediction, field, value.strip())
    assert prediction.campaign and prediction.advertiser
    if prediction.advertiser_source is None:
        return "candidate"
    # A campaign must be a short name (hashtag-like); sentences never match later posts.
    if len(prediction.campaign) > 30 or any(ch in prediction.campaign for ch in "、。！？!?"):
        return "candidate"
    if not any(_ground(prediction.campaign) in _ground(s.caption) for s in data.samples):
        return "candidate"
    bodies = [text for s in data.samples for text in (s.caption, s.subtitle_text)]
    if not prediction.evidence or any(
        not _ground(quote) or not any(_ground(quote) in _ground(body) for body in bodies)
        for quote in prediction.evidence
    ):
        return "candidate"
    if prediction.advertiser_source == "text" and not any(
        _ground(prediction.advertiser) in _ground(body) for body in bodies
    ):
        return "candidate"
    pair_exists = any(
        c.advertiser == prediction.advertiser and c.campaign == prediction.campaign
        for c in data.existing_cases
    )
    if prediction.decision == "existing":
        return "existing" if pair_exists else "candidate"
    if pair_exists:
        return "existing"
    return "registered" if prediction.confidence >= 0.75 else "candidate"


def normalize_tag(tag: str) -> str:
    return unicodedata.normalize("NFKC", tag).strip().lstrip("#").lower()


def select_clusters(
    videos: list[DiscoveryVideo],
    history: list[DiscoveryHistory],
    *,
    config: DiscoveryConfig,
    now: datetime,
) -> list[Cluster]:
    blocked = {
        row.tag
        for row in history
        if row.status in {"registered", "existing"}
        or (
            row.status in {"candidate", "failed", "unidentifiable"}
            and now
            < row.attempted_at + timedelta(days=90 if row.status == "unidentifiable" else 30)
        )
    }
    grouped: dict[str, dict[str, DiscoveryVideo]] = defaultdict(dict)
    for video in videos:
        for tag in {normalize_tag(tag) for tag in video.hashtags} - GENERIC_TAGS - {""}:
            grouped[tag][video.video_id] = video
    selected: list[Cluster] = []
    for tag, members in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(selected) >= config.max_tags:
            break
        if len(members) < config.min_videos or tag in blocked:
            continue
        ids = set(members)
        # Compare each candidate's membership to each selected tag (including exactly 80%).
        if any(len(ids & other.video_ids) * 5 >= len(ids) * 4 for other in selected):
            continue
        latest = sorted(
            members.values(),
            key=lambda video: (video.posted_at or datetime.min.replace(tzinfo=UTC), video.video_id),
            reverse=True,
        )[:2]
        selected.append(Cluster(tag=tag, video_ids=ids, videos=latest))
    return selected


def is_subtitle_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and not any(ord(char) <= 32 or ord(char) == 127 for char in url)
            and any(
                host == domain or host.endswith("." + domain)
                for domain in (
                    "tiktok.com",
                    "tiktokcdn.com",
                    "tiktokcdn-us.com",
                    "tiktokcdn-eu.com",
                )
            )
        )
    except ValueError:
        return False


def webvtt_text(body: str) -> str:
    lines: list[str] = []
    for block in re.split(r"\n\s*\n", body.replace("\r\n", "\n").lstrip("\ufeff")):
        cue = block.splitlines()
        if not cue or cue[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing = next((i for i, line in enumerate(cue) if "-->" in line), None)
        if timing is not None:
            lines.extend(
                unescape(re.sub(r"<[^>]*>", "", line)).strip()
                for line in cue[timing + 1 :]
                if line.strip() and "-->" not in line
            )
    return "\n".join(line for line in lines if line)[:3000]


def _subtitle(item: dict[str, object], http_client: httpx.Client, counts: Counter[str]) -> str:
    meta = item.get("videoMeta")
    links = meta.get("subtitleLinks") if isinstance(meta, dict) else None
    if not isinstance(links, list):
        return ""
    tracks = [link for link in links if isinstance(link, dict)]
    tracks.sort(
        key=lambda link: (
            not str(link.get("language", "")).lower().startswith(("ja", "jpn", "日本語"))
        )
    )
    for track in tracks:
        url = track.get("downloadLink") or track.get("tiktokLink")
        if not isinstance(url, str) or not is_subtitle_url(url):
            counts["subtitle_url_rejected"] += 1
            continue
        try:
            with http_client.stream("GET", url, follow_redirects=False, timeout=20) as response:
                if not 200 <= response.status_code < 300:
                    counts["subtitle_http_error"] += 1
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_SUBTITLE_BYTES:
                        break
                if len(body) > MAX_SUBTITLE_BYTES:
                    counts["subtitle_too_large"] += 1
                    continue
                text = webvtt_text(body.decode("utf-8-sig", errors="replace"))
                if text:
                    return text
        except httpx.HTTPError:
            counts["subtitle_http_error"] += 1
    return ""


def _save(
    pg: PgVectorClient,
    clusters: list[Cluster],
    samples: dict[str, Sample],
    *,
    status: Status,
    now: datetime,
) -> None:
    with pg.connection(app_role="teamagent_app", user_email=None, user_role="admin") as conn:
        for cluster in clusters:
            ids = [video.video_id for video in cluster.videos]
            conn.execute(
                """INSERT INTO vl_case_discovery
                   (tag,video_count,sample_video_ids,samples,status,attempted_at)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tag) DO UPDATE SET
                   video_count=EXCLUDED.video_count,sample_video_ids=EXCLUDED.sample_video_ids,
                   samples=EXCLUDED.samples,status=EXCLUDED.status,attempted_at=EXCLUDED.attempted_at,
                   advertiser=NULL,campaign=NULL,confidence=NULL,evidence=NULL,decided_at=NULL
                   WHERE vl_case_discovery.status NOT IN ('registered','existing')""",
                (
                    cluster.tag,
                    len(cluster.video_ids),
                    ids,
                    Jsonb([samples[vid].model_dump() for vid in ids]),
                    status,
                    now,
                ),
            )


def collect(
    pg: PgVectorClient,
    client: ApifyClient,
    *,
    http_client: httpx.Client,
    request_id: str,
    config: DiscoveryConfig | None = None,
    now: datetime | None = None,
) -> CollectResult:
    started = time.perf_counter()
    config = config or DiscoveryConfig.from_env()
    now = now or datetime.now(UTC)
    with pg.connection(app_role="teamagent_app", user_email=None, user_role="admin") as conn:
        videos = [
            DiscoveryVideo.model_validate(row) for row in conn.execute(TARGET_VIDEOS_SQL).fetchall()
        ]
        history = [
            DiscoveryHistory.model_validate(row)
            for row in conn.execute(
                "SELECT tag,status,attempted_at FROM vl_case_discovery"
            ).fetchall()
        ]
    clusters = select_clusters(videos, history, config=config, now=now)
    counts: Counter[str] = Counter()
    chosen: list[Cluster] = []
    requested: dict[str, DiscoveryVideo] = {}
    for cluster in clusters:
        proposed = requested | {video.video_id: video for video in cluster.videos}
        if len(proposed) * PRICE_PER_VIDEO > config.max_usd:
            counts["budget_skipped"] += 1
            continue
        chosen.append(cluster)
        requested = proposed
    result = CollectResult(
        tags=len(chosen),
        videos=len(requested),
        reserved_usd=float(len(requested) * PRICE_PER_VIDEO),
    )
    samples = {
        vid: Sample(video_id=vid, caption=video.caption or "") for vid, video in requested.items()
    }
    if requested:
        urls = []
        for vid, video in requested.items():
            identity = video_identity(video.url)
            if identity is None or identity[:2] != ("tiktok", vid):
                raise ValueError("invalid_sample_url")
            urls.append(f"https://www.tiktok.com/@{identity[2]}/video/{vid}")
        try:
            response = client.run_actor_sync(
                ACTOR_TIKTOK_VIDEO,
                {
                    "postURLs": urls,
                    "shouldDownloadSubtitles": True,
                    "shouldDownloadVideos": False,
                    "shouldDownloadCovers": False,
                    "shouldDownloadSlideshowImages": False,
                },
                max_items=len(requested),
                deadline_s=900,
                request_id=request_id,
            )
        except Exception:
            # A timeout may already be charged; retain the full reservation and cool down failures.
            result.failed = len(chosen)
            result.cost_usd = result.reserved_usd
            _save(pg, chosen, samples, status="failed", now=now)
        else:
            result.items = len(response.items)
            result.cost_usd = response.estimated_cost_usd
            seen: set[str] = set()
            for item in response.items:
                if item.get("error") or item.get("errorCode"):
                    counts["error_items"] += 1
                    continue
                vid = str(item.get("id") or "")
                if vid not in requested:
                    counts["unrequested_items"] += 1
                    continue
                if vid in seen:
                    counts["duplicate_items"] += 1
                    continue
                seen.add(vid)
                caption = item.get("text")
                if isinstance(caption, str):
                    samples[vid].caption = caption
                text = _subtitle(item, http_client, counts)
                samples[vid].subtitle_text = text
                samples[vid].no_subtitle = not bool(text)
            counts["missing_items"] = len(requested.keys() - seen)
            counts["no_subtitle"] = sum(sample.no_subtitle for sample in samples.values())
            _save(pg, chosen, samples, status="sampled", now=now)
            result.sampled = len(chosen)
    result.counts = dict(counts)
    logger.info(
        "case_discovery_collected",
        request_id=request_id,
        skill="video_library_case_discovery",
        token_usage=0,
        latency_ms=int((time.perf_counter() - started) * 1000),
        **result.model_dump(),
    )
    return result


def _save_decision(
    pg: PgVectorClient,
    data: DiscoveryInput,
    prediction: DiscoveryDecision | None,
    status: Status,
    attempted_at: datetime,
) -> bool:
    with pg.connection(app_role="teamagent_app", user_email=None, user_role="admin") as conn:
        saved = conn.execute(
            """UPDATE vl_case_discovery SET status=%s,advertiser=%s,campaign=%s,
               confidence=%s,evidence=%s,decided_at=clock_timestamp()
               WHERE tag=%s AND status='sampled' AND attempted_at=%s""",
            (
                status,
                prediction.advertiser if prediction else None,
                prediction.campaign if prediction else None,
                prediction.confidence if prediction else None,
                Jsonb(prediction.evidence) if prediction else None,
                data.tag,
                attempted_at,
            ),
        )
        if saved.rowcount != 1:
            return False
        if status == "registered":
            assert prediction and prediction.advertiser and prediction.campaign
            conn.execute(
                """INSERT INTO vl_campaigns(advertiser,campaign,created_by,created_via,created_at)
                   VALUES (%s,%s,'case_discovery@system','case_discovery',clock_timestamp())
                   ON CONFLICT DO NOTHING""",
                (prediction.advertiser, prediction.campaign),
            )
    return True


def decide(
    pg: PgVectorClient,
    inference: DiscoveryInference,
    *,
    request_id: str,
    max_tags: int = 5,
) -> DecisionResult:
    if max_tags < 0:
        raise ValueError("invalid_tag_limit")
    with pg.connection(app_role="teamagent_app", user_email=None, user_role="admin") as conn:
        cases = CaseRepository.cases(conn)
        rows = conn.execute(
            """SELECT tag,samples,attempted_at FROM vl_case_discovery
               WHERE status='sampled' ORDER BY video_count DESC,tag LIMIT %s""",
            (max_tags,),
        ).fetchall()
        videos = [
            DiscoveryVideo.model_validate(row) for row in conn.execute(TARGET_VIDEOS_SQL).fetchall()
        ]
    result = DecisionResult()
    counts: Counter[str] = Counter()
    for row in rows:
        data = DiscoveryInput(tag=row["tag"], samples=row["samples"], existing_cases=cases)
        prediction = None
        try:
            prediction = inference.predict_cluster(data, request_id=request_id)
            status = decision_status(prediction, data)
        except BudgetExceededError:
            counts["budget_stop"] += 1
            break
        except Exception:
            status = "failed"
        if not _save_decision(pg, data, prediction, status, row["attempted_at"]):
            counts["stale"] += 1
            continue
        counts[status] += 1
        if status == "registered":
            assert prediction and prediction.advertiser and prediction.campaign
            cases.append(Case(advertiser=prediction.advertiser, campaign=prediction.campaign))
            result.video_ids.update(
                video.video_id
                for video in videos
                if data.tag in {normalize_tag(tag) for tag in video.hashtags}
            )
        logger.info(
            "case_discovery_decided",
            request_id=request_id,
            tag=data.tag,
            counts={status: 1},
            cost_usd=inference.cost_usd,
            reserved_usd=inference.reserved_usd,
        )
    result.counts = dict(counts)
    return result


def _skip_reason() -> str | None:
    if os.environ.get("VIDEO_LIBRARY_CASE_DISCOVERY_ENABLED", "1").lower() in {"0", "false"}:
        return "disabled"
    if not os.environ.get("APIFY_API_TOKEN", "").strip():
        return "apify_unconfigured"
    if not BedrockClient.credentials_available():
        return "bedrock_unconfigured"
    return None


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    configure_logging()
    request_id = uuid.uuid4().hex
    pg = None
    try:
        if reason := _skip_reason():
            logger.info("case_discovery_skipped", request_id=request_id, reason=reason, cost_usd=0)
            return 0
        config = DiscoveryConfig.from_env()
        if config.haiku_max_usd == 0:
            logger.info(
                "case_discovery_skipped",
                request_id=request_id,
                reason="haiku_budget_zero",
                cost_usd=0,
            )
            return 0
        inference = DiscoveryInference(
            BedrockClient.from_env(
                model_id_override=os.environ.get("VIDEO_LIBRARY_CASE_MODEL", DEFAULT_MODEL)
            ),
            max_cost_usd=float(config.haiku_max_usd),
        )
        pg = PgVectorClient.from_env()
        with httpx.Client() as http_client:
            result = collect(
                pg,
                ApifyClient.from_env(),
                http_client=http_client,
                config=config,
                request_id=request_id,
            )
        decisions = decide(pg, inference, request_id=request_id, max_tags=config.max_tags)
        inferred: Counter[str] = Counter()
        attach_cost = 0.0
        if decisions.video_ids:
            path = Path(os.environ.get("VIDEO_LIBRARY_CASE_POLICY") or default_policy_path())
            policy = Policy.model_validate_json(path.read_text()) if path.is_file() else None
            # Same-night attach gets its own reservation budget: case inference reserves ~7x its
            # actual cost, so sharing the $0.2 discovery cap would stop after ~10 videos.
            attach = Inference(
                inference.client,
                max_cost_usd=float(
                    os.environ.get("VIDEO_LIBRARY_CASE_DISCOVERY_INFER_MAX_USD", "2")
                ),
            )
            inferred = infer_cases(
                CaseRepository(pg),
                attach,
                policy=policy,
                limit=200,
                dry_run=False,
                request_id=request_id,
                video_ids=decisions.video_ids,
            )
            attach_cost = attach.cost_usd
        logger.info(
            "case_discovery_complete",
            request_id=request_id,
            counts=decisions.counts,
            inferred=dict(inferred),
            cost_usd=inference.cost_usd + attach_cost,
            reserved_usd=inference.reserved_usd,
        )
        return int(
            result.failed > 0 or decisions.counts.get("failed", 0) > 0 or inferred["failed"] > 0
        )
    except Exception:
        logger.error("case_discovery_failed", request_id=request_id, token_usage=0, cost_usd=0)
        return 1
    finally:
        if pg is not None:
            pg.close()


if __name__ == "__main__":
    raise SystemExit(main())
