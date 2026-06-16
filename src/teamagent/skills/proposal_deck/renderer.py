"""python-pptx 経由のテンプレート流し込みレンダラ（proposal_deck Skill 専用）。

teamagent_consulting の rendering/pptx_renderer.py + agents/slidewriter.py を移植。

placeholder 表記: 全角「｛N：ラベル｝」「｛N｝」/ 半角「{N:ラベル}」「{N}」（N は十進整数）。

**段落跨ぎ対応**: 実テンプレ（界隈マトリクス）では 1 placeholder が複数段落 (<a:p>) に
分割される。テキストフレーム単位で全段落を連結して置換・監査し、書き戻し時は段落構造を保つ
（マッチ開始段落に値を入れ、消費された段落は空にする）。書式損失は許容（自動生成テキスト主役）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teamagent.skills.proposal_deck.contract import VALID_IDS, ComposerOutput

# 全角・半角両対応。{N:ラベル} の `N` だけをキャプチャ。`[^｝}]*` は改行も含むため
# テキストフレーム単位で連結すれば段落跨ぎトークンも 1 マッチになる。
PLACEHOLDER_PATTERN = re.compile(r"[｛{]\s*(\d+)\s*[:：]?[^｝}]*[｝}]")

# python-pptx の GROUP shape type 値（MSO_SHAPE_TYPE.GROUP == 6）。
_GROUP_SHAPE_TYPE = 6


class UnfilledPlaceholderError(RuntimeError):
    """fail_if_missing=True で render_pptx を呼んで残存 placeholder があった。"""


@dataclass(slots=True)
class AuditResult:
    """audit_presentation の戻り値。"""

    filled_ids: frozenset[int]
    unfilled_ids: list[int]
    extra_braces: list[str]

    @property
    def is_clean(self) -> bool:
        return not self.unfilled_ids and not self.extra_braces


def _iter_text_frames(prs: Any) -> Iterator[Any]:
    """全 slide の全 shape（group / table 内も再帰）からテキストフレームを yield。"""

    def walk(shapes: Any) -> Iterator[Any]:
        for shape in shapes:
            if getattr(shape, "shape_type", None) == _GROUP_SHAPE_TYPE:
                yield from walk(shape.shapes)
                continue
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        yield cell.text_frame
                continue
            if getattr(shape, "has_text_frame", False):
                yield shape.text_frame

    for slide in prs.slides:
        yield from walk(slide.shapes)


def _emit_range(
    cat: str, a: int, b: int, bounds: list[tuple[int, int]], pieces: list[list[str]]
) -> None:
    """連結文字列 cat[a:b] を、出現元の段落ごとに pieces へ振り分ける。"""
    if a >= b:
        return
    for i, (s, e) in enumerate(bounds):
        lo = max(a, s)
        hi = min(b, e)
        if lo < hi:
            pieces[i].append(cat[lo:hi])


def _set_paragraph_text(paragraph: Any, text: str) -> None:
    """段落の text を再設定。書式は先頭 run のものに統一。"""
    runs = list(paragraph.runs)
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r.text = ""
    elif text:
        paragraph.add_run().text = text


def _replace_in_text_frame(text_frame: Any, placeholders: dict[int, str]) -> set[int]:
    """1 text frame の全段落を連結 → placeholder 置換 → 段落構造を保って書き戻し。"""
    paragraphs = list(text_frame.paragraphs)
    texts = [p.text for p in paragraphs]
    bounds: list[tuple[int, int]] = []
    pos = 0
    for t in texts:
        bounds.append((pos, pos + len(t)))
        pos += len(t)
    cat = "".join(texts)
    if not cat:
        return set()

    matches = [m for m in PLACEHOLDER_PATTERN.finditer(cat) if int(m.group(1)) in placeholders]
    if not matches:
        return set()

    def para_of(p: int) -> int:
        for i, (s, e) in enumerate(bounds):
            if s <= p < e:
                return i
        return len(bounds) - 1

    filled: set[int] = set()
    pieces: list[list[str]] = [[] for _ in texts]
    cursor = 0
    for m in matches:
        _emit_range(cat, cursor, m.start(), bounds, pieces)
        pid = int(m.group(1))
        pieces[para_of(m.start())].append(placeholders[pid])
        filled.add(pid)
        cursor = m.end()
    _emit_range(cat, cursor, len(cat), bounds, pieces)

    new_texts = ["".join(pp) for pp in pieces]
    for i, paragraph in enumerate(paragraphs):
        if new_texts[i] != texts[i]:
            _set_paragraph_text(paragraph, new_texts[i])
    return filled


def render_pptx(
    template_path: Path,
    placeholders: dict[int, str],
    out_path: Path,
    *,
    fail_if_missing: bool = True,
) -> Path:
    """テンプレ pptx を読み、placeholder を全置換して out_path に保存。"""
    from pptx import Presentation

    prs = Presentation(str(template_path))
    filled: set[int] = set()
    for tf in _iter_text_frames(prs):
        filled |= _replace_in_text_frame(tf, placeholders)

    audit = audit_presentation(prs, expected_filled_ids=set(placeholders))
    if fail_if_missing and not audit.is_clean:
        raise UnfilledPlaceholderError(
            f"unfilled placeholders remain: {audit.unfilled_ids[:20]} "
            f"(samples: {audit.extra_braces[:5]})"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return out_path


def audit_presentation(prs: Any, *, expected_filled_ids: set[int] | None = None) -> AuditResult:
    """全 slide を走査し、残存 `{N}` を検出する（テキストフレーム単位 = 段落跨ぎ対応）。"""
    unfilled: set[int] = set()
    extra: list[str] = []
    for tf in _iter_text_frames(prs):
        cat = "".join(p.text for p in tf.paragraphs)
        for m in PLACEHOLDER_PATTERN.finditer(cat):
            unfilled.add(int(m.group(1)))
            if len(extra) < 20:
                extra.append(m.group(0))
    filled = (expected_filled_ids or set()) - unfilled
    return AuditResult(
        filled_ids=frozenset(filled), unfilled_ids=sorted(unfilled), extra_braces=extra
    )


def materialize_placeholders(out: ComposerOutput) -> dict[int, str]:
    """skipped も『要確認（データ未検出）』で実体化し、全 VALID_IDS をカバーする。

    renderer 段階では unfilled を残さないため、未検出 placeholder にも必ず文字列を割り当てる。
    """
    materialized = dict(out.placeholders)
    for skip in out.skipped_placeholders:
        materialized.setdefault(skip.id, "要確認（データ未検出）")
    for pid in VALID_IDS:
        materialized.setdefault(pid, "要確認（データ未検出）")
    return materialized


def render_deck(
    composer_out: ComposerOutput,
    template_path: Path,
    out_path: Path,
    *,
    fail_if_missing: bool = True,
) -> Path:
    """ComposerOutput を pptx に流し込み、ファイルパスを返す。"""
    placeholders = materialize_placeholders(composer_out)
    return render_pptx(template_path, placeholders, out_path, fail_if_missing=fail_if_missing)


__all__ = [
    "PLACEHOLDER_PATTERN",
    "AuditResult",
    "UnfilledPlaceholderError",
    "audit_presentation",
    "materialize_placeholders",
    "render_deck",
    "render_pptx",
]
