"""Internal short-video archive. Video reads always use vl_videos_effective."""

from __future__ import annotations

import html
import json
import os
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from teamagent.adapters.video_library_page import image_media_type
from teamagent.adapters.video_library_storage import VideoLibraryStorage
from teamagent.connect_web.library_analysis import (
    ANALYSIS_KIND,
    ANALYSIS_VERSION,
    TAG_GROUPS,
    render_analysis,
)
from teamagent.connect_web.library_assets import SCRIPT as _SCRIPT
from teamagent.connect_web.library_assets import STYLE as _STYLE
from teamagent.connect_web.portal import PORTAL_STYLE, render_header
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


def _retention_chart(counts: list[int]) -> str:
    if len(counts) < 2 or counts[0] <= 0:
        return "<p>データ不足：視聴の推移を表示できません。</p>"
    ratios = [min(1, max(0, count / counts[0])) for count in counts]
    points = [
        (second / (len(counts) - 1) * 100, 100 - ratio * 100) for second, ratio in enumerate(ratios)
    ]
    half = next((second for second, ratio in enumerate(ratios) if ratio < 0.5), None)
    description = "0秒の視聴者数を100%とした残っている割合。" + "、".join(
        f"{second:,}秒: {ratio:.1%}" for second, ratio in enumerate(ratios)
    )
    body = ""
    if half is not None:
        body += f'<p class="retention-highlight"><strong>{half:,}秒</strong>で半分以上が離脱</p>'
    else:
        body += "<p>記録された範囲では半分以上が視聴を続けています。</p>"
    body += (
        "<p>0秒の視聴者数を100%として表示</p>"
        '<div class="retention-chart" role="img" aria-label="視聴を続けている人の割合" '
        'aria-describedby="retention-description">'
        f'<span class="sr-only" id="retention-description">{_e(description)}</span>'
        '<div class="retention-plot" aria-hidden="true">'
        '<svg viewBox="0 0 100 100" preserveAspectRatio="none" width="100%" height="100%">'
        '<path class="chart-axis" d="M0 0V100H100" vector-effect="non-scaling-stroke"/>'
        '<path class="chart-half" d="M0 50H100" vector-effect="non-scaling-stroke"/>'
        '<polyline class="chart-line" vector-effect="non-scaling-stroke" points="'
        + " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        + '"/></svg>'
        '<span class="chart-label chart-y chart-top">100%</span>'
        '<span class="chart-label chart-y chart-middle">50%</span>'
        '<span class="chart-label chart-y chart-bottom">0%</span>'
        '<span class="chart-label chart-x chart-start">0</span>'
        f'<span class="chart-label chart-x chart-end">{len(counts) - 1:,}</span>'
        '<span class="chart-label chart-x chart-seconds">秒</span>'
    )
    if half is not None:
        x, y = points[half]
        # The HTML renderer adds the CSP nonce to style elements; style attributes are blocked.
        body += (
            f"<style>#retention-marker{{left:{x:.2f}%;top:{y:.2f}%}}</style>"
            '<span class="chart-marker" id="retention-marker"></span>'
        )
    return body + "</div></div>"


def _insights(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return ""
    latest = rows[0]
    body = (
        '<section class="metric-group video-insights" aria-labelledby="insights-title">'
        '<h2 id="insights-title">視聴データ（TikTok 分析）</h2><dl class="stats">'
    )
    for field, label, spec, suffix in (
        ("reach", "リーチ", ",.0f", ""),
        ("completion_rate", "完了率", ".1%", ""),
        ("avg_watch_seconds", "平均視聴時間", ",.2f", "秒"),
        ("profile_views", "プロフィール閲覧", ",.0f", ""),
    ):
        value = latest[field]
        display = format(value, spec) + suffix if value is not None else "データなし"
        body += f"<div><dt>{_e(label)}</dt><dd>{_e(display)}</dd></div>"
    body += "</dl><h3>視聴を続けている人の割合</h3>"
    body += _retention_chart(latest["retention"])
    body += '<h3>男女比</h3><dl class="audience-ratios">'
    for field, label in (("male_ratio", "男性"), ("female_ratio", "女性")):
        ratio = latest[field]
        display = f"{ratio:.1%}" if ratio is not None else "データなし"
        body += f"<div><dt>{_e(label)}</dt><dd>{_e(display)}</dd>"
        if ratio is not None:
            body += (
                '<svg viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">'
                '<rect class="ratio-track" width="100" height="8"/>'
                f'<rect class="ratio-fill" width="{ratio * 100:.2f}" height="8"/></svg>'
            )
        body += "</div>"
    body += "</dl><h3>年齢の上位</h3>"
    ages = sorted(latest["age_bands"], key=lambda age: age["ratio"], reverse=True)[:3]
    if ages:
        body += (
            "<ol>"
            + "".join(f"<li>{_e(age['band'])}：{age['ratio']:.1%}</li>" for age in ages)
            + "</ol>"
        )
    else:
        body += "<p>データなし</p>"
    captured = latest["captured_at"].astimezone(JST).strftime("%Y/%m/%d %H:%M")
    body += f'<p class="muted">取得日時: {_e(captured)}</p>'
    if len(rows) > 1:
        previous = rows[1]
        day = previous["captured_at"].astimezone(JST).strftime("%Y/%m/%d")
        if latest["reach"] is not None and previous["reach"] is not None:
            delta = latest["reach"] - previous["reach"]
            body += f"<p>前回（{_e(day)}）から リーチ {delta:+,}</p>"
        else:
            body += f"<p>前回（{_e(day)}）からのリーチ差: データ不足</p>"
    return body + "</section>"


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
    for field in ("pure_organic", "general", "url_import", "external_case"):
        if params.get(field, "") not in {"", "yes"}:
            raise HTTPException(400, "絞り込み指定が不正です")
    if params.get("external_case") == "yes":
        clauses.append("v.is_external_case")
    if params.get("general") == "yes":
        clauses.append(_GENERAL_POST)
    if params.get("pure_organic") == "yes":
        clauses.append("""NOT v.is_pr AND EXISTS (SELECT 1 FROM vl_videos pv
            WHERE pv.platform=v.platform AND pv.video_id=v.video_id
            AND pv.page_is_ad=false AND pv.page_disclosure->'disclosures'='[]'::jsonb)""")
    if params.get("url_import") == "yes":
        clauses.append("""EXISTS (SELECT 1 FROM vl_videos pv
            WHERE pv.platform=v.platform AND pv.video_id=v.video_id
            AND 'url_manual'=ANY(pv.ingest_sources))""")
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


_GENERAL_POST = "(v.origin='external' AND NOT v.is_external_case)"
_PAGE_COLUMNS = (
    "page_is_ad",
    "page_disclosure",
    "page_observed_at",
    "page_fetch_status",
    "page_last_attempted_at",
    "page_fetch_error_code",
    "ingest_sources",
)
# Choose a whole snapshot by route; never coalesce individual sheet/public metrics.
_SHEET_METRICS = ", ".join(f"m0.{c}" for c in METRIC_COLUMNS)
_PUBLIC_METRICS = ", ".join(
    f"p0.{c}::double precision" if c.startswith("total_") else "NULL::double precision"
    for c in METRIC_COLUMNS
)
# Column names come only from the fixed metric mapping, never from request input.
_METRICS_JOIN = f"""LEFT JOIN vl_videos pv
    ON pv.platform=v.platform AND pv.video_id=v.video_id
    LEFT JOIN LATERAL (
    SELECT snapshot.* FROM (
        SELECT {_SHEET_METRICS},
            m0.captured_on, NULL::timestamptz AS captured_at, 'sheet' AS metric_source
        FROM vl_metrics m0 WHERE m0.platform=v.platform AND m0.video_id=v.video_id
        UNION ALL
        SELECT {_PUBLIC_METRICS}, p0.captured_on,p0.captured_at,'public_page' AS metric_source
        FROM vl_public_metrics p0 WHERE p0.platform=v.platform AND p0.video_id=v.video_id
        AND NOT ('sheet'=ANY(pv.ingest_sources))
    ) snapshot ORDER BY (metric_source='sheet') DESC, captured_on DESC LIMIT 1
) m ON true"""  # nosec B608
_METRIC_SELECT = ", ".join(f"m.{column}" for column in METRIC_COLUMNS) + (
    ", m.metric_source, m.captured_at, " + ", ".join(f"pv.{column}" for column in _PAGE_COLUMNS)
)
_CAMPAIGN_TOTALS_JOIN = """LEFT JOIN campaign_totals ct
    ON ct.advertiser=v.advertiser AND ct.campaign=v.campaign AND v.origin='own'"""


def _campaign_totals_cte(admin: bool) -> str:
    # Apply visibility before aggregation, but never the video search filters.
    where, _, _, _ = _where({}, admin)
    return f"""WITH campaign_totals AS (
        SELECT v.advertiser, v.campaign,
        CASE WHEN count(m.total_plays)=count(*) THEN sum(m.total_plays) END
            AS campaign_total_plays
        FROM vl_videos_effective v {_METRICS_JOIN} WHERE {where}
        AND v.origin='own' AND COALESCE(v.advertiser,'')<>'' AND COALESCE(v.campaign,'')<>''
        GROUP BY v.advertiser, v.campaign)"""  # nosec B608


def library_connection(pg: Any, email: str, *, admin: bool) -> Any:
    """Keep HTTP and MCP reads on the same application role and user context."""
    return pg.connection(
        app_role="teamagent_app",
        user_email=email,
        user_groups=[email.split("@")[-1]],
        user_role="admin" if admin else "user",
    )


def read_video(conn: Any, platform: str, video_id: str, *, admin: bool) -> Any:
    # Identifiers and visibility are always bound parameters, including for MCP.
    sql = f"SELECT v.*, {_METRIC_SELECT}, m.captured_on FROM vl_videos_effective v {_METRICS_JOIN} WHERE v.platform=%s AND v.video_id=%s AND (%s OR NOT v.is_private)"  # noqa: E501  # nosec B608
    row = conn.execute(sql, (platform, video_id, admin)).fetchone()
    if row is None:
        raise HTTPException(404)
    return row


def search_videos(
    conn: Any,
    params: Mapping[str, str],
    *,
    admin: bool,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    if type(page_size) is not int or not 1 <= page_size <= PAGE_SIZE:
        raise ValueError("Invalid page size")
    where, values, page, order = _where(params, admin)
    count = conn.execute(
        # 固定の句と許可リストの並び順だけを連結し、利用者の値は全て %s で渡す
        f"SELECT count(*) AS n FROM vl_videos_effective v WHERE {where}",  # nosec B608
        values,
    ).fetchone()["n"]
    # 同上: 列名・JOIN・句・並び順はコード内の固定値で、利用者の値は %s で渡す
    list_sql = f"{_campaign_totals_cte(admin)} SELECT v.*, {_METRIC_SELECT}, m.captured_on, ct.campaign_total_plays FROM vl_videos_effective v {_METRICS_JOIN} {_CAMPAIGN_TOTALS_JOIN} WHERE {where} ORDER BY {order} DESC NULLS LAST, v.platform, v.video_id LIMIT %s OFFSET %s"  # noqa: E501  # nosec B608
    rows = conn.execute(
        list_sql,
        [*values, page_size, (page - 1) * page_size],
    ).fetchall()
    for row in rows:
        row["rates"] = _rates(row)
        row["post_url"] = post_url(row)
        row["has_thumbnail"] = bool(row.pop("thumbnail_s3_key", None))
        row.pop("video_s3_key", None)
    return {"videos": rows, "total": count, "page": page, "page_size": page_size}


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
    email: str,
    admin: bool = False,
    current: str = "cases",
    params: Mapping[str, str] | None = None,
    hero: bool = False,
    workspace: bool = False,
) -> str:
    params = params or {}
    links = [
        ("home", "/library", "おすすめ"),
        ("cases", "/library/cases", "事例一覧"),
        ("videos", "/library/videos", "動画一覧"),
        ("import", "/library/import", "URL取り込み"),
    ]
    nav = (
        '<nav class="main-nav" aria-label="ライブラリの表示切替">'
        + "".join(
            f'<a href="{url}"'
            + (' aria-current="page"' if key == current else "")
            + f">{label}</a>"
            for key, url, label in links
        )
        + "</nav>"
    )
    brand = "<span>動画ライブラリ<small>VIDEO ARCHIVE</small></span>"
    soon = (
        '<button class="soon" type="button" disabled>構成（型）から探す '
        "<span>準備中</span></button>"
    )
    admin_link = (
        '<a class="library-admin" href="/library/admin/campaigns"'
        + (' aria-current="page"' if current == "admin" else "")
        + ">管理 ↗</a>"
        if admin
        else ""
    )
    if workspace:
        body = _workspace(body, params, current=current, nav=nav)
    else:
        body = nav + ("" if hero else f'<div class="page-title"><h1>{_e(title)}</h1></div>') + body
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)} | 動画ライブラリ</title>"
        f"<style>{_STYLE}{PORTAL_STYLE}</style></head><body>"
        + render_header(email, current_path="/library")
        + '<header class="library-header"><div class="header-inner">'
        '<a class="brand" href="/library">'
        + brand
        + "</a>"
        + '<div class="library-utility">'
        + soon
        + admin_link
        + "</div></div></header>"
        + "<main>"
        + body
        + '</main><footer><a class="brand" href="/library">'
        + brand
        + "</a>"
        + _category_nav(params.get("category", ""), current=current, footer=True)
        + "<p>シート数値は毎日 18 時に更新・公開数値は取得時点<span>社内限定</span></p></footer>"
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
    # aria-label overrides any wrapping <label>, so every select needs its own accurate name.
    label = {
        "category": "区分",
        "pr": "PRの有無",
        "sort": "並び順",
        "requested_origin": "投稿の区分",
        "case": "広告主・案件（任意）",
    }.get(name, "種別")
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
    filters = ""
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
        + f"<p>{data['total']:,}件 · {data['page']:,}ページ目</p>"
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


def _inference_badge(row: Mapping[str, Any]) -> str:
    return (
        '<small class="badge" title="広告主・案件をAIが推定">AI推定</small>'
        if row.get("case_inferred")
        else ""
    )


def _page_evidence(row: Mapping[str, Any]) -> str:
    disclosure = row.get("page_disclosure") or {}
    body = '<section class="page-evidence"><h3>#PR・広告の根拠</h3>'
    body += "<p>#PR: " + ("あり（投稿文の #PR）" if row.get("is_pr") else "なし") + "</p>"
    body += "<p>公開ページ観測日時: " + _e(row.get("page_observed_at") or "未取得") + "</p>"
    ad = row.get("page_is_ad")
    body += (
        "<p>広告配信: " + ("あり" if ad is True else "なし" if ad is False else "未確認") + "</p>"
    )
    if disclosure.get("caption") is not None:
        body += "<p>取得した投稿文: " + _e(disclosure["caption"]) + "</p>"
    if disclosure.get("disclosures"):
        body += "<p>補助の広告表記: " + _e(" / ".join(disclosure["disclosures"])) + "</p>"
    if row.get("page_fetch_status") == "failed":
        body += "<p>直近の取得に失敗しました。数値は最終正常取得時の値です。</p>"
    return body + "</section>"


def _post_badges(row: Mapping[str, Any]) -> str:
    labels = []
    if row.get("origin") == "external":
        labels.append("他社事例" if row.get("is_external_case") else "一般投稿")
    if row.get("is_pr"):
        labels.append("#PR")
        if row.get("origin") != "external" and (
            not row.get("advertiser") or not row.get("campaign")
        ):
            labels.append("案件未設定")
    if row.get("page_is_ad") is True:
        labels.append("広告配信あり")
    return "".join(f'<small class="badge">{label}</small>' for label in labels)


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
    has_thumbnail = bool(row.get("has_thumbnail") or row.get("thumbnail_s3_key"))
    # Under a loaded thumbnail the placeholder is only decoration; keep it out of screen readers.
    hidden = ' aria-hidden="true"' if has_thumbnail else ""
    body = (
        f'<span class="placeholder"{hidden} '
        f'data-category="{_e(row.get("business_category"))}"><strong>'
        f"{_e(account[0].upper())}</strong>"
        f'<span class="placeholder-account" title="{_e(account)}">{_e(account)}</span>'
        "<small>サムネ準備中</small></span>"
    )
    if has_thumbnail:
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


def _card(
    row: Mapping[str, Any],
    *,
    href: str,
    label: str,
    preview: str,
    context: str,
    context_title: str,
    title: str,
    meta: str,
    metrics: list[tuple[str, float | None]],
    selection: str = "",
) -> str:
    plays = []
    for i, (name, value) in enumerate(metrics):
        tag = "strong" if i == 0 else "span"
        plays.append(
            f'<{tag} class="meta-plays" title="{_e(name)}の再生数: {_metric(value)}">'
            f"{_e(name)} {compact_plays(value)}</{tag}>"
        )
    tags = "".join(
        f'<span class="card-tag"><span class="filter-label">{name}</span>'
        f'<span class="badge">{_e(row[field])}</span></span>'
        for field, name in (("industry", "業種"), ("purpose", "目的"))
        if row.get(field)
    )
    return (
        f'<article class="card"><a class="card-link" href="{_e(href)}" '
        f'aria-label="{_e(label)}">{preview}<div class="card-body">'
        f'<p class="card-context" title="{_e(context_title)}">{context}{_inference_badge(row)}</p>'
        f'<h3 title="{_e(title)}">{_e(title)}</h3><p class="card-meta">'
        f'<span class="meta-source">{meta}</span></p><p class="card-plays">{"".join(plays)}</p>'
        f'<div class="card-tags">{_post_badges(row)}{tags}</div></div></a>{selection}</article>'
    )


def video_title(row: Mapping[str, Any]) -> str:
    return next(
        (line.strip() for line in str(row.get("caption") or "").splitlines() if line.strip()),
        str(row.get("account_name") or row.get("account_handle") or "動画"),
    )


def _video_card(row: Mapping[str, Any], *, selectable: bool = False) -> str:
    path = "/".join(quote(str(row[key]), safe="") for key in ("platform", "video_id"))
    url = post_url(row)
    account = str(row.get("account_name") or row.get("account_handle") or "動画")
    title = video_title(row)
    flags = " · ".join(
        name for field, name in (("is_pr", "PR"), ("is_private", "非公開")) if row.get(field)
    )
    context = " · ".join(
        part
        for part in (
            _category_label(row.get("business_category")),
            _e(flags),
            _e(row.get("campaign")),
        )
        if part
    )
    meta = f'<span class="account-name" title="{_e(account)}">{_e(account)}</span>'
    if isinstance(row.get("posted_at"), datetime):
        meta += " · " + _day(row["posted_at"])
    if row.get("external_advertiser"):
        meta += " · 他社の広告主: " + _e(row["external_advertiser"])
    metrics = [("この動画", row.get("total_plays"))]
    if (
        row.get("origin") != "external"
        and row.get("advertiser")
        and row.get("campaign")
        and row.get("campaign_total_plays") is not None
    ):
        metrics.append(("施策計", row["campaign_total_plays"]))
    selection = (
        f'<label class="card-select"><input type="checkbox" data-url="{_e(url)}" '
        f'aria-label="{_e(account)}: {_e(title)}の動画を選択">選択</label>'
        if selectable and url
        else ""
    )
    return _card(
        row,
        href=f"/library/v/{path}",
        label=f"{account}: {title}の動画詳細",
        preview=_preview(row),
        context=context,
        context_title=str(row.get("campaign") or ""),
        title=title,
        meta=meta,
        metrics=metrics,
        selection=selection,
    )


def _workspace(body: str, params: Mapping[str, str], *, current: str, nav: str) -> str:
    _, is_pr = parse_search(params)
    params = {**params, "pr": "" if is_pr is None else "yes" if is_pr else "no"}
    catalog = current == "cases"
    base = "/library/cases" if catalog else "/library/videos"
    sorts = (
        [("plays", "施策の再生数順"), ("new", "新着順"), ("count", "動画の本数順")]
        if catalog
        else [("new", "新着順"), ("plays", "再生数順"), ("saves", "保存率順"), ("eng", "ENG率順")]
    )
    sidebar = (
        '<details class="sidebar" open><summary>条件を指定</summary>'
        '<fieldset><legend>区分</legend><div class="category-options">'
    )
    for code, label in [("", "すべて"), *CATEGORY_LABELS.items(), ("unset", "未設定")]:
        checked = " checked" if code == params.get("category", "") else ""
        sidebar += (
            '<label class="category-option"><input type="radio" name="category" '
            f'value="{code}"{checked}>'
            f"<span>{label}</span></label>"
        )
    sidebar += '</div></fieldset><div class="sidebar-fields">'
    for field, label, kind in (
        ("industry", "業種", "text"),
        ("purpose", "目的", "text"),
        ("account", "アカウント", "text"),
        ("from", "投稿日（開始）", "date"),
        ("to", "投稿日（終了）", "date"),
    ):
        sidebar += (
            f'<label>{label}<input type="{kind}" name="{field}" '
            f'value="{_e(params.get(field, ""))}"></label>'
        )
    sidebar += (
        "<label>PRの有無"
        + _select("pr", [("", "すべて"), ("yes", "PRあり"), ("no", "PRなし")], params.get("pr"))
        + "</label></div>"
    )
    if not catalog:
        for field, label in (
            ("pure_organic", "純オーガニックのみ"),
            ("general", "一般投稿"),
            ("external_case", "他社事例"),
            ("url_import", "URL 取り込み"),
        ):
            checked = " checked" if params.get(field) == "yes" else ""
            sidebar += (
                f'<label><input type="checkbox" name="{field}" value="yes"{checked}>{label}</label>'
            )
    for key in ("advertiser", "campaign", "unassigned", "tag_group", "tag"):
        if params.get(key):
            sidebar += f'<input type="hidden" name="{key}" value="{_e(params[key])}">'
    sidebar += (
        '<button type="submit" class="primary apply-filters">条件を適用</button>'
        f'<a class="reset-filters" href="{base}">条件をリセット</a>'
        '<p class="sidebar-note">企業名だけでなく、業種・目的・区分を組み合わせて探せます。'
        "<br>構成（型）から探す：準備中</p></details>"
    )
    labels = {
        "q": "検索語",
        "category": "区分",
        "industry": "業種",
        "purpose": "目的",
        "account": "アカウント",
        "from": "開始",
        "to": "終了",
        "advertiser": "広告主",
        "campaign": "施策",
        "tag": "タグ",
        "unassigned": "施策未設定",
        "pure_organic": "純オーガニックのみ",
        "general": "一般投稿",
        "external_case": "他社事例",
        "url_import": "URL 取り込み",
    }
    active = []
    for key, label in labels.items():
        if value := params.get(key):
            if key == "category":
                value = CATEGORY_LABELS.get(value, "未設定" if value == "unset" else value)
            if key == "unassigned":
                value = "のみ" if value == "yes" else value
            active.append(f"{label}: {value}")
    if is_pr is not None:
        active.append("PR投稿のみ" if is_pr else "PR以外")
    if params.get("sort"):
        active.append("並び順: " + dict(sorts).get(params["sort"], params["sort"]))
    heading = (
        "条件に合う事例を探す" if current == "home" else "事例を探す" if catalog else "動画を探す"
    )
    return (
        f'<form method="get" action="{base}" class="filters workspace" role="search">'
        + sidebar
        + '<div class="workspace-main"><p class="eyebrow">YOUR REFERENCE DESK</p>'
        f'<h1>{heading}</h1><div class="workspace-search"><label>'
        '<span class="sr-only">企業名・キーワード・#PRで検索</span>'
        f'<input type="search" name="q" value="{_e(params.get("q", ""))}" '
        'placeholder="企業名・キーワード・#PR で検索"></label>'
        '<button type="submit" class="primary">検索</button></div>'
        '<p class="search-hint">検索例：企業名 #PR · 複数の語はすべて含むものを検索</p>'
        '<p class="active-filters" role="status">'
        + ("絞り込み中: " + _e(" / ".join(active)) if active else "絞り込みなし · すべての区分")
        + '</p><div class="workspace-toolbar">'
        + nav
        + "<label>並び順 "
        + _select("sort", sorts, params.get("sort", "plays" if catalog else "new"))
        + "</label></div>"
        + body
        + "</div></form>"
    )


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
        AND v.origin='own' AND COALESCE(v.advertiser,'')<>'' AND COALESCE(v.campaign,'')<>'')
        SELECT v.advertiser, v.campaign, count(*) AS video_count,
        min(v.posted_at) AS first_posted, max(v.posted_at) AS last_posted,
        array_agg(DISTINCT v.business_category) AS categories,
        (array_agg(v.business_category ORDER BY v.category_count DESC,
            CASE v.business_category WHEN 'media' THEN 0 WHEN 'tto' THEN 1
                WHEN 'kirinuki' THEN 2 WHEN 'other' THEN 3 WHEN 'organic' THEN 4
                ELSE 5 END))[1] AS primary_category,
        bool_or(v.case_inferred) AS case_inferred,
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
    if params.get("general") != "yes":
        where += " AND NOT " + _GENERAL_POST
    # Rank each campaign/account before the bound, so a posting burst cannot fill the pool.
    sql = f"""{_campaign_totals_cte(admin)}, ranked AS (
        SELECT v.*, {_METRIC_SELECT}, ct.campaign_total_plays,
        row_number() OVER (PARTITION BY COALESCE(NULLIF(v.advertiser,''),v.platform),
            COALESCE(NULLIF(v.campaign,''),v.video_id)
            ORDER BY m.total_plays DESC NULLS LAST, v.video_id) AS campaign_rank,
        row_number() OVER (PARTITION BY v.platform,
            COALESCE(v.account_handle,v.account_name,v.video_id)
            ORDER BY m.total_plays DESC NULLS LAST, v.video_id) AS account_rank
        FROM vl_videos_effective v {_METRICS_JOIN} {_CAMPAIGN_TOTALS_JOIN} WHERE {where})
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
    meta = " · ".join(value for value in (period, f"{row['video_count']:,}本の動画") if value)
    context = " · ".join(
        [label for code in row["categories"] if (label := _category_label(code))]
        + [_e(row["advertiser"])]
    )
    return _card(
        row,
        href=_campaign_url(row),
        label=f"{row['advertiser']}: {row['campaign']}の事例",
        preview='<span class="preview">' + _cover(row["representative"]) + "</span>",
        context=context,
        context_title=str(row["advertiser"]),
        title=str(row["campaign"]),
        meta=_e(meta),
        metrics=[("施策計", row.get("total_plays"))],
    )


def recent_imports(conn: Any, *, admin: bool) -> list[Any]:
    where, values, _, _ = _where({"url_import": "yes"}, admin)
    sql = f"""SELECT v.*, {_METRIC_SELECT} FROM vl_videos_effective v {_METRICS_JOIN}
        JOIN LATERAL (
            SELECT max(r.completed_at) AS imported_at FROM vl_url_import_rows r
            JOIN vl_url_import_batches b USING(batch_id)
            WHERE b.source='url_manual' AND v.platform='tiktok' AND r.video_id=v.video_id
            AND r.status IN ('succeeded','partial')
        ) recent ON recent.imported_at IS NOT NULL
        WHERE {where} ORDER BY recent.imported_at DESC,v.platform,v.video_id LIMIT 12"""  # nosec B608
    return list(conn.execute(sql, values).fetchall())


def archive_home(
    cases: list[Any],
    videos: list[Any],
    general: list[Any] | None = None,
    imported: list[Any] | None = None,
) -> str:
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
    body = ""
    if imported:
        body += '<section class="archive-section" id="recent-imports">' + _section_heading(
            "RECENT IMPORTS",
            "最近取り込んだ動画",
            "全員のURL取り込みから。",
            "/library/videos?url_import=yes",
        )
        body += (
            '<div class="video-grid video-shelf">'
            + "".join(_video_card(r) for r in imported[:12])
            + "</div></section>"
        )
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
            "grid",
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
    if general:
        body += '<section class="archive-section" id="general">' + _section_heading(
            "GENERAL POSTS",
            "一般投稿",
            "外部の一般投稿を、社内の参考資料に。",
            "/library/videos?general=yes",
        )
        body += '<div class="video-grid video-shelf">' + "".join(_video_card(r) for r in general)
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
        body += '<div class="grid campaign-grid">' + "".join(_campaign_card(r) for r in rows)
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
        return library_connection(get_pg(), email, admin=is_admin(email))

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
        workspace: bool = False,
    ) -> HTMLResponse:
        response = render(
            _page(
                title,
                body,
                email=email,
                admin=is_admin(email),
                current=current,
                params=params,
                hero=hero,
                workspace=workspace,
            )
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
            general = featured_videos(conn, {"general": "yes"}, admin=is_admin(email))
            imported = recent_imports(conn, admin=is_admin(email))
        return page(
            "動画の事例を、次のアイデアに。",
            archive_home(cases, videos, general, imported),
            email,
            "home",
            hero=True,
            workspace=True,
        )

    @router.get("/cases")
    def case_listing(request: Request, email: str = Depends(identity)) -> HTMLResponse:
        params = request.query_params
        with connection(email) as conn:
            cases = search_campaigns(conn, params, admin=is_admin(email))
        _, _, number, _ = _where({**params, "sort": "plays"}, is_admin(email))
        size = 12
        visible = cases[(number - 1) * size : number * size]
        body = f'<p class="result-count">{len(cases):,}件の事例 · {number:,}ページ目</p>'
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
        return page("事例一覧", body, email, params=params, workspace=True)

    @router.get("/c")
    def campaign_detail(request: Request, email: str = Depends(identity)) -> HTMLResponse:
        params = {key: request.query_params.get(key, "") for key in ("advertiser", "campaign")}
        if not all(params.values()):
            raise HTTPException(400, "広告主と案件を指定してください")
        where, values, number, _ = _where(
            {**params, "page": request.query_params.get("page", "1")}, is_admin(email)
        )
        where += " AND v.origin='own'"
        with connection(email) as conn:
            cases = search_campaigns(conn, params, admin=is_admin(email))
            if not cases:
                raise HTTPException(404)
            # Keep the entire case for one-click copy; only the visible grid is paginated.
            sql = f"{_campaign_totals_cte(is_admin(email))} SELECT v.*, {_METRIC_SELECT}, ct.campaign_total_plays FROM vl_videos_effective v {_METRICS_JOIN} {_CAMPAIGN_TOTALS_JOIN} WHERE {where} ORDER BY m.total_plays DESC NULLS LAST, v.platform, v.video_id"  # noqa: E501  # nosec B608
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
            [_video_card(row) for row in videos[(number - 1) * PAGE_SIZE : number * PAGE_SIZE]],
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
            workspace=True,
        )

    @router.get("/api/videos")
    def api(request: Request, email: str = Depends(identity)) -> JSONResponse:
        with connection(email) as conn:
            data = search_videos(conn, request.query_params, admin=is_admin(email))
        for row in data["videos"]:
            row.pop("campaign_total_plays", None)
        return JSONResponse(jsonable_encoder(data))

    def video(conn: Any, platform: str, video_id: str, email: str) -> Any:
        return read_video(conn, platform, video_id, admin=is_admin(email))

    @router.get("/v/{platform}/{video_id}")
    def detail(platform: str, video_id: str, email: str = Depends(identity)) -> HTMLResponse:
        with connection(email) as conn:
            row = video(conn, platform, video_id, email)
            insights = conn.execute(
                "SELECT captured_at, reach, completion_rate, avg_watch_seconds, profile_views, "
                "male_ratio, female_ratio, age_bands, retention FROM vl_video_insights "
                "WHERE platform=%s AND video_id=%s ORDER BY captured_at DESC LIMIT 2",
                (platform, video_id),
            ).fetchall()
            analyses = conn.execute(
                "SELECT kind, version, payload FROM vl_video_analysis "
                "WHERE platform=%s AND video_id=%s ORDER BY analyzed_at DESC",
                (platform, video_id),
            ).fetchall()
            related = []
            if row.get("origin") != "external" and row.get("advertiser") and row.get("campaign"):
                where, values, _, _ = _where(
                    {"advertiser": row["advertiser"], "campaign": row["campaign"]}, is_admin(email)
                )
                where += " AND v.origin='own'"
                related_sql = f"{_campaign_totals_cte(is_admin(email))} SELECT v.*, {_METRIC_SELECT}, ct.campaign_total_plays FROM vl_videos_effective v {_METRICS_JOIN} {_CAMPAIGN_TOTALS_JOIN} WHERE {where} AND NOT (v.platform=%s AND v.video_id=%s) ORDER BY m.total_plays DESC NULLS LAST, v.platform, v.video_id LIMIT 6"  # noqa: E501  # nosec B608
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
        body += _post_badges(row) + _inference_badge(row) + _row_tags(row)
        body += _page_evidence(row)
        own = row.get("origin") == "own"
        body += (
            f'<form method="post" action="/library/v/{_e(platform)}/{_e(video_id)}/origin">'
            f'<input type="hidden" name="origin" value="{"external" if own else "own"}">'
            f'<input type="hidden" name="decision_id" value="{uuid.uuid4()}">'
            "<button>" + ("他社事例にする" if own else "自社の案件にする") + "</button></form>"
            '<p class="muted">シート掲載・自社アカウントの動画は自社が優先されます。'
            "変更は記録されます。</p>"
        )
        if row.get("origin") == "external":
            body += (
                f'<form method="post" action="/library/v/{_e(platform)}/{_e(video_id)}/advertiser">'
                '<label>他社の広告主<input name="advertiser" maxlength="500" '
                f'value="{_e(row.get("external_advertiser"))}"></label><button>保存</button></form>'
            )
        if str(row.get("caption") or "").strip():
            body += "<h3>投稿文</h3><pre>" + _e(row["caption"]) + "</pre>"
        if row.get("origin") != "external" and row.get("advertiser") and row.get("campaign"):
            body += (
                f'<p><a href="{_e(_campaign_url(row))}">{_e(row["advertiser"])} / '
                f"{_e(row['campaign'])} の事例を見る</a></p>"
            )
        elif row.get("external_advertiser"):
            body += f"<p>他社の広告主: {_e(row['external_advertiser'])}</p>"
        elif row.get("advertiser"):
            body += f"<p>広告主: {_e(row['advertiser'])}</p>"
        body += (
            '</div></div><div><p class="muted">数値取得日: '
            + _e(row.get("captured_on") or "不明")
            + '</p><div class="metric-groups">'
        )
        public = row.get("metric_source") == "public_page"
        if public:
            body += (
                "<p>公開ページの数値（取得日時: " + _e(row.get("captured_at") or "不明") + "）</p>"
            )
        for family, label in (
            ("organic", "オーガニック"),
            ("total", "公開ページの数値" if public else "総計"),
            ("ad", "広告"),
        ):
            if public and family != "total":
                continue
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
        body += "</div>" + _insights(insights) + "</div></div>"
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
        return Response(
            body, media_type=image_media_type(body[:12]) or "image/jpeg", headers=headers
        )

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

    async def form_data(request: Request, *, import_form: bool = False) -> dict[str, str]:
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
            if len(raw) > (1024 * 1024 if import_form else 8192):
                raise HTTPException(413)
        from urllib.parse import parse_qs

        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=12)
        except (UnicodeError, ValueError):
            raise HTTPException(400) from None
        if any(len(values) != 1 for values in parsed.values()):
            raise HTTPException(400)
        if import_form and set(parsed) - {
            "accounts",
            "operator_label",
            "urls",
            "case",
            "action",
            "idempotency_key",
            "advertiser",
            "campaign",
            "requested_origin",
        }:
            raise HTTPException(400)
        return {key: values[0] for key, values in parsed.items()}

    @router.post("/v/{platform}/{video_id}/origin")
    async def change_origin(
        request: Request, platform: str, video_id: str, email: str = Depends(identity)
    ) -> RedirectResponse:
        form = await form_data(request)
        if form.get("origin") not in {"own", "external"}:
            raise HTTPException(400)
        try:
            decision_id = uuid.UUID(form.get("decision_id", ""))
        except ValueError:
            raise HTTPException(400) from None
        with connection(email) as conn:
            video(conn, platform, video_id, email)
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,41))",
                (f"{platform}:{video_id}",),
            )
            conn.execute(
                """INSERT INTO vl_video_origin_decisions
                   (decision_id,platform,video_id,origin,decided_by) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (decision_id, platform, video_id, form["origin"], email),
            )
        return RedirectResponse(f"/library/v/{platform}/{video_id}", 303)

    @router.post("/v/{platform}/{video_id}/advertiser")
    async def change_external_advertiser(
        request: Request, platform: str, video_id: str, email: str = Depends(identity)
    ) -> RedirectResponse:
        form = await form_data(request)
        advertiser = form.get("advertiser", "").strip()
        if len(advertiser) > 500 or any(ord(c) < 32 for c in advertiser):
            raise HTTPException(400)
        with connection(email) as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,41))",
                (f"{platform}:{video_id}",),
            )
            row = video(conn, platform, video_id, email)
            if row["origin"] != "external":
                raise HTTPException(409)
            conn.execute(
                """INSERT INTO vl_video_campaign_links
                   (platform,video_id,advertiser,campaign,source,status,match_type,confidence,
                    model,prompt_version,input_hash,decided_by)
                   VALUES (%s,%s,%s,NULL,'human',%s,'unknown',1,'human','external-v1','human',%s)
                   ON CONFLICT (platform,video_id,source) DO UPDATE SET
                   advertiser=EXCLUDED.advertiser,campaign=NULL,status=EXCLUDED.status,
                   match_type='unknown',confidence=1,evidence='[]',model='human',
                   prompt_version='external-v1',input_hash='human',
                   decided_by=EXCLUDED.decided_by,decided_at=clock_timestamp()""",
                (
                    platform,
                    video_id,
                    advertiser or None,
                    "applied" if advertiser else "rejected",
                    email,
                ),
            )
        return RedirectResponse(f"/library/v/{platform}/{video_id}", 303)

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

    from teamagent.connect_web.library_import import register_import_routes

    register_import_routes(
        router,
        identity=identity,
        connection=connection,
        get_pg=get_pg,
        page=page,
        form_data=form_data,
        is_admin=is_admin,
    )
    app.include_router(router)
