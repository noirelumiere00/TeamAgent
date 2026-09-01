"""固定フォーマットの LLM 本文を、見出し単位のブロックへ割る（純粋関数）。

対象は「出力フォーマット（順序固定）」を system prompt で強制している skill の本文。
``### 1. サマリ`` のような見出しで切り、``(見出し, 中身)`` の並びを返す。

**構造を拾うだけで、解釈は一切足さない。** 見出しが 1 つも見つからなければ空を返し、
呼び出し側は従来どおり本文をひと続きで流す（フォーマットが変わっても壊れない）。
"""

from __future__ import annotations

import re

# "### 1. サマリ" / "## サマリ" / "#### 2) フック" などを見出しとして扱う。
_HEADING_RE = re.compile(r"^#{2,6}\s*(?:\d+\s*[.)]\s*)?(.+?)\s*$")


def split_sections(md: str) -> list[tuple[str, str]]:
    """本文を ``[(見出し, 中身), ...]`` へ。見出しが無ければ空リスト。"""
    if not md or not md.strip():
        return []
    sections: list[tuple[str, list[str]]] = []
    for raw in md.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        heading = _HEADING_RE.match(line.strip()) if line.strip().startswith("#") else None
        if heading:
            sections.append((heading.group(1), []))
        elif sections:
            sections[-1][1].append(line)
    return [(title, "\n".join(body).strip()) for title, body in sections]


__all__ = ["split_sections"]
