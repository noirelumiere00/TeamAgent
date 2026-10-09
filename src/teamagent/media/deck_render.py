"""DeckSpec → 編集できる PowerPoint（python-pptx・media worker 側）と CSV。

- マスターとレイアウトは DeckSpec の ``template`` からコードで作る（外部 FMT を土台にしない）。
  レイアウトは名前で選ぶ（R_表紙 等）。寸法・色・書体はすべて template の値で、
  ここに数字を持たない。
- 部品はネイティブ: 題・本文は入力欄（placeholder）、表はネイティブの表、比べる数字は
  埋め込みデータ付きのネイティブのグラフ、画像は 1 枚ずつの図（切り取り 0）、細部はノート。
- 色はテーマ色（schemeClr）で指定する。デザイン → 配色／フォントで一括で変えられるようにするため。
- 書体は run に直接書かず、テーマの見出し／本文（+mj / +mn）を参照させる。lang は ja-JP。

ここは文言を作らない（DeckSpec の文字をそのまま置く）。
"""

from __future__ import annotations

import copy
import csv
import io
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from teamagent.media.deck_contracts import (
    BoxSpec,
    ChartFill,
    DeckSpec,
    LayoutSpec,
    PictureFill,
    TableFill,
    TemplateSpec,
    TextFill,
    ThemeColor,
    ThemeSpec,
    TypographySpec,
)

PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
_NS_DECL = f'xmlns:a="{_NS_A}" xmlns:p="{_NS_P}" xmlns:r="{_NS_R}"'
_SECTION_EXT_URI = "{521415D9-36F7-43E2-AB2F-B90AF26B5E84}"
_SLIDENUM_FIELD_ID = "{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}"
# テーマの色の枠 → スライド上の schemeClr（マスターの clrMap: tx1=dk1, bg1=lt1, tx2=dk2, bg2=lt2）。
_SCHEME_VAL: dict[str, str] = {"dk1": "tx1", "lt1": "bg1", "dk2": "tx2", "lt2": "bg2"}
_PH_TYPE: dict[str, str] = {
    "title": "title",
    "body": "body",
    "picture": "pic",
    "table": "tbl",
    "chart": "chart",
    "footer": "ftr",
    "slide_number": "sldNum",
}


class DeckRenderError(ValueError):
    """DeckSpec と画像が描けない（画像が足りない・形式が違う 等）。"""


def _q(tag: str) -> str:
    prefix, local = tag.split(":")
    ns = {"a": _NS_A, "p": _NS_P, "r": _NS_R, "p14": _NS_P14}[prefix]
    return f"{{{ns}}}{local}"


def _scheme(color: ThemeColor | str) -> str:
    return _SCHEME_VAL.get(color, color)


def _parse(xml: str | bytes) -> Any:
    from pptx.oxml import parse_xml

    return parse_xml(xml)


# ── テーマ ────────────────────────────────────────────────────────────────────


def _theme_xml(theme: ThemeSpec) -> bytes:
    order = ["dk1", "lt1", "dk2", "lt2"] + [f"accent{i}" for i in range(1, 7)]
    order += ["hlink", "folHlink"]
    colors = "".join(f'<a:{k}><a:srgbClr val="{theme.colors[k]}"/></a:{k}>' for k in order)

    def fonts(face: str) -> str:
        font = quoteattr(face)
        return f'<a:latin typeface={font}/><a:ea typeface={font}/><a:cs typeface=""/>'

    solid = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    lines = "".join(
        f'<a:ln w="{w}"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
        for w in (6350, 12700, 19050)
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<a:theme xmlns:a="{_NS_A}" name={quoteattr(theme.name)}><a:themeElements>'
        f"<a:clrScheme name={quoteattr(theme.name)}>{colors}</a:clrScheme>"
        f"<a:fontScheme name={quoteattr(theme.name)}>"
        f"<a:majorFont>{fonts(theme.major_font)}</a:majorFont>"
        f"<a:minorFont>{fonts(theme.minor_font)}</a:minorFont></a:fontScheme>"
        f"<a:fmtScheme name={quoteattr(theme.name)}>"
        f"<a:fillStyleLst>{solid * 3}</a:fillStyleLst><a:lnStyleLst>{lines}</a:lnStyleLst>"
        "<a:effectStyleLst>"
        + "<a:effectStyle><a:effectLst/></a:effectStyle>"
        * 3
        + f"</a:effectStyleLst><a:bgFillStyleLst>{solid * 3}</a:bgFillStyleLst>"
        "</a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>"
    )
    return xml.encode("utf-8")


# ── マスター・レイアウトの入力欄 ──────────────────────────────────────────────


def _rpr_xml(
    tag: str,
    *,
    lang: str,
    size_pt: float | None = None,
    bold: bool | None = None,
    color: str | None = None,
    major: bool = False,
) -> str:
    attrs = f' lang="{lang}" altLang="en-US"'
    if size_pt is not None:
        attrs += f' sz="{round(size_pt * 100)}"'
    if bold is not None:
        attrs += f' b="{1 if bold else 0}"'
    fill = f'<a:solidFill><a:schemeClr val="{_scheme(color)}"/></a:solidFill>' if color else ""
    kind = "mj" if major else "mn"
    faces = f'<a:latin typeface="+{kind}-lt"/><a:ea typeface="+{kind}-ea"/>'
    faces += f'<a:cs typeface="+{kind}-cs"/>'
    return f"<a:{tag}{attrs}>{fill}{faces}</a:{tag}>"


def _ppr_xml(box: BoxSpec, *, t: TemplateSpec, level: int = 1) -> str:
    lang = t.theme.lang
    align = f' algn="{box.align}"'
    if box.bullets:
        indent = t.typography.bullet_indent_emu
        bullet = '<a:buFontTx/><a:buChar char="・"/>'
        attrs = f' marL="{indent * level}" indent="-{indent}"{align}'
    else:
        bullet = "<a:buNone/>"
        attrs = f' marL="0" indent="0"{align}'
    spacing = f'<a:lnSpc><a:spcPct val="{box.line_spacing_pct * 1000}"/></a:lnSpc>'
    if box.space_before_pt:
        spacing += f'<a:spcBef><a:spcPts val="{round(box.space_before_pt * 100)}"/></a:spcBef>'
    rpr = _rpr_xml(
        "defRPr",
        lang=lang,
        size_pt=box.font_pt,
        bold=box.bold if box.font_pt is not None else None,
        color=box.color,
        major=box.kind == "title",
    )
    return f"<a:lvl{level}pPr{attrs}>{spacing}{bullet}{rpr}</a:lvl{level}pPr>"


def _body_pr(box: BoxSpec, t: TemplateSpec) -> str:
    inset = t.typography.text_inset_emu
    fit = "<a:normAutofit/>" if box.shrink_on_overflow else "<a:noAutofit/>"
    return (
        f'<a:bodyPr vert="horz" wrap="square" lIns="0" tIns="{inset}" rIns="0" bIns="{inset}" '
        f'rtlCol="0" anchor="{box.anchor}" anchorCtr="0">{fit}</a:bodyPr>'
    )


def _placeholder_sp_xml(box: BoxSpec, shape_id: int, *, t: TemplateSpec) -> str:
    lang = t.theme.lang
    ph_type = _PH_TYPE[box.kind]
    ph_attrs = f'type="{ph_type}"' if box.kind == "title" else f'type="{ph_type}" idx="{box.idx}"'
    if box.kind in ("footer", "slide_number"):
        ph_attrs += ' sz="quarter"'
    levels = "".join(_ppr_xml(box, t=t, level=i) for i in (1, 2))
    rpr = f'<a:rPr lang="{lang}" altLang="en-US"/>'
    if box.kind == "slide_number":
        para = (
            f'<a:p><a:fld id="{_SLIDENUM_FIELD_ID}" type="slidenum">{rpr}<a:t>‹#›</a:t>'
            "</a:fld></a:p>"
        )
    else:
        para = f"<a:p><a:r>{rpr}<a:t>{escape(box.prompt or box.name)}</a:t></a:r></a:p>"
    return (
        f'<p:sp {_NS_DECL}><p:nvSpPr><p:cNvPr id="{shape_id}" name={quoteattr(box.name)}/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        f"<p:nvPr><p:ph {ph_attrs}/></p:nvPr></p:nvSpPr>"
        f'<p:spPr><a:xfrm><a:off x="{box.x}" y="{box.y}"/><a:ext cx="{box.w}" cy="{box.h}"/>'
        '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        f"<p:txBody>{_body_pr(box, t)}<a:lstStyle>{levels}</a:lstStyle>{para}</p:txBody></p:sp>"
    )


def _tile_sp_xml(shape_id: int, x: int, y: int, w: int, h: int, color: str) -> str:
    return (
        f'<p:sp {_NS_DECL}><p:nvSpPr><p:cNvPr id="{shape_id}" name="タイルの地 {shape_id}"/>'
        '<p:cNvSpPr/><p:nvPr userDrawn="1"/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:schemeClr val="{_scheme(color)}"/></a:solidFill><a:ln><a:noFill/></a:ln>'
        "</p:spPr></p:sp>"
    )


def _reset_sp_tree(sp_tree: Any) -> None:
    for child in list(sp_tree):
        if child.tag not in (_q("p:nvGrpSpPr"), _q("p:grpSpPr")):
            sp_tree.remove(child)


def _text_style_xml(tag: str, box: BoxSpec, *, t: TemplateSpec) -> str:
    levels = "".join(_ppr_xml(box, t=t, level=i) for i in range(1, 10))
    return f"<p:{tag}>{levels}</p:{tag}>"


def _build_master(master: Any, template: TemplateSpec) -> None:
    sp_tree = master.shapes._spTree
    _reset_sp_tree(sp_tree)
    for i, box in enumerate(template.master_boxes):
        sp_tree.append(_parse(_placeholder_sp_xml(box, 2 + i, t=template)))
    title = next(b for b in template.master_boxes if b.kind == "title")
    body = next(b for b in template.master_boxes if b.kind == "body")
    other = body.model_copy(update={"bullets": False, "font_pt": None, "color": "dk1"})
    styles = master._element.find(_q("p:txStyles"))
    for child in list(styles):
        styles.remove(child)
    styles.append(
        _parse(f"<p:x {_NS_DECL}>{_text_style_xml('titleStyle', title, t=template)}</p:x>")[0]
    )
    styles.append(
        _parse(f"<p:x {_NS_DECL}>{_text_style_xml('bodyStyle', body, t=template)}</p:x>")[0]
    )
    styles.append(
        _parse(f"<p:x {_NS_DECL}>{_text_style_xml('otherStyle', other, t=template)}</p:x>")[0]
    )


def _build_layout(layout: Any, spec: LayoutSpec, *, t: TemplateSpec) -> None:
    element = layout._element
    element.cSld.set("name", spec.name)
    for attr in ("type", "preserve"):
        if attr in element.attrib:
            del element.attrib[attr]
    element.set("preserve", "1")
    sp_tree = layout.shapes._spTree
    _reset_sp_tree(sp_tree)
    shape_id = 2
    for tile in spec.tiles:
        sp_tree.append(_parse(_tile_sp_xml(shape_id, tile.x, tile.y, tile.w, tile.h, tile.color)))
        shape_id += 1
    for box in spec.boxes:
        sp_tree.append(_parse(_placeholder_sp_xml(box, shape_id, t=t)))
        shape_id += 1


def _apply_template(prs: Any, template: TemplateSpec) -> dict[str, Any]:
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.util import Emu

    prs.slide_width = Emu(template.slide_w)
    prs.slide_height = Emu(template.slide_h)
    master = prs.slide_master
    theme_part = master.part.part_related_by(RT.THEME)
    theme_part._blob = _theme_xml(template.theme)
    notes_theme = prs.notes_master.part.part_related_by(RT.THEME)
    notes_theme._element = _parse(_theme_xml(template.theme))
    _build_master(master, template)
    layouts = list(prs.slide_layouts)
    if len(layouts) < len(template.layouts):
        raise DeckRenderError("base template has too few layouts")
    by_name: dict[str, Any] = {}
    for layout, spec in zip(layouts, template.layouts, strict=False):
        _build_layout(layout, spec, t=template)
        by_name[spec.name] = layout
    for layout in layouts[len(template.layouts) :]:
        prs.slide_layouts.remove(layout)
    _japanese_default_text(prs, template.theme.lang)
    return by_name


def _japanese_default_text(prs: Any, lang: str) -> None:
    style = prs.part._element.find(_q("p:defaultTextStyle"))
    if style is None:
        return
    for rpr in style.iter(_q("a:defRPr")):
        rpr.set("lang", lang)


# ── スライドの中身 ────────────────────────────────────────────────────────────


def _theme_color(color: ThemeColor) -> Any:
    from pptx.enum.dml import MSO_THEME_COLOR

    mapping = {
        "dk1": MSO_THEME_COLOR.TEXT_1,
        "lt1": MSO_THEME_COLOR.BACKGROUND_1,
        "dk2": MSO_THEME_COLOR.TEXT_2,
        "lt2": MSO_THEME_COLOR.BACKGROUND_2,
    }
    if color in mapping:
        return mapping[color]
    return getattr(MSO_THEME_COLOR, f"ACCENT_{color[-1]}")


def _set_lang(rpr: Any, lang: str) -> None:
    rpr.set("lang", lang)
    rpr.set("altLang", "en-US")


def _fill_text(shape: Any, fill: TextFill, box: BoxSpec, *, lang: str) -> None:
    from pptx.enum.text import PP_ALIGN

    align = {"l": PP_ALIGN.LEFT, "ctr": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}
    frame = shape.text_frame
    frame.clear()
    for index, spec in enumerate(fill.paragraphs):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        if spec.align is not None:
            paragraph.alignment = align[spec.align]
        if box.bullets and not spec.bullet:
            ppr = paragraph._p.get_or_add_pPr()
            ppr.set("marL", "0")
            ppr.set("indent", "0")
            ppr.append(_parse(f'<a:buNone xmlns:a="{_NS_A}"/>'))
        for run_spec in spec.runs:
            text_run = paragraph.add_run()
            text_run.text = run_spec.text
            _set_lang(text_run.font._rPr, lang)
            if run_spec.bold:
                text_run.font.bold = True
            if run_spec.color is not None:
                text_run.font.color.theme_color = _theme_color(run_spec.color)
            if run_spec.link:
                text_run.hyperlink.address = run_spec.link
        end = paragraph._p.get_or_add_endParaRPr()
        _set_lang(end, lang)
    shape.name = fill.shape_name


def _cell_borders(cell: Any, color: str) -> None:
    """表の罫線: 横線だけ（accent4）・縦線なし。tcPr の子の順番（ln* → fill）を守る。"""
    tc_pr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        existing = tc_pr.find(_q(tag))
        if existing is not None:
            tc_pr.remove(existing)
    lines = []
    for tag in ("lnL", "lnR"):
        lines.append(f'<a:{tag} xmlns:a="{_NS_A}" w="0"><a:noFill/></a:{tag}>')
    for tag in ("lnT", "lnB"):
        lines.append(
            f'<a:{tag} xmlns:a="{_NS_A}" w="9525" cmpd="sng"><a:solidFill>'
            f'<a:schemeClr val="{_scheme(color)}"/></a:solidFill></a:{tag}>'
        )
    for index, xml in enumerate(lines):
        tc_pr.insert(index, _parse(xml))


def _fill_table(
    shape: Any, fill: TableFill, box: BoxSpec, *, lang: str, typo: TypographySpec
) -> Any:
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Emu, Inches, Pt

    align = {"l": PP_ALIGN.LEFT, "ctr": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}
    n_rows = len(fill.rows) + 1
    n_cols = len(fill.columns)
    frame = shape.insert_table(n_rows, n_cols)
    table = frame.table
    table.first_row = True
    table.horz_banding = False
    weights = fill.col_weights or tuple(1.0 for _ in fill.columns)
    total = sum(weights)
    widths = [int(box.w * w / total) for w in weights]
    widths[-1] = box.w - sum(widths[:-1])
    for col, width in zip(table.columns, widths, strict=True):
        col.width = Emu(width)
    row_h = min(int(box.h / n_rows), int(Inches(fill.font_pt * typo.table_row_in_per_pt)))
    for row in table.rows:
        row.height = Emu(row_h)
    frame.height = Emu(row_h * n_rows)

    def put(
        cell: Any, text: str, *, bold: bool, header: bool, how: str, link: str | None, col: int
    ) -> None:
        cell.margin_left = cell.margin_right = Emu(typo.table_cell_margin_emu)
        if col > 0 and col - 1 in fill.numeric_cols and col not in fill.numeric_cols:
            cell.margin_left = Emu(182880)
        cell.margin_top = cell.margin_bottom = Emu(typo.table_cell_margin_emu // 2)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        frame_ = cell.text_frame
        frame_.clear()
        paragraph = frame_.paragraphs[0]
        paragraph.alignment = align[how]
        end = paragraph._p.get_or_add_endParaRPr()
        end.set("sz", str(int(fill.font_pt * 100)))
        _set_lang(end, lang)
        text_run = paragraph.add_run() if text else None
        if text_run is not None:
            text_run.text = text
        font = text_run.font if text_run is not None else paragraph.font
        font.size = Pt(fill.font_pt)
        _set_lang(font._rPr, lang)
        if bold or header:
            font.bold = True
        if header:
            font.color.theme_color = _theme_color("lt1")
        if link and text_run is not None:
            text_run.hyperlink.address = link
        cell.fill.solid()
        cell.fill.fore_color.theme_color = _theme_color("accent1" if header else "lt1")
        _cell_borders(cell, "accent4")

    for col, name in enumerate(fill.columns):
        how = "r" if col in fill.numeric_cols else "l"
        put(table.cell(0, col), name, bold=True, header=True, how=how, link=None, col=col)
    for r, row_cells in enumerate(fill.rows, start=1):
        for col, spec in enumerate(row_cells):
            how = "r" if col in fill.numeric_cols else spec.align
            put(
                table.cell(r, col),
                spec.text,
                bold=spec.bold or (r - 1) in fill.emphasis_rows,
                header=False,
                how=how,
                link=spec.link,
                col=col,
            )
    frame.name = fill.shape_name
    return frame


def _fill_chart(shape: Any, fill: ChartFill, *, lang: str, typo: TypographySpec) -> Any:
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
    from pptx.util import Pt

    data = CategoryChartData(number_format=fill.number_format)  # type: ignore[no-untyped-call]
    data.categories = list(fill.categories)
    for series in fill.series:
        data.add_series(  # type: ignore[no-untyped-call]
            series.name, list(series.values), number_format=fill.number_format
        )
    stacked = fill.chart_type == "bar_stacked_100"
    kind = XL_CHART_TYPE.BAR_STACKED_100 if stacked else XL_CHART_TYPE.BAR_CLUSTERED
    frame = shape.insert_chart(kind, data)
    chart = frame.chart
    chart.font.size = Pt(typo.chart_pt)
    chart.font.language_id = _lang_id(lang)
    chart.has_title = False
    chart.has_legend = fill.legend
    if fill.legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(typo.chart_pt)
    category_axis = chart.category_axis
    category_axis.reverse_order = True  # 1 位（1 つ目）を上に
    category_axis.has_major_gridlines = False
    category_axis.tick_labels.font.size = Pt(typo.chart_pt)
    category_axis.format.line.color.theme_color = _theme_color("accent4")
    value_axis = chart.value_axis
    value_axis.has_major_gridlines = False
    value_axis.visible = False
    plot = chart.plots[0]
    plot.gap_width = typo.chart_gap_width
    if stacked:
        plot.overlap = 100
    plot.has_data_labels = True
    labels = plot.data_labels
    labels.number_format = fill.number_format
    labels.number_format_is_linked = False
    labels.show_value = True
    labels.font.size = Pt(typo.chart_pt)
    labels.position = XL_LABEL_POSITION.CENTER if stacked else XL_LABEL_POSITION.OUTSIDE_END
    for series_spec, series in zip(fill.series, plot.series, strict=True):
        series.format.fill.solid()
        series.format.fill.fore_color.theme_color = _theme_color(series_spec.color)
        series.format.line.color.theme_color = _theme_color("lt1")
        if stacked:
            series.data_labels.font.size = Pt(typo.chart_pt)
            dark = series_spec.color in ("accent1", "accent3", "accent6", "dk1", "dk2")
            series.data_labels.font.color.theme_color = _theme_color("lt1" if dark else "dk1")
            series.data_labels.number_format = (
                f'[<{typo.chart_min_label_share}]"";{fill.number_format}'
            )
            series.data_labels.number_format_is_linked = False
            series.data_labels.show_value = True
            series.data_labels.position = XL_LABEL_POSITION.CENTER
    if fill.point_colors:
        series = plot.series[0]
        for index, color in enumerate(fill.point_colors):
            point = series.points[index]
            point.format.fill.solid()
            point.format.fill.fore_color.theme_color = _theme_color(color)
    frame.name = fill.shape_name
    return frame


def _lang_id(lang: str) -> Any:
    from pptx.enum.lang import MSO_LANGUAGE_ID

    return MSO_LANGUAGE_ID.JAPANESE if lang == "ja-JP" else MSO_LANGUAGE_ID.ENGLISH_US


def _fill_picture(
    shape: Any, fill: PictureFill, box: BoxSpec, spec: DeckSpec, images: Mapping[str, bytes]
) -> Any:
    from pptx.util import Emu

    if fill.image is None:
        shape.name = fill.shape_name
        return shape
    meta = next(img for img in spec.images if img.name == fill.image)
    picture = shape.insert_picture(io.BytesIO(images[fill.image]))
    if picture._element.spPr.find(_q("a:prstGeom")) is None:
        picture._element.spPr.append(
            _parse(
                '<a:prstGeom xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'prst="rect"><a:avLst/></a:prstGeom>'
            )
        )
    # 切り取り 0・元の比率のまま枠の中央に収める（insert_picture は枠いっぱいに切り抜くため戻す）。
    picture.crop_left = picture.crop_right = picture.crop_top = picture.crop_bottom = 0.0
    scale = min(box.w / meta.width_px, box.h / meta.height_px)
    width = int(meta.width_px * scale)
    height = int(meta.height_px * scale)
    picture.left = Emu(box.x + (box.w - width) // 2)
    picture.top = Emu(box.y + (box.h - height) // 2)
    picture.width = Emu(width)
    picture.height = Emu(height)
    picture._element.nvPicPr.cNvPr.set("descr", fill.alt_text)
    if fill.link:
        picture.click_action.hyperlink.address = fill.link
    picture.name = fill.shape_name
    return picture


def _add_footer_shapes(slide: Any, layout: Any, *, footer: str, number: int, lang: str) -> None:
    """フッターとページ番号の欄をスライドに置く（python-pptx は複製しないため）。"""
    from pptx.enum.shapes import PP_PLACEHOLDER

    sp_tree = slide.shapes._spTree
    for ph in layout.placeholders:
        ph_type = ph.placeholder_format.type
        if ph_type not in (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER):
            continue
        is_number = ph_type == PP_PLACEHOLDER.SLIDE_NUMBER
        if not is_number and not footer:
            continue
        element = copy.deepcopy(ph._element)
        sp_pr = element.find(_q("p:spPr"))
        for child in list(sp_pr):
            sp_pr.remove(child)
        body = element.find(_q("p:txBody"))
        for paragraph in body.findall(_q("a:p")):
            body.remove(paragraph)
        lst = body.find(_q("a:lstStyle"))
        for child in list(lst):
            lst.remove(child)
        rpr = f'<a:rPr lang="{lang}" altLang="en-US"/>'
        if is_number:
            xml = (
                f'<a:p xmlns:a="{_NS_A}"><a:fld id="{_SLIDENUM_FIELD_ID}" type="slidenum">'
                f"{rpr}<a:t>{number}</a:t></a:fld></a:p>"
            )
        else:
            xml = f'<a:p xmlns:a="{_NS_A}"><a:r>{rpr}<a:t>{escape(footer)}</a:t></a:r></a:p>'
        body.append(_parse(xml))
        element.find(_q("p:nvSpPr")).find(_q("p:cNvPr")).set("id", str(_next_shape_id(sp_tree)))
        sp_tree.append(element)


def _next_shape_id(sp_tree: Any) -> int:
    ids = [int(e.get("id")) for e in sp_tree.iter(_q("p:cNvPr")) if e.get("id", "").isdigit()]
    return max(ids, default=1) + 1


def _add_sections(prs: Any, spec: DeckSpec) -> None:
    slide_ids = [sld_id.get("id") for sld_id in prs.slides._sldIdLst]
    element = prs.part._element
    ext_lst = element.find(_q("p:extLst"))
    if ext_lst is None:
        ext_lst = _parse(f'<p:extLst xmlns:p="{_NS_P}"/>')
        element.append(ext_lst)
    for ext in ext_lst.findall(_q("p:ext")):
        if ext.get("uri") == _SECTION_EXT_URI:
            ext_lst.remove(ext)
    sections = "".join(
        f'<p14:section name={quoteattr(name)} id="{{{str(uuid.uuid5(uuid.NAMESPACE_URL, name + spec.properties.report_id)).upper()}}}">'  # noqa: E501
        "<p14:sldIdLst>"
        + "".join(f'<p14:sldId id="{slide_ids[i]}"/>' for i in indexes)
        + "</p14:sldIdLst></p14:section>"
        for name, indexes in spec.sections()
    )
    xml = (
        f'<p:ext xmlns:p="{_NS_P}" xmlns:p14="{_NS_P14}" uri="{_SECTION_EXT_URI}">'
        f"<p14:sectionLst>{sections}</p14:sectionLst></p:ext>"
    )
    ext_lst.append(_parse(xml))


def _set_properties(prs: Any, spec: DeckSpec) -> None:
    props = prs.core_properties
    meta = spec.properties
    props.title = meta.title
    props.subject = meta.subject
    props.keywords = meta.keywords
    props.author = meta.author
    props.last_modified_by = meta.author
    props.category = "レポート"
    props.comments = f"レポート ID {meta.report_id}｜取得時点 {meta.measured_at}"
    props.identifier = meta.report_id
    props.language = spec.template.theme.lang
    props.revision = 1
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    props.created = now
    props.modified = now


def _check_images(spec: DeckSpec, images: Mapping[str, bytes]) -> None:
    for meta in spec.images:
        data = images.get(meta.name)
        if not data:
            raise DeckRenderError(f"image {meta.name!r} is missing")
        is_jpeg = data[:2] == b"\xff\xd8"
        is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
        if (meta.media_type == "image/jpeg" and not is_jpeg) or (
            meta.media_type == "image/png" and not is_png
        ):
            raise DeckRenderError(f"image {meta.name!r} does not match {meta.media_type}")


def render_deck_pptx(spec: DeckSpec, images: Mapping[str, bytes]) -> bytes:
    """DeckSpec と画像（名前 → bytes）から .pptx の bytes を作る。"""
    from pptx import Presentation

    _check_images(spec, images)
    prs = Presentation()
    lang = spec.template.theme.lang
    layouts = _apply_template(prs, spec.template)
    for number, slide_spec in enumerate(spec.slides, start=1):
        layout_spec = spec.template.layout(slide_spec.layout)
        layout = layouts[slide_spec.layout]
        slide = prs.slides.add_slide(layout)
        by_idx = {ph.placeholder_format.idx: ph for ph in slide.placeholders}
        filled: set[int] = set()
        for fill in slide_spec.fills:
            box = layout_spec.box(fill.box)
            idx = 0 if box.kind == "title" else box.idx
            shape = by_idx.get(idx)
            if shape is None:
                raise DeckRenderError(f"{slide_spec.slide_id}: placeholder {box.key!r} is missing")
            if isinstance(fill, TextFill):
                _fill_text(shape, fill, box, lang=lang)
            elif isinstance(fill, TableFill):
                _fill_table(shape, fill, box, lang=lang, typo=spec.template.typography)
            elif isinstance(fill, ChartFill):
                _fill_chart(shape, fill, lang=lang, typo=spec.template.typography)
            else:
                _fill_picture(shape, fill, box, spec, images)
            filled.add(idx)
        # 中身の無い欄は消す（「クリックして…」を残さない）。画像が無い図は PictureFill(image=None)
        # で「枠だけ」を明示したときだけ残る（その場合は filled に入っている）。
        for idx, shape in by_idx.items():
            if idx not in filled:
                shape._element.getparent().remove(shape._element)
        _add_footer_shapes(slide, layout, footer=spec.footer, number=number, lang=lang)
        if slide_spec.notes:
            notes = slide.notes_slide.notes_text_frame
            notes.text = slide_spec.notes
            for paragraph in notes.paragraphs:
                for text_run in paragraph.runs:
                    _set_lang(text_run.font._rPr, lang)
    _add_sections(prs, spec)
    _set_properties(prs, spec)
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def _csv_safe(value: str) -> str:
    # 空白・制御文字で式の先頭を隠した場合も無害化する。
    return (
        "\u200b" + value
        if value.lstrip(" \t\r\n")[:1] in ("=", "+", "-", "@") or value[:1] in ("\t", "\r", "\n")
        else value
    )


def render_deck_csv(spec: DeckSpec) -> bytes | None:
    """同時に渡す CSV（BOM 付き UTF-8・Excel でそのまま開ける）。"""
    if spec.csv is None:
        return None
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([_csv_safe(c) for c in spec.csv.columns])
    writer.writerows([[_csv_safe(c) for c in row] for row in spec.csv.rows])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def render_deck_files(spec_json: bytes | str, images: Mapping[str, bytes]) -> dict[str, bytes]:
    """media の operation から呼ぶ入口: DeckSpec の JSON → {"deck.pptx", "deck.csv"}。"""
    spec = DeckSpec.model_validate_json(spec_json)
    files = {"deck.pptx": render_deck_pptx(spec, images)}
    table = render_deck_csv(spec)
    if table is not None:
        files["deck.csv"] = table
    return files


__all__ = [
    "PPTX_CONTENT_TYPE",
    "DeckRenderError",
    "render_deck_csv",
    "render_deck_files",
    "render_deck_pptx",
]
