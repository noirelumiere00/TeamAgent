"""検索系 skill 共通の HTML レポートレンダラ（純粋関数・JS なし・外部アセットなし）。

なぜ共通化するか:
    tiktok_search / video_analysis / proposal_draft / proposal_review の出力は、スキーマ名こそ
    違うが **「LLM が生成した本文」＋「出典リスト」** という同じ形をしている。ツールごとに
    HTML を書くと同じ CSS が 4 本に増え、崩れ方も 4 通りになる。ここに 1 本だけ置き、各 skill は
    :class:`Report` へ詰め替えるだけにする（詰め替えは skill 側の ``report.py``）。

ライトテーマ固定:
    x_research/report.py と同じ判断。OS のダーク設定で白文字が消える納品事故があったため
    ``prefers-color-scheme`` 分岐は**作らない**。配布先（営業・クライアント）の閲覧環境を
    こちらで制御できない以上、片方に寄せて確実に読める方を採る。

安全性:
    - 文字列は全て :func:`html.escape` を通してから組み立てる（動画の説明文には攻撃者が
      自由に書ける本文が入る＝納品HTML上での任意JS実行の入口になり得る）。
    - リンクは :func:`safe_href` を通ったものだけ ``<a>`` にし、それ以外は素のテキストへ落とす。
    - 本文は Markdown "風" の最小変換のみ行う。生 HTML は一切通さない（escape 後に変換するため
      LLM が ``<script>`` を書いても構造タグにはならない）。
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import re
from dataclasses import dataclass, field

from teamagent.skills._html.theme import FONT_STACK_JP
from teamagent.skills._shared.text_safety import safe_href

_JST = _dt.timezone(_dt.timedelta(hours=9))


@dataclass(frozen=True)
class Chip:
    """見出し直下に並べる小さな事実（件数・モデル・コスト等）。"""

    label: str
    value: str


@dataclass(frozen=True)
class Column:
    """出典テーブルの列定義。``align="right"`` は数値列（等幅・桁揃え）。"""

    label: str
    align: str = "left"


@dataclass(frozen=True)
class Cell:
    """テーブル 1 セル。

    Attributes:
        text: 表示文字列（エスケープはレンダラ側で行う）。
        href: リンク先。``safe_href`` を通らない値は無視してプレーン表示に落ちる。
        sub: 補助行（フォロワー数など）。
        bar: 0.0-1.0。指定すると数値の左に横棒を描く（大小を目で比べるため）。
        tone: ``"ok" | "warn" | "muted"``。指標の高低を色で示すピル表示にする。
    """

    text: str
    href: str | None = None
    sub: str | None = None
    bar: float | None = None
    tone: str | None = None


@dataclass(frozen=True)
class Table:
    """出典テーブル 1 つ。"""

    columns: list[Column]
    rows: list[list[Cell]]
    caption: str = ""
    note: str = ""


@dataclass(frozen=True)
class Report:
    """レンダラへの唯一の入力。skill 側はこれを組むだけでよい。"""

    title: str
    #: 本文の 1 行要約（``headline.make_headline``）。空なら描画しない。
    headline: str = ""
    subtitle: str = ""
    chips: list[Chip] = field(default_factory=list)
    body_md: str = ""
    tables: list[Table] = field(default_factory=list)
    source_note: str = ""


_CSS = f"""
:root{{--ink:#1b1f24;--ink2:#48525e;--muted:#7b848f;--line:#e3e7ec;--line2:#cfd6de;
  --bg:#f6f7f9;--surface:#fff;--surface2:#f2f4f7;--accent:#1d6fdc;--accent-soft:#e6effb;
  --ok:#137333;--ok-soft:#e6f4ea;--warn:#8a5a05;--warn-soft:#fdf1d8}}
*{{box-sizing:border-box}}
body{{font-family:{FONT_STACK_JP};background:var(--bg);color:var(--ink);margin:0;
  padding:24px 16px 56px;line-height:1.75;font-size:15px}}
.wrap{{max-width:960px;margin:0 auto}}
h1{{font-size:24px;line-height:1.35;margin:0 0 6px;font-weight:700}}
.lead{{font-size:17px;font-weight:700;line-height:1.6;margin:0 0 8px;color:var(--ink)}}
.sub{{color:var(--ink2);font-size:13px;margin:0 0 14px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 22px;padding:0;list-style:none}}
.chips li{{background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:6px 12px;font-size:12px;color:var(--ink2)}}
.chips b{{color:var(--ink);font-size:14px;font-weight:700;margin-left:6px;
  font-variant-numeric:tabular-nums}}
.body{{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:6px 20px 18px;margin:0 0 22px}}
.body h2{{font-size:16px;margin:20px 0 8px;padding-bottom:6px;border-bottom:2px solid var(--line2)}}
.body h3{{font-size:14px;margin:16px 0 6px;color:var(--ink2)}}
.body p{{margin:0 0 10px;font-size:14px}}
.body ul,.body ol{{margin:0 0 12px;padding-left:1.3em}}
.body li{{font-size:14px;margin-bottom:4px}}
.tbl{{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  margin:0 0 22px;overflow:hidden}}
.tbl .cap{{padding:12px 16px 0;font-size:14px;font-weight:700}}
.tbl .note{{padding:2px 16px 10px;font-size:12px;color:var(--muted)}}
.scroll{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;min-width:640px;font-size:13px}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);
  vertical-align:top}}
th{{background:var(--surface2);font-size:11px;letter-spacing:.06em;color:var(--muted);
  font-weight:700;white-space:nowrap}}
tr:last-child td{{border-bottom:none}}
td.r,th.r{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td a{{color:var(--accent);text-decoration:none;font-weight:700;word-break:break-all}}
td a:hover{{text-decoration:underline}}
.sub2{{display:block;color:var(--muted);font-size:11px;margin-top:2px}}
.barwrap{{display:flex;align-items:center;gap:8px;justify-content:flex-end}}
.bar{{height:8px;background:var(--accent);border-radius:0 3px 3px 0;flex:none;min-width:2px}}
.pill{{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:700;
  font-variant-numeric:tabular-nums}}
.pill.ok{{background:var(--ok-soft);color:var(--ok)}}
.pill.warn{{background:var(--warn-soft);color:var(--warn)}}
.pill.muted{{background:var(--surface2);color:var(--muted)}}
.foot{{color:var(--muted);font-size:11px;border-top:1px solid var(--line);padding-top:10px;
  margin-top:26px}}
.foot .warnline{{color:var(--warn);font-weight:700}}
@media print{{body{{background:#fff;padding:0}}.tbl,.body{{break-inside:avoid}}}}
"""

_H3_RE = re.compile(r"^#{3,6}\s+(.*)$")
_H2_RE = re.compile(r"^#{1,2}\s+(.*)$")
_UL_RE = re.compile(r"^[-*・]\s+(.*)$")
_OL_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _esc(s: str) -> str:
    return _html.escape(s or "", quote=True)


def _inline(escaped: str) -> str:
    """**強調** だけを許す。入力は escape 済みであること（生HTMLは通さない）。"""
    return _BOLD_RE.sub(r"<strong>\1</strong>", escaped)


def render_body(md: str) -> str:
    """LLM 本文（Markdown 風）を最小の HTML へ変換する。

    見出し・箇条書き・番号リスト・``**強調**`` のみ対応。**escape してから**変換するため、
    本文に ``<script>`` や ``<img onerror=...>`` が混じっても構造タグにはならない。
    """
    if not md or not md.strip():
        return ""
    out: list[str] = []
    list_tag: str | None = None
    para: list[str] = []

    def close_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in md.replace("\r\n", "\n").split("\n"):
        line = _esc(raw.strip())
        if not line:
            close_para()
            close_list()
            continue
        m3 = _H3_RE.match(line)
        m2 = None if m3 else _H2_RE.match(line)
        mul = _UL_RE.match(line)
        mol = None if mul else _OL_RE.match(line)
        if m3 or m2:
            close_para()
            close_list()
            text = _inline((m3 or m2).group(1))  # type: ignore[union-attr]
            out.append(f"<h3>{text}</h3>" if m3 else f"<h2>{text}</h2>")
        elif mul or mol:
            close_para()
            want = "ul" if mul else "ol"
            if list_tag != want:
                close_list()
                out.append(f"<{want}>")
                list_tag = want
            item = mul.group(1) if mul else mol.group(2)  # type: ignore[union-attr]
            out.append(f"<li>{_inline(item)}</li>")
        else:
            close_list()
            para.append(line)
    close_para()
    close_list()
    return "".join(out)


def _cell_html(cell: Cell, align: str) -> str:
    text = _esc(cell.text)
    if cell.tone in ("ok", "warn", "muted"):
        inner = f"<span class='pill {cell.tone}'>{text}</span>"
    elif cell.href:
        href = safe_href(cell.href)
        inner = (
            f"<a href='{_esc(href)}' target='_blank' rel='noopener'>{text}</a>" if href else text
        )
    else:
        inner = text
    if cell.bar is not None:
        # 最大 90px の横棒。値そのものは隣に出すので、棒は「大小の比較」だけを担う。
        px = max(0.0, min(1.0, cell.bar)) * 90
        inner = (
            f"<span class='barwrap'><span class='bar' style='width:{px:.1f}px'></span>"
            f"<span>{inner}</span></span>"
        )
    if cell.sub:
        inner += f"<span class='sub2'>{_esc(cell.sub)}</span>"
    klass = " class='r'" if align == "right" else ""
    return f"<td{klass}>{inner}</td>"


def _table_html(table: Table) -> str:
    if not table.rows:
        return ""
    head = "".join(
        f"<th class='r'>{_esc(c.label)}</th>" if c.align == "right" else f"<th>{_esc(c.label)}</th>"
        for c in table.columns
    )
    body: list[str] = []
    for row in table.rows:
        cells = "".join(
            _cell_html(cell, table.columns[i].align if i < len(table.columns) else "left")
            for i, cell in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    cap = f"<div class='cap'>{_esc(table.caption)}</div>" if table.caption else ""
    note = f"<div class='note'>{_esc(table.note)}</div>" if table.note else ""
    return (
        f"<div class='tbl'>{cap}{note}<div class='scroll'><table>"
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody>"
        f"</table></div></div>"
    )


def render_report(report: Report, *, now: _dt.datetime | None = None) -> str:
    """:class:`Report` を自己完結 HTML（1ファイル・外部参照なし）へ変換する。"""
    stamp = (now or _dt.datetime.now(_JST)).astimezone(_JST).strftime("%Y-%m-%d %H:%M")
    chips = "".join(
        f"<li>{_esc(c.label)}<b>{_esc(c.value)}</b></li>" for c in report.chips if c.value
    )
    chips_html = f"<ul class='chips'>{chips}</ul>" if chips else ""
    body = render_body(report.body_md)
    body_html = f"<div class='body'>{body}</div>" if body else ""
    tables = "".join(_table_html(t) for t in report.tables)
    lead = f"<p class='lead'>{_esc(report.headline)}</p>" if report.headline else ""
    sub = f"<p class='sub'>{_esc(report.subtitle)}</p>" if report.subtitle else ""
    src = f"{_esc(report.source_note)}<br>" if report.source_note else ""
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        f"<title>{_esc(report.title)}</title><style>{_CSS}</style></head><body><div class='wrap'>"
        f"<h1>{_esc(report.title)}</h1>{lead}{sub}{chips_html}{body_html}{tables}"
        f"<div class='foot'>{src}生成 {stamp} JST（TeamAgent）"
        "<br><span class='warnline'>社外共有不可。</span></div>"
        "</div></body></html>"
    )


__all__ = ["Cell", "Chip", "Column", "Report", "Table", "render_body", "render_report"]
