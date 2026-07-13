"""コメント欄マイニングの HTML レンダラ（分布バー＋カテゴリ別代表コメント＋語彙）。

ライトテーマ固定・JSなし（x_research/report.py と同方針）。
"""

from __future__ import annotations

import datetime as _dt
import html as _html

from teamagent.skills._html.theme import FONT_STACK_JP
from teamagent.skills._shared.text_safety import safe_href
from teamagent.skills.tiktok_comment_mining.schema import VideoCommentInsight

_CSS = f"""
body{{font-family:{FONT_STACK_JP};background:#f6f7f9;color:#1b1f24;margin:0;padding:24px}}
.wrap{{max-width:860px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}
h2{{font-size:15px;margin:24px 0 8px;word-break:break-all}}
.meta{{color:#5b6570;font-size:12px;margin-bottom:14px}}
.dist{{margin:8px 0}}
.row{{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}}
.label{{width:110px;text-align:right;color:#31405a;white-space:nowrap}}
.bar{{height:14px;background:#1d6fdc;border-radius:3px;min-width:2px}}
.cnt{{color:#5b6570}}
.ex{{background:#fff;border:1px solid #e3e7ec;border-radius:8px;padding:8px 12px;
  margin:4px 0 4px 118px;font-size:13px;line-height:1.6;white-space:pre-wrap}}
.vocab{{background:#eef4fb;border:1px solid #d4e2f4;border-radius:10px;padding:12px 16px;
  margin:14px 0;font-size:13px;line-height:1.9}}
.chip{{display:inline-block;background:#fff;border:1px solid #c9d8ec;border-radius:12px;
  padding:2px 10px;margin:2px}}
.axes{{font-size:12px;color:#31405a;margin:8px 0;line-height:1.8}}
.footer{{color:#8a939c;font-size:11px;margin-top:22px;border-top:1px solid #e3e7ec;
  padding-top:8px}}
a{{color:#1d6fdc;text-decoration:none}}
"""


def _esc(s: str) -> str:
    return _html.escape(str(s), quote=True)


def _video_section(ins: VideoCommentInsight) -> str:
    _href = safe_href(ins.video_url)
    _title = f"<a href='{_esc(_href)}'>{_esc(ins.video_url)}</a>" if _href else _esc(ins.video_url)
    parts = [f"<h2>{_title}</h2>"]
    parts.append(
        f"<div class='meta'>コメント {ins.total_comments}件・"
        f"全体トーン: {_esc(ins.overall_sentiment or '不明')}・取得経路: {_esc(ins.source)}</div>"
    )
    if ins.buckets:
        max_c = max((b.count for b in ins.buckets), default=1) or 1
        parts.append("<div class='dist'>")
        for b in sorted(ins.buckets, key=lambda x: x.count, reverse=True):
            width = max(2, int(b.count / max_c * 380))
            parts.append(
                f"<div class='row'><span class='label'>{_esc(b.category)}</span>"
                f"<div class='bar' style='width:{width}px'></div>"
                f"<span class='cnt'>{b.count}件</span></div>"
            )
            for ex in b.examples:
                parts.append(f"<div class='ex'>{_esc(ex)}</div>")
        parts.append("</div>")
    axes: list[tuple[str, list[str]]] = [
        ("よくある質問", ins.common_questions),
        ("不満・ペイン", ins.pain_points),
        ("願望", ins.desires),
        ("購入シグナル", ins.purchase_signals),
        ("テーマ", ins.key_themes),
    ]
    ax_html = "".join(
        f"<b>{_esc(label)}:</b> {_esc('／'.join(items))}<br>" for label, items in axes if items
    )
    if ax_html:
        parts.append(f"<div class='axes'>{ax_html}</div>")
    return "".join(parts)


def render_comment_report(
    *,
    videos: list[VideoCommentInsight],
    cross_vocabulary: list[str],
    client_name: str | None,
) -> str:
    today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    total = sum(v.total_comments for v in videos)
    parts: list[str] = []
    if cross_vocabulary:
        chips = "".join(f"<span class='chip'>{_esc(w)}</span>" for w in cross_vocabulary[:20])
        parts.append(
            f"<div class='vocab'>🗣️ <b>生活者の語彙（広告文言の元ネタ）</b><br>{chips}</div>"
        )
    parts.extend(_video_section(v) for v in videos)
    client = f"クライアント: {client_name}・" if client_name else ""
    return (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>コメント欄マイニング</title><style>{_CSS}</style></head><body>"
        f"<div class='wrap'><h1>コメント欄マイニング</h1>"
        f"<div class='meta'>{_esc(client)}{len(videos)}動画・計{total}コメント・実測 {today}</div>"
        f"{''.join(parts)}"
        "<div class='footer'>代表コメントは原文のまま掲載（創作・要約なし）。"
        "コメントの少ない動画では取得0件になる場合があります。</div></div></body></html>"
    )


__all__ = ["render_comment_report"]
