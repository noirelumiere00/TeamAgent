"""共通 :class:`Report` → 16:9 スライド HTML（PPTX 変換用）。

``MediaJobClient.slides_to_pptx`` は **``.slide`` 要素を 1 枚ずつスクショして PPTX 化**する。
したがって PPTX を作るのに ``python-pptx`` を mcp image へ入れる必要はない（media worker 側で
完結する）。ここは「1 ``<section class="slide">`` = 1 スライド = 1280x720」を守った HTML を
出すだけの純粋関数。

video_algorithm/slides.py と同じ規約に揃える:
  - サイズは 1280x720 固定（変換時の解像度と一致させ、文字のにじみを避ける）
  - 入る量だけ載せる。**溢れさせない**（スクショなのでスクロールは無い＝はみ出しは消える）
  - 空セクションは描画しない

スライド割り:
  1 表紙（タイトル＋一行見出し＋chips）
  2..N セクション（1 ブロック 1 枚）
  N+1.. 一覧表（``_ROWS_PER_SLIDE`` 行ずつ・画像列は落とす）
"""

from __future__ import annotations

import html as _html

from teamagent.skills._html.report import Report, Table, render_body
from teamagent.skills._html.theme import FONT_STACK_JP

_W = 1280
_H = 720
# 1 枚に載る行数（720px から見出し・余白を引いた実測値。増やすと最終行が切れる）。
_ROWS_PER_SLIDE = 9
# 1 枚に収まる本文の目安。超えるぶんは切って「続きは HTML レポート」に委ねる。
_BODY_MAX = 520


def _esc(s: str) -> str:
    return _html.escape(s or "", quote=True)


_CSS = f"""
*{{box-sizing:border-box}}
body{{margin:0;background:#e9ecf0;font-family:{FONT_STACK_JP};color:#1b1f24;
  display:flex;flex-direction:column;align-items:center;gap:16px;padding:16px}}
.slide{{width:{_W}px;height:{_H}px;background:#fff;position:relative;overflow:hidden;
  padding:56px 64px;display:flex;flex-direction:column;gap:18px}}
.slide h1{{font-size:44px;line-height:1.25;margin:0;font-weight:700}}
.slide h2{{font-size:30px;margin:0;font-weight:700;padding-bottom:12px;
  border-bottom:3px solid #1d6fdc}}
.lead{{font-size:26px;font-weight:700;color:#1d6fdc;line-height:1.5;margin:0}}
.sub{{font-size:16px;color:#48525e;margin:0}}
.chips{{display:flex;flex-wrap:wrap;gap:10px;margin:0;padding:0;list-style:none}}
.chips li{{border:1px solid #d7dbe4;border-radius:8px;padding:8px 14px;font-size:15px;
  color:#48525e}}
.chips b{{color:#1b1f24;font-size:19px;margin-left:8px}}
.body{{font-size:19px;line-height:1.75;overflow:hidden}}
.body p{{margin:0 0 10px}}
.body ul,.body ol{{margin:0;padding-left:1.3em}}
.body li{{margin-bottom:9px}}
.body strong{{color:#0f4fb0}}
.body h3{{font-size:19px;margin:12px 0 6px;color:#48525e}}
table{{border-collapse:collapse;width:100%;font-size:16px}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid #e3e7ec}}
th{{background:#f2f4f7;font-size:13px;color:#7b848f;letter-spacing:.06em}}
td.r,th.r{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.foot{{position:absolute;left:64px;right:64px;bottom:22px;font-size:12px;color:#98a0aa;
  display:flex;justify-content:space-between}}
.foot .conf{{color:#8a5a05;font-weight:700}}
"""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _foot(report: Report, page: str) -> str:
    return (
        f"<div class='foot'><span>{_esc(_clip(report.title, 60))}</span>"
        f"<span class='conf'>社外共有不可</span><span>{_esc(page)}</span></div>"
    )


def _cover(report: Report) -> str:
    chips = "".join(
        f"<li>{_esc(c.label)}<b>{_esc(c.value)}</b></li>" for c in report.chips if c.value
    )
    lead = f"<p class='lead'>{_esc(report.headline)}</p>" if report.headline else ""
    sub = f"<p class='sub'>{_esc(report.subtitle)}</p>" if report.subtitle else ""
    body = render_body(_clip(report.body_md, _BODY_MAX)) if report.body_md else ""
    return (
        "<section class='slide'>"
        f"<h1>{_esc(report.title)}</h1>{lead}{sub}"
        f"{f'<ul class=chips>{chips}</ul>' if chips else ''}"
        f"<div class='body'>{body}</div>"
        f"{_foot(report, '1')}</section>"
    )


def _section_slide(report: Report, title: str, body_md: str, page: str) -> str:
    return (
        "<section class='slide'>"
        f"<h2>{_esc(title)}</h2>"
        f"<div class='body'>{render_body(_clip(body_md, _BODY_MAX))}</div>"
        f"{_foot(report, page)}</section>"
    )


def _table_slides(report: Report, table: Table, start_page: int) -> list[str]:
    """一覧表を ``_ROWS_PER_SLIDE`` 行ずつに割る。画像列（幅固定のサムネ）は落とす。"""
    keep = [i for i, col in enumerate(table.columns) if col.label != ""]
    if not keep:
        return []
    head = "".join(
        f"<th class='r'>{_esc(table.columns[i].label)}</th>"
        if table.columns[i].align == "right"
        else f"<th>{_esc(table.columns[i].label)}</th>"
        for i in keep
    )
    slides: list[str] = []
    chunks = [
        table.rows[i : i + _ROWS_PER_SLIDE] for i in range(0, len(table.rows), _ROWS_PER_SLIDE)
    ]
    for n, chunk in enumerate(chunks):
        body = []
        for row in chunk:
            cells = []
            for i in keep:
                cell = row[i] if i < len(row) else None
                align = table.columns[i].align
                text = _esc(_clip(cell.text, 44)) if cell else ""
                klass = " class='r'" if align == "right" else ""
                sub = f"<br><small>{_esc(cell.sub)}</small>" if cell and cell.sub else ""
                cells.append(f"<td{klass}>{text}{sub}</td>")
            body.append(f"<tr>{''.join(cells)}</tr>")
        caption = table.caption or "一覧"
        suffix = f"（{n + 1}/{len(chunks)}）" if len(chunks) > 1 else ""
        slides.append(
            "<section class='slide'>"
            f"<h2>{_esc(caption)}{suffix}</h2>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
            f"{_foot(report, str(start_page + n))}</section>"
        )
    return slides


def render_slides(report: Report) -> str:
    """:class:`Report` を ``.slide`` 群の HTML にする（``slides_to_pptx`` にそのまま渡せる）。"""
    slides = [_cover(report)]
    for sec in report.sections:
        if sec.body_md and sec.body_md.strip():
            slides.append(_section_slide(report, sec.title, sec.body_md, str(len(slides) + 1)))
    for table in report.tables:
        if table.rows:
            slides.extend(_table_slides(report, table, len(slides) + 1))
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        f"<title>{_esc(report.title)}</title><style>{_CSS}</style></head>"
        f"<body>{''.join(slides)}</body></html>"
    )


__all__ = ["render_slides"]
