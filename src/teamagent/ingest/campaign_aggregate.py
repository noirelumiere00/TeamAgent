"""ショート動画データベース（gsheets）を **案件単位** に集計して 1 文書にする（2026-09-15）。

背景: 「ショート動画データベース」スプレッドシートの本体タブは動画 1 行 × 40 列で、
広告主名・案件名が入っている行（実測 1,151 本・45 社・54 案件）と、自社メディアの通常投稿
（広告主名が ``#N/A``・1,482 本）が混在する。1 行 = 1 document で入れると同型の短い文書が
数千件並び、検索で本物の資料を押しのける。ここでは **案件（広告主名 × 案件名）ごとに
1 文書** を作り、「その案件でどんな投稿を何本出し、どういう結果だったか」を 1 枚で引ける形にする。

設計:
- 広告主名・案件名のどちらかが欠ける行（空・``#N/A`` 等）は **案件に結びつけない**
  （テキスト一致などの推測はしない。誤った案件に動画が付くと営業が誤引用するため）。
- external_id は行番号ではなく **案件キーのハッシュ**（``<sheet_id>:<gid>:campaign:<sha1[:16]>``）。
  行の挿入・削除・並び替えで文書が付け替わらない。
- 本文は決定論的に組む（LLM を使わない）。集計値・上位動画・投稿文の特徴（ハッシュタグ・長さ）。
- pipeline 側は ``extra_metadata.campaign_aggregate: "true"`` を宣言した spec だけがこの経路に入る。
"""

from __future__ import annotations

import hashlib
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

ADVERTISER_COL = "広告主名"
CAMPAIGN_COL = "案件名"
URL_COL = "動画URL"
ACCOUNT_COL = "アカウント名"
TEXT_COL = "テキスト"
PLAYS_COL = "総再生数"
LIKES_COL = "総いいね"
SHARES_COL = "総シェア"
COMMENTS_COL = "総コメント"
SAVES_COL = "総保存"
COST_COL = "コスト"
IMPRESSIONS_COL = "インプレッション"
CTR_COL = "CTR（誘導先）"
AD_NAME_COL = "広告名"

#: 投稿日は本体タブに無く「全体Raw」タブ（Apify 生データ）にある。URL で結合する。
POST_DATE_TAB_NAME = "全体Raw"
POST_DATE_URL_COL = "webVideoUrl"
POST_DATE_COL = "createTimeISO"

REQUIRED_COLUMNS: tuple[str, ...] = (ADVERTISER_COL, CAMPAIGN_COL, URL_COL)

#: 「値なし」とみなす文字列（表計算の参照エラーも含む）。
MISSING_VALUES: frozenset[str] = frozenset(
    {"", "#n/a", "#ref!", "#value!", "-", "n/a", "na", "none", "null"}
)

TOP_VIDEOS = 5
TOP_HASHTAGS = 5
TEXT_PREVIEW_CHARS = 60

_HASHTAG_RE = re.compile(r"#([^\s#＃　]+)")
_WS_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean(value: object) -> str:
    if value is None:
        return ""
    return _WS_RE.sub(" ", str(value)).strip()


def is_missing(value: object) -> bool:
    """空セル・``#N/A`` などの「値なし」判定（大文字小文字と全角空白を吸収）。"""
    return _clean(value).lower() in MISSING_VALUES


def to_number(value: object) -> float | None:
    """``1,125`` / ``0.12%`` / ``1125.0`` を数値にする。数値でなければ None。"""
    text = _clean(value).replace(",", "").replace("％", "%")
    if not text or text.lower() in MISSING_VALUES:
        return None
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number / 100.0 if percent else number


@dataclass(frozen=True)
class CampaignVideo:
    url: str
    account: str
    text: str
    plays: float | None
    likes: float | None
    shares: float | None
    comments: float | None
    saves: float | None
    cost: float | None
    impressions: float | None
    ctr: float | None
    ad_name: str
    advertiser: str = ""
    campaign: str = ""
    posted_on: str = ""  # YYYY-MM-DD（全体Raw から結合・無ければ空）

    @property
    def save_rate(self) -> float | None:
        if self.plays and self.saves is not None and self.plays > 0:
            return self.saves / self.plays
        return None

    @property
    def is_ad(self) -> bool:
        return bool(self.cost and self.cost > 0)


@dataclass(frozen=True)
class _VideoStats:
    """動画の集合に対する集計（案件単位・アカウント単位で共通）。"""

    videos: tuple[CampaignVideo, ...]

    @property
    def period(self) -> tuple[str, str] | None:
        dates = sorted(v.posted_on for v in self.videos if v.posted_on)
        return (dates[0], dates[-1]) if dates else None

    @property
    def video_count(self) -> int:
        return len(self.videos)

    @property
    def accounts(self) -> tuple[str, ...]:
        return tuple(sorted({v.account for v in self.videos if v.account}))

    @property
    def ad_video_count(self) -> int:
        return sum(1 for v in self.videos if v.is_ad)

    def _values(self, attr: str) -> list[float]:
        return [x for x in (getattr(v, attr) for v in self.videos) if x is not None]

    def total(self, attr: str) -> float | None:
        values = self._values(attr)
        return sum(values) if values else None

    def median(self, attr: str) -> float | None:
        values = self._values(attr)
        return statistics.median(values) if values else None

    def mean(self, attr: str) -> float | None:
        values = self._values(attr)
        return statistics.fmean(values) if values else None

    def maximum(self, attr: str) -> float | None:
        values = self._values(attr)
        return max(values) if values else None

    @property
    def save_rate_median(self) -> float | None:
        rates = [r for r in (v.save_rate for v in self.videos) if r is not None]
        return statistics.median(rates) if rates else None

    @property
    def top_videos(self) -> tuple[CampaignVideo, ...]:
        """再生数の多い順（再生数なしは末尾）。同数は入力順を保つ。"""
        ranked = sorted(
            enumerate(self.videos),
            key=lambda iv: (-(iv[1].plays if iv[1].plays is not None else -1.0), iv[0]),
        )
        return tuple(v for _, v in ranked[:TOP_VIDEOS])

    @property
    def top_hashtags(self) -> tuple[tuple[str, int], ...]:
        """投稿文に出るハッシュタグを「使った本数」順に。1 本の中の重複は 1 回に数える。"""
        counter: Counter[str] = Counter()
        for v in self.videos:
            counter.update({tag.lower() for tag in _HASHTAG_RE.findall(v.text)})
        return tuple(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_HASHTAGS])

    @property
    def text_length_median(self) -> int | None:
        lengths = [len(v.text) for v in self.videos if v.text]
        return int(statistics.median(lengths)) if lengths else None


@dataclass(frozen=True)
class CampaignAggregate(_VideoStats):
    advertiser: str = ""
    campaign: str = ""


@dataclass(frozen=True)
class AccountAggregate(_VideoStats):
    """投稿アカウント単位の集計。案件の無い投稿（自社メディアの通常投稿）も含む。"""

    account: str = ""

    @property
    def campaigns(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    f"{v.advertiser} / {v.campaign}"
                    for v in self.videos
                    if v.advertiser and v.campaign
                }
            )
        )

    @property
    def campaign_video_count(self) -> int:
        return sum(1 for v in self.videos if v.advertiser and v.campaign)


def build_post_date_index(
    headers: Sequence[str], rows: Iterable[Sequence[object]]
) -> dict[str, str]:
    """「全体Raw」タブから 動画URL → 投稿日（YYYY-MM-DD）の索引を作る。列が無ければ空。"""
    idx = _index(headers)
    iu = idx.get(POST_DATE_URL_COL)
    it = idx.get(POST_DATE_COL)
    if iu is None or it is None:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        url = _clean(row[iu]) if iu < len(row) else ""
        day = _clean(row[it])[:10] if it < len(row) else ""
        if url and _DATE_RE.match(day):
            out.setdefault(url, day)
    return out


def _index(headers: Sequence[str]) -> dict[str, int]:
    return {_clean(h): i for i, h in enumerate(headers) if _clean(h)}


def _cell(row: Sequence[object], idx: dict[str, int], column: str) -> str:
    i = idx.get(column)
    if i is None or i >= len(row):
        return ""
    return _clean(row[i])


def _norm_key_part(value: str) -> str:
    """案件キーの片側を正規化（NFKC・前後空白除去・連続空白の圧縮・大小同一視）。"""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip().casefold()


def _campaign_key(advertiser: str, campaign: str) -> str:
    return f"{_norm_key_part(advertiser)}\n{_norm_key_part(campaign)}"


def _video_from_row(
    row: Sequence[object], idx: dict[str, int], post_dates: dict[str, str] | None
) -> CampaignVideo | None:
    url = _cell(row, idx, URL_COL)
    if not url:
        return None
    advertiser = _cell(row, idx, ADVERTISER_COL)
    campaign = _cell(row, idx, CAMPAIGN_COL)
    linked = not (is_missing(advertiser) or is_missing(campaign))
    return CampaignVideo(
        url=url,
        account=_cell(row, idx, ACCOUNT_COL),
        text=_cell(row, idx, TEXT_COL),
        plays=to_number(_cell(row, idx, PLAYS_COL)),
        likes=to_number(_cell(row, idx, LIKES_COL)),
        shares=to_number(_cell(row, idx, SHARES_COL)),
        comments=to_number(_cell(row, idx, COMMENTS_COL)),
        saves=to_number(_cell(row, idx, SAVES_COL)),
        cost=to_number(_cell(row, idx, COST_COL)),
        impressions=to_number(_cell(row, idx, IMPRESSIONS_COL)),
        ctr=to_number(_cell(row, idx, CTR_COL)),
        ad_name=_cell(row, idx, AD_NAME_COL),
        advertiser=advertiser if linked else "",
        campaign=campaign if linked else "",
        posted_on=(post_dates or {}).get(url, ""),
    )


def aggregate_campaigns(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    post_dates: dict[str, str] | None = None,
) -> tuple[CampaignAggregate, ...]:
    """行を案件（広告主名 × 案件名）ごとに集計する。

    広告主名か案件名が「値なし」の行は捨てる（案件への推測結合はしない）。
    必須列（広告主名・案件名・動画URL）がヘッダに無いときは ``ValueError``。
    戻り値は広告主名・案件名の順で安定ソート。
    """
    idx = _index(headers)
    missing = [c for c in REQUIRED_COLUMNS if c not in idx]
    if missing:
        raise ValueError(f"campaign aggregate: required columns missing: {missing}")
    grouped: dict[str, tuple[str, str, list[CampaignVideo]]] = {}
    for row in rows:
        video = _video_from_row(row, idx, post_dates)
        if video is None or not video.advertiser:
            continue
        key = _campaign_key(video.advertiser, video.campaign)
        if key not in grouped:
            grouped[key] = (video.advertiser, video.campaign, [])
        grouped[key][2].append(video)
    aggregates = [
        CampaignAggregate(advertiser=a, campaign=c, videos=tuple(v)) for a, c, v in grouped.values()
    ]
    aggregates.sort(key=lambda x: (x.advertiser, x.campaign))
    return tuple(aggregates)


def aggregate_accounts(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    post_dates: dict[str, str] | None = None,
) -> tuple[AccountAggregate, ...]:
    """行を投稿アカウントごとに集計する。案件の無い投稿も含める（アカウントの実力を見るため）。"""
    idx = _index(headers)
    missing = [c for c in (URL_COL, ACCOUNT_COL) if c not in idx]
    if missing:
        raise ValueError(f"account aggregate: required columns missing: {missing}")
    grouped: dict[str, tuple[str, list[CampaignVideo]]] = {}
    for row in rows:
        video = _video_from_row(row, idx, post_dates)
        if video is None or is_missing(video.account):
            continue
        key = _norm_key_part(video.account)
        if key not in grouped:
            grouped[key] = (video.account, [])
        grouped[key][1].append(video)
    aggregates = [AccountAggregate(account=a, videos=tuple(v)) for a, v in grouped.values()]
    aggregates.sort(key=lambda x: x.account)
    return tuple(aggregates)


def account_external_id(sheet_id: str, gid: int, account: str) -> str:
    digest = hashlib.sha1(_norm_key_part(account).encode("utf-8")).hexdigest()[:16]
    return f"{sheet_id}:{gid}:account:{digest}"


def account_title(agg: AccountAggregate) -> str:
    return f"投稿アカウント実績 {agg.account}"


def campaign_external_id(sheet_id: str, gid: int, advertiser: str, campaign: str) -> str:
    """行番号に依存しない external_id（案件キーのハッシュ）。"""
    key = _campaign_key(advertiser, campaign).encode("utf-8")
    digest = hashlib.sha1(key).hexdigest()[:16]
    return f"{sheet_id}:{gid}:campaign:{digest}"


def campaign_title(agg: CampaignAggregate) -> str:
    return f"施策実績 {agg.advertiser} {agg.campaign}"


def _fmt_int(value: float | None) -> str:
    return "不明" if value is None else f"{round(value):,}"


def _fmt_pct(value: float | None) -> str:
    return "不明" if value is None else f"{value * 100:.1f}%"


def format_campaign_document(agg: CampaignAggregate) -> str:
    """検索で引ける 1 文書の本文。数値は集計値、上位動画は再生数順、投稿文は 60 字まで。"""
    lines: list[str] = []
    lines.append(f"施策実績: {agg.advertiser} / {agg.campaign}")
    accounts = "、".join(agg.accounts) if agg.accounts else "不明"
    lines.append(
        f"投稿本数: {agg.video_count} 本（アカウント: {accounts}）。"
        f"広告配信あり: {agg.ad_video_count} 本。"
    )
    if agg.period:
        lines.append(f"投稿期間: {agg.period[0]} 〜 {agg.period[1]}")
    lines.append(
        "再生数: 合計 "
        f"{_fmt_int(agg.total('plays'))}、中央値 {_fmt_int(agg.median('plays'))}、"
        f"平均 {_fmt_int(agg.mean('plays'))}、最大 {_fmt_int(agg.maximum('plays'))}"
    )
    lines.append(
        "反応: いいね 合計 "
        f"{_fmt_int(agg.total('likes'))}、シェア 合計 {_fmt_int(agg.total('shares'))}、"
        f"コメント 合計 {_fmt_int(agg.total('comments'))}、"
        f"保存 合計 {_fmt_int(agg.total('saves'))}、"
        f"保存率の中央値 {_fmt_pct(agg.save_rate_median)}"
    )
    if agg.ad_video_count:
        lines.append(
            "広告: 費用 合計 "
            f"{_fmt_int(agg.total('cost'))}、"
            f"インプレッション 合計 {_fmt_int(agg.total('impressions'))}、"
            f"CTR の中央値 {_fmt_pct(agg.median('ctr'))}"
        )
    lines.append("上位の投稿（再生数順）:")
    for n, v in enumerate(agg.top_videos, start=1):
        preview = v.text[:TEXT_PREVIEW_CHARS] + ("…" if len(v.text) > TEXT_PREVIEW_CHARS else "")
        lines.append(
            f"{n}. 再生 {_fmt_int(v.plays)}、保存 {_fmt_int(v.saves)}、{v.account or '不明'}、"
            f"{preview or '（投稿文なし）'} {v.url}"
        )
    tags = " ".join(f"#{tag}（{count} 本）" for tag, count in agg.top_hashtags)
    length = agg.text_length_median
    lines.append(
        "投稿文の特徴: "
        + (f"よく使うハッシュタグ {tags}。" if tags else "ハッシュタグの使用なし。")
        + (f"投稿文の長さの中央値 {length} 字。" if length is not None else "")
    )
    return "\n".join(lines)


def campaign_metadata(agg: CampaignAggregate) -> dict[str, str]:
    """documents.metadata に載せる集計値（すべて文字列・JSONB の既存規約に合わせる）。

    ``cls_doc_type`` / ``cls_project`` を決定論的に付ける（取込時の LLM 分類は使わない。
    案件文書は広告主名が案件の真実源で、検索側のクライアント一致ガードがこれを引く）。
    """

    def _s(value: float | None) -> str:
        return "" if value is None else f"{value:.4f}".rstrip("0").rstrip(".")

    return {
        "campaign_aggregate": "true",
        "advertiser": agg.advertiser,
        "campaign": agg.campaign,
        "video_count": str(agg.video_count),
        "ad_video_count": str(agg.ad_video_count),
        "accounts": "、".join(agg.accounts)[:500],
        "plays_total": _s(agg.total("plays")),
        "plays_median": _s(agg.median("plays")),
        "plays_max": _s(agg.maximum("plays")),
        "saves_total": _s(agg.total("saves")),
        "save_rate_median": _s(agg.save_rate_median),
        "cost_total": _s(agg.total("cost")),
        "first_posted_on": agg.period[0] if agg.period else "",
        "last_posted_on": agg.period[1] if agg.period else "",
        "cls_doc_type": "施策実績",
        "cls_project": agg.advertiser,
    }


def format_account_document(agg: AccountAggregate) -> str:
    """投稿アカウント 1 件の本文。案件付きの投稿と通常投稿を分けて数える。"""
    lines: list[str] = [f"投稿アカウント実績: {agg.account}"]
    campaigns = "、".join(agg.campaigns[:8]) + ("、他" if len(agg.campaigns) > 8 else "")
    lines.append(
        f"投稿本数: {agg.video_count} 本（案件付き {agg.campaign_video_count} 本、"
        f"通常投稿 {agg.video_count - agg.campaign_video_count} 本）。"
        + (f"関わった案件: {campaigns}。" if campaigns else "案件付きの投稿なし。")
    )
    if agg.period:
        lines.append(f"投稿期間: {agg.period[0]} 〜 {agg.period[1]}")
    lines.append(
        "再生数: 合計 "
        f"{_fmt_int(agg.total('plays'))}、中央値 {_fmt_int(agg.median('plays'))}、"
        f"平均 {_fmt_int(agg.mean('plays'))}、最大 {_fmt_int(agg.maximum('plays'))}"
    )
    lines.append(
        "反応: いいね 合計 "
        f"{_fmt_int(agg.total('likes'))}、保存 合計 {_fmt_int(agg.total('saves'))}、"
        f"保存率の中央値 {_fmt_pct(agg.save_rate_median)}"
    )
    lines.append("上位の投稿（再生数順）:")
    for n, v in enumerate(agg.top_videos, start=1):
        preview = v.text[:TEXT_PREVIEW_CHARS] + ("…" if len(v.text) > TEXT_PREVIEW_CHARS else "")
        label = f"{v.advertiser} / {v.campaign}" if v.advertiser and v.campaign else "通常投稿"
        lines.append(
            f"{n}. 再生 {_fmt_int(v.plays)}、保存 {_fmt_int(v.saves)}、{label}、"
            f"{preview or '（投稿文なし）'} {v.url}"
        )
    tags = " ".join(f"#{tag}（{count} 本）" for tag, count in agg.top_hashtags)
    lines.append(
        "投稿文の特徴: "
        + (f"よく使うハッシュタグ {tags}。" if tags else "ハッシュタグの使用なし。")
    )
    return "\n".join(lines)


def account_metadata(agg: AccountAggregate) -> dict[str, str]:
    def _s(value: float | None) -> str:
        return "" if value is None else f"{value:.4f}".rstrip("0").rstrip(".")

    return {
        "account_aggregate": "true",
        "account": agg.account,
        "video_count": str(agg.video_count),
        "campaign_video_count": str(agg.campaign_video_count),
        "campaign_count": str(len(agg.campaigns)),
        "plays_total": _s(agg.total("plays")),
        "plays_median": _s(agg.median("plays")),
        "saves_total": _s(agg.total("saves")),
        "save_rate_median": _s(agg.save_rate_median),
        "first_posted_on": agg.period[0] if agg.period else "",
        "last_posted_on": agg.period[1] if agg.period else "",
        "cls_doc_type": "投稿アカウント実績",
    }


__all__ = [
    "ADVERTISER_COL",
    "CAMPAIGN_COL",
    "POST_DATE_TAB_NAME",
    "REQUIRED_COLUMNS",
    "AccountAggregate",
    "CampaignAggregate",
    "CampaignVideo",
    "account_external_id",
    "account_metadata",
    "account_title",
    "aggregate_accounts",
    "aggregate_campaigns",
    "build_post_date_index",
    "campaign_external_id",
    "campaign_metadata",
    "campaign_title",
    "format_account_document",
    "format_campaign_document",
    "is_missing",
    "to_number",
]
