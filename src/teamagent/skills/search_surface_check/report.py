"""検索面チェックの媒体比較 HTML レンダラ（TikTok列×IG列・勢力図帯・クライアント赤枠）。

ライトテーマ固定・JSなし（x_research/report.py と同方針）。
"""

from __future__ import annotations

import datetime as _dt
import html as _html

from teamagent.skills._html.theme import FONT_STACK_JP
from teamagent.skills.search_surface_check.schema import KwSurface, SurfacePost

_CAT_LABEL = {
    "news": "ニュース",
    "gourmet": "グルメ",
    "ugc": "一般",
    "brand_official": "公式",
    "influencer": "インフル",
    "other": "その他",
    "unknown": "未分類",
}
_CAT_COLOR = {
    "news": "#5b6570",
    "gourmet": "#e07f00",
    "ugc": "#1d6fdc",
    "brand_official": "#137333",
    "influencer": "#8f42c9",
    "other": "#9aa3ac",
    "unknown": "#c4ccd4",
}

_CSS = f"""
body{{font-family:{FONT_STACK_JP};background:#f6f7f9;color:#1b1f24;margin:0;padding:24px}}
.wrap{{max-width:1000px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}
h2{{font-size:16px;margin:26px 0 8px}}
.meta{{color:#5b6570;font-size:12px;margin-bottom:14px}}
.grid{{display:flex;gap:14px;flex-wrap:wrap}}
.col{{flex:1;min-width:320px}}
.colhead{{font-weight:700;font-size:13px;margin:6px 0}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e7ec;
  border-radius:8px;overflow:hidden;font-size:12px}}
th{{background:#eef1f5;text-align:left;padding:6px 8px;font-size:11px;color:#5b6570}}
td{{padding:6px 8px;border-top:1px solid #eef1f5;vertical-align:top}}
tr.client td{{background:#fdeeee;border-left:3px solid #e0245e}}
.cat{{display:inline-block;border-radius:4px;color:#fff;font-size:10px;padding:1px 6px}}
.num{{text-align:right;white-space:nowrap}}
.ratio{{display:flex;height:14px;border-radius:7px;overflow:hidden;margin:6px 0 2px;
  border:1px solid #e3e7ec}}
.legend{{font-size:10px;color:#5b6570;margin-bottom:6px}}
.summary{{background:#e9f5ef;border:1px solid #cbe7d8;border-radius:10px;padding:12px 16px;
  margin:14px 0;font-size:13px;line-height:1.7}}
.inbox{{font-size:12px;color:#31405a;margin:2px 0 8px}}
.footer{{color:#8a939c;font-size:11px;margin-top:22px;border-top:1px solid #e3e7ec;
  padding-top:8px}}
a{{color:#1d6fdc;text-decoration:none}}
"""


def _esc(s: str) -> str:
    return _html.escape(str(s), quote=True)


def _fmt(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return f"{n:,}"


def _ratio_bar(ratio: dict[str, float]) -> str:
    if not ratio:
        return ""
    segs = "".join(
        f"<div style='width:{max(1.0, v * 100):.1f}%;background:{_CAT_COLOR.get(k, '#ccc')}' "
        f"title='{_esc(_CAT_LABEL.get(k, k))} {v * 100:.0f}%'></div>"
        for k, v in sorted(ratio.items(), key=lambda kv: -kv[1])
        if v > 0
    )
    legend = "・".join(
        f"{_CAT_LABEL.get(k, k)} {v * 100:.0f}%"
        for k, v in sorted(ratio.items(), key=lambda kv: -kv[1])
        if v > 0
    )
    return f"<div class='ratio'>{segs}</div><div class='legend'>{legend}</div>"


def _post_row(p: SurfacePost) -> str:
    cat = (
        f"<span class='cat' style='background:{_CAT_COLOR.get(p.category, '#ccc')}'>"
        f"{_esc(_CAT_LABEL.get(p.category, p.category))}</span>"
    )
    views = p.play_count or p.like_count
    author = _esc(p.author or "不明")
    if p.url:
        author = f"<a href='{_esc(p.url)}'>{author}</a>"
    extra = f"×{p.appearances}" if p.appearances > 1 else ""
    cls = " class='client'" if p.is_client else ""
    star = "⭐ " if p.is_client else ""
    return (
        f"<tr{cls}><td class='num'>{p.rank}{extra}</td>"
        f"<td>{star}{author}<br><span style='color:#5b6570'>{_esc(p.desc[:40])}</span></td>"
        f"<td>{cat}</td><td class='num'>{_fmt(views)}</td></tr>"
    )


def _platform_col(surface: KwSurface | None, label: str, unit: str) -> str:
    if surface is None or not surface.posts:
        return f"<div class='col'><div class='colhead'>{_esc(label)}</div><p>データなし</p></div>"
    inbox = (
        f"<div class='inbox'>⭐ クライアント在圏: {len(surface.client_ranks)}件"
        f"（順位 {', '.join(map(str, surface.client_ranks[:5]))}）</div>"
        if surface.client_ranks
        else "<div class='inbox'>クライアント投稿は面に出ていません</div>"
    )
    rows = "".join(_post_row(p) for p in surface.posts[:15])
    return (
        f"<div class='col'><div class='colhead'>{_esc(label)}</div>"
        f"{_ratio_bar(surface.category_ratio)}{inbox}"
        f"<table><tr><th>順位</th><th>投稿者</th><th>分類</th><th>{unit}</th></tr>{rows}</table>"
        "</div>"
    )


def render_surface_report(
    *,
    keywords: list[str],
    surfaces: list[KwSurface],
    comparison_summary: str,
    client_name: str | None,
) -> str:
    by_key = {(s.keyword, s.platform): s for s in surfaces}
    today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    parts: list[str] = []
    if comparison_summary:
        parts.append(f"<div class='summary'>📊 <b>面の性格比較</b><br>{_esc(comparison_summary)}</div>")
    for kw in keywords:
        parts.append(f"<h2>「{_esc(kw)}」の検索面</h2><div class='grid'>")
        parts.append(_platform_col(by_key.get((kw, "tiktok")), "TikTok（検索面表示順）", "再生数"))
        parts.append(
            _platform_col(by_key.get((kw, "instagram")), "Instagramリール（出現頻度順）", "再生/いいね")
        )
        parts.append("</div>")
    client = f"クライアント: {client_name}・" if client_name else ""
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>検索面チェック</title><style>{_CSS}</style></head><body><div class='wrap'>"
        f"<h1>検索面チェック（TikTok × Instagram）</h1>"
        f"<div class='meta'>{_esc(client)}KW: {_esc('・'.join(keywords))}・実測 {today}</div>"
        f"{''.join(parts)}"
        "<div class='footer'>検索結果はパーソナライズの影響を受けるため「傾向・定点比較」用途。"
        "IGは同じ人気リールが繰り返し出る仕様のため、順位でなく出現頻度×エンゲージで序列化"
        "（×n は重複出現回数=面の定着度）。</div></div></body></html>"
    )


__all__ = ["render_surface_report"]
