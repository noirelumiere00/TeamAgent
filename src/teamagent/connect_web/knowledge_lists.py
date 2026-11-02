"""検索シェル内の RLS 一覧用 query 検証と HTML 描画。"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

PAGE_SIZE = 50
MAX_PAGE = 10_000
VIEWS = ("clients", "docs")


def search_list_target(raw: str) -> str | None:
    """既知の一覧 GET query だけを検証・再構築する。前方一致では許可しない。"""
    if len(raw) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in raw) or "\\" in raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or parts.path != "/search" or parts.fragment:
        return None
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    values = dict(pairs)
    if len(values) != len(pairs) or set(values) - {"view", "page"}:
        return None
    view, page = values.get("view"), values.get("page", "1")
    if view not in VIEWS or not re.fullmatch(r"[1-9][0-9]{0,4}", page):
        return None
    if int(page) > MAX_PAGE:
        return None
    return "/search?" + urlencode({"view": view, "page": int(page)})


def list_tabs(view: str) -> str:
    out = ""
    for key, label, url in (
        ("list", "ノート", "/search"),
        ("graph", "グラフ", "/search/graph"),
        ("clients", "取引先一覧", "/search?view=clients&amp;page=1"),
        ("docs", "資料一覧", "/search?view=docs&amp;page=1"),
    ):
        active = " active" if key == view else ""
        current = ' aria-current="page"' if key == view else ""
        out += f'<a class="ws-tab{active}" href="{url}"{current}>{label}</a>'
    return out


LIST_STYLE = (
    ".knowledge-list{padding:20px;max-width:1200px;width:100%;box-sizing:border-box;margin:auto}"
    ".knowledge-list table{width:100%;border-collapse:collapse;font-size:13px}"
    ".knowledge-list th,.knowledge-list td{padding:12px;text-align:left;"
    "border-bottom:1px solid var(--border);overflow-wrap:anywhere}"
    ".knowledge-list a{color:var(--accent)}"
    ".knowledge-list .meta{line-height:1.8}"
    ".knowledge-list nav{display:flex;gap:20px;margin:20px 0}"
    ".ws-tabs a.ws-tab{text-decoration:none;white-space:nowrap}"
    ".ws-tabs{overflow-x:auto}"
)


def render_list(view: str, page: int, rows: list[dict[str, Any]], total: int | None) -> str:
    """total=None は取得失敗。空集合・範囲外ページと混同させない。"""
    label = "取引先" if view == "clients" else "資料"
    out = f'<main class="knowledge-list"><h1>{label}一覧</h1>'
    out += (
        '<p class="meta">資料件数はFBを除く資料の数です。'
        "重複として非表示の資料・研究ノートは対象外です。"
        "最新FB日は閲覧できるFBのみから集計します。"
        "リンク先のカルテは名前の部分一致で取得し、FB・関連資料を各最大50件表示するため、"
        "この一覧の資料件数と異なる場合があります。</p>"
        if view == "clients"
        else '<p class="meta">閲覧できる資料を更新日の新しい順に並べています。'
        "FB・重複として非表示の資料・研究ノートは対象外です。</p>"
    )
    if total is None:
        return (
            out + '<p role="alert">一覧を取得できませんでした。'
            "時間をおいて再度お試しください。</p></main>"
        )
    if not rows:
        message = "閲覧できる項目はありません。" if total == 0 else "このページは範囲外です。"
        return (
            out + f"<p>{label} {total} 件中 表示0件</p><p>{message}</p>"
            f'<a href="/search?view={view}&amp;page=1">先頭ページへ</a></main>'
        )
    start = (page - 1) * PAGE_SIZE + 1
    out += f"<p>{label} {total} 件中 {start}–{start + len(rows) - 1} 件</p>"
    columns = (
        ("名前", "資料件数", "最新FB日")
        if view == "clients"
        else ("タイトル", "更新日", "資料種別", "取引先", "出典")
    )
    out += "<table><thead><tr>" + "".join(f'<th scope="col">{c}</th>' for c in columns)
    out += "</tr></thead><tbody>"
    for row in rows:
        if view == "clients":
            name = str(row["name"])
            cells = [
                f'<a href="/search/client/{quote(name, safe="")}">{html.escape(name)}</a>',
                html.escape(str(row["document_count"])),
                html.escape(str(row.get("latest_fb_at") or "—")),
            ]
        else:
            uri = str(row.get("source_uri") or "")
            if row.get("source_type") == "gdrive":
                from teamagent.skills.knowledge_deliver.skill import extract_drive_file_id

                if fid := extract_drive_file_id(uri):
                    uri = f"https://drive.google.com/file/d/{fid}/view"
            source = html.escape(uri or "—")
            try:
                safe_uri = urlsplit(uri)
            except ValueError:
                safe_uri = urlsplit("")
            if (
                safe_uri.scheme in {"http", "https"}
                and safe_uri.netloc
                and not any(ord(c) < 32 for c in uri)
                and "\\" not in uri
            ):
                source = (
                    f'<a href="{html.escape(uri)}" target="_blank" '
                    'rel="noopener noreferrer">開く</a>'
                )
            cells = [
                f'<span data-doc-id="{html.escape(str(row["id"]))}">'
                + html.escape(str(row.get("title") or "（無題）"))
                + "</span>",
                html.escape(str(row.get("modified_at") or "—")),
                html.escape(str(row.get("doc_type") or "—")),
                html.escape(str(row.get("client_name") or "—")),
                source,
            ]
        out += "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"
    out += '</tbody></table><nav aria-label="ページ切替">'
    if page > 1:
        out += f'<a href="/search?view={view}&amp;page={page - 1}">前の50件</a>'
    if start + len(rows) <= total and page < MAX_PAGE:
        out += f'<a href="/search?view={view}&amp;page={page + 1}">次の50件</a>'
    return out + "</nav></main>"
