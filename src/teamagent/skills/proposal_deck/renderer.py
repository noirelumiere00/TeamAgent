"""python-pptx 経由のテンプレート流し込みレンダラ（proposal_deck Skill 専用）。

teamagent_consulting の rendering/pptx_renderer.py + agents/slidewriter.py を移植。

placeholder 表記: 全角「｛N：ラベル｝」「｛N｝」/ 半角「{N:ラベル}」「{N}」（N は十進整数）。

**段落跨ぎ対応**: 実テンプレ（界隈マトリクス）では 1 placeholder が複数段落 (<a:p>) に
分割される。テキストフレーム単位で全段落を連結して置換・監査し、書き戻し時は段落構造を保つ
（マッチ開始段落に値を入れ、消費された段落は空にする）。書式損失は許容（自動生成テキスト主役）。
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teamagent.skills.proposal_deck.contract import VALID_IDS, ComposerOutput, EvidenceImage

# 全角・半角両対応。{N:ラベル} の `N` だけをキャプチャ。`[^｝}]*` は改行も含むため
# テキストフレーム単位で連結すれば段落跨ぎトークンも 1 マッチになる。
PLACEHOLDER_PATTERN = re.compile(r"[｛{]\s*(\d+)\s*[:：]?[^｝}]*[｝}]")

# python-pptx の GROUP shape type 値（MSO_SHAPE_TYPE.GROUP == 6）。
_GROUP_SHAPE_TYPE = 6

# template_v2.pptx の {58-92} マトリクス最下段サムネ空枠（EMU・1inch=914400）。
# 0.67"角(=609905)・y≈5.98"(=5454720)。座標一致の許容誤差 ±80000 EMU。
# NOTE: テンプレ固有値。別テンプレ採用時はスロット仕様の拡張が必要（座標未一致なら静かに skip）。
_PICTURE_SHAPE_TYPE = 13  # MSO_SHAPE_TYPE.PICTURE（既存スロット除外用）
_SLOT_TOP_EMU = 5454720
_SLOT_SIZE_EMU = 609905
_COORD_TOL_EMU = 80000


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


def _resolve_evidence_image_bytes(img: EvidenceImage) -> bytes | None:
    """EvidenceImage を画像バイトに解決。image_path のみ参照（httpx/fetch 非依存）。

    image_path が無い／読めない／source_url のみ → None（graceful skip）。
    フィーダ（Phase4）が正規化済み JPEG を image_path に用意する前提。
    """
    if not img.image_path:
        return None
    try:
        data = Path(img.image_path).read_bytes()
    except OSError:
        return None
    return data or None


def _iter_image_slots(prs: Any) -> Iterator[tuple[Any, Any]]:
    """全 slide から「空サムネ枠」候補を (slide, shape) で yield（group 再帰）。

    座標(top≈_SLOT_TOP_EMU)・サイズ(width≈_SLOT_SIZE_EMU)が許容誤差内の shape を
    候補とし、PICTURE は除外。slide ごとに left 昇順で返す。
    """

    def walk(shapes: Any) -> Iterator[Any]:
        for shape in shapes:
            if getattr(shape, "shape_type", None) == _GROUP_SHAPE_TYPE:
                yield from walk(shape.shapes)
                continue
            yield shape

    for slide in prs.slides:
        candidates: list[Any] = []
        for shape in walk(slide.shapes):
            if getattr(shape, "shape_type", None) == _PICTURE_SHAPE_TYPE:
                continue
            try:
                w = int(shape.width)
                t = int(shape.top)
            except (TypeError, ValueError):
                continue
            if (
                abs(w - _SLOT_SIZE_EMU) <= _COORD_TOL_EMU
                and abs(t - _SLOT_TOP_EMU) <= _COORD_TOL_EMU
            ):
                candidates.append(shape)
        for shape in sorted(candidates, key=lambda s: int(s.left)):
            yield slide, shape


def _add_picture_fit(slide: Any, img_bytes: bytes, shape: Any) -> None:
    """空枠の高さに等比で合わせて add_picture（歪み無し）。左上は枠に合わせる。"""
    from pptx.util import Emu

    left, top, box_h = int(shape.left), int(shape.top), int(shape.height)
    pic = slide.shapes.add_picture(io.BytesIO(img_bytes), Emu(left), Emu(top))
    if pic.width and pic.height:
        scale = box_h / pic.height
        pic.height = Emu(int(box_h))
        pic.width = Emu(int(pic.width * scale))
        pic.left = Emu(left)
        pic.top = Emu(top)


def _inject_evidence_images(prs: Any, evidence_images: dict[int, list[EvidenceImage]]) -> int:
    """evidence_images を placeholder_id 昇順→rank 昇順で空枠へ順に add_picture。

    image_path を解決できない画像はスキップ。空枠が尽きたら以降は捨てる
    （pptx はテキスト主役・graceful by design）。注入した枚数を返す。
    add_picture が画像形式を認識できない（壊れたファイル等）場合もスキップする。
    """
    from PIL import UnidentifiedImageError  # python-pptx は Pillow backend

    ordered: list[EvidenceImage] = []
    for pid in sorted(evidence_images):
        for img in sorted(evidence_images[pid], key=lambda e: e.rank):
            ordered.append(img)
    if not ordered:
        return 0

    slots = _iter_image_slots(prs)
    injected = 0
    for img in ordered:
        data = _resolve_evidence_image_bytes(img)
        if data is None:
            continue
        try:
            slide, shape = next(slots)
        except StopIteration:
            break
        try:
            _add_picture_fit(slide, data, shape)
        except UnidentifiedImageError:
            continue
        injected += 1
    return injected


def render_pptx(
    template_path: Path,
    placeholders: dict[int, str],
    out_path: Path,
    *,
    fail_if_missing: bool = True,
    evidence_images: dict[int, list[EvidenceImage]] | None = None,
) -> Path:
    """テンプレ pptx を読み、placeholder を全置換して out_path に保存。

    evidence_images（任意）が渡されたら、テキスト置換・audit 後・保存前に空枠へ
    画像を add_picture する（image_path のみ解決・httpx 非依存）。
    """
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

    if evidence_images:
        _inject_evidence_images(prs, evidence_images)

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
    enable_images: bool = False,
) -> Path:
    """ComposerOutput を pptx に流し込み、ファイルパスを返す。

    enable_images=True のとき composer_out.evidence_images を空枠へ画像注入する
    （既定 False＝従来どおりテキストのみ・後方互換）。
    """
    placeholders = materialize_placeholders(composer_out)
    return render_pptx(
        template_path,
        placeholders,
        out_path,
        fail_if_missing=fail_if_missing,
        evidence_images=composer_out.evidence_images if enable_images else None,
    )


__all__ = [
    "PLACEHOLDER_PATTERN",
    "AuditResult",
    "UnfilledPlaceholderError",
    "audit_presentation",
    "materialize_placeholders",
    "render_deck",
    "render_pptx",
]
