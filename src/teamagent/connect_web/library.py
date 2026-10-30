"""Internal short-video archive. Video reads always use vl_videos_effective."""

from __future__ import annotations

import html
import json
import os
import re
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


def _where(params: Mapping[str, str], admin: bool) -> tuple[str, list[Any], int, str]:
    clauses = [] if admin else ["NOT v.is_private"]
    values: list[Any] = []
    query = params.get("q", "").strip()
    if len(query) > 500:
        raise HTTPException(400, "検索語が長すぎます")
    if query:
        clauses.append("""concat_ws(' ', v.caption, array_to_string(v.hashtags, ' '),
            v.account_name, v.account_handle, v.advertiser, v.campaign,
            v.industry, v.purpose) ILIKE %s""")
        values.append("%" + query + "%")
    category = params.get("category", "")
    if category == "unset":
        clauses.append("v.business_category IS NULL")
    elif category:
        clauses.append("v.business_category = %s")
        values.append(category)
    pr = params.get("pr", "")
    if pr:
        if pr not in {"yes", "no"}:
            raise HTTPException(400, "PR指定が不正です")
        clauses.append("v.is_pr = %s")
        values.append(pr == "yes")
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


def _page(title: str, body: str, *, admin: bool = False, current: str = "cases") -> str:
    links = [("cases", "/library", "事例"), ("videos", "/library/videos", "動画一覧")]
    if admin:
        links.append(("admin", "/library/admin/campaigns", "管理"))
    nav = "".join(
        f'<a href="{url}"' + (' aria-current="page"' if key == current else "") + f">{label}</a>"
        for key, url, label in links
    )
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)} | 動画ライブラリ</title><style>{_STYLE}</style></head><body>"
        '<header><div class="header-inner"><a class="brand" href="/library">'
        '<svg width="25" height="25" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
        '<rect x="2" y="2" width="20" height="20" rx="5" stroke="currentColor" stroke-width="2"/>'
        '<path d="m10 7 7 5-7 5V7Z" fill="currentColor"/></svg>動画ライブラリ</a>'
        f'<nav aria-label="メインナビゲーション">{nav}</nav></div></header><main>'
        f'<p class="eyebrow">VIDEO LIBRARY / '
        f"{'CASES' if current == 'cases' else 'VIDEOS' if current == 'videos' else 'MANAGE'}</p>"
        f"<h1>{_e(title)}</h1>{body}</main><script>{_SCRIPT}</script></body></html>"
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


def _listing(data: dict[str, Any], params: Mapping[str, str], categories: list[Any]) -> str:
    filters = _filters(params, categories, catalog=False)
    filters += (
        "<p class=muted>再生・保存率・ENG率と並び順は総計の最新数値です。"
        "オーガニック再生は値がある場合に併記します。</p>"
    )
    cards = [_video_card(row, selectable=True) for row in data["videos"]]
    pagination = []
    for page, label in ((data["page"] - 1, "前へ"), (data["page"] + 1, "次へ")):
        if page >= 1 and (page - 1) * PAGE_SIZE < data["total"]:
            pagination.append(
                f'<a href="/library/videos?{_e(urlencode({**params, "page": page}))}">{label}</a>'
            )
    return (
        filters
        + f"<p>{data['total']:,}件 · {data['page']:,}ページ</p>"
        + '<button id="copy" type="button">選択したURLをコピー</button>'
        + '<p id="copy-status" role="status"></p><h2 class="sr-only">検索結果の動画</h2>'
        + '<div class="grid video-grid">'
        + "".join(cards)
        + "</div><p>"
        + " · ".join(pagination)
        + "</p>"
    )


CATEGORY_LABELS = {
    "media": "メディア",
    "tto": "TTO",
    "kirinuki": "切り抜き",
    "other": "その他",
    "organic": "オーガニック",
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
        f"{_e(account[0].upper())}</strong><small>サムネ準備中</small></span>"
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
    if url:
        return (
            f'<a class="preview" href="{_e(url)}" target="_blank" rel="noopener '
            f'noreferrer" aria-label="投稿を開く"{player}>{cover}</a>'
        )
    return f'<div class="preview">{cover}</div>'


def _video_card(row: Mapping[str, Any], *, selectable: bool = False) -> str:
    path = "/".join(quote(str(row[key]), safe="") for key in ("platform", "video_id"))
    url = post_url(row)
    checkbox = (
        f'<label><input type="checkbox" data-url="{_e(url)}">選択</label>'
        if selectable and url
        else ""
    )
    rates = _rates(row)
    plays = _metric(row.get("total_plays"))
    if row.get("organic_plays") is not None:
        plays += f"（オーガニック {_metric(row['organic_plays'])}）"
    tags = (
        '<div class="card-tag"><span class="filter-label">区分</span>'
        + _badge(row.get("business_category"))
        + "</div>"
    )
    for field, label in (("industry", "業種"), ("purpose", "目的")):
        if row.get(field):
            tags += (
                f'<div class="card-tag"><span class="filter-label">{label}</span>'
                f'<a class="badge" href="/library/videos?{_e(urlencode({field: row[field]}))}">'
                f"{_e(row[field])}</a></div>"
            )
    flags = ("PR" if row.get("is_pr") else "") + (" / 非公開" if row.get("is_private") else "")
    return (
        f'<article class="card">{_preview(row)}<div class="card-body"><div '
        f'class="card-tags">{tags}</div>'
        f'<h3><a href="/library/v/{path}">'
        f"{_e(row.get('account_name') or row.get('account_handle'))}</a>"
        f"</h3>"
        f'<p class="card-stats">再生 {plays} · 保存率 '
        f"{_rate(rates['save_rate'], rates['is_reference'])}</p>"
        f'<p class="period">{_day(row.get("posted_at"))} · {flags}</p>'
        f'<div class="actions"><a href="/library/v/{path}">詳細を見る</a>'
        f"{checkbox}</div></div></article>"
    )


def _filters(
    params: Mapping[str, str],
    categories: list[Any],
    *,
    catalog: bool,
    cases: list[Any] | None = None,
) -> str:
    base = "/library" if catalog else "/library/videos"
    body = (
        f'<form method="get" action="{base}" class="filters"><div '
        f'class="search-row"><label><span class="sr-only">事例・動画を検索</span>'
        f'<input type="search" name="q" value="{_e(params.get("q", ""))}" '
        f'placeholder="案件名・業種・目的・キーワードで探す"></label><button class="primary">'
        f"検索</button></div>"
    )
    for key in (
        "category",
        "pr",
        "advertiser",
        "campaign",
        "unassigned",
        "tag_group",
        "tag",
    ):
        if params.get(key):
            body += f'<input type="hidden" name="{key}" value="{_e(params[key])}">'
    for field, options in (
        (
            "category",
            [
                ("", "すべて"),
                *[(r["code"], r["display_name"]) for r in categories],
                ("unset", "未設定"),
            ],
        ),
        ("pr", [("", "PRすべて"), ("yes", "PRあり"), ("no", "PRなし")]),
    ):
        body += (
            '<div class="chips" role="group" aria-label="'
            + ("区分" if field == "category" else "PRの有無")
            + '"><span class="filter-label">'
            + ("区分" if field == "category" else "PR")
            + "</span>"
        )
        for value, label in options:
            href = base + "?" + urlencode({**params, field: value, "page": 1})
            selected = ' aria-current="true"' if params.get(field, "") == value else ""
            body += f'<a class="chip" href="{_e(href)}"{selected}>{_e(label)}</a>'
        body += "</div>"
    body += '<div class="filter-row">'
    for field, label in (("industry", "業種"), ("purpose", "目的")):
        choices = sorted({str(row[field]) for row in cases or [] if row.get(field)})
        if catalog and choices:
            body += (
                f'<div class="chips" role="group" aria-label="{label}"><span '
                f'class="filter-label">{label}</span>'
            )
            for value in choices:
                href = base + "?" + urlencode({**params, field: value, "page": 1})
                selected = ' aria-current="true"' if params.get(field) == value else ""
                body += f'<a class="chip" href="{_e(href)}"{selected}>{_e(value)}</a>'
            body += "</div>"
        body += (
            f'<label>{label}<input name="{field}" '
            f'value="{_e(params.get(field, ""))}" placeholder="{label}名で絞り込み">'
            f"</label>"
            if not catalog or not choices
            else f'<input type="hidden" name="{field}" value="{_e(params.get(field, ""))}">'
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
    sorts = (
        [("plays", "合計再生の多い順"), ("new", "新着順"), ("count", "本数の多い順")]
        if catalog
        else [("new", "新しい順"), ("plays", "再生順"), ("saves", "保存率順"), ("eng", "ENG率順")]
    )
    body += _select("sort", sorts, params.get("sort", "plays" if catalog else "new"))
    body += '<button type="submit">適用</button>'
    body += (
        f'<a href="{base}">条件をクリア</a></div><div class="filter-row"><button '
        f'type="button" disabled>構成・フックで探す（近日公開）</button>'
    )
    if params.get("tag"):
        body += (
            f'<span class="badge">'
            f"{_e(TAG_GROUPS.get(params.get('tag_group', ''), '制作基準'))}: "
            f"{_e(params['tag'])}</span>"
        )
    if params.get("industry") or params.get("purpose"):
        body += (
            f'<span class="badge">{_e(params.get("industry", ""))} '
            f"{_e(params.get('purpose', ''))}</span>"
        )
    return body + "</div></form>"


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
    sql = f"""SELECT v.advertiser, v.campaign, count(*) AS video_count,
        min(v.posted_at) AS first_posted, max(v.posted_at) AS last_posted,
        array_agg(DISTINCT v.business_category) AS categories,
        max(v.industry) AS industry, max(v.purpose) AS purpose, {totals},
        avg(CASE WHEN m.total_plays > 0
            THEN m.total_saves / m.total_plays END) AS average_save_rate,
        (array_agg(jsonb_build_object('platform',v.platform,'video_id',v.video_id,
            'account_name',v.account_name,'account_handle',v.account_handle,
            'business_category',v.business_category,
            'has_thumbnail',NULLIF(v.thumbnail_s3_key,'') IS NOT NULL)
            ORDER BY (NULLIF(v.thumbnail_s3_key,'') IS NOT NULL) DESC,
            m.total_plays DESC NULLS LAST, v.platform, v.video_id))[1] AS representative
        FROM vl_videos_effective v {_METRICS_JOIN} WHERE {where}
        AND COALESCE(v.advertiser,'')<>'' AND COALESCE(v.campaign,'')<>''
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
    rows.sort(
        key=lambda row: (row.get("total_plays") is not None, row.get("total_plays") or 0),
        reverse=True,
    )
    return diversify(rows)


def _campaign_card(row: Mapping[str, Any]) -> str:
    url = _e(_campaign_url(row))
    tags = "".join(_badge(code) for code in row["categories"])
    return (
        f'<article class="card"><a class="preview" href="{url}" '
        f'aria-label="{_e(row["campaign"])}の事例を見る">'
        + _cover(row["representative"])
        + f'<span class="cover-note">{row["video_count"]:,}本の動画</span></a>'
        '<div class="card-body">'
        + f'<div class="chips">{tags}</div><p class="muted">{_e(row["advertiser"])}</p>'
        + f'<h3><a href="{url}">{_e(row["campaign"])}</a></h3>'
        + f'<p class="card-stats">合計再生 <strong>{_metric(row["total_plays"])}</strong><br>'
        f"平均保存率 {_rate(row['average_save_rate'], False)}</p>"
        + f'<p class="period">{_day(row["first_posted"])} — {_day(row["last_posted"])}</p>'
        "</div></article>"
    )


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

    def page(title: str, body: str, email: str, current: str = "cases") -> HTMLResponse:
        response = render(_page(title, body, admin=is_admin(email), current=current))
        response.headers["Content-Security-Policy"] = response.headers[
            "Content-Security-Policy"
        ].replace(
            "frame-src https://accounts.google.com",
            "frame-src https://accounts.google.com https://www.tiktok.com",
        )
        return response

    @router.get("")
    def catalog(request: Request, email: str = Depends(identity)) -> HTMLResponse:
        with connection(email) as conn:
            cases = search_campaigns(conn, request.query_params, admin=is_admin(email))
            videos = featured_videos(conn, request.query_params, admin=is_admin(email))
            categories = conn.execute(
                "SELECT * FROM vl_business_categories ORDER BY sort_order"
            ).fetchall()
        body = (
            '<p class="intro muted">提案に合う事例を、ここから。'
            "業種や目的で探して、動画と成果をまとめて持ち出せます。</p>"
        )
        body += _filters(request.query_params, categories, catalog=True, cases=cases)
        body += (
            f'<div class="section-heading"><h2>事例</h2><p class="muted">'
            f"{len(cases):,}件の事例</p></div>"
        )
        body += (
            '<p class="muted">平均保存率は、再生が1以上で保存数がある動画の'
            "保存率の単純平均です。</p>"
        )
        body += (
            '<div class="grid campaign-grid">'
            + "".join(_campaign_card(row) for row in cases)
            + "</div>"
            if cases
            else '<p class="empty">条件に合う事例がありません。'
            "検索語や絞り込み条件を変えてみてください。</p>"
        )
        body += (
            '<div class="section-heading"><h2>よく伸びた動画</h2><a '
            'href="/library/videos?sort=plays">動画一覧で見る</a></div><p '
            'class="muted">再生上位から、案件とアカウントが続かないようにピックアップ。</p>'
        )
        body += (
            '<div class="grid video-grid">' + "".join(_video_card(row) for row in videos) + "</div>"
            if videos
            else '<p class="empty">条件に合う動画がありません。</p>'
        )
        organic_url = "/library/videos?" + urlencode(
            {
                **request.query_params,
                "category": "organic",
                "unassigned": "yes",
                "sort": "plays",
                "page": 1,
            }
        )
        body += (
            f'<div class="section-heading"><h2>オーガニック投稿</h2></div><p '
            f'class="muted">案件に紐づかない投稿も、アカウントやキーワードで探せます。</p>'
            f'<a class="button" '
            f'href="{_e(organic_url)}">オーガニック投稿を見る</a>'
        )
        return page("提案につながる、動画の事例。", body, email)

    @router.get("/c")
    def campaign_detail(request: Request, email: str = Depends(identity)) -> HTMLResponse:
        params = {key: request.query_params.get(key, "") for key in ("advertiser", "campaign")}
        if not all(params.values()):
            raise HTTPException(400, "広告主と案件を指定してください")
        with connection(email) as conn:
            cases = search_campaigns(conn, params, admin=is_admin(email))
            if not cases:
                raise HTTPException(404)
            where, values, _, _ = _where(params, is_admin(email))
            # An entire case is needed for both the video grid and one-click copy.
            sql = f"SELECT v.*, {_METRIC_SELECT} FROM vl_videos_effective v {_METRICS_JOIN} WHERE {where} ORDER BY m.total_plays DESC NULLS LAST, v.platform, v.video_id"  # noqa: E501  # nosec B608
            videos = list(conn.execute(sql, values).fetchall())
        case = cases[0]
        body = (
            f'<p><a href="/library">事例一覧</a> / {_e(case["advertiser"])}</p><div class="chips">'
            + "".join(_badge(code) for code in case["categories"])
            + "</div>"
        )
        body += (
            f'<p class="muted">{_day(case["first_posted"])} — '
            f"{_day(case['last_posted'])} · {case['video_count']:,}本</p>"
        )
        for field, label in (("industry", "業種"), ("purpose", "目的")):
            if case.get(field):
                body += f"<p>{label}: {_e(case[field])}</p>"
        body += _campaign_stats(case)
        body += (
            '<p class="muted">'
            "各動画の最新の総計。保存率＝合計保存÷合計再生、ENG率＝（いいね＋保存＋コメント＋シェア）÷合計再生。欠損を含む合計は算出不可。</p>"
        )
        body += (
            '<div class="actions"><button type="button" class="cta" '
            'data-copy="campaign-copy">この事例をコピー</button><button type="button" '
            'data-copy="campaign-urls">URLだけコピー</button></div><p '
            'id="copy-status" role="status"></p>'
        )
        body += f'<pre id="campaign-copy" hidden>{_e(campaign_copy_text(case, videos))}</pre>'
        urls = "\n".join(url for row in videos if (url := post_url(row)))
        body += (
            f'<pre id="campaign-urls" hidden>{_e(urls)}</pre><div '
            f'class="section-heading"><h2>この事例の動画</h2><p>再生の多い順</p></div>'
        )
        body += (
            '<div class="grid video-grid">' + "".join(_video_card(row) for row in videos) + "</div>"
        )
        return page(str(case["campaign"]), body, email)

    @router.get("/videos")
    def listing(request: Request, email: str = Depends(identity)) -> Response:
        with connection(email) as conn:
            data = search_videos(conn, request.query_params, admin=is_admin(email))
            categories = conn.execute(
                "SELECT * FROM vl_business_categories ORDER BY sort_order"
            ).fetchall()
        return page("動画一覧", _listing(data, request.query_params, categories), email, "videos")

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
        body = '<div class="detail-layout"><div>' + _preview(row, detail=True)
        url = post_url(row)
        if url:
            body += (
                f'<p><a href="{_e(url)}" target="_blank" rel="noopener noreferrer">'
                f"TikTokで開く</a></p>"
            )
        body += (
            f'<div class="detail-meta"><h2>'
            f"{_e(row.get('account_name') or row.get('account_handle'))}</h2><p "
            f'class="muted">{_day(row.get("posted_at"))}</p>'
        )
        body += _row_tags(row) + "<h3>投稿文</h3><pre>" + _e(row.get("caption")) + "</pre>"
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
