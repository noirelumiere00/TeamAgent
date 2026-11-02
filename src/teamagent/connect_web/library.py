"""Internal short-video archive. Video reads always use vl_videos_effective."""

from __future__ import annotations

import html
import json
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.connect_web.library_analysis import (
    ANALYSIS_KIND,
    ANALYSIS_VERSION,
    TAG_GROUPS,
    render_analysis,
)
from teamagent.connect_web.library_assets import SCRIPT as _SCRIPT
from teamagent.connect_web.library_assets import STYLE as _STYLE
from teamagent.ingest.video_library import (
    METRIC_COLUMNS,
    RATE_METRICS,
    derived_rates,
    video_identity,
)

JST = timezone(timedelta(hours=9))
PAGE_SIZE = 50


def library_target(raw: str) -> str | None:
    """Accept a relative library path, including its search query, never an origin."""
    try:
        parsed = urlsplit(raw)
        decoded = unquote(raw)
        path = unquote(parsed.path)
        if (
            not raw.startswith("/library")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not (path == "/library" or path.startswith("/library/"))
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", parsed.path)
            or "//" in path
            or "\\" in decoded
            or any(part in {".", ".."} for part in path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        ):
            return None
        return raw
    except ValueError:
        return None


def _e(value: object) -> str:
    return html.escape(str(value) if value is not None else "")


def post_url(row: Mapping[str, Any]) -> str | None:
    """Only link/copy a validated public post, even if a database row was corrupted."""
    url = str(row.get("url") or "")
    identity = video_identity(url)
    return url if identity and identity[:2] == (row["platform"], row["video_id"]) else None


def _rates(row: Mapping[str, Any], family: str = "total") -> dict[str, Any]:
    return derived_rates(
        **{
            name: row.get(f"{family}_{name}")
            for name in ("plays", "likes", "saves", "comments", "shares")
        }
    ).model_dump()


def _rate(value: float | None, reference: bool) -> str:
    if value is None:
        return "算出不可"
    return f"{value:.2%}" + ("（参考値）" if reference else "")


def _metric(value: float | None, column: str = "") -> str:
    if value is None:
        return "算出不可"
    if column in RATE_METRICS:
        return f"{value:.1%}"
    if column == "ad_cost":
        return f"¥{value:,.0f}"
    if column == "ad_average_watch_seconds":
        return f"{value:.2f}秒"
    return f"{value:,.0f}"


def _day(value: Any) -> str:
    return value.astimezone(JST).strftime("%Y-%m-%d") if isinstance(value, datetime) else "不明"


def parse_search(params: Mapping[str, str]) -> tuple[list[str], bool | None]:
    """Share word/hashtag interpretation between SQL, filters and result labels."""
    query = params.get("q", "").strip()
    normalized = unicodedata.normalize("NFKC", query)
    if max(len(query), len(normalized)) > 500:
        raise HTTPException(400, "検索語が長すぎます")
    words = normalized.split()
    if len(words) > 10:
        raise HTTPException(400, "検索語は10語以内で指定してください")
    pr = params.get("pr", "")
    if pr not in {"", "yes", "no"}:
        raise HTTPException(400, "PR指定が不正です")
    is_pr = (pr == "yes") if pr else None
    terms = []
    for word in words:
        if word.casefold() == "#pr":
            is_pr = True
        elif term := word.lstrip("#"):
            terms.append(term)
    return terms, is_pr


def _where(params: Mapping[str, str], admin: bool) -> tuple[str, list[Any], int, str]:
    clauses = [] if admin else ["NOT v.is_private"]
    values: list[Any] = []
    terms, is_pr = parse_search(params)
    for term in terms:
        clauses.append("""concat_ws(' ', v.caption, array_to_string(v.hashtags, ' '),
            v.account_name, v.account_handle, v.advertiser, v.campaign,
            v.industry, v.purpose) ILIKE %s""")
        values.append("%" + term + "%")
    category = params.get("category", "")
    if category == "unset":
        clauses.append("v.business_category IS NULL")
    elif category:
        clauses.append("v.business_category = %s")
        values.append(category)
    if is_pr is not None:
        clauses.append("v.is_pr = %s")
        values.append(is_pr)
    for param, operator in (("from", ">="), ("to", "<")):
        if params.get(param):
            try:
                day = date.fromisoformat(params[param])
                if param == "to":
                    day += timedelta(days=1)
                values.append(datetime.combine(day, datetime.min.time(), JST))
            except (ValueError, OverflowError):
                raise HTTPException(400, "日付が不正です") from None
            clauses.append(f"v.posted_at {operator} %s")
    if params.get("account"):
        clauses.append("(v.account_handle = %s OR v.account_name = %s)")
        values.extend([params["account"], params["account"]])
    for field in ("advertiser", "campaign", "industry", "purpose"):
        if params.get(field):
            if len(params[field]) > 500:
                raise HTTPException(400, "絞り込みが長すぎます")
            clauses.append(f"v.{field} = %s")
            values.append(params[field])
    if params.get("unassigned") == "yes":
        clauses.append("(COALESCE(v.advertiser, '')='' OR COALESCE(v.campaign, '')='')")
    group, tag = params.get("tag_group", ""), params.get("tag", "")
    if group or tag:
        if group not in {*TAG_GROUPS, "standards"} or not tag or len(tag) > 200:
            raise HTTPException(400, "タグ指定が不正です")
        needle = {"standards": {tag: True}} if group == "standards" else {"tags": {group: [tag]}}
        clauses.append("""(SELECT a.payload FROM vl_video_analysis a
            WHERE a.platform=v.platform AND a.video_id=v.video_id
            AND a.kind=%s AND a.version=%s ORDER BY a.analyzed_at DESC LIMIT 1) @> %s::jsonb""")
        values.extend([ANALYSIS_KIND, ANALYSIS_VERSION, json.dumps(needle)])
    try:
        page = int(params.get("page", "1"))
        if not 1 <= page <= 1_000_000:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "ページが不正です") from None
    orders = {
        "new": "v.posted_at",
        "plays": "m.total_plays",
        "saves": "CASE WHEN m.total_plays > 0 THEN m.total_saves / m.total_plays END",
        "eng": (
            "CASE WHEN m.total_plays > 0 THEN (m.total_likes + m.total_saves + "
            "m.total_comments + m.total_shares) / m.total_plays END"
        ),
    }
    sort = params.get("sort", "new")
    if sort not in orders:
        raise HTTPException(400, "並び順が不正です")
    return " AND ".join(clauses) or "true", values, page, orders[sort]


_METRICS_JOIN = """LEFT JOIN LATERAL (
    SELECT * FROM vl_metrics m0 WHERE m0.platform=v.platform AND m0.video_id=v.video_id
    ORDER BY captured_on DESC LIMIT 1
) m ON true"""
_METRIC_SELECT = ", ".join(f"m.{column}" for column in METRIC_COLUMNS)


def search_videos(conn: Any, params: Mapping[str, str], *, admin: bool) -> dict[str, Any]:
    where, values, page, order = _where(params, admin)
    count = conn.execute(
        # 固定の句と許可リストの並び順だけを連結し、利用者の値は全て %s で渡す
        f"SELECT count(*) AS n FROM vl_videos_effective v WHERE {where}",  # nosec B608
        values,
    ).fetchone()["n"]
    # 同上: 列名・JOIN・句・並び順はコード内の固定値で、利用者の値は %s で渡す
    list_sql = f"SELECT v.*, {_METRIC_SELECT}, m.captured_on FROM vl_videos_effective v {_METRICS_JOIN} WHERE {where} ORDER BY {order} DESC NULLS LAST, v.platform, v.video_id LIMIT %s OFFSET %s"  # noqa: E501  # nosec B608
    rows = conn.execute(
        list_sql,
        [*values, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    for row in rows:
        row["rates"] = _rates(row)
        row["post_url"] = post_url(row)
        row["has_thumbnail"] = bool(row.pop("thumbnail_s3_key", None))
        row.pop("video_s3_key", None)
    return {"videos": rows, "total": count, "page": page, "page_size": PAGE_SIZE}


def _category_nav(category: str, *, current: str, footer: bool = False) -> str:
    base = "/library/videos" if current == "videos" else "/library/cases"
    links = []
    for code, label in [("", "すべて"), *CATEGORY_LABELS.items()]:
        url = base + ("?" + urlencode({"category": code}) if code else "")
        selected = ' aria-current="page"' if category == code else ""
        links.append(f'<a href="{_e(url)}"{selected}>{label}</a>')
    return (
        '<nav class="category-nav" aria-label="'
        + ("フッターの種別" if footer else "種別ナビゲーション")
        + '">'
        + "".join(links)
        + "</nav>"
    )


def _page(
    title: str,
    body: str,
    *,
    admin: bool = False,
    current: str = "cases",
    params: Mapping[str, str] | None = None,
    hero: bool = False,
) -> str:
    params = params or {}
    links = [
        ("connect", "/", "Connect ホーム"),
        ("home", "/library", "ホーム"),
        ("cases", "/library/cases", "事例一覧"),
        ("videos", "/library/videos", "動画一覧"),
    ]
    if admin:
        links.append(("admin", "/library/admin/campaigns", "管理"))
    nav = "".join(
        f'<a href="{url}"' + (' aria-current="page"' if key == current else "") + f">{label}</a>"
        for key, url, label in links
    )
    brand = "<span>動画ライブラリ<small>VIDEO ARCHIVE</small></span>"
    search = (
        '<details class="header-search"><summary aria-label="検索を開く">'
        '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
        '<circle cx="10" cy="10" r="6" stroke="currentColor" stroke-width="2"/>'
        '<path d="m15 15 6 6" stroke="currentColor" stroke-width="2"/></svg></summary>'
        '<form action="/library/videos" role="search"><label class="sr-only" '
        'for="header-query">事例・動画を検索</label>'
        f'<input id="header-query" type="search" name="q" value="{_e(params.get("q", ""))}" '
        'placeholder="企業名・キーワード #PR"><button type="submit">検索</button></form></details>'
    )
    category = params.get("category", "")
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)} | 動画ライブラリ</title><style>{_STYLE}</style></head><body>"
        '<header><div class="header-inner"><a class="brand" href="/library">'
        + brand
        + "</a>"
        + search
        + f'<nav class="main-nav" aria-label="メインナビゲーション">{nav}</nav></div>'
        + _category_nav(category, current=current)
        + "</header><main>"
        + (
            ""
            if hero
            else f'<div class="page-title"><p class="eyebrow">VIDEO ARCHIVE</p>'
            f"<h1>{_e(title)}</h1></div>"
        )
        + body
        + '</main><footer><a class="brand" href="/library">'
        + brand
        + "</a>"
        + _category_nav(category, current=current, footer=True)
        + "<p>数値は毎平日 18 時に更新<span>社内限定</span></p></footer>"
        + f"<script>{_SCRIPT}</script></body></html>"
    )


def compact_plays(value: float | None) -> str:
    """Round down to one decimal in Japanese units; exact values live on details."""
    if value is None:
        return "再生数未集計"
    count = max(0, int(value))
    for unit, size in (("億", 100_000_000), ("万", 10_000)):
        if count >= size:
            tenth = count * 10 // size
            number = str(tenth // 10) + (f".{tenth % 10}" if tenth % 10 else "")
            return f"{number}{unit}回再生"
    return f"{count:,}回再生"


def _category_label(code: str | None) -> str:
    if code is None or code not in CATEGORY_LABELS:
        return ""
    label = CATEGORY_LABELS[code]
    return (
        f'<span class="category-label" title="{label}" aria-label="{label}">'
        f"{CATEGORY_ENGLISH[code]}</span>"
    )


def _section_heading(english: str, japanese: str, description: str, url: str) -> str:
    return (
        '<div class="section-heading"><div><h2>'
        f"{english}<span>{japanese}</span></h2><p>{description}</p></div>"
        f'<a class="more-link" href="{_e(url)}" aria-label="{japanese}をもっと見る">'
        'もっと見る <span aria-hidden="true">→</span></a></div>'
    )


def _select(name: str, options: list[tuple[str, str]], value: str | None = "") -> str:
    label = {"category": "区分", "pr": "PRの有無", "sort": "並び順"}.get(name, "種別")
    return (
        f'<select name="{_e(name)}" aria-label="{label}">'
        + "".join(
            f'<option value="{_e(code)}"'
            + (" selected" if code == (value or "") else "")
            + f">{_e(label)}</option>"
            for code, label in options
        )
        + "</select>"
    )


def _search_summary(params: Mapping[str, str], total: int, *, catalog: bool = False) -> str:
    if not params.get("q", "").strip() and not params.get("pr"):
        return ""
    terms, is_pr = parse_search(params)
    subject = "事例" if catalog else "動画"
    if is_pr is not None:
        subject = ("PR投稿" if is_pr else "PRなしの投稿") + ("を含む事例" if catalog else "")
    query = f"「{_e(' / '.join(terms))}」に一致する" if terms else ""
    body = f'<p class="search-summary">{query}{subject} {total:,}件'
    body += "（すべての語を含む）" if len(terms) > 1 else ""
    body += "</p>"
    if total == 0:
        body += (
            '<p class="empty">一致する結果がありません。'
            "検索する語を減らすか、別の語をお試しください。"
        )
        if is_pr is True:
            body += "#PR を外す・PRの絞り込みを解除すると、PR以外の投稿も探せます。"
        body += "</p>"
    return body


def _listing(
    data: dict[str, Any], params: Mapping[str, str], cases: list[Any] | None = None
) -> str:
    filters = _filters(params, catalog=False)
    if cases:
        filters += '<section class="archive-section" aria-labelledby="matching-cases">'
        filters += '<h2 id="matching-cases">一致した事例</h2><div class="grid campaign-grid">'
        filters += "".join(_campaign_card(row) for row in cases[:4]) + "</div></section>"
    if params.get("q", "").strip():
        filters += "<h2>一致した動画</h2>"
    filters += _search_summary(params, data["total"])
    filters += (
        "<p class=muted>再生・保存率・ENG率と並び順は総計の最新数値です。"
        "カードは丸めた再生数を表示します。正確な数値・保存率は動画詳細で確認できます。</p>"
    )
    cards = [_video_card(row, selectable=True) for row in data["videos"]]
    return (
        filters
        + f"<p>{data['total']:,}件 · {data['page']:,}ページ</p>"
        + '<button id="copy" type="button">選択したURLをコピー</button>'
        + '<p id="copy-status" role="status"></p><h2 class="sr-only">検索結果の動画</h2>'
        + _paged_cards(
            cards,
            total=data["total"],
            number=data["page"],
            size=PAGE_SIZE,
            path="/library/videos",
            params=params,
            layout="video-grid",
        )
    )


def _paged_cards(
    cards: list[str],
    *,
    total: int,
    number: int,
    size: int,
    path: str,
    params: Mapping[str, str],
    layout: str,
) -> str:
    links = []
    for n, label, relation in ((number - 1, "前へ", "prev"), (number + 1, "次へ", "next")):
        if 1 <= n <= 1_000_000 and (n - 1) * size < total:
            url = path + "?" + urlencode({**params, "page": n})
            links.append(f'<a rel="{relation}" href="{_e(url)}">{label}</a>')
    return (
        f'<div data-autoload data-total="{total}"><div class="grid {layout}" data-items>'
        + "".join(cards)
        + '</div><nav class="pagination" aria-label="ページ送り">'
        + "".join(links)
        + "</nav>"
        + '<p data-load-status role="status" aria-live="polite">'
        + f"{len(cards):,}件表示（全{total:,}件）</p>"
        + '<button type="button" data-load-more hidden>さらに読み込む</button></div>'
    )


CATEGORY_LABELS = {
    "media": "メディア",
    "tto": "TTO",
    "kirinuki": "切り抜き",
    "other": "その他",
    "organic": "オーガニック",
}
CATEGORY_ENGLISH = {
    "media": "MEDIA",
    "tto": "TTO",
    "kirinuki": "CLIP",
    "other": "OTHER",
    "organic": "ORGANIC",
}


def _campaign_url(row: Mapping[str, Any]) -> str:
    return "/library/c?" + urlencode({key: row[key] for key in ("advertiser", "campaign")})


def _badge(code: str | None) -> str:
    return (
        f'<a class="badge" data-category="{_e(code)}" '
        f'href="/library/videos?{_e(urlencode({"category": code or "unset"}))}">'
        f"{_e(CATEGORY_LABELS.get(code or '', '未設定'))}</a>"
    )


def _row_tags(row: Mapping[str, Any]) -> str:
    body = (
        '<div class="tag-groups"><div class="tag-group"><h3>区分</h3>'
        + _badge(row.get("business_category"))
        + "</div>"
    )
    for field, label in (("industry", "業種"), ("purpose", "目的")):
        value = row.get(field)
        tag = (
            f'<a class="tag" href="/library/videos?{_e(urlencode({field: value}))}">{_e(value)}</a>'
            if value
            else '<span class="tag">未設定</span>'
        )
        body += f'<div class="tag-group"><h3>{label}</h3>{tag}</div>'
    return body + "</div>"


def _cover(row: Mapping[str, Any]) -> str:
    account = str(
        row.get("account_name") or row.get("account_handle") or row.get("advertiser") or "V"
    )
    body = (
        f'<span class="placeholder" '
        f'data-category="{_e(row.get("business_category"))}"><strong>'
        f"{_e(account[0].upper())}</strong>"
        f'<span class="placeholder-account" title="{_e(account)}">{_e(account)}</span>'
        "<small>サムネ準備中</small></span>"
    )
    if row.get("has_thumbnail") or row.get("thumbnail_s3_key"):
        path = "/".join(quote(str(row[key]), safe="") for key in ("platform", "video_id"))
        body += (
            f'<img loading="lazy" src="/library/thumb/{path}" alt="{_e(account)}の動画サムネイル">'
        )
    return body


def _preview(row: Mapping[str, Any], *, detail: bool = False) -> str:
    url = post_url(row)
    vid = str(row["video_id"])
    player = (
        f' data-player="https://www.tiktok.com/player/v1/{_e(vid)}?autoplay=1'
        f'&amp;muted=1&amp;controls={1 if detail else 0}&amp;loop=1"'
        if row["platform"] == "tiktok" and vid.isdigit() and url
        else ""
    )
    cover = _cover(row)
    if detail:
        button = (
            '<button class="play-overlay" type="button" data-play="detail-player">'
            '<span class="primary button">動画を再生</span></button>'
            if player
            else ""
        )
        return (
            f'<div class="preview detail-player" id="detail-player"{player}>{cover}{button}</div>'
        )
    return f'<span class="preview"{player}>{cover}</span>'


def _video_card(
    row: Mapping[str, Any], *, selectable: bool = False, in_campaign: bool = False
) -> str:
    path = "/".join(quote(str(row[key]), safe="") for key in ("platform", "video_id"))
    url = post_url(row)
    account = str(row.get("account_name") or row.get("account_handle") or "動画")
    title = str(row.get("campaign") or row.get("caption") or account).strip()
    if in_campaign:
        caption = str(row.get("caption") or "").splitlines()
        title = (caption[0].strip() if caption else "") or account
    category = _category_label(row.get("business_category"))
    metadata = [_day(row["posted_at"])] if isinstance(row.get("posted_at"), datetime) else []
    flags = " · ".join(
        label for field, label in (("is_pr", "PR"), ("is_private", "非公開")) if row.get(field)
    )
    tags = "".join(
        f'<span class="card-tag"><span class="filter-label">{label}</span>'
        f'<span class="badge">{_e(row[field])}</span></span>'
        for field, label in (("industry", "業種"), ("purpose", "目的"))
        if row.get(field)
    )
    checkbox = (
        f'<label class="card-select"><input type="checkbox" data-url="{_e(url)}" '
        f'aria-label="{_e(account)}の動画を選択">選択</label>'
        if selectable and url
        else ""
    )
    return (
        f'<article class="card"><a class="card-link" href="/library/v/{path}" '
        f'aria-label="{_e(account)}: {_e(title)}の動画詳細">{_preview(row)}'
        '<div class="card-body">'
        + category
        + (f'<span class="period">{" · " if category else ""}{flags}</span>' if flags else "")
        + f'<h3>{_e(title)}</h3><p class="card-meta" '
        f'title="合計再生 {_metric(row.get("total_plays"))}">' + '<span class="meta-source">'
        f'<span class="account-name" title="{_e(account)}">{_e(account)}</span>'
        + (" · " + " · ".join(metadata) if metadata else "")
        + '</span><span class="meta-plays">'
        + compact_plays(row.get("total_plays"))
        + "</span></p>"
        + f'<div class="card-tags">{tags}</div></div></a>{checkbox}</article>'
    )


def _filters(
    params: Mapping[str, str],
    *,
    catalog: bool,
) -> str:
    _, is_pr = parse_search(params)
    params = {**params, "pr": "" if is_pr is None else "yes" if is_pr else "no"}
    base = "/library/cases" if catalog else "/library/videos"
    sorts = (
        [("plays", "合計再生順"), ("new", "新着順"), ("count", "本数順")]
        if catalog
        else [("new", "新着順"), ("plays", "再生順"), ("saves", "保存率順"), ("eng", "ENG率順")]
    )
    body = (
        f'<form method="get" action="{base}" class="filters"><div class="search-row">'
        '<label><span class="sr-only">事例・動画を検索</span>'
        f'<input type="search" name="q" value="{_e(params.get("q", ""))}" '
        'placeholder="キーワードで探す"></label>'
        + _select("sort", sorts, params.get("sort", "plays" if catalog else "new"))
        + '<button type="submit" class="primary">検索</button></div>'
    )
    for key in ("category", "advertiser", "campaign", "unassigned", "tag_group", "tag"):
        if params.get(key):
            body += f'<input type="hidden" name="{key}" value="{_e(params[key])}">'
    body += '<details class="advanced-filters"><summary>詳しく絞り込む</summary><div>'
    body += _select("pr", [("", "PRすべて"), ("yes", "PRあり"), ("no", "PRなし")], params.get("pr"))
    body += '<div class="filter-row">'
    for field, label in (("industry", "業種"), ("purpose", "目的")):
        body += (
            f'<label>{label}<input name="{field}" '
            f'value="{_e(params.get(field, ""))}" placeholder="{label}名"></label>'
        )
    if not catalog:
        for field, label, kind in (
            ("from", "開始", "date"),
            ("to", "終了", "date"),
            ("account", "アカウント", "text"),
        ):
            body += (
                f'<label>{label}<input type="{kind}" name="{field}" '
                f'value="{_e(params.get(field, ""))}"></label>'
            )
    body += '<button type="submit">適用</button></div><div class="filter-row">'
    body += '<button type="button" disabled>構成・フックで探す（近日公開）</button>'
    body += f'<a href="{base}">条件をクリア</a></div></div></details>'
    labels = {"industry": "業種", "purpose": "目的", "tag": "タグ", "account": "アカウント",
              "from": "開始", "to": "終了"}  # fmt: skip
    active = [{"yes": "PR投稿のみ", "no": "PR以外"}.get(params.get("pr", ""), "")] + [
        f"{labels[key]}: {params[key]}" for key in labels if params.get(key)
    ]
    active = [item for item in active if item]
    if active:
        body += '<p class="active-filters">絞り込み中: ' + _e(" / ".join(active)) + "</p>"
    return body + "</form>"


def search_campaigns(conn: Any, params: Mapping[str, str], *, admin: bool) -> list[Any]:
    """One GROUP BY over latest snapshots; unknown totals stay unknown."""
    orders = {"plays": "total_plays", "new": "last_posted", "count": "video_count"}
    sort = params.get("sort", "plays")
    if sort not in orders:
        raise HTTPException(400, "並び順が不正です")
    where, values, _, _ = _where({**params, "sort": "plays"}, admin)
    totals = ", ".join(
        f"CASE WHEN count(m.total_{field})=count(*) THEN sum(m.total_{field}) END AS total_{field}"
        for field in ("plays", "saves", "likes", "comments", "shares")
    )
    # All interpolated fragments are fixed SQL/allowlisted orders; values remain parameters.
    sql = f"""WITH categorized AS (
        SELECT v.*, count(*) OVER (
            PARTITION BY v.advertiser, v.campaign, v.business_category) AS category_count
        FROM vl_videos_effective v WHERE {where}
        AND COALESCE(v.advertiser,'')<>'' AND COALESCE(v.campaign,'')<>'')
        SELECT v.advertiser, v.campaign, count(*) AS video_count,
        min(v.posted_at) AS first_posted, max(v.posted_at) AS last_posted,
        array_agg(DISTINCT v.business_category) AS categories,
        (array_agg(v.business_category ORDER BY v.category_count DESC,
            CASE v.business_category WHEN 'media' THEN 0 WHEN 'tto' THEN 1
                WHEN 'kirinuki' THEN 2 WHEN 'other' THEN 3 WHEN 'organic' THEN 4
                ELSE 5 END))[1] AS primary_category,
        max(v.industry) AS industry, max(v.purpose) AS purpose, {totals},
        avg(CASE WHEN m.total_plays > 0
            THEN m.total_saves / m.total_plays END) AS average_save_rate,
        (array_agg(jsonb_build_object('platform',v.platform,'video_id',v.video_id,
            'account_name',v.account_name,'account_handle',v.account_handle,
            'business_category',v.business_category,
            'has_thumbnail',NULLIF(v.thumbnail_s3_key,'') IS NOT NULL)
            ORDER BY (NULLIF(v.thumbnail_s3_key,'') IS NOT NULL) DESC,
            m.total_plays DESC NULLS LAST, v.platform, v.video_id))[1] AS representative
        FROM categorized v {_METRICS_JOIN}
        GROUP BY v.advertiser, v.campaign
        ORDER BY {orders[sort]} DESC NULLS LAST, v.advertiser, v.campaign"""  # nosec B608
    return list(conn.execute(sql, values).fetchall())


def diversify(rows: list[Any], limit: int = 12) -> list[Any]:
    """Keep ranking order where possible; omit repeats if no distinct neighbour exists."""
    remaining = list(rows)
    result: list[Any] = []
    while remaining and len(result) < limit:

        def compatible(row: Mapping[str, Any]) -> bool:
            if not result:
                return True
            previous = result[-1]
            same_campaign = bool(row.get("advertiser") and row.get("campaign")) and (
                row.get("advertiser"),
                row.get("campaign"),
            ) == (previous.get("advertiser"), previous.get("campaign"))
            account = row.get("account_handle") or row.get("account_name")
            same_account = bool(account) and (row["platform"], account) == (
                previous["platform"],
                previous.get("account_handle") or previous.get("account_name"),
            )
            return not same_campaign and not same_account

        index = next((i for i, row in enumerate(remaining) if compatible(row)), None)
        if index is None:
            break
        result.append(remaining.pop(index))
    return result


def featured_videos(conn: Any, params: Mapping[str, str], *, admin: bool) -> list[Any]:
    where, values, _, _ = _where({**params, "sort": "plays"}, admin)
    # Rank each campaign/account before the bound, so a posting burst cannot fill the pool.
    sql = f"""WITH ranked AS (
        SELECT v.*, {_METRIC_SELECT},
        row_number() OVER (PARTITION BY COALESCE(NULLIF(v.advertiser,''),v.platform),
            COALESCE(NULLIF(v.campaign,''),v.video_id)
            ORDER BY m.total_plays DESC NULLS LAST, v.video_id) AS campaign_rank,
        row_number() OVER (PARTITION BY v.platform,
            COALESCE(v.account_handle,v.account_name,v.video_id)
            ORDER BY m.total_plays DESC NULLS LAST, v.video_id) AS account_rank
        FROM vl_videos_effective v {_METRICS_JOIN} WHERE {where})
        SELECT * FROM ranked ORDER BY greatest(campaign_rank,account_rank),
        total_plays DESC NULLS LAST, platform, video_id LIMIT 240"""  # nosec B608
    rows = list(conn.execute(sql, values).fetchall())
    # A browse shelf: thumbnailed videos lead, so the first cards are not placeholders.
    rows.sort(
        key=lambda row: (
            bool(row.get("thumbnail_s3_key")),
            row.get("total_plays") is not None,
            row.get("total_plays") or 0,
        ),
        reverse=True,
    )
    return diversify(rows)


def _campaign_card(row: Mapping[str, Any]) -> str:
    period = " — ".join(
        _day(row[key])
        for key in ("first_posted", "last_posted")
        if isinstance(row.get(key), datetime)
    )
    meta = " · ".join(
        value
        for value in (str(row["advertiser"]), period, f"{row['video_count']:,}本の動画")
        if value
    )
    return (
        f'<article class="card"><a class="card-link" href="{_e(_campaign_url(row))}" '
        f'aria-label="{_e(row["advertiser"])}: {_e(row["campaign"])}の事例">'
        '<span class="preview">' + _cover(row["representative"]) + "</span>"
        '<div class="card-body">'
        + " · ".join(label for code in row["categories"] if (label := _category_label(code)))
        + f'<h3>{_e(row["campaign"])}</h3><p class="card-meta" title="{_e(meta)}">'
        + f'<span class="meta-source">{_e(meta)}</span><span class="meta-plays">'
        + compact_plays(row.get("total_plays"))
        + "</span></p></div></a></article>"
    )


def archive_home(cases: list[Any], videos: list[Any]) -> str:
    # Reuse one aggregate result for every shelf, without a query per category.
    ranked = sorted(cases, key=lambda r: r.get("total_plays") or 0, reverse=True)
    pickup = sorted(
        ranked,
        key=lambda r: bool(
            r["representative"].get("has_thumbnail") or r["representative"].get("thumbnail_s3_key")
        ),
        reverse=True,
    )[:5]
    recent = sorted(
        cases,
        key=lambda r: (
            r["last_posted"].timestamp()
            if isinstance(r.get("last_posted"), datetime)
            else float("-inf")
        ),
        reverse=True,
    )[:8]
    body = '<h1 class="sr-only">動画の事例を、次のアイデアに。</h1>'
    for key, heading, label, description, rows, layout, url in (
        (
            "pickup",
            "PICK UP",
            "注目の事例",
            "いま見ておきたい、成果につながるクリエイティブ。",
            pickup,
            "pickup-grid",
            "/library/cases?sort=plays",
        ),
        (
            "new",
            "NEW",
            "新着の事例",
            "最新の投稿から、施策の動きをチェック。",
            recent,
            "new-grid",
            "/library/cases?sort=new",
        ),
    ):
        body += f'<section class="archive-section" id="{key}">'
        body += _section_heading(heading, label, description, url)
        body += (
            '<div class="campaign-grid '
            + layout
            + '">'
            + "".join(_campaign_card(row) for row in rows)
            + "</div>"
            if rows
            else ('<p class="empty">条件に合う事例がありません。</p>')
        )
        body += "</section>"
    body += '<section class="archive-section" id="trending">' + _section_heading(
        "TRENDING",
        "よく伸びた動画",
        "再生上位から、異なる案件・アカウントをピックアップ。",
        "/library/videos?sort=plays",
    )
    body += '<div class="video-grid video-shelf">' + "".join(_video_card(r) for r in videos)
    body += "</div></section>"
    for code in ("media", "tto", "kirinuki", "organic", "other", None):
        rows = [r for r in ranked if code == r["primary_category"]][:4]
        if not rows:
            continue
        label = CATEGORY_LABELS.get(code or "", "種別未設定")
        url = "/library/cases?" + urlencode({"category": code or "unset"})
        body += (
            f'<section class="archive-section category-section" data-category="{code or "unset"}">'
        )
        body += _section_heading(
            CATEGORY_ENGLISH.get(code or "", "UNCLASSIFIED"),
            label,
            f"{label}の事例から、次のヒントを。",
            url,
        )
        layout = "new-grid" if len(rows) < 4 else "grid"
        body += f'<div class="{layout} campaign-grid">' + "".join(_campaign_card(r) for r in rows)
        body += "</div></section>"
    body += '<section class="archive-section archive-entry">' + _section_heading(
        "ARCHIVE", "すべての事例", "業種や目的で絞って、企画に合う事例を探す。", "/library/cases"
    )
    body += (
        '<a class="button primary" href="/library/cases">'
        f"すべての事例を見る（{len(cases):,}件）</a>"
    )
    body += '<a class="organic-entry" href="/library/videos?category=organic&unassigned=yes">'
    return body + "案件に紐づかないオーガニック投稿を見る →</a></section>"


def _campaign_stats(row: Mapping[str, Any]) -> str:
    rates = _rates(row)
    items = [
        ("合計再生", _metric(row["total_plays"])),
        ("合計保存", _metric(row["total_saves"])),
        ("保存率", _rate(rates["save_rate"], rates["is_reference"])),
        ("ENG率", _rate(rates["engagement_rate"], rates["is_reference"])),
    ]
    return (
        '<dl class="stats">'
        + "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in items)
        + "</dl>"
    )


def campaign_copy_text(row: Mapping[str, Any], videos: list[Any]) -> str:
    rates = _rates(row)
    lines = [
        f"事例: {row['advertiser']} / {row['campaign']}",
        f"期間: {_day(row['first_posted'])} 〜 {_day(row['last_posted'])}",
        f"動画: {row['video_count']:,}本",
        f"合計再生: {_metric(row['total_plays'])}",
        f"合計保存: {_metric(row['total_saves'])}",
        f"合計いいね: {_metric(row.get('total_likes'))}",
        f"合計コメント: {_metric(row.get('total_comments'))}",
        f"合計シェア: {_metric(row.get('total_shares'))}",
        f"保存率: {_rate(rates['save_rate'], rates['is_reference'])}",
        f"ENG率: {_rate(rates['engagement_rate'], rates['is_reference'])}",
        "数値は各動画の最新の総計。保存率=合計保存÷合計再生。ENG率=(いいね+保存+コメント+シェア)÷合計再生。欠損を含む合計は算出不可。",
        "",
        "動画一覧:",
    ]
    for video in videos:
        url = post_url(video)
        if url:
            lines.append(f"{url} | 再生 {_metric(video.get('total_plays'))}")
    return "\n".join(lines)


def parse_handle(raw: str) -> str:
    value = raw.strip()
    if "://" in value:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
            ):
                raise ValueError
            value = parsed.path.rstrip("/")
            if not value.startswith("/@"):
                raise ValueError
            value = value[2:]
        except ValueError:
            raise HTTPException(400, "TikTokのアカウントURLを入力してください") from None
    value = value.removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_][a-z0-9_.]{0,23}", value):
        raise HTTPException(400, "ハンドルが不正です")
    return value


def register_library_routes(
    app: FastAPI,
    *,
    authenticate: Callable[[Request], str | None],
    admins: Callable[[], frozenset[str]],
    get_pg: Callable[[], Any],
    render: Callable[..., HTMLResponse],
) -> None:
    def identity(request: Request) -> str:
        email = authenticate(request)
        if email is None:
            target = (
                library_target(
                    request.url.path + ("?" + request.url.query if request.url.query else "")
                )
                or "/library"
            )
            raise HTTPException(
                303, headers={"Location": "/search/login?next=" + quote(target, safe="")}
            )
        return email

    router = APIRouter(prefix="/library", dependencies=[Depends(identity)])

    def is_admin(email: str) -> bool:
        return email.strip().lower() in admins()

    def connection(email: str) -> Any:
        return get_pg().connection(
            app_role="teamagent_app",
            user_email=email,
            user_groups=[email.split("@")[-1]],
            user_role="admin" if is_admin(email) else "user",
        )

    def admin_identity(request: Request) -> str:
        email = identity(request)
        if not is_admin(email):
            raise HTTPException(403)
        return email

    def page(
        title: str,
        body: str,
        email: str,
        current: str = "cases",
        *,
        params: Mapping[str, str] | None = None,
        hero: bool = False,
    ) -> HTMLResponse:
        response = render(
            _page(title, body, admin=is_admin(email), current=current, params=params, hero=hero)
        )
        response.headers["Content-Security-Policy"] = response.headers[
            "Content-Security-Policy"
        ].replace(
            "frame-src https://accounts.google.com",
            "frame-src https://accounts.google.com https://www.tiktok.com",
        )
        return response

    @router.get("")
    def catalog(request: Request, email: str = Depends(identity)) -> Response:
        if request.query_params:
            params = {k: v for k, v in request.query_params.items() if k != "all"}
            return RedirectResponse(
                "/library/cases" + ("?" + urlencode(params) if params else ""), status_code=303
            )
        with connection(email) as conn:
            cases = search_campaigns(conn, {}, admin=is_admin(email))
            videos = featured_videos(conn, {}, admin=is_admin(email))
        return page(
            "動画の事例を、次のアイデアに。", archive_home(cases, videos), email, "home", hero=True
        )

    @router.get("/cases")
    def case_listing(request: Request, email: str = Depends(identity)) -> HTMLResponse:
        params = request.query_params
        with connection(email) as conn:
            cases = search_campaigns(conn, params, admin=is_admin(email))
        _, _, number, _ = _where({**params, "sort": "plays"}, is_admin(email))
        size = 12
        visible = cases[(number - 1) * size : number * size]
        body = _filters(params, catalog=True)
        body += f'<p class="result-count">{len(cases):,}件の事例 · {number:,}ページ</p>'
        body += _search_summary(params, len(cases), catalog=True)
        body += _paged_cards(
            [_campaign_card(r) for r in visible],
            total=len(cases),
            number=number,
            size=size,
            path="/library/cases",
            params=params,
            layout="campaign-grid",
        )
        if not visible:
            body += '<p class="empty">条件に合う事例がありません。</p>'
        body += '<p><a href="/library/videos?category=organic&unassigned=yes">'
        body += "案件に紐づかないオーガニック投稿を見る →</a></p>"
        return page("事例一覧", body, email, params=params)

    @router.get("/c")
    def campaign_detail(request: Request, email: str = Depends(identity)) -> HTMLResponse:
        params = {key: request.query_params.get(key, "") for key in ("advertiser", "campaign")}
        if not all(params.values()):
            raise HTTPException(400, "広告主と案件を指定してください")
        where, values, number, _ = _where(
            {**params, "page": request.query_params.get("page", "1")}, is_admin(email)
        )
        with connection(email) as conn:
            cases = search_campaigns(conn, params, admin=is_admin(email))
            if not cases:
                raise HTTPException(404)
            # Keep the entire case for one-click copy; only the visible grid is paginated.
            sql = f"SELECT v.*, {_METRIC_SELECT} FROM vl_videos_effective v {_METRICS_JOIN} WHERE {where} ORDER BY m.total_plays DESC NULLS LAST, v.platform, v.video_id"  # noqa: E501  # nosec B608
            videos = list(conn.execute(sql, values).fetchall())
        case = cases[0]
        body = '<p class="breadcrumb"><a href="/library/cases">事例一覧</a> / 事例詳細</p>'
        body += '<div class="case-hero"><div class="case-cover preview">' + _cover(
            case["representative"]
        )
        body += '</div><div class="case-intro"><p class="eyebrow">CASE STUDY</p>'
        body += (
            f'<p class="case-advertiser">{_e(case["advertiser"])}</p>'
            f"<h1>{_e(case['campaign'])}</h1>"
        )
        body += '<div class="chips">' + "".join(_badge(c) for c in case["categories"]) + "</div>"
        body += (
            f'<p class="period">{_day(case["first_posted"])} — '
            f"{_day(case['last_posted'])} · {case['video_count']:,}本の動画</p>"
        )
        for field, label in (("industry", "業種"), ("purpose", "目的")):
            if case.get(field):
                body += f"<p>{label}: {_e(case[field])}</p>"
        body += (
            '<div class="actions"><button type="button" class="cta" '
            'data-copy="campaign-copy">この事例をコピー</button><button type="button" '
            'data-copy="campaign-urls">URLだけコピー</button></div>'
            '<p id="copy-status" role="status"></p></div>'
        )
        body += '<div class="case-numbers">' + _campaign_stats(case)
        body += (
            '<p class="muted">各動画の最新の総計。保存率＝合計保存÷合計再生、'
            "ENG率＝（いいね＋保存＋コメント＋シェア）÷合計再生。"
            "欠損を含む合計は算出不可。</p></div></div>"
        )
        body += f'<pre id="campaign-copy" hidden>{_e(campaign_copy_text(case, videos))}</pre>'
        urls = "\n".join(url for row in videos if (url := post_url(row)))
        body += (
            f'<pre id="campaign-urls" hidden>{_e(urls)}</pre><div '
            f'class="section-heading"><h2>この事例の動画</h2><p>再生の多い順</p></div>'
        )
        body += _paged_cards(
            [
                _video_card(row, in_campaign=True)
                for row in videos[(number - 1) * PAGE_SIZE : number * PAGE_SIZE]
            ],
            total=len(videos),
            number=number,
            size=PAGE_SIZE,
            path="/library/c",
            params=request.query_params,
            layout="video-grid",
        )
        return page(str(case["campaign"]), body, email, hero=True)

    @router.get("/videos")
    def listing(request: Request, email: str = Depends(identity)) -> Response:
        cases = []
        with connection(email) as conn:
            data = search_videos(conn, request.query_params, admin=is_admin(email))
            if request.query_params.get("q", "").strip() and data["page"] == 1:
                cases = search_campaigns(
                    conn, {**request.query_params, "sort": "plays"}, admin=is_admin(email)
                )
        return page(
            "動画一覧",
            _listing(data, request.query_params, cases),
            email,
            "videos",
            params=request.query_params,
        )

    @router.get("/api/videos")
    def api(request: Request, email: str = Depends(identity)) -> JSONResponse:
        with connection(email) as conn:
            return JSONResponse(
                jsonable_encoder(search_videos(conn, request.query_params, admin=is_admin(email)))
            )

    def video(conn: Any, platform: str, video_id: str, email: str) -> Any:
        # 列名と JOIN はコード内の固定値。識別子と権限は %s で渡す
        detail_sql = f"SELECT v.*, {_METRIC_SELECT}, m.captured_on FROM vl_videos_effective v {_METRICS_JOIN} WHERE v.platform=%s AND v.video_id=%s AND (%s OR NOT v.is_private)"  # noqa: E501  # nosec B608
        row = conn.execute(
            detail_sql,
            (platform, video_id, is_admin(email)),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        return row

    @router.get("/v/{platform}/{video_id}")
    def detail(platform: str, video_id: str, email: str = Depends(identity)) -> HTMLResponse:
        with connection(email) as conn:
            row = video(conn, platform, video_id, email)
            analyses = conn.execute(
                "SELECT kind, version, payload FROM vl_video_analysis "
                "WHERE platform=%s AND video_id=%s ORDER BY analyzed_at DESC",
                (platform, video_id),
            ).fetchall()
            related = []
            if row.get("advertiser") and row.get("campaign"):
                where, values, _, _ = _where(
                    {"advertiser": row["advertiser"], "campaign": row["campaign"]}, is_admin(email)
                )
                related_sql = f"SELECT v.*, {_METRIC_SELECT} FROM vl_videos_effective v {_METRICS_JOIN} WHERE {where} AND NOT (v.platform=%s AND v.video_id=%s) ORDER BY m.total_plays DESC NULLS LAST, v.platform, v.video_id LIMIT 6"  # noqa: E501  # nosec B608
                related = conn.execute(related_sql, [*values, platform, video_id]).fetchall()
        body = '<div class="detail-layout"><div>' + _preview(row, detail=True)
        url = post_url(row)
        if url:
            body += (
                f'<p><a href="{_e(url)}" target="_blank" rel="noopener noreferrer">'
                f"TikTokで開く</a></p>"
            )
        account = row.get("account_name") or row.get("account_handle") or ""
        body += (
            f'<div class="detail-meta"><h2 class="account-name" title="{_e(account)}">'
            f"{_e(account)}</h2>"
        )
        if isinstance(row.get("posted_at"), datetime):
            body += f'<p class="posted-date muted">{_day(row["posted_at"])}</p>'
        body += _row_tags(row)
        if str(row.get("caption") or "").strip():
            body += "<h3>投稿文</h3><pre>" + _e(row["caption"]) + "</pre>"
        if row.get("advertiser") and row.get("campaign"):
            body += (
                f'<p><a href="{_e(_campaign_url(row))}">{_e(row["advertiser"])} / '
                f"{_e(row['campaign'])} の事例を見る</a></p>"
            )
        body += (
            '</div></div><div><p class="muted">数値取得日: '
            + _e(row.get("captured_on") or "不明")
            + '</p><div class="metric-groups">'
        )
        for family, label in (("organic", "オーガニック"), ("total", "総計"), ("ad", "広告")):
            if family == "ad":
                body += (
                    '<details class="metric-group paid"><summary><h2>広告</h2><span>'
                    f"コスト {_metric(row.get('ad_cost'), 'ad_cost')} · "
                    f"再生 {_metric(row.get('ad_views'))} · "
                    f"CTR {_metric(row.get('ad_ctr'), 'ad_ctr')}"
                    "</span>詳細指標を開く</summary><table>"
                )
            else:
                body += f'<section class="metric-group"><h2>{label}</h2><table>'
            for column in METRIC_COLUMNS:
                if column.startswith(family + "_"):
                    metric_label = {
                        "ad_likes": "有料いいね",
                        "ad_shares": "有料シェア",
                        "ad_comments": "有料コメント",
                        "ad_saves": "有料保存",
                    }.get(column, METRIC_COLUMNS[column])
                    body += (
                        f"<tr><th>{_e(metric_label)}</th><td>"
                        f"{_metric(row.get(column), column)}</td></tr>"
                    )
            body += "</table>"
            if family != "ad":
                rates = _rates(row, family)
                body += (
                    f"<p>保存率 {_rate(rates['save_rate'], rates['is_reference'])} · ENG率 "
                    f"{_rate(rates['engagement_rate'], rates['is_reference'])}</p>"
                )
            body += "</details>" if family == "ad" else "</section>"
        body += "</div></div></div>"
        payload = next(
            (
                a["payload"]
                for a in analyses
                if a["kind"] == ANALYSIS_KIND and a["version"] == ANALYSIS_VERSION
            ),
            None,
        )
        body += render_analysis(payload)
        if related:
            body += '<section class="archive-section" id="related">' + _section_heading(
                "RELATED",
                "同じ事例の他の動画",
                "同じ施策のクリエイティブもチェック。",
                _campaign_url(row),
            )
            body += '<div class="grid video-grid">' + "".join(_video_card(r) for r in related)
            body += "</div></section>"
        return page("動画詳細", body, email, "videos")

    @router.get("/thumb/{platform}/{video_id}")
    def thumbnail(
        request: Request, platform: str, video_id: str, email: str = Depends(identity)
    ) -> Response:
        with connection(email) as conn:
            row = video(conn, platform, video_id, email)
        if not row.get("thumbnail_s3_key"):
            raise HTTPException(404)
        try:
            stored = VideoLibraryStorage().get(platform, video_id)
        except ValueError:
            raise HTTPException(404) from None
        if stored is None:
            raise HTTPException(404)
        body, etag = stored
        headers = {"Cache-Control": "private, max-age=86400", "ETag": etag, "Vary": "Cookie"}
        if etag in [part.strip() for part in request.headers.get("if-none-match", "").split(",")]:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="image/jpeg", headers=headers)

    @router.get("/admin/campaigns")
    def campaigns(request: Request, email: str = Depends(admin_identity)) -> HTMLResponse:
        with connection(email) as conn:
            rows = conn.execute(
                """SELECT c.advertiser, c.campaign, s.business_category,
                    s.industry, s.purpose, s.is_private FROM vl_campaigns c
                    LEFT JOIN vl_campaign_settings s USING(advertiser, campaign)
                    ORDER BY c.advertiser, c.campaign"""
            ).fetchall()
            categories = conn.execute(
                "SELECT * FROM vl_business_categories ORDER BY sort_order"
            ).fetchall()
            missing = conn.execute(
                "SELECT count(*) AS n FROM vl_videos_effective WHERE is_pr AND "
                "(advertiser IS NULL OR campaign IS NULL OR advertiser='' OR campaign='')"
            ).fetchone()["n"]
        body = f"<p>#PR ありで案件の無い動画: {missing}本</p>"
        for row in rows:
            body += (
                '<section><form method="post"><h2>'
                + _e(row["advertiser"])
                + " / "
                + _e(row["campaign"])
                + "</h2>"
            )
            body += "".join(
                f'<input type="hidden" name="{name}" value="{_e(row[name])}">'
                for name in ("advertiser", "campaign")
            )
            body += (
                "<label>種別 "
                + _select(
                    "business_category",
                    [
                        ("", "未設定"),
                        *[
                            (r["code"], r["display_name"])
                            for r in categories
                            if r["code"] != "organic"
                        ],
                    ],
                    row["business_category"],
                )
                + "</label>"
            )
            body += "".join(
                f'<label>{label}<input name="{name}" maxlength="500" '
                f'value="{_e(row[name])}"></label>'
                for name, label in (("industry", "業種"), ("purpose", "目的"))
            )
            body += (
                '<label><input type="checkbox" name="is_private" value="1"'
                + (" checked" if row["is_private"] else "")
                + ">非公開</label><button>保存</button></form></section>"
            )
        return page(
            "案件設定",
            '<p><a href="/library/admin/accounts">アカウント登録へ</a></p>' + body,
            email,
            "admin",
        )

    async def form_data(request: Request) -> dict[str, str]:
        # Same exact-origin check as /admin/analyze, also for URL-encoded admin forms.
        base = urlsplit(os.environ.get("CONNECT_BASE_URL") or str(request.base_url))
        if request.headers.get("origin") != f"{base.scheme}://{base.netloc}":
            raise HTTPException(403)
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413)
        from urllib.parse import parse_qs

        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=12)
        except (UnicodeError, ValueError):
            raise HTTPException(400) from None
        if any(len(values) != 1 for values in parsed.values()):
            raise HTTPException(400)
        return {key: values[0] for key, values in parsed.items()}

    @router.post("/admin/campaigns")
    async def save_campaign(
        request: Request, email: str = Depends(admin_identity)
    ) -> RedirectResponse:
        form = await form_data(request)
        advertiser, campaign = form.get("advertiser", ""), form.get("campaign", "")
        if not advertiser or not campaign or any(len(v) > 500 for v in form.values()):
            raise HTTPException(400)
        category = form.get("business_category") or None
        if category == "organic":
            raise HTTPException(400, "オーガニックはPRなし動画の自動判定です")
        if form.get("is_private", "") not in {"", "1"}:
            raise HTTPException(400)
        with connection(email) as conn:
            if not conn.execute(
                "SELECT 1 FROM vl_campaigns WHERE advertiser=%s AND campaign=%s",
                (advertiser, campaign),
            ).fetchone():
                raise HTTPException(400, "案件が存在しません")
            if (
                category
                and not conn.execute(
                    "SELECT 1 FROM vl_business_categories WHERE code=%s", (category,)
                ).fetchone()
            ):
                raise HTTPException(400, "種別が不正です")
            conn.execute(
                """INSERT INTO vl_campaign_settings
                    (advertiser, campaign, business_category, industry, purpose,
                     is_private, updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (advertiser, campaign) DO UPDATE SET
                    business_category=EXCLUDED.business_category, industry=EXCLUDED.industry,
                    purpose=EXCLUDED.purpose, is_private=EXCLUDED.is_private,
                    updated_by=EXCLUDED.updated_by, updated_at=now()""",
                (
                    advertiser,
                    campaign,
                    category,
                    form.get("industry") or None,
                    form.get("purpose") or None,
                    form.get("is_private") == "1",
                    email,
                ),
            )
        return RedirectResponse("/library/admin/campaigns", status_code=303)

    @router.get("/admin/accounts")
    def accounts(email: str = Depends(admin_identity)) -> HTMLResponse:
        with connection(email) as conn:
            rows = conn.execute(
                "SELECT * FROM vl_account_settings ORDER BY platform, handle"
            ).fetchall()
        body = (
            '<p>登録のみ。自動取得は近日公開です。</p><form method="post" class="account-form">'
            '<label>TikTok URL / ハンドル<input name="handle" required maxlength="300"></label>'
            '<label>表示名<input name="display_name" maxlength="100"></label>'
            '<label>メモ<input name="note" maxlength="1000"></label><button>登録</button></form>'
            '<table class="account-table"><tr><th>アカウント</th><th>表示名</th><th>メモ</th></tr>'
        )
        body += (
            "".join(
                f"<tr><td>{_e(r['platform'])} / @{_e(r['handle'])}</td>"
                f"<td>{_e(r['display_name'])}</td><td>{_e(r['note'])}</td></tr>"
                for r in rows
            )
            + "</table>"
        )
        return page(
            "アカウント登録",
            '<p><a href="/library/admin/campaigns">案件設定へ</a></p>' + body,
            email,
            "admin",
        )

    @router.post("/admin/accounts")
    async def save_account(
        request: Request, email: str = Depends(admin_identity)
    ) -> RedirectResponse:
        form = await form_data(request)
        handle = parse_handle(form.get("handle", ""))
        if len(form.get("display_name", "")) > 100 or len(form.get("note", "")) > 1000:
            raise HTTPException(400)
        with connection(email) as conn:
            conn.execute(
                """INSERT INTO vl_account_settings
                    (platform, handle, display_name, note, registered_by)
                VALUES ('tiktok',%s,%s,%s,%s) ON CONFLICT (platform, handle) DO UPDATE SET
                display_name=EXCLUDED.display_name, note=EXCLUDED.note, updated_at=now()""",
                (handle, form.get("display_name") or None, form.get("note") or None, email),
            )
        return RedirectResponse("/library/admin/accounts", status_code=303)

    app.include_router(router)
