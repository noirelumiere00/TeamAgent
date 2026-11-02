"""検索シェル内の RLS 一覧用 query 検証と HTML 描画。"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from uuid import UUID

from teamagent.adapters.pgvector_client import KNOWLEDGE_TAG_AXES

PAGE_SIZE = 50
MAX_PAGE = 10_000
VIEWS = ("clients", "docs", "doc", "tags")


def parse_tag(raw: str) -> tuple[str, str] | None:
    axis, separator, value = raw.partition(":")
    if (
        not separator
        or axis not in KNOWLEDGE_TAG_AXES
        or not value.strip()
        or len(value) > 500
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or "\\" in value
    ):
        return None
    return axis, value


def search_list_target(raw: str) -> str | None:
    """既知のナレッジ GET query だけを検証・再構築する。前方一致では許可しない。"""
    if len(raw) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in raw) or "\\" in raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme or parts.netloc or parts.path != "/search" or parts.fragment:
        return None
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    values = dict(pairs)
    if len(values) != len(pairs):
        return None
    view, page = values.get("view"), values.get("page", "1")
    if view == "doc":
        if set(values) != {"view", "id"}:
            return None
        try:
            document_id = str(UUID(values["id"]))
        except ValueError:
            return None
        if document_id != values["id"].lower():
            return None
        return "/search?" + urlencode({"view": "doc", "id": document_id})
    if view == "tags":
        return "/search?view=tags" if set(values) == {"view"} else None
    allowed_keys = {"view", "page", "tag"} if view == "docs" else {"view", "page"}
    if (
        view not in VIEWS
        or set(values) - allowed_keys
        or not re.fullmatch(r"[1-9][0-9]{0,4}", page)
    ):
        return None
    if int(page) > MAX_PAGE:
        return None
    query: dict[str, str | int] = {"view": view, "page": int(page)}
    if "tag" in values:
        if parse_tag(values["tag"]) is None:
            return None
        query["tag"] = values["tag"]
    return "/search?" + urlencode(query)


def list_tabs(view: str) -> str:
    if view == "doc":
        view = "docs"
    out = ""
    for key, label, url in (
        ("list", "検索", "/search"),
        ("graph", "関係図", "/search/graph"),
        ("clients", "取引先一覧", "/search?view=clients&amp;page=1"),
        ("docs", "資料一覧", "/search?view=docs&amp;page=1"),
        ("tags", "タグ", "/search?view=tags"),
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
    ".ws-tabs{flex-wrap:wrap}"
    ".knowledge-list pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.8}"
    ".knowledge-list dt{color:var(--muted);margin-top:12px}"
    ".knowledge-list dd{margin:4px 0;overflow-wrap:anywhere}"
    ".knowledge-list h1{margin:16px 0 12px;font-size:26px}"
    ".knowledge-list h2{font-size:17px;margin:24px 0 12px}"
    ".knowledge-list th{background:var(--bg-secondary);color:var(--muted);font-weight:600}"
    ".knowledge-list tbody tr:hover{background:var(--bg-secondary)}"
    ".knowledge-list table{table-layout:fixed}.knowledge-list td:first-child{font-weight:600}"
    ".document-meta{display:grid;grid-template-columns:100px minmax(0,1fr);gap:12px 20px;"
    "padding:20px;background:var(--bg-secondary);border:1px solid var(--border);border-radius:8px}"
    ".knowledge-list .document-meta dt,.knowledge-list .document-meta dd{margin:0}"
    ".knowledge-list pre{padding:20px;border:1px solid var(--border);border-radius:8px}"
    ".tag-list{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding:0}"
    ".tag-list li{padding:6px 10px;background:var(--bg-secondary);border-radius:6px;"
    "font-size:13px;overflow-wrap:anywhere;max-width:100%}"
    ".list-note{margin:16px 0;font-size:13px;color:var(--muted)}"
    ".list-note summary{cursor:pointer}.knowledge-list [role=alert]{padding:20px;"
    "border:1px solid #b84a4a;border-radius:8px;background:#fff2f1}"
    "@media(max-width:767px){.knowledge-list{padding:16px}.knowledge-list h1{font-size:22px}"
    ".knowledge-list table,.knowledge-list tbody,.knowledge-list tr,"
    ".knowledge-list td{display:block}"
    ".knowledge-list thead{position:absolute;width:1px;height:1px;overflow:hidden;"
    "clip-path:inset(50%)}.knowledge-list tr{border:1px solid var(--border);border-radius:8px;"
    "margin:12px 0;padding:8px}.knowledge-list td{padding:6px;border:0}"
    ".knowledge-list td:before{content:attr(data-label);display:block;font-size:11px;"
    "color:var(--muted);font-weight:400}.document-meta{grid-template-columns:70px minmax(0,1fr);"
    "padding:14px;gap:10px}.knowledge-list nav{flex-wrap:wrap}}"
)


def source_link(row: dict[str, Any]) -> str:
    uri = str(row.get("source_uri") or "")
    if row.get("source_type") == "gdrive":
        from teamagent.skills.knowledge_deliver.skill import extract_drive_file_id

        if fid := extract_drive_file_id(uri):
            uri = f"https://drive.google.com/file/d/{fid}/view"
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
        return f'<a href="{html.escape(uri)}" target="_blank" rel="noopener noreferrer">開く</a>'
    return html.escape(uri or "—")


def tag_link(axis: str, value: str) -> str:
    url = "/search?" + urlencode({"view": "docs", "tag": f"{axis}:{value}", "page": 1})
    return f'<a href="{html.escape(url)}">{html.escape(value)}</a>'


def render_list(
    view: str,
    page: int,
    rows: list[dict[str, Any]],
    total: int | None,
    *,
    tag: str | None = None,
) -> str:
    """total=None は取得失敗。空集合・範囲外ページと混同させない。"""
    label = "取引先" if view == "clients" else "資料"
    out = f'<main class="knowledge-list"><h1>{label}一覧</h1>'
    query = {"view": view}
    if tag is not None:
        query["tag"] = tag
        axis, _, value = tag.partition(":")
        tag_label = KNOWLEDGE_TAG_AXES.get(axis, axis)
        out += (
            f"<p>絞り込み：{html.escape(tag_label)}：{html.escape(value)} "
            '<a href="/search?view=docs">解除</a></p>'
        )
    first_url = html.escape("/search?" + urlencode({**query, "page": 1}))
    out += (
        '<p class="sub">取引先名からカルテを開き、最新の反応と次アクションを確認できます。</p>'
        '<details class="list-note"><summary>件数・表示範囲について</summary>'
        '<p class="meta">資料件数はFBを除く資料の数です。'
        "重複として非表示の資料・研究ノートは対象外です。"
        "最新FB日は閲覧できるFBのみから集計します。"
        "リンク先のカルテは名前の部分一致で取得し、FB・関連資料を各最大50件表示するため、"
        "この一覧の資料件数と異なる場合があります。</p></details>"
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
            f'<a href="{first_url}">先頭ページへ</a></main>'
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
            detail_url = html.escape("/search?" + urlencode({"view": "doc", "id": row["id"]}))
            cells = [
                f'<a data-doc-id="{html.escape(str(row["id"]))}" href="{detail_url}">'
                + html.escape(str(row.get("title") or "（無題）"))
                + "</a>",
                html.escape(str(row.get("modified_at") or "—")),
                html.escape(str(row.get("doc_type") or "—")),
                html.escape(str(row.get("client_name") or "—")),
                source_link(row),
            ]
        out += (
            "<tr>"
            + "".join(
                f'<td data-label="{label}">{cell}</td>'
                for label, cell in zip(columns, cells, strict=True)
            )
            + "</tr>"
        )
    out += '</tbody></table><nav aria-label="ページ切替">'
    if page > 1:
        url = html.escape("/search?" + urlencode({**query, "page": page - 1}))
        out += f'<a href="{url}">前の50件</a>'
    if start + len(rows) <= total and page < MAX_PAGE:
        url = html.escape("/search?" + urlencode({**query, "page": page + 1}))
        out += f'<a href="{url}">次の50件</a>'
    return out + "</nav></main>"


def render_document(document: dict[str, Any] | None, *, failed: bool = False) -> str:
    out = '<main class="knowledge-list"><a href="/search?view=docs">資料一覧へ</a>'
    if failed:
        return out + '<h1>資料詳細</h1><p role="alert">資料を取得できませんでした。</p></main>'
    if document is None:
        return out + "<h1>資料詳細</h1><p>見つからないか、閲覧できません。</p></main>"
    out += (
        "<h1>"
        + html.escape(str(document.get("title") or "（無題）"))
        + '</h1><dl class="document-meta">'
    )
    client = str(document.get("client_name") or "")
    client_html = (
        f'<a href="/search/client/{quote(client, safe="")}">{html.escape(client)}</a>'
        if client
        else "—"
    )
    for label, value in (
        ("更新日", html.escape(str(document.get("modified_at") or "—"))),
        ("資料種別", html.escape(str(document.get("doc_type") or "—"))),
        ("取引先", client_html),
        ("業界", html.escape(str(document.get("industry") or "—"))),
        ("出典", source_link(document)),
    ):
        out += f"<dt>{label}</dt><dd>{value}</dd>"
    out += '</dl><h2>関連する資料をタグから探す</h2><ul class="tag-list">'
    for axis, label in KNOWLEDGE_TAG_AXES.items():
        if tag_value := document.get(axis):
            out += f"<li>{label}：{tag_link(axis, str(tag_value))}</li>"
    out += (
        '</ul><h2>本文抜粋</h2><p class="meta">代表部分の最大2,000文字を表示しています。</p><pre>'
    )
    return (
        out
        + html.escape(str(document.get("excerpt") or "本文抜粋がありません。"))
        + "</pre></main>"
    )


def render_tags(rows: list[dict[str, Any]] | None) -> str:
    out = '<main class="knowledge-list"><h1>タグ一覧</h1>'
    if rows is None:
        return out + '<p role="alert">タグを取得できませんでした。</p></main>'
    out += (
        '<p class="meta">閲覧できる資料全件の資料数です。'
        "FB・非表示の重複資料・研究ノートは対象外です。</p>"
    )
    if not rows:
        return out + "<p>閲覧できるタグはありません。</p></main>"
    for axis, label in KNOWLEDGE_TAG_AXES.items():
        entries = [row for row in rows if row["axis"] == axis]
        if not entries:
            continue
        out += f'<h2>{label}</h2><ul class="tag-list">'
        for row in entries:
            out += (
                "<li>"
                + tag_link(axis, str(row["value"]))
                + "："
                + html.escape(str(row["document_count"]))
                + " 件</li>"
            )
        out += "</ul>"
    return out + "</main>"
