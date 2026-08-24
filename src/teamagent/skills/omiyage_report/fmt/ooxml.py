"""最小 OOXML (PPTX) ライタ（stdlib のみ・編集用ネイティブPPTX の出力層）。

mcp イメージは python-pptx を同梱しない（Dockerfile が import 遮断を検証済み）ため、
編集用PPTXは zipfile + 手書きXMLで生成する。テキストボックス・矩形・画像の
3種のシェイプだけを扱う（テーブルは矩形+テキストで近似 = 編集可能性は保たれる）。

座標系は spec の 1920x1080 px。EMU = px × 6350（spec ``meta.canvas.coordinate_system``）。
"""

from __future__ import annotations

import io
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Literal
from xml.sax.saxutils import escape

EMU_PER_PX = 6350
SLIDE_W_PX = 1920
SLIDE_H_PX = 1080

Align = Literal["l", "ctr", "r"]
Anchor = Literal["t", "ctr", "b"]


class OoxmlError(RuntimeError):
    """編集用PPTXの組み立て失敗（不正画像等）。"""


def _emu(px: float) -> int:
    return round(px * EMU_PER_PX)


def _sz(px: float) -> int:
    """フォント px → OOXML sz（pt×100）。96dpi 換算 pt = px × 0.75。"""
    return max(100, round(px * 0.75 * 100))


@dataclass(frozen=True)
class Run:
    text: str
    font: str
    size_px: float
    color: str  # "#RRGGBB"
    bold: bool = False


@dataclass(frozen=True)
class Paragraph:
    runs: tuple[Run, ...]
    align: Align = "l"
    line_spacing_pct: int | None = None


@dataclass(frozen=True)
class TextBox:
    x: float
    y: float
    w: float
    h: float
    paragraphs: tuple[Paragraph, ...]
    anchor: Anchor = "t"


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float
    fill: str | None = None  # "#RRGGBB"
    line: str | None = None
    line_w_px: float = 1.0


@dataclass(frozen=True)
class Picture:
    x: float
    y: float
    w: float
    h: float
    data: bytes
    ext: Literal["jpeg", "png"]


Shape = TextBox | Rect | Picture


@dataclass
class SlidePage:
    background: str  # "#RRGGBB"
    shapes: list[Shape] = field(default_factory=list)


def image_size(data: bytes, ext: str) -> tuple[int, int]:
    """PNG/JPEG のピクセル寸法（contain 配置の計算用・stdlibのみ）。"""
    if ext == "png":
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
            raise OoxmlError("invalid PNG data")
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if ext == "jpeg":
        index = 2
        if data[:2] != b"\xff\xd8":
            raise OoxmlError("invalid JPEG data")
        limit = len(data)
        while index + 9 < limit:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return int(width), int(height)
            index += 2 + length
        raise OoxmlError("JPEG size not found")
    raise OoxmlError(f"unsupported image ext: {ext!r}")


def contain_box(
    natural_w: int, natural_h: int, box_x: float, box_y: float, box_w: float, box_h: float
) -> tuple[float, float, float, float]:
    """contain 配置（crop禁止・元比率維持・中央寄せ）。"""
    if natural_w <= 0 or natural_h <= 0:
        raise OoxmlError("image has invalid natural size")
    scale = min(box_w / natural_w, box_h / natural_h)
    width = natural_w * scale
    height = natural_h * scale
    return box_x + (box_w - width) / 2, box_y + (box_h - height) / 2, width, height


# ---------------------------------------------------------------------------
# XML parts
# ---------------------------------------------------------------------------

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)

_EMPTY_TREE_HEAD = (
    "<p:nvGrpSpPr>"
    '<p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
    "</p:nvGrpSpPr>"
    "<p:grpSpPr><a:xfrm>"
    '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
    "</a:xfrm></p:grpSpPr>"
)


def _color_val(color: str) -> str:
    if not color.startswith("#") or len(color) != 7:
        raise OoxmlError(f"invalid color {color!r}")
    return color[1:].upper()


def _run_xml(run: Run) -> str:
    bold = ' b="1"' if run.bold else ""
    font = escape(run.font, {'"': "&quot;"})
    return (
        f'<a:r><a:rPr lang="ja-JP" altLang="en-US" sz="{_sz(run.size_px)}"{bold} dirty="0">'
        f'<a:solidFill><a:srgbClr val="{_color_val(run.color)}"/></a:solidFill>'
        f'<a:latin typeface="{font}"/><a:ea typeface="{font}"/>'
        f"</a:rPr><a:t>{escape(run.text)}</a:t></a:r>"
    )


def _paragraph_xml(paragraph: Paragraph) -> str:
    spacing = (
        f'<a:lnSpc><a:spcPct val="{paragraph.line_spacing_pct * 1000}"/></a:lnSpc>'
        if paragraph.line_spacing_pct is not None
        else ""
    )
    runs = "".join(_run_xml(run) for run in paragraph.runs) or "<a:endParaRPr/>"
    return f'<a:p><a:pPr algn="{paragraph.align}">{spacing}</a:pPr>{runs}</a:p>'


def _textbox_xml(shape: TextBox, shape_id: int) -> str:
    paragraphs = "".join(_paragraph_xml(paragraph) for paragraph in shape.paragraphs)
    return (
        "<p:sp><p:nvSpPr>"
        f'<p:cNvPr id="{shape_id}" name="TextBox {shape_id}"/>'
        '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        "<p:spPr><a:xfrm>"
        f'<a:off x="{_emu(shape.x)}" y="{_emu(shape.y)}"/>'
        f'<a:ext cx="{_emu(shape.w)}" cy="{_emu(shape.h)}"/>'
        '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" '
        f'anchor="{shape.anchor}"/><a:lstStyle/>{paragraphs}</p:txBody></p:sp>'
    )


def _rect_xml(shape: Rect, shape_id: int) -> str:
    fill = (
        f'<a:solidFill><a:srgbClr val="{_color_val(shape.fill)}"/></a:solidFill>'
        if shape.fill is not None
        else "<a:noFill/>"
    )
    line = ""
    if shape.line is not None:
        line = (
            f'<a:ln w="{_emu(shape.line_w_px)}">'
            f'<a:solidFill><a:srgbClr val="{_color_val(shape.line)}"/></a:solidFill></a:ln>'
        )
    return (
        "<p:sp><p:nvSpPr>"
        f'<p:cNvPr id="{shape_id}" name="Rect {shape_id}"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        "<p:spPr><a:xfrm>"
        f'<a:off x="{_emu(shape.x)}" y="{_emu(shape.y)}"/>'
        f'<a:ext cx="{_emu(shape.w)}" cy="{_emu(shape.h)}"/>'
        f'</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>{fill}{line}</p:spPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></p:txBody></p:sp>"
    )


def _picture_xml(shape: Picture, shape_id: int, rel_id: str) -> str:
    return (
        "<p:pic><p:nvPicPr>"
        f'<p:cNvPr id="{shape_id}" name="Picture {shape_id}"/>'
        "<p:cNvPicPr/><p:nvPr/></p:nvPicPr>"
        f'<p:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch>'
        "</p:blipFill><p:spPr><a:xfrm>"
        f'<a:off x="{_emu(shape.x)}" y="{_emu(shape.y)}"/>'
        f'<a:ext cx="{_emu(shape.w)}" cy="{_emu(shape.h)}"/>'
        '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    )


def _slide_xml(page: SlidePage, image_rel_ids: dict[int, str]) -> str:
    shapes_xml: list[str] = []
    shape_id = 2
    for index, shape in enumerate(page.shapes):
        if isinstance(shape, TextBox):
            shapes_xml.append(_textbox_xml(shape, shape_id))
        elif isinstance(shape, Rect):
            shapes_xml.append(_rect_xml(shape, shape_id))
        else:
            shapes_xml.append(_picture_xml(shape, shape_id, image_rel_ids[index]))
        shape_id += 1
    return (
        _XML_DECL
        + f"<p:sld {_NS}><p:cSld><p:bg><p:bgPr>"
        + f'<a:solidFill><a:srgbClr val="{_color_val(page.background)}"/></a:solidFill>'
        + "<a:effectLst/></p:bgPr></p:bg>"
        + f"<p:spTree>{_EMPTY_TREE_HEAD}{''.join(shapes_xml)}</p:spTree></p:cSld>"
        + "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


_THEME_XML = (
    _XML_DECL
    + '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Omiyage">'
    "<a:themeElements>"
    '<a:clrScheme name="Omiyage">'
    '<a:dk1><a:srgbClr val="1A1B1F"/></a:dk1><a:lt1><a:srgbClr val="F6F4EF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="17181C"/></a:dk2><a:lt2><a:srgbClr val="EFEBE1"/></a:lt2>'
    '<a:accent1><a:srgbClr val="A24765"/></a:accent1>'
    '<a:accent2><a:srgbClr val="2F5397"/></a:accent2>'
    '<a:accent3><a:srgbClr val="B9B2A4"/></a:accent3>'
    '<a:accent4><a:srgbClr val="8A847A"/></a:accent4>'
    '<a:accent5><a:srgbClr val="55514A"/></a:accent5>'
    '<a:accent6><a:srgbClr val="C9C2B4"/></a:accent6>'
    '<a:hlink><a:srgbClr val="2F5397"/></a:hlink>'
    '<a:folHlink><a:srgbClr val="A24765"/></a:folHlink>'
    "</a:clrScheme>"
    '<a:fontScheme name="Omiyage">'
    '<a:majorFont><a:latin typeface="Instrument Sans"/><a:ea typeface="Shippori Mincho B1"/>'
    '<a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Instrument Sans"/><a:ea typeface="Zen Kaku Gothic New"/>'
    '<a:cs typeface=""/></a:minorFont>'
    "</a:fontScheme>"
    '<a:fmtScheme name="Omiyage">'
    "<a:fillStyleLst>"
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    "</a:fillStyleLst>"
    "<a:lnStyleLst>"
    '<a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    "</a:lnStyleLst>"
    "<a:effectStyleLst>"
    "<a:effectStyle><a:effectLst/></a:effectStyle>"
    "<a:effectStyle><a:effectLst/></a:effectStyle>"
    "<a:effectStyle><a:effectLst/></a:effectStyle>"
    "</a:effectStyleLst>"
    "<a:bgFillStyleLst>"
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    "</a:bgFillStyleLst>"
    "</a:fmtScheme>"
    "</a:themeElements></a:theme>"
)

_MASTER_XML = (
    _XML_DECL + f"<p:sldMaster {_NS}><p:cSld><p:spTree>{_EMPTY_TREE_HEAD}</p:spTree></p:cSld>"
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
    'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" '
    'folHlink="folHlink"/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    "</p:sldMaster>"
)

_LAYOUT_XML = (
    _XML_DECL + f'<p:sldLayout {_NS} type="blank" preserve="1">'
    f'<p:cSld name="Blank"><p:spTree>{_EMPTY_TREE_HEAD}</p:spTree></p:cSld>'
    "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
)


def _rels(entries: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{rel_id}" Type="{rel_type}" Target="{target}"/>'
        for rel_id, rel_type, target in entries
    )
    return (
        _XML_DECL
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + body
        + "</Relationships>"
    )


_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def build_pptx(pages: list[SlidePage], *, title: str) -> bytes:
    """SlidePage 群 → PPTX バイト列（決定論・stdlibのみ）。"""
    if not pages:
        raise OoxmlError("no slides")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:

        def write(name: str, text: str) -> None:
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), text)

        overrides = [
            (
                "/ppt/presentation.xml",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
            ),
            (
                "/ppt/slideMasters/slideMaster1.xml",
                "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
            ),
            (
                "/ppt/slideLayouts/slideLayout1.xml",
                "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
            ),
            (
                "/ppt/theme/theme1.xml",
                "application/vnd.openxmlformats-officedocument.theme+xml",
            ),
            (
                "/docProps/core.xml",
                "application/vnd.openxmlformats-package.core-properties+xml",
            ),
        ]
        for index in range(len(pages)):
            overrides.append(
                (
                    f"/ppt/slides/slide{index + 1}.xml",
                    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
                )
            )
        content_types = (
            _XML_DECL
            + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="jpeg" ContentType="image/jpeg"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            + "".join(
                f'<Override PartName="{part}" ContentType="{content_type}"/>'
                for part, content_type in overrides
            )
            + "</Types>"
        )
        write("[Content_Types].xml", content_types)
        write(
            "_rels/.rels",
            _rels(
                [
                    ("rId1", f"{_REL_NS}/officeDocument", "ppt/presentation.xml"),
                    (
                        "rId2",
                        "http://schemas.openxmlformats.org/package/2006/relationships"
                        "/metadata/core-properties",
                        "docProps/core.xml",
                    ),
                ]
            ),
        )
        write(
            "docProps/core.xml",
            _XML_DECL + "<cp:coreProperties "
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{escape(title)}</dc:title>"
            "</cp:coreProperties>",
        )

        presentation_rels: list[tuple[str, str, str]] = [
            ("rId1", f"{_REL_NS}/slideMaster", "slideMasters/slideMaster1.xml")
        ]
        slide_ids: list[str] = []
        for index in range(len(pages)):
            rel_id = f"rId{index + 2}"
            presentation_rels.append((rel_id, f"{_REL_NS}/slide", f"slides/slide{index + 1}.xml"))
            slide_ids.append(f'<p:sldId id="{256 + index}" r:id="{rel_id}"/>')
        presentation = (
            _XML_DECL + f"<p:presentation {_NS}>"
            '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            f"<p:sldIdLst>{''.join(slide_ids)}</p:sldIdLst>"
            f'<p:sldSz cx="{_emu(SLIDE_W_PX)}" cy="{_emu(SLIDE_H_PX)}"/>'
            '<p:notesSz cx="6858000" cy="9144000"/>'
            "</p:presentation>"
        )
        write("ppt/presentation.xml", presentation)
        write("ppt/_rels/presentation.xml.rels", _rels(presentation_rels))
        write("ppt/slideMasters/slideMaster1.xml", _MASTER_XML)
        write(
            "ppt/slideMasters/_rels/slideMaster1.xml.rels",
            _rels(
                [
                    ("rId1", f"{_REL_NS}/slideLayout", "../slideLayouts/slideLayout1.xml"),
                    ("rId2", f"{_REL_NS}/theme", "../theme/theme1.xml"),
                ]
            ),
        )
        write("ppt/slideLayouts/slideLayout1.xml", _LAYOUT_XML)
        write(
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
            _rels([("rId1", f"{_REL_NS}/slideMaster", "../slideMasters/slideMaster1.xml")]),
        )
        write("ppt/theme/theme1.xml", _THEME_XML)

        media_counter = 0
        for page_index, page in enumerate(pages, start=1):
            slide_rels: list[tuple[str, str, str]] = [
                ("rId1", f"{_REL_NS}/slideLayout", "../slideLayouts/slideLayout1.xml")
            ]
            image_rel_ids: dict[int, str] = {}
            for shape_index, shape in enumerate(page.shapes):
                if isinstance(shape, Picture):
                    media_counter += 1
                    media_name = f"image{media_counter}.{shape.ext}"
                    archive.writestr(
                        zipfile.ZipInfo(f"ppt/media/{media_name}", (1980, 1, 1, 0, 0, 0)),
                        shape.data,
                    )
                    rel_id = f"rId{len(slide_rels) + 1}"
                    slide_rels.append((rel_id, f"{_REL_NS}/image", f"../media/{media_name}"))
                    image_rel_ids[shape_index] = rel_id
            write(f"ppt/slides/slide{page_index}.xml", _slide_xml(page, image_rel_ids))
            write(f"ppt/slides/_rels/slide{page_index}.xml.rels", _rels(slide_rels))
    return buffer.getvalue()
