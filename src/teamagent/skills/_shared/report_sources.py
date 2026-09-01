"""「過去提案/FB を根拠に使った」系 skill 共通の出典テーブル生成。

proposal_draft / proposal_review の ``sources`` は別クラスだが同じ形（chunk_id・source_type・
title・client_name・preview）なので、表の組み方をここに 1 本だけ置く。片方だけ列が増える/減る
といったズレを構造的に防ぐ。duck typing で受けるのは、両者に共通の基底クラスが無いため。
"""

from __future__ import annotations

from typing import Any

from teamagent.skills._html.report import Cell, Column, Table

_PREVIEW_MAX = 120


def _clip(text: str, limit: int = _PREVIEW_MAX) -> str:
    body = " ".join((text or "").split())
    return body[:limit] + "…" if len(body) > limit else body


def sources_table(sources: list[Any], *, caption: str = "根拠にした過去提案・FB") -> Table:
    """``sources`` を出典テーブルへ。空リストならレンダラ側で描画ごと省かれる。"""
    rows: list[list[Cell]] = []
    for s in sources:
        title = getattr(s, "title", None) or f"chunk #{getattr(s, 'chunk_id', '')}"
        rows.append(
            [
                Cell(str(title)),
                Cell(str(getattr(s, "client_name", None) or "—")),
                Cell(str(getattr(s, "source_type", None) or "—")),
                Cell(_clip(str(getattr(s, "preview", "") or ""))),
            ]
        )
    return Table(
        columns=[
            Column("資料"),
            Column("クライアント"),
            Column("種別"),
            Column("抜粋"),
        ],
        rows=rows,
        caption=caption,
        note="社内ナレッジの検索結果。抜粋は本文冒頭のみ。",
    )


__all__ = ["sources_table"]
