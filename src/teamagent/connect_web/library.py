"""Internal short-video archive. Video reads always use vl_videos_effective."""

from __future__ import annotations

import html
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
            v.account_name, v.account_handle, v.advertiser, v.campaign) ILIKE %s""")
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


_STYLE = """
body{margin:0;background:#0f1420;color:#e8edf7;font-family:system-ui,sans-serif}
main{max-width:1200px;margin:auto;padding:24px}a{color:#8cb5ff}nav,form{margin-bottom:20px}
input,select,button,textarea{font:inherit;background:#1a2233;color:inherit;border:1px solid #46536d;
padding:8px;border-radius:5px;margin:4px}button{cursor:pointer}label{display:inline-block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
article,section{background:#1a2233;border:1px solid #283450;padding:12px;border-radius:10px}
.preview{display:block;position:relative;aspect-ratio:9/16;background:#394252}
.preview img,.preview iframe{position:absolute;inset:0;width:100%;height:100%;
object-fit:cover;border:0}
.preview iframe{pointer-events:none}p{overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:8px;border-bottom:1px solid #46536d}pre{white-space:pre-wrap}
"""
_SCRIPT = """
document.querySelectorAll('.preview img').forEach(img=>{
 img.addEventListener('error',()=>img.remove());
 if(img.complete&&img.naturalWidth===0)img.remove();
});
let active=null;
function clearPlayer(){if(active){active.remove();active=null;}}
document.querySelectorAll('.preview[data-player]').forEach(card=>{
 card.addEventListener('mouseenter',()=>{clearPlayer();active=document.createElement('iframe');
 active.src=card.dataset.player;active.title='TikTok プレビュー';
 active.allow='autoplay; encrypted-media';card.appendChild(active);});
 card.addEventListener('mouseleave',clearPlayer);
});
window.addEventListener('blur',clearPlayer);
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearPlayer();});
const copy=document.getElementById('copy');
if(copy)copy.addEventListener('click',async()=>{
 const urls=[...document.querySelectorAll('input[data-url]:checked')].map(el=>el.dataset.url);
 try{await navigator.clipboard.writeText(urls.join('\\n'));
 document.getElementById('copy-status').textContent=urls.length+'件コピーしました';}
 catch(e){document.getElementById('copy-status').textContent='コピーできませんでした';}
});
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)}</title><style>{_STYLE}</style></head><body><main>"
        '<nav><a href="/search">資料検索</a> · <a href="/library">動画ライブラリ</a> · '
        '<a href="/library/admin/campaigns">案件設定</a> · '
        '<a href="/library/admin/accounts">アカウント登録</a></nav>'
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
    filters = (
        '<form method="get"><label>検索 <input name="q" value="'
        + _e(params.get("q", ""))
        + '"></label>'
    )
    filters += _select(
        "category",
        [
            ("", "全区分"),
            ("unset", "未設定"),
            *[(r["code"], r["display_name"]) for r in categories],
        ],
        params.get("category", ""),
    )
    filters += _select(
        "pr", [("", "PRすべて"), ("yes", "PRあり"), ("no", "PRなし")], params.get("pr", "")
    )
    filters += "".join(
        f'<label>{label} <input type="{kind}" name="{name}" '
        f'value="{_e(params.get(name, ""))}"></label>'
        for name, label, kind in (
            ("from", "開始", "date"),
            ("to", "終了", "date"),
            ("account", "アカウント", "text"),
        )
    )
    filters += _select(
        "sort",
        [("new", "新しい順"), ("plays", "再生順"), ("saves", "保存率順"), ("eng", "ENG率順")],
        params.get("sort", "new"),
    )
    filters += (
        "<button>絞り込み</button></form>"
        "<p>再生・保存率・ENG率と並び順は総計の最新数値です。"
        "オーガニック再生は値がある場合に併記します。</p>"
    )
    cards = []
    labels = {r["code"]: r["display_name"] for r in categories}
    for row in data["videos"]:
        platform, vid = row["platform"], row["video_id"]
        path = f"{quote(platform, safe='')}/{quote(vid, safe='')}"
        url = row["post_url"]
        player = (
            f' data-player="https://www.tiktok.com/player/v1/{_e(vid)}?autoplay=1&amp;muted=1&amp;controls=0&amp;loop=1"'
            if platform == "tiktok" and vid.isdigit() and url
            else ""
        )
        cover = (
            f'<img loading="lazy" src="/library/thumb/{path}" alt="">'
            if row["has_thumbnail"]
            else ""
        )
        preview = (
            f'<a class="preview" href="{_e(url)}" target="_blank" '
            f'rel="noopener noreferrer" aria-label="投稿を開く"{player}>{cover}</a>'
            if url
            else f'<div class="preview">{cover}</div>'
        )
        rates = row["rates"]
        checkbox = f'<label><input type="checkbox" data-url="{_e(url)}">選択</label>' if url else ""
        account = _e(row.get("account_name") or row.get("account_handle"))
        plays = _metric(row.get("total_plays"))
        if row.get("organic_plays") is not None:
            plays += f"（オーガニック {_metric(row['organic_plays'])}）"
        category = _e(labels.get(row.get("business_category"), "未設定"))
        flags = ("PR " if row["is_pr"] else "") + ("非公開" if row["is_private"] else "")
        cards.append(
            f"<article>{preview}{checkbox}<p>{account} · {_day(row.get('posted_at'))}</p>"
            f"<p>再生 {plays} · "
            f"保存率 {_rate(rates['save_rate'], rates['is_reference'])}</p>"
            f"<p>{category} {flags}</p>"
            f'<a href="/library/v/{path}">詳細</a></article>'
        )
    pagination = []
    for page, label in ((data["page"] - 1, "前へ"), (data["page"] + 1, "次へ")):
        if page >= 1 and (page - 1) * PAGE_SIZE < data["total"]:
            pagination.append(
                f'<a href="/library?{_e(urlencode({**params, "page": page}))}">{label}</a>'
            )
    return (
        filters
        + f"<p>{data['total']}件 · {data['page']}ページ</p>"
        + '<button id="copy" type="button">選択したURLをコピー</button>'
        + '<p id="copy-status" role="status"></p><div class="grid">'
        + "".join(cards)
        + "</div><p>"
        + " · ".join(pagination)
        + "</p>"
    )


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

    def page(title: str, body: str) -> HTMLResponse:
        response = render(_page(title, body))
        response.headers["Content-Security-Policy"] = response.headers[
            "Content-Security-Policy"
        ].replace(
            "frame-src https://accounts.google.com",
            "frame-src https://accounts.google.com https://www.tiktok.com",
        )
        return response

    @router.get("")
    def listing(request: Request, email: str = Depends(identity)) -> Response:
        with connection(email) as conn:
            data = search_videos(conn, request.query_params, admin=is_admin(email))
            categories = conn.execute(
                "SELECT * FROM vl_business_categories ORDER BY sort_order"
            ).fetchall()
        return page("動画ライブラリ", _listing(data, request.query_params, categories))

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
        body = (
            f"<p>{_e(row.get('account_name'))} · {_day(row.get('posted_at'))}</p>"
            f"<pre>{_e(row.get('caption'))}</pre><p>広告主 {_e(row.get('advertiser'))} · "
            f"案件 {_e(row.get('campaign'))} · "
            f"区分 {_e(row.get('business_category') or '未設定')}</p>"
        )
        url = post_url(row)
        if url:
            body += (
                f'<p><a href="{_e(url)}" target="_blank" '
                'rel="noopener noreferrer">投稿を開く</a></p>'
            )
        body += f"<p>数値取得日: {_e(row.get('captured_on') or '不明')}</p>"
        for family, label in (("organic", "オーガニック"), ("total", "総計"), ("ad", "広告")):
            body += f"<h2>{label}</h2><table>"
            for column in METRIC_COLUMNS:
                if column.startswith(family + "_"):
                    value = row.get(column)
                    metric_label = {
                        "ad_likes": "有料いいね",
                        "ad_shares": "有料シェア",
                        "ad_comments": "有料コメント",
                        "ad_saves": "有料保存",
                    }.get(column, METRIC_COLUMNS[column])
                    body += f"<tr><th>{_e(metric_label)}</th><td>{_metric(value, column)}</td></tr>"
            body += "</table>"
            if family != "ad":
                rates = _rates(row, family)
                body += (
                    f"<p>保存率 {_rate(rates['save_rate'], rates['is_reference'])} · "
                    f"ENG率 {_rate(rates['engagement_rate'], rates['is_reference'])}</p>"
                )
        body += (
            "<section><h2>構成</h2>"
            + (
                "".join(f"<pre>{_e(a['payload'])}</pre>" for a in analyses)
                if analyses
                else "<p>近日公開</p>"
            )
            + "</section>"
        )
        return page("動画詳細", body)

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
        return page("案件設定", body)

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
            '<p>登録のみ。自動取得は近日公開です。</p><form method="post">'
            '<label>TikTok URL / ハンドル<input name="handle" required maxlength="300"></label>'
            '<label>表示名<input name="display_name" maxlength="100"></label>'
            '<label>メモ<input name="note" maxlength="1000"></label><button>登録</button></form>'
            "<table><tr><th>アカウント</th><th>表示名</th><th>メモ</th></tr>"
        )
        body += (
            "".join(
                f"<tr><td>{_e(r['platform'])} / @{_e(r['handle'])}</td>"
                f"<td>{_e(r['display_name'])}</td><td>{_e(r['note'])}</td></tr>"
                for r in rows
            )
            + "</table>"
        )
        return page("アカウント登録", body)

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
