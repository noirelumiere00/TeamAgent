"""手元/CI 専用の描画 QA。本番 core からは import しない。

座標は左上原点の pt。判定関数は描画器やファイルに依存しない。
"""

from __future__ import annotations

import io
import json
import math
import posixpath
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

from teamagent.media.deck_qa import Affine, _body_properties, _dimensions, _inherited_style, _walk

MAX_UNCOMPRESSED = 1 << 30
MAX_ENTRIES = 10_000
MAX_RATIO = 100
COLOR_STEP = 8
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# 完全一致の許可リスト。危険な既知部品は検証後に除去する。
CONTENT_TYPES = frozenset(
    {
        "application/xml",
        "application/vnd.openxmlformats-package.relationships+xml",
        "application/vnd.openxmlformats-package.core-properties+xml",
        "application/vnd.openxmlformats-officedocument.extended-properties+xml",
        "application/vnd.openxmlformats-officedocument.custom-properties+xml",
        "application/vnd.openxmlformats-officedocument.oleObject",
        "application/vnd.ms-office.activeX+xml",
        "application/vnd.ms-office.activeX",
        "application/vnd.ms-office.vbaProject",
        "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.printerSettings",
        "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.handoutMaster+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presProps+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml",
        "application/vnd.openxmlformats-officedocument.theme+xml",
        "application/vnd.openxmlformats-officedocument.themeOverride+xml",
        "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
        "application/vnd.openxmlformats-officedocument.drawingml.chartshapes+xml",
        "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
        "application/vnd.openxmlformats-officedocument.drawingml.diagramLayout+xml",
        "application/vnd.openxmlformats-officedocument.drawingml.diagramStyle+xml",
        "application/vnd.openxmlformats-officedocument.drawingml.diagramColors+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.comments+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.commentAuthors+xml",
        "application/x-fontdata",
        "application/vnd.openxmlformats-officedocument.obfuscatedFont",
        "application/octet-stream",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/bmp",
        "image/tiff",
        "image/svg+xml",
        "image/x-emf",
        "image/x-wmf",
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "video/mp4",
        "video/x-msvideo",
    }
)


def _parse(data: bytes) -> Any:
    from lxml import etree  # type: ignore[import-untyped]

    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("xml_doctype")
    tree = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    if tree.getroottree().docinfo.doctype:
        raise ValueError("xml_doctype")
    return tree


def _serialize(tree: Any) -> bytes:
    from lxml import etree

    return bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))


def sanitize_pptx(source: Path, destination: Path) -> dict[str, int]:
    """展開前の ZIP 検査、危険部品・参照除去、bitmap 再生成。子プロセス専用。"""
    from PIL import Image

    counts: Counter[str] = Counter()
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_UNCOMPRESSED:
            raise ValueError("zip_limit")
        names: set[str] = set()
        for entry in entries:
            name = entry.filename
            if (
                name.startswith("/")
                or "\\" in name
                or ":" in name
                or ".." in name.split("/")
                or name in names
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
                or entry.flag_bits & 1
            ):
                raise ValueError("zip_path")
            if entry.file_size > max(1, entry.compress_size) * MAX_RATIO:
                raise ValueError("zip_ratio")
            names.add(name)
        types = _parse(archive.read("[Content_Types].xml"))
        defaults = {
            n.get("Extension", "").lower(): n.get("ContentType")
            for n in types
            if n.tag.endswith("Default")
        }
        overrides = {
            n.get("PartName", "").lstrip("/"): n.get("ContentType")
            for n in types
            if n.tag.endswith("Override")
        }
        if any(n.get("ContentType") not in CONTENT_TYPES for n in types):
            raise ValueError("content_type")
        parts: dict[str, bytes] = {}
        removed: set[str] = set()
        for entry in entries:
            name = entry.filename
            if entry.is_dir() or name == "[Content_Types].xml":
                continue
            content_type = overrides.get(name, defaults.get(name.rsplit(".", 1)[-1].lower()))
            if content_type not in CONTENT_TYPES:
                raise ValueError("content_type")
            category = ""
            if name.startswith("ppt/embeddings/") or content_type == (
                "application/vnd.openxmlformats-officedocument.oleObject"
            ):
                category = "embeddings"
            elif name.startswith("ppt/activeX/") or content_type in {
                "application/vnd.ms-office.activeX",
                "application/vnd.ms-office.activeX+xml",
            }:
                category = "activex"
            elif name.lower().endswith("vbaproject.bin") or content_type == (
                "application/vnd.ms-office.vbaProject"
            ):
                category = "vba"
            elif name.startswith("ppt/fonts/") or content_type in {
                "application/x-fontdata",
                "application/vnd.openxmlformats-officedocument.obfuscatedFont",
            }:
                category = "fonts"
            elif name.startswith("ppt/media/") or str(content_type).startswith(
                ("image/", "audio/", "video/")
            ):
                if content_type not in {"image/png", "image/jpeg"}:
                    category = "media"
            if category:
                removed.add(name)
                counts[category] += 1
                continue
            data = archive.read(name)
            if content_type in {"image/png", "image/jpeg"}:
                with Image.open(io.BytesIO(data)) as image:
                    if (
                        image.format not in {"PNG", "JPEG"}
                        or image.width * image.height > 25_000_000
                    ):
                        raise ValueError("image_limit")
                    image.load()
                    buffer = io.BytesIO()
                    mode = "RGB" if content_type == "image/jpeg" else "RGBA"
                    clean = Image.new(mode, image.size)
                    clean.paste(image.convert(mode))
                    clean.save(buffer, format="JPEG" if content_type == "image/jpeg" else "PNG")
                    data = buffer.getvalue()
                    counts["images_reencoded"] += 1
            if name.endswith((".xml", ".rels")):
                data = _serialize(_parse(data))
            parts[name] = data
        # リレーションだけでなく owner XML の r:id / r:embed / r:link も除く。
        for name in list(parts):
            if not name.endswith(".rels"):
                continue
            tree = _parse(parts[name])
            owner = posixpath.join(
                posixpath.dirname(posixpath.dirname(name)), posixpath.basename(name)[:-5]
            )
            removed_ids: set[str] = set()
            for rel in list(tree):
                target = rel.get("Target", "")
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(owner), target)
                ).lstrip("/")
                external = rel.get("TargetMode") == "External"
                if external or resolved in removed:
                    removed_ids.add(rel.get("Id", ""))
                    tree.remove(rel)
                    counts["external" if external else "removed_relationships"] += 1
                elif resolved not in parts:
                    raise ValueError("relationship_target")
            parts[name] = _serialize(tree)
            if removed_ids and owner in parts:
                owner_tree = _parse(parts[owner])
                for node in list(owner_tree.iter()):
                    if any(
                        node.get(f"{{{NS_R}}}{key}") in removed_ids
                        for key in ("id", "embed", "link")
                    ):
                        parent = node.getparent()
                        if parent is not None:
                            parent.remove(node)
                parts[owner] = _serialize(owner_tree)
        for node in list(types):
            if node.get("PartName", "").lstrip("/") in removed:
                types.remove(node)
            elif (
                node.get("ContentType")
                == "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
            ):
                node.set(
                    "ContentType",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
                    "application/vnd.openxmlformats-officedocument.presentationml.printerSettings",
                )
        parts["[Content_Types].xml"] = _serialize(types)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as output:
            for name, data in parts.items():
                output.writestr(name, data)
    return dict(counts)


Rect = tuple[float, float, float, float]
Color = tuple[int, int, int]


@dataclass
class ShapeBox:
    slide: int
    key: str
    name: str
    rect: Rect
    inner: Rect
    color: Color | None = None
    has_text: bool = False
    picture: bool = False
    autofit: str = ""
    rotation: float = 0
    expected_fonts: tuple[str, ...] = ()


@dataclass
class Glyph:
    slide: int
    rect: Rect
    baseline: float
    size: float
    color: Color
    font: str = ""
    # 本文は判定入力のみ。結果へのシリアライズは禁止。
    text: str = field(default="", repr=False)


def probe_color(index: int) -> Color:
    levels = 256 // COLOR_STEP
    if index >= levels**3:
        raise ValueError("probe_palette_exhausted")
    return (
        (index // levels**2) * COLOR_STEP,
        ((index // levels) % levels) * COLOR_STEP,
        (index % levels) * COLOR_STEP,
    )


def prepare_probe(source: Path, destination: Path) -> tuple[list[ShapeBox], float, float]:
    from pptx import Presentation
    from pptx.oxml.xmlchemy import OxmlElement

    deck = Presentation(str(source))
    boxes: list[ShapeBox] = []
    for number, slide in enumerate(deck.slides, 1):
        index = 1
        walked = _walk(slide.shapes, slide=slide)
        if not walked:
            boxes.append(ShapeBox(number, "__empty_slide__", "", (0, 0, 0, 0), (0, 0, 0, 0)))
        for shape, rect_emu, transform in walked:
            _, _, width, height, rotation = _dimensions(shape, slide)
            rotated = rotation or math.degrees(math.atan2(transform[1], transform[0]))
            frames: list[tuple[Any, float, float, float, float, str]] = []
            if shape.has_table:
                top = 0.0
                for row_index, row in enumerate(shape.table.rows):
                    left = 0.0
                    for col_index, column in enumerate(shape.table.columns):
                        cell = shape.table.cell(row_index, col_index)
                        if not cell.is_spanned:
                            cw = sum(
                                shape.table.columns[c].width
                                for c in range(col_index, col_index + cell.span_width)
                            )
                            ch = sum(
                                shape.table.rows[r].height
                                for r in range(row_index, row_index + cell.span_height)
                            )
                            frames.append(
                                (cell.text_frame, left, top, cw, ch, f"{row_index}:{col_index}")
                            )
                        left += column.width
                    top += row.height
            elif shape.has_text_frame:
                frames.append((shape.text_frame, 0, 0, width, height, ""))
            if not frames:
                rect = cast(Rect, tuple(v / 12700 for v in rect_emu))
                boxes.append(
                    ShapeBox(
                        number,
                        str(shape.shape_id),
                        shape.name[:40],
                        rect,
                        rect,
                        picture=bool(shape._element.xpath(".//a:blip")),
                    )
                )
            if frames:
                for effect in shape._element.xpath("./p:spPr/a:effectLst | ./p:spPr/a:effectDag"):
                    effect.getparent().remove(effect)
            for frame, x, y, w, h, suffix in frames:

                def bounds(
                    left: float,
                    top: float,
                    right: float,
                    bottom: float,
                    transform: Affine = transform,
                ) -> Rect:
                    points = [
                        (
                            transform[0] * u + transform[2] * v + transform[4],
                            transform[1] * u + transform[3] * v + transform[5],
                        )
                        for u, v in ((left, top), (right, top), (left, bottom), (right, bottom))
                    ]
                    return (
                        min(p[0] for p in points) / 12700,
                        min(p[1] for p in points) / 12700,
                        max(p[0] for p in points) / 12700,
                        max(p[1] for p in points) / 12700,
                    )

                attrs, autofit, _ = _body_properties(frame, shape, slide)
                rect = bounds(x, y, x + w, y + h)
                inner = bounds(
                    x + int(attrs.get("lIns", "91440")),
                    y + int(attrs.get("tIns", "45720")),
                    x + w - int(attrs.get("rIns", "91440")),
                    y + h - int(attrs.get("bIns", "45720")),
                )
                color = probe_color(index)
                index += 1
                fonts = tuple(
                    sorted(
                        {
                            n.get("typeface", "")
                            for n in frame._txBody.xpath(".//a:latin | .//a:ea")
                            if n.get("typeface")
                        }
                    )
                )
                _, inherited_font = _inherited_style(shape, slide)
                fonts = tuple(sorted({*fonts, inherited_font} - {""}))
                # run/field/default/end properties all override inherited paint, never geometry.
                nodes = list(frame._txBody.xpath(".//a:rPr | .//a:defRPr | .//a:endParaRPr"))
                for run in frame._txBody.xpath(".//a:r | .//a:fld"):
                    if not run.xpath("./a:rPr"):
                        props = OxmlElement("a:rPr")
                        run.insert(0, props)
                        nodes.append(props)
                for paragraph in frame.paragraphs:
                    ppr = paragraph._p.get_or_add_pPr()
                    if not ppr.xpath("./a:defRPr"):
                        props = OxmlElement("a:defRPr")
                        ppr.append(props)
                        nodes.append(props)
                for props in nodes:
                    for child in list(props):
                        if child.tag.rsplit("}", 1)[-1] in {
                            "solidFill",
                            "gradFill",
                            "pattFill",
                            "blipFill",
                            "noFill",
                            "grpFill",
                            "ln",
                            "effectLst",
                            "effectDag",
                            "highlight",
                        }:
                            props.remove(child)
                    fill = OxmlElement("a:solidFill")
                    rgb = OxmlElement("a:srgbClr")
                    rgb.set("val", "".join(f"{v:02X}" for v in color))
                    fill.append(rgb)
                    props.insert(0, fill)
                    outline = OxmlElement("a:ln")
                    outline.append(OxmlElement("a:noFill"))
                    props.insert(0, outline)
                    props.insert(2, OxmlElement("a:effectLst"))
                boxes.append(
                    ShapeBox(
                        number,
                        f"{shape.shape_id}:{suffix}",
                        shape.name[:40],
                        rect,
                        inner,
                        color,
                        bool(frame.text.strip()),
                        autofit=autofit,
                        rotation=rotated,
                        expected_fonts=fonts,
                    )
                )
    deck.save(str(destination))
    assert deck.slide_width is not None and deck.slide_height is not None
    return boxes, deck.slide_width / 12700, deck.slide_height / 12700


def extract_glyphs(path: Path) -> tuple[list[Glyph], list[tuple[float, float]]]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar

    glyphs: list[Glyph] = []
    pages: list[tuple[float, float]] = []
    for slide, page in enumerate(extract_pages(str(path)), 1):
        pages.append((page.width, page.height))

        def walk(node: Any, slide: int = slide, page: Any = page) -> None:
            if isinstance(node, LTChar):
                value = node.graphicstate.ncolor
                if isinstance(value, (tuple, list)) and len(value) == 3:
                    color = cast(Color, tuple(round(float(v) * 255) for v in value))
                elif isinstance(value, (int, float)):
                    color = (round(value * 255),) * 3
                else:
                    color = (-100, -100, -100)
                # matrix の原点が baseline。bbox の descender 値は使わない。
                glyphs.append(
                    Glyph(
                        slide,
                        (node.x0, page.height - node.y1, node.x1, page.height - node.y0),
                        page.height - node.matrix[5],
                        node.size,
                        color,
                        node.fontname,
                        node.get_text(),
                    )
                )
            elif hasattr(node, "__iter__"):
                for child in node:
                    walk(child)

        walk(page)
    return glyphs, pages


def _overlap(a: Rect, b: Rect) -> float:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


# 置き換えの表（fontconfig・LibreOffice の FontPairs）と同じ集合。
# 和文が中国語の字形（Noto CJK SC）に落ちたら指摘する。
JA_GOTHIC_FAMILIES = frozenset(
    [
        "Yu Gothic UI",
        "Yu Gothic",
        "游ゴシック",
        "游ゴシック体",
        "ＭＳ Ｐゴシック",
        "MS PGothic",
        "ＭＳ ゴシック",
        "MS Gothic",
        "メイリオ",
        "Meiryo",
        "Meiryo UI",
        "Hiragino Sans",
        "ヒラギノ角ゴ ProN",
        "ヒラギノ角ゴシック",
    ]
)
JA_MINCHO_FAMILIES = frozenset(
    [
        "ＭＳ Ｐ明朝",
        "MS PMincho",
        "ＭＳ 明朝",
        "MS Mincho",
        "游明朝",
        "Yu Mincho",
        "ヒラギノ明朝 ProN",
    ]
)


def _font_allowed(font: str, expected: tuple[str, ...], palt: bool) -> bool:
    normal = font.split("+", 1)[-1].replace(" ", "").replace("-", "").lower()
    allowed = list(expected)
    for family in expected:
        if family in JA_GOTHIC_FAMILIES:
            allowed.append("TA Palt Gothic")
            if not palt:
                allowed.append("Noto Sans CJK JP")
        if family in JA_MINCHO_FAMILIES:
            allowed.append("Noto Serif CJK JP")
        if family in {"Arial", "Helvetica Neue"}:
            allowed.append("Liberation Sans")
    return not expected or any(
        normal.startswith(f.replace(" ", "").replace("-", "").lower()) for f in allowed
    )


def evaluate(
    shapes: list[ShapeBox],
    glyphs: list[Glyph],
    width: float,
    height: float,
    *,
    thresholds: dict[str, float] | None = None,
    baseline: list[dict[str, Any]] | None = None,
    font_substitute: str = "palt",
) -> dict[str, Any]:
    """LTChar 相当の入力に対する純粋判定。本文・書体名を結果へ写さない。"""
    thresholds = thresholds or {
        "error_lines": 0.5,
        "warn_lines": 0.2,
        "plain_noto_warn_lines": 0.4,
        "baseline_tolerance_pt": 1.0,
        "color_tolerance": 3.0,
    }
    findings: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    assigned: dict[tuple[int, str], list[Glyph]] = {}
    by_slide: dict[int, list[ShapeBox]] = {}
    for shape in shapes:
        by_slide.setdefault(shape.slide, []).append(shape)
    for glyph in glyphs:
        candidates = [
            s
            for s in by_slide.get(glyph.slide, [])
            if s.slide == glyph.slide
            and s.color is not None
            and max(abs(a - b) for a, b in zip(s.color, glyph.color, strict=True))
            <= thresholds["color_tolerance"]
        ]
        if len(candidates) == 1:
            assigned.setdefault((glyph.slide, candidates[0].key), []).append(glyph)
        else:
            counts["unmatched_glyphs"] += 1

    def add(shape: ShapeBox, kind: str, severity: str, **details: Any) -> None:
        # 校正済みの exact numeric baseline のみ。図形名だけの広い抑制は禁止。
        for item in baseline or []:
            if (
                item.get("slide") == shape.slide
                and item.get("shape_key") == shape.key
                and item.get("kind") == kind
                and item.get("details") == details
            ):
                counts["baseline_excluded"] += 1
                return
        findings.append(
            {
                "slide": shape.slide,
                "shape": shape.name[:40],
                "kind": kind,
                "severity": severity,
                "details": {"shape_key": shape.key, **details},
            }
        )

    text_boxes: dict[tuple[int, str], Rect] = {}
    warn = (
        thresholds["warn_lines"]
        if font_substitute == "palt"
        else thresholds["plain_noto_warn_lines"]
    )
    for shape in shapes:
        if not shape.has_text:
            continue
        if abs(shape.rotation) > 0.01:
            counts["unsupported_rotation"] += 1
            continue
        chars = assigned.get((shape.slide, shape.key), [])
        if not chars:
            add(shape, "text_missing", "warn", rect=list(shape.rect))
            continue
        lines: list[float] = []
        for char in sorted(chars, key=lambda g: g.baseline):
            if not lines or char.baseline - lines[-1] > thresholds["baseline_tolerance_pt"]:
                lines.append(char.baseline)
        em = max(g.size for g in chars)
        line_height = min(
            (b - a for a, b in pairwise(lines) if b - a > em * 0.5), default=em * 1.33
        )
        last_em = max(
            g.size
            for g in chars
            if abs(g.baseline - lines[-1]) <= thresholds["baseline_tolerance_pt"]
        )
        bottom = lines[-1] + 0.25 * last_em
        rect = (
            min(g.rect[0] for g in chars),
            min(g.rect[1] for g in chars),
            max(g.rect[2] for g in chars),
            bottom,
        )
        text_boxes[(shape.slide, shape.key)] = rect
        for kind, excess in (
            ("text_overflow_bottom", bottom - shape.inner[3]),
            ("text_overflow_right", rect[2] - shape.inner[2]),
        ):
            if kind == "text_overflow_bottom" and shape.autofit == "spAutoFit":
                continue
            ratio = excess / line_height
            if ratio > thresholds["error_lines"] or ratio > warn:
                add(
                    shape,
                    kind,
                    "error" if ratio > thresholds["error_lines"] else "warn",
                    lines=round(ratio, 4),
                    line_count=len(lines),
                    rect=list(rect),
                )
        actual = (rect[0], rect[1], rect[2], max(bottom, max(g.rect[3] for g in chars)))
        if actual[0] < 0 or actual[1] < 0 or actual[2] > width or actual[3] > height:
            add(shape, "text_outside_slide", "error", rect=list(actual))
        if any(
            g.text in {"\ufffd", "\u25a1", "\u25a0"} or "(cid:" in g.text or ".notdef" in g.font
            for g in chars
        ):
            add(shape, "notdef", "error", rect=list(rect))
        fallback_fonts = sorted(
            {
                g.font.split("+", 1)[-1][:40]
                for g in chars
                if not _font_allowed(g.font, shape.expected_fonts, font_substitute == "palt")
                or any(ord(c) >= 0x1F000 for c in g.text)
            }
        )
        if fallback_fonts or (chars and "Helvetica Neue" in shape.expected_fonts):
            # 書体名は資料の本文ではないので、原因の切り分けのために添える（最大 5 件）。
            add(
                shape,
                "glyph_fallback",
                "warn",
                rect=list(rect),
                fonts=fallback_fonts[:5],
                expected=list(shape.expected_fonts)[:5],
            )
    for i, shape in enumerate(shapes):
        current_rect = text_boxes.get((shape.slide, shape.key))
        if current_rect is None:
            continue
        rect = current_rect
        for other_index, other in enumerate(shapes):
            if other.slide != shape.slide or other.key == shape.key or abs(other.rotation) > 0.01:
                continue
            other_text = text_boxes.get((other.slide, other.key))
            if other.picture and _overlap(rect, other.rect) > 0:
                add(shape, "text_over_picture", "warn", other_key=other.key, rect=list(rect))
            if (
                shape.autofit == "spAutoFit"
                and rect[3] > shape.rect[3]
                and _overlap(rect, other.rect) > _overlap(shape.rect, other.rect)
            ):
                add(shape, "autofit_grow_collision", "error", other_key=other.key, rect=list(rect))
            if other_text is not None and other_index > i and _overlap(rect, other_text) > 0:
                add(shape, "text_collision", "error", other_key=other.key, rect=list(rect))
    counts.update(f["kind"] for f in findings)
    counts["error"] = sum(f["severity"] == "error" for f in findings)
    counts["warn"] = sum(f["severity"] == "warn" for f in findings)
    counts["slides"] = max((s.slide for s in shapes), default=0)
    return {
        "status": "needs_review"
        if findings or counts["unmatched_glyphs"] or font_substitute != "palt"
        else "ok",
        "findings": findings,
        "counts": dict(counts),
        "sanitized": {},
        "lo_version": "",
        "font_sha": "",
        "font_substitute": font_substitute,
        "elapsed_ms": 0,
    }


def load_profile(profile: str | None) -> tuple[dict[str, float], list[dict[str, Any]]]:
    root = Path(__file__).parent
    thresholds = json.loads((root / "render_qa_thresholds.json").read_text())
    if profile is None:
        return thresholds, []
    if profile != "proposal_builder_v1":
        raise ValueError("unknown_profile")
    data = json.loads((root / "render_qa_baseline_proposal_builder_v1.json").read_text())
    return thresholds, data["findings"]
