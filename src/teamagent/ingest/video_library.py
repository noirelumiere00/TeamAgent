"""Video library P1: deterministic sheet mapping, without network or AI calls."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import UTC, date, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from teamagent.adapters.gsheets_client import TabRows
from teamagent.ingest import campaign_aggregate as ca

ACCOUNTS_TABS = ("ベクトルメディア", "オフショア込み")
Platform = Literal["tiktok", "instagram", "youtube"]
ACCOUNT_COLUMNS: tuple[tuple[Platform, str], ...] = (
    ("tiktok", "TT"),
    ("instagram", "IG"),
    ("youtube", "YT"),
)


# Sheet columns are mapped explicitly: totals, organic and paid must never be combined.
METRIC_COLUMNS = {
    "organic_plays": "オーガニック再生数",
    "organic_likes": "オーガニックいいね",
    "organic_shares": "オーガニックシェア",
    "organic_comments": "オーガニックコメント",
    "organic_saves": "オーガニック保存",
    "total_plays": ca.PLAYS_COL,
    "total_likes": ca.LIKES_COL,
    "total_shares": ca.SHARES_COL,
    "total_comments": ca.COMMENTS_COL,
    "total_saves": ca.SAVES_COL,
    "ad_cost": ca.COST_COL,
    "ad_impressions": ca.IMPRESSIONS_COL,
    "ad_clicks": "クリック（誘導先）",
    "ad_ctr": ca.CTR_COL,
    "ad_views": "動画視聴数",
    "ad_views_2s": "2秒動画再生数",
    "ad_views_6s": "6秒動画再生数",
    "ad_views_25pct": "25％までの再生数",
    "ad_views_50pct": "50％までの再生数",
    "ad_views_75pct": "75％までの再生数",
    "ad_completions": "再生完了数",
    "ad_average_watch_seconds": "動画視聴1回あたりの平均視聴時間",
    "ad_rate_2s": "2秒視聴率",
    "ad_rate_6s": "6秒視聴率",
    "ad_rate_25pct": "25％動画視聴率",
    "ad_rate_50pct": "50％動画視聴率",
    "ad_rate_75pct": "75％動画視聴率",
    "ad_rate_100pct": "100％動画視聴率",
    "ad_likes": "いいね",
    "ad_shares": "シェア",
    "ad_comments": "コメント",
    "ad_saves": "有料のお気に入り",
}
RATE_METRICS = frozenset(
    field for field in METRIC_COLUMNS if field == "ad_ctr" or field.startswith("ad_rate_")
)


class Video(BaseModel):
    platform: Platform
    video_id: str
    url: str
    account_name: str | None = None
    account_handle: str | None = None
    posted_at: datetime | None = None
    duration_seconds: float | None = None
    music_name: str | None = None
    music_author: str | None = None
    music_original: bool | None = None
    caption: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    advertiser: str | None = None
    campaign: str | None = None
    origin: Literal["own", "external"] = "own"
    is_pr: bool = False


class Metrics(BaseModel):
    platform: Platform
    video_id: str
    captured_on: date
    organic_plays: float | None = None
    organic_likes: float | None = None
    organic_shares: float | None = None
    organic_comments: float | None = None
    organic_saves: float | None = None
    total_plays: float | None = None
    total_likes: float | None = None
    total_shares: float | None = None
    total_comments: float | None = None
    total_saves: float | None = None
    ad_cost: float | None = None
    ad_impressions: float | None = None
    ad_clicks: float | None = None
    ad_ctr: float | None = None
    ad_views: float | None = None
    ad_views_2s: float | None = None
    ad_views_6s: float | None = None
    ad_views_25pct: float | None = None
    ad_views_50pct: float | None = None
    ad_views_75pct: float | None = None
    ad_completions: float | None = None
    ad_average_watch_seconds: float | None = None
    ad_rate_2s: float | None = None
    ad_rate_6s: float | None = None
    ad_rate_25pct: float | None = None
    ad_rate_50pct: float | None = None
    ad_rate_75pct: float | None = None
    ad_rate_100pct: float | None = None
    ad_likes: float | None = None
    ad_shares: float | None = None
    ad_comments: float | None = None
    ad_saves: float | None = None


class Campaign(BaseModel):
    advertiser: str
    campaign: str


class Account(BaseModel):
    platform: Platform
    handle: str
    display_name: str | None = None
    operator_label: str | None = None
    classification: str | None = None


class SyncBatch(BaseModel):
    videos: list[Video] = Field(default_factory=list)
    metrics: list[Metrics] = Field(default_factory=list)
    campaigns: list[Campaign] = Field(default_factory=list)
    accounts: list[Account] = Field(default_factory=list)
    rows_read: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    warnings: dict[str, int] = Field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "rows_read": self.rows_read,
            "unique_videos": len(self.videos),
            "categories": dict(
                Counter(resolve_business_category(is_pr=v.is_pr) or "unset" for v in self.videos)
            ),
            "pr_videos": sum(v.is_pr for v in self.videos),
            "skipped": self.skipped,
            "warnings": self.warnings,
        }


class DerivedRates(BaseModel):
    save_rate: float | None
    engagement_rate: float | None
    is_reference: bool


def resolve_business_category(*, is_pr: bool, campaign_category: str | None = None) -> str | None:
    """P3 の設定とビューでも使う規則。P1 は案件種別未設定の集計だけに使い、DB には保存しない。"""
    return campaign_category if is_pr else "organic"


def derived_rates(
    *,
    plays: float | None,
    likes: float | None,
    saves: float | None,
    comments: float | None,
    shares: float | None,
) -> DerivedRates:
    """Pass one metric family only; incomplete engagement stays unavailable."""
    valid = plays is not None and plays > 0
    complete = all(value is not None for value in (likes, saves, comments, shares))
    return DerivedRates(
        save_rate=saves / plays if valid and plays is not None and saves is not None else None,
        engagement_rate=(
            sum(value for value in (likes, saves, comments, shares) if value is not None) / plays
        )
        if valid and plays is not None and complete
        else None,
        is_reference=plays is not None and plays < 5000,
    )


def _text(value: object) -> str | None:
    return None if ca.is_missing(value) else str(value).strip()


def _rows(tab: TabRows) -> list[dict[str, str]]:
    return [dict(zip((h.strip() for h in tab.headers), row, strict=False)) for row in tab.rows]


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _boolean(value: object) -> bool | None:
    text = _text(value)
    if text is None:
        return None
    normalized = _norm(text).casefold()
    if normalized in {"true", "1", "yes", "はい", "非公開"}:
        return True
    if normalized in {"false", "0", "no", "いいえ", "公開"}:
        return False
    return None


def video_identity(url: str) -> tuple[Platform, str, str | None] | None:
    """Accept full public post URLs; never resolve short links with network I/O."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
            return None
        host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
        path = parsed.path.rstrip("/")
        if host == "tiktok.com":
            match = re.fullmatch(r"/@([^/]+)/video/(\d+)", path)
            if match:
                return "tiktok", match[2], _norm(match[1]).casefold()
        if host == "instagram.com":
            match = re.fullmatch(r"/(?:reel|reels|p)/([\w-]+)", path, re.ASCII)
            if match:
                return "instagram", match[1], None
        if host == "youtube.com":
            match = re.fullmatch(r"/shorts/([\w-]{11})", path, re.ASCII)
            if match:
                return "youtube", match[1], None
    except ValueError:
        return None
    return None


def _account_handle(url: str, platform: Platform) -> str | None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        expected = {"tiktok": "tiktok.com", "instagram": "instagram.com", "youtube": "youtube.com"}
        if parsed.scheme not in {"https", "http"} or host != expected[platform]:
            return None
        path = parsed.path.strip("/")
        if platform in {"tiktok", "youtube"} and not path.startswith("@"):
            return None
        handle = path.removeprefix("@")
        return _norm(handle).casefold() if handle and "/" not in handle else None
    except ValueError:
        return None


def _accounts(tab: TabRows | None, warnings: Counter[str]) -> list[Account]:
    if tab is None:
        warnings["accounts_unconfigured"] += 1
        return []
    if not {"運営", "分類", "アカウント名", "TT", "IG", "YT"}.issubset(tab.headers):
        warnings["accounts_columns_missing"] += 1
    found: dict[tuple[Platform, str], Account] = {}
    for row in _rows(tab):
        operator_label = row.get("運営", "")
        for platform, column in ACCOUNT_COLUMNS:
            handle = _account_handle(row.get(column, ""), platform)
            if not handle:
                continue
            account = Account(
                platform=platform,
                handle=handle,
                display_name=_text(row.get("アカウント名")),
                operator_label=operator_label if not ca.is_missing(operator_label) else None,
                classification=_text(row.get("分類")),
            )
            found.setdefault((account.platform, handle), account)
    return list(found.values())


def _timestamp(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(ca.normalize_post_date(text).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def build_batch(
    database: TabRows,
    raw: TabRows,
    *,
    captured_on: date,
    account_ledger: TabRows | None = None,
) -> SyncBatch:
    """First valid sheet row wins per (platform, video_id); rows are never summed.

    Ledger duplicates also keep the first row. Raw metadata uses the
    first exact URL match, as the existing aggregate's date join does.
    """
    if not {ca.URL_COL, ca.ADVERTISER_COL, ca.CAMPAIGN_COL}.issubset(database.headers):
        raise ValueError("database_columns_missing")
    warnings: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    accounts = _accounts(account_ledger, warnings)
    by_handle = {(a.platform, a.handle): a for a in accounts}
    campaigns: dict[tuple[str, str], Campaign] = {}
    raw_index: dict[str, dict[str, str]] = {}
    for url, row in ca.iter_raw_url_rows(raw.headers, raw.rows):
        raw_index.setdefault(url, row)
    post_dates = ca.build_post_date_index(raw.headers, raw.rows)
    seen: set[tuple[Platform, str]] = set()
    batch = SyncBatch(rows_read=len(database.rows))
    for row in _rows(database):
        if all(ca.is_missing(value) for value in row.values()):
            skipped["empty_row"] += 1
            continue
        if any(
            "審査落ち" in row.get(column, "")
            for column in (ca.URL_COL, "動画ID(抽出)", ca.AD_NAME_COL, "審査結果", "審査ステータス")
        ):
            skipped["rejected"] += 1
            continue
        url = row.get(ca.URL_COL, "").strip()
        if ca.is_missing(url):
            skipped["url_missing"] += 1
            continue
        identity = video_identity(url)
        if identity is None:
            skipped["invalid_url"] += 1
            continue
        platform, url_id, handle = identity
        video_id = _text(row.get("動画ID(抽出)")) or url_id
        if video_id != url_id:
            skipped["id_mismatch"] += 1
            continue
        key = (platform, video_id)
        if key in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(key)
        account_name = _text(row.get(ca.ACCOUNT_COL))
        if handle is None and account_name:
            matching = [
                a for a in accounts if a.platform == platform and a.display_name == account_name
            ]
            if len(matching) == 1:
                handle = matching[0].handle
        account = by_handle.get((platform, handle)) if handle else None
        if handle and account is None:
            account = Account(platform=platform, handle=handle, display_name=account_name)
            accounts.append(account)
            by_handle[(platform, handle)] = account
        advertiser, campaign_name = (
            _text(row.get(ca.ADVERTISER_COL)),
            _text(row.get(ca.CAMPAIGN_COL)),
        )
        if advertiser and campaign_name:
            campaigns[(advertiser, campaign_name)] = Campaign(
                advertiser=advertiser, campaign=campaign_name
            )
        caption = _text(row.get(ca.TEXT_COL))
        hashtags = list(
            dict.fromkeys(
                re.findall(r"#([^\s#【】\[\](){}「」『』〈〉《》]+)", _norm(caption or ""))
            )
        )
        is_pr = any(tag.casefold() == "pr" for tag in hashtags)
        if not is_pr and advertiser is not None:
            warnings["advertiser_without_pr"] += 1
        metadata = raw_index.get(url, {})
        batch.videos.append(
            Video(
                platform=platform,
                video_id=video_id,
                url=url,
                account_name=account_name,
                account_handle=handle,
                posted_at=_timestamp(metadata.get(ca.POST_DATE_COL))
                or _timestamp(post_dates.get(url)),
                duration_seconds=ca.to_number(metadata.get("videoMeta.duration")),
                music_name=_text(metadata.get("musicMeta.musicName")),
                music_author=_text(metadata.get("musicMeta.musicAuthor")),
                music_original=_boolean(metadata.get("musicMeta.musicOriginal")),
                caption=caption,
                hashtags=hashtags,
                advertiser=advertiser,
                campaign=campaign_name,
                is_pr=is_pr,
            )
        )
        values = {field: ca.to_number(row.get(column)) for field, column in METRIC_COLUMNS.items()}
        for field, value in values.items():
            if value is not None and value < 0:
                values[field] = None
                warnings["negative_metric"] += 1
            elif field in RATE_METRICS and value is not None and value > 1:
                values[field] = None
                warnings["invalid_rate"] += 1
        batch.metrics.append(
            Metrics(platform=platform, video_id=video_id, captured_on=captured_on, **values)
        )
    batch.campaigns = list(campaigns.values())
    batch.accounts = accounts
    batch.skipped = dict(skipped)
    batch.warnings = dict(warnings)
    return batch
