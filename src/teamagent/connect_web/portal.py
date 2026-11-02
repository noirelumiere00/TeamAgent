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
    section: Literal["archive", "knowledge", "personal"] = "knowledge"


SITES = (
    Site(
        "library",
        "動画事例を探す",
        "/library",
        "注目の事例、新着の施策、よく伸びた動画から、次のアイデアを見つけます。",
        "video",
        section="archive",
    ),
    Site(
        "search",
        "資料・議事録を検索",
        "/search",
        "提案書・議事録・Slack を言葉で検索。自分が見られる最新の資料から探します。",
        "search",
    ),
    Site(
        "vault",
        "共有ナレッジを一覧",
        "/app",
        "整理済みの共有資料を、取引先やタグからたどります。",
        "book",
    ),
    Site(
        "history",
        "自分の会話履歴",
        "/app/history",
        "自分と Aico の会話を振り返ります。",
        "chat",
        section="personal",
    ),
    Site(
        "admin",
        "利用状況",
        "/admin",
        "Connect の利用状況を確認します。",
        "chart",
        "admin",
    ),
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


# library_assets.STYLE の値に合わせ、検索シェルへ影響しない名前空間に閉じる。
PORTAL_STYLE = """
:root{--connect-header-height:64px}
.connect-header,.connect-home,.connect-page{
--connect-ink:#20252d;--connect-muted:#5b6470;--connect-blue:#234e89;--connect-line:#e3e6eb;
color-scheme:light;color:var(--connect-ink);
font:16px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",
"Yu Gothic UI",sans-serif}
.connect-page{margin:0;background:#fff}
.connect-header,.connect-header *,.connect-home,.connect-home *{box-sizing:border-box}
.connect-header{height:var(--connect-header-height);background:#fff;
border-bottom:1px solid var(--connect-line);position:relative;z-index:20}
.connect-header-inner{max-width:1328px;height:100%;margin:auto;padding:0 24px;
display:flex;align-items:center;gap:24px}
.connect-header a{color:inherit;text-decoration:none}
.connect-header .connect-brand{font-size:20px;font-weight:800;letter-spacing:-.04em;
line-height:1.3;white-space:nowrap}
.connect-header .connect-brand[aria-current]{color:var(--connect-blue)}
.connect-switch{position:relative;flex-shrink:0;font-size:13px}
.connect-switch summary{cursor:pointer;padding:8px;border-radius:6px}
.connect-header a:focus-visible,.connect-switch summary:focus-visible,
.connect-card:focus-visible{outline:3px solid #2563eb;outline-offset:4px}
.connect-header .connect-menu{display:block;position:absolute;top:100%;left:0;width:260px;
max-width:calc(100vw - 32px);max-height:calc(100vh - 80px);overflow:auto;margin:0;
padding:8px;background:#fff;border:1px solid var(--connect-line);border-radius:6px;
box-shadow:0 8px 24px #20252d14}
.connect-menu a{display:block;padding:10px 14px;border-radius:6px;font-size:14px;
line-height:1.6;overflow-wrap:anywhere}
.connect-menu a:hover,.connect-menu a[aria-current]{background:#eff4ff;color:var(--connect-blue)}
.connect-menu a[aria-current]{font-weight:700}
.connect-email{margin-left:auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;color:var(--connect-muted);font-size:12px}
.connect-home{max-width:1328px;margin:auto;padding:32px 24px 64px;background:#fff}
.connect-home h1,.connect-home h2,.connect-home h3,.connect-home p{overflow-wrap:anywhere}
.connect-hero{margin:12px 0 52px;padding-bottom:32px;border-bottom:1px solid var(--connect-line)}
.connect-home .connect-eyebrow{font-size:12px;font-weight:800;letter-spacing:.14em;
color:var(--connect-blue);margin:0 0 12px}
.connect-home h1{font-size:40px;line-height:1.35;letter-spacing:-.03em;margin:0 0 14px}
.connect-intro{max-width:720px;margin:0;color:var(--connect-muted)}
.connect-section{margin:12px 0 70px;min-width:0}
.connect-section:last-child{margin-bottom:0}
.connect-section-heading{margin:0 0 26px}
.connect-section-heading h2{font-size:34px;font-weight:900;letter-spacing:.025em;
line-height:1.15;padding-top:18px;position:relative;margin:0}
.connect-section-heading h2:before{content:"";position:absolute;top:0;left:0;
width:32px;height:3px;background:var(--connect-blue)}
.connect-section-heading h2 span{display:block;font-size:12px;letter-spacing:.14em;
color:var(--connect-blue);margin-top:10px}
.connect-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}
.connect-card{display:flex;flex-direction:column;align-items:flex-start;min-width:0;
padding:24px;background:#f5f7fa;border:1px solid var(--connect-line);
border-radius:5px;text-decoration:none;color:var(--connect-ink)}
.connect-card:hover{background:#eff4ff;border-color:var(--connect-blue)}
.connect-card svg{width:32px;height:32px;color:var(--connect-blue);margin-bottom:24px}
.connect-card h3{font-size:18px;line-height:1.5;margin:0 0 8px}
.connect-card p{color:var(--connect-muted);font-size:14px;line-height:1.6;margin:0 0 24px}
.connect-card-action{display:block;margin-top:auto;font-size:13px;font-weight:700;
color:var(--connect-blue)}
.connect-section-archive .connect-cards{grid-template-columns:1fr}
.connect-section-archive .connect-card{position:relative;padding:32px 96px 32px 32px}
.connect-section-archive .connect-card svg{position:absolute;right:32px;top:32px;
width:40px;height:40px}
.connect-section-archive .connect-card h3{font-size:26px}
@media(max-width:767px){
.connect-header-inner{gap:12px;padding:0 16px}
.connect-header .connect-brand{font-size:17px}
.connect-switch summary{font-size:12px;padding:8px 4px}
.connect-header .connect-menu{position:fixed;top:var(--connect-header-height);left:16px}
.connect-email{font-size:11px}
.connect-home{padding:28px 16px 48px}
.connect-home h1{font-size:28px}
.connect-hero{margin-bottom:40px;padding-bottom:28px}
.connect-section{margin-bottom:48px}
.connect-section-heading h2{font-size:28px}
.connect-cards{grid-template-columns:1fr;gap:16px}
.connect-section-archive .connect-card{padding:24px}
.connect-section-archive .connect-card svg{position:static;width:32px;height:32px}
.connect-section-archive .connect-card h3{font-size:24px}
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
        '<header class="connect-header"><div class="connect-header-inner">'
        f'<a class="connect-brand" href="/"{home_current}>NewsTV Connect</a>'
        '<details class="connect-switch"><summary>サイト切替</summary>'
        f'<nav class="connect-menu" aria-label="サイト一覧">{links}</nav></details>'
        f'<span class="connect-email">{html.escape(email)}</span></div></header>'
    )


def render_home(email: str) -> str:
    """外部サービスへ問い合わせず、利用できるサイトだけを並べる。"""
    sites = visible_sites(email)
    sections = []
    for key, heading, label in (
        ("archive", "VIDEO ARCHIVE", "動画の事例から探す"),
        ("knowledge", "KNOWLEDGE", "社内の知見をたどる"),
        ("personal", "MY AICO", "自分の会話を振り返る"),
        ("admin", "ADMIN", "管理者向け"),
    ):
        cards = "".join(
            f'<a class="connect-card" href="{html.escape(site.path)}">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f"{_ICONS[site.icon]}</svg><h3>{html.escape(site.name)}</h3>"
            f"<p>{html.escape(site.description)}</p>"
            '<span class="connect-card-action">サイトを開く <span aria-hidden="true">→</span>'
            "</span></a>"
            for site in sites
            if ("admin" if site.permission != "all" else site.section) == key
        )
        if cards:
            sections.append(
                f'<section class="connect-section connect-section-{key}" '
                f'aria-labelledby="connect-{key}-heading">'
                f'<div class="connect-section-heading"><h2 id="connect-{key}-heading">'
                f"{label}<span>{heading}</span></h2></div>"
                f'<div class="connect-cards">{cards}</div></section>'
            )
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>NewsTV Connect</title><style>{PORTAL_STYLE}</style></head>"
        '<body class="connect-page">'
        + render_header(email, current_path="/")
        + '<main class="connect-home"><div class="connect-hero">'
        '<p class="connect-eyebrow">NEWSTV CONNECT</p>'
        "<h1>事例と知見を、次のアイデアに。</h1>"
        '<p class="connect-intro">動画の事例、社内のナレッジ、Aico との会話。<br>'
        "今日の仕事に役立つ情報を、ここから。</p></div>"
        + "".join(sections)
        + "</main></body></html>"
    )
