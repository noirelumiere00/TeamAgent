"""NewsTV Connect の入口と、権限に応じた共通ナビ。"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Site:
    """利用できるサイト。アイコンは固定 SVG の識別子だけを持つ。"""

    key: str
    name: str
    path: str
    description: str
    icon: str
    permission: Literal["all", "admin", "memory_admin", "history_admin"] = "all"


SITES = (
    Site("search", "ナレッジ検索", "/search", "社内の資料やナレッジを検索します。", "search"),
    Site("vault", "ナレッジ Vault", "/app", "ナレッジをノートやグラフでたどります。", "book"),
    Site("library", "動画ライブラリ", "/library", "動画の事例と配信結果を探します。", "video"),
    Site("history", "Aico との会話", "/app/history", "自分と Aico の会話を振り返ります。", "chat"),
    Site("admin", "管理: 利用状況", "/admin", "Connect の利用状況を確認します。", "chart", "admin"),
    Site(
        "memory_admin",
        "本人メモ管理",
        "/admin/memory",
        "本人メモを管理者として確認します。",
        "book",
        "memory_admin",
    ),
    Site(
        "history_admin",
        "会話履歴管理",
        "/admin/history",
        "会話履歴を管理者として確認します。",
        "chat",
        "history_admin",
    ),
)


def visible_sites(email: str) -> tuple[Site, ...]:
    """各画面と同じ管理者判定で、カードとナビに共通の一覧を返す。"""
    from teamagent.adapters import dm_history, personal_memory_admin
    from teamagent.connect_web.app import _admin_emails

    permissions = {
        "all": True,
        "admin": email.strip().lower() in _admin_emails(),
        "memory_admin": personal_memory_admin.is_admin(email),
        "history_admin": dm_history.is_admin(email),
    }
    return tuple(site for site in SITES if permissions[site.permission])


PORTAL_STYLE = """
:root{--connect-header-height:64px}
.connect-header,.connect-header *{box-sizing:border-box}
.connect-header{height:var(--connect-header-height);display:flex;align-items:center;gap:20px;
padding:0 24px;background:#141c2e;border-bottom:1px solid #2b3a5e;color:#e8edf7;
font:14px/1.5 system-ui,sans-serif;position:relative;z-index:20}
.connect-header a{color:inherit;text-decoration:none}
.connect-header .connect-brand{font-size:17px;font-weight:700;white-space:nowrap}
.connect-header .connect-brand[aria-current]{color:#a8caff}
.connect-switch{position:relative;flex-shrink:0}
.connect-switch summary{cursor:pointer;padding:8px;border-radius:6px}
.connect-header a:focus-visible,.connect-switch summary:focus-visible{
outline:2px solid #8cb5ff;outline-offset:3px}
.connect-header .connect-menu{position:absolute;top:100%;left:0;width:260px;
max-width:calc(100vw - 32px);max-height:calc(100vh - 80px);overflow:auto;margin:0;
padding:8px;background:#1a2233;border:1px solid #46536d;border-radius:8px;
box-shadow:0 8px 24px #0006}
.connect-menu a{display:block;padding:10px;border-radius:5px}
.connect-menu a:hover,.connect-menu a[aria-current]{background:#293c60;color:#fff}
.connect-menu a[aria-current]{font-weight:700}
.connect-email{margin-left:auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;color:#aebbd3}
.connect-home{max-width:1080px;margin:0 auto;padding:48px 24px}
.connect-home h1{font-size:30px;margin:0 0 12px}
.connect-home>p{color:#aebbd3;margin-bottom:32px}
.connect-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px}
.connect-card{display:block;padding:24px;background:#1a2233;border:1px solid #34425e;
border-radius:12px;text-decoration:none;color:#e8edf7}
.connect-card:hover{background:#23304a;border-color:#8cb5ff}
.connect-card:focus-visible{outline:2px solid #8cb5ff;outline-offset:3px}
.connect-card svg{width:28px;height:28px;color:#8cb5ff}
.connect-card h2{font-size:18px;margin:16px 0 8px}
.connect-card p{color:#aebbd3;font-size:14px;line-height:1.7;margin:0}
@media(max-width:600px){
.connect-header{gap:8px;padding:0 12px}
.connect-header .connect-brand{font-size:14px}
.connect-switch summary{font-size:12px;padding:8px 4px}
.connect-header .connect-menu{position:fixed;top:var(--connect-header-height);left:16px}
.connect-email{font-size:11px}
.connect-home{padding:32px 16px}
}
"""

_ICONS = {
    "search": '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    "book": '<path d="M3 4h7l2 2 2-2h7v16h-7l-2 2-2-2H3zM12 6v16"/>',
    "video": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/>',
    "chat": '<path d="M4 4h16v13H9l-5 4zM8 9h8M8 13h5"/>',
    "chart": '<path d="M4 3v18h17M8 16v-5M13 16V7M18 16V4"/>',
}


def render_header(email: str, *, current_path: str) -> str:
    """JavaScript なしで使えるサイト切替。現在地をリンク上で示す。"""
    links = "".join(
        f'<a href="{html.escape(site.path)}"'
        + (' aria-current="page"' if site.path == current_path else "")
        + f">{html.escape(site.name)}</a>"
        for site in visible_sites(email)
    )
    home_current = ' aria-current="page"' if current_path == "/" else ""
    return (
        '<header class="connect-header">'
        f'<a class="connect-brand" href="/"{home_current}>NewsTV Connect</a>'
        '<details class="connect-switch"><summary>サイト切替</summary>'
        f'<nav class="connect-menu" aria-label="サイト一覧">{links}</nav></details>'
        f'<span class="connect-email">{html.escape(email)}</span></header>'
    )


def render_home(email: str) -> str:
    """外部サービスへ問い合わせず、利用できるサイトだけを並べる。"""
    cards = "".join(
        f'<a class="connect-card" href="{html.escape(site.path)}">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f"{_ICONS[site.icon]}</svg><h2>{html.escape(site.name)}</h2>"
        f"<p>{html.escape(site.description)}</p></a>"
        for site in visible_sites(email)
    )
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>NewsTV Connect</title><style>{PORTAL_STYLE}"
        "body{margin:0;background:#0f1420;color:#e8edf7;font-family:system-ui,sans-serif}"
        "</style></head><body>"
        + render_header(email, current_path="/")
        + '<main class="connect-home"><h1>NewsTV Connect</h1>'
        "<p>社内のナレッジ、動画、Aico との会話へ。</p>"
        f'<div class="connect-cards">{cards}</div></main></body></html>'
    )
