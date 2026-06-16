"""提案書 ComposerOutput → PDF（weasyprint で HTML→PDF）。

PPTX が正本だが、Slack/メールで素早く共有・レビューするための **PDF コンパニオン**を作る。
weasyprint は HTML/CSS→PDF 専用（PPTX→PDF ではない）なので、ここでは ComposerOutput の
埋まった placeholder を読みやすい HTML に流し込んでから PDF 化する。

設計:
- ``build_proposal_html`` は **純関数**（weasyprint 非依存・テスト可）。
- ``_html_to_pdf`` だけが weasyprint を遅延 import（重い C 依存・CI 非導入）。
- 失敗は呼び出し側で握って PDF 無し（None）に落とす＝Skill 全体は成功扱い。
"""

from __future__ import annotations

import html
from pathlib import Path

from teamagent.skills.proposal_deck.contract import VALID_IDS, ComposerOutput

_CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: 'Hiragino Sans', 'Noto Sans JP', sans-serif; font-size: 10.5pt;
       color: #1a1a1a; line-height: 1.6; }
h1 { font-size: 18pt; margin: 0 0 4pt; }
.meta { color: #666; font-size: 9pt; margin-bottom: 16pt; }
.item { margin-bottom: 10pt; page-break-inside: avoid; }
.pid { color: #0a6; font-weight: bold; font-size: 9pt; }
.cite { color: #888; font-size: 8.5pt; }
.skipped { color: #b00; }
hr { border: none; border-top: 1px solid #ddd; margin: 12pt 0; }
"""


def build_proposal_html(
    composer_out: ComposerOutput,
    *,
    product_name: str,
    version_id: str,
) -> str:
    """ComposerOutput を読みやすい HTML 文字列にする（純関数）。"""
    rows: list[str] = []
    for pid in sorted(VALID_IDS):
        text = composer_out.placeholders.get(pid)
        if text is None:
            continue
        cites = composer_out.citations_per_placeholder.get(pid) or []
        cite_html = ""
        if cites:
            joined = " / ".join(html.escape(c) for c in cites)
            cite_html = f'<div class="cite">出典: {joined}</div>'
        rows.append(
            f'<div class="item"><span class="pid">{{{pid}}}</span> '
            f"{html.escape(text)}{cite_html}</div>"
        )
    skipped_rows = "".join(
        f'<div class="item skipped"><span class="pid">{{{s.id}}}</span> '
        f"{html.escape(s.reason)}</div>"
        for s in sorted(composer_out.skipped_placeholders, key=lambda s: s.id)
    )
    skipped_block = f"<hr><h1>要確認（データ未検出）</h1>{skipped_rows}" if skipped_rows else ""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{html.escape(product_name)} 提案書</h1>"
        f'<div class="meta">version: {html.escape(version_id)}'
        f"（埋め {len(composer_out.placeholders)} / {len(VALID_IDS)} 項目）</div>"
        f"{''.join(rows)}{skipped_block}"
        "</body></html>"
    )


def _html_to_pdf(html_str: str, out_path: Path) -> None:
    """HTML 文字列を PDF に書き出す（weasyprint を遅延 import）。"""
    from weasyprint import HTML

    HTML(string=html_str).write_pdf(str(out_path))


def render_proposal_pdf(
    composer_out: ComposerOutput,
    *,
    product_name: str,
    version_id: str,
    out_path: Path,
) -> Path:
    """ComposerOutput → PDF を out_path に書き出してそのパスを返す。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_str = build_proposal_html(composer_out, product_name=product_name, version_id=version_id)
    _html_to_pdf(html_str, out_path)
    return out_path
