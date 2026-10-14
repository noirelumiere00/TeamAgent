"""Noto 自身の GPOS palt を焼き込んだ TA Palt Gothic を作る。"""

from __future__ import annotations

import argparse
from pathlib import Path

from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection, TTFont


def load_noto(path, style):
    if path.suffix.lower() in {".ttc", ".otc"}:
        collection = TTCollection(path, recalcTimestamp=False)
        for font in collection.fonts:
            family = font["name"].getDebugName(1) or ""
            if "Noto Sans CJK JP" in family:
                return font
        raise ValueError(f"JP face missing: {style}")
    font = TTFont(path, recalcTimestamp=False)
    family = font["name"].getDebugName(1) or ""
    if "Noto Sans CJK JP" not in family:
        font.close()
        raise ValueError("Only Noto Sans CJK JP outlines are accepted")
    return font


def palt_adjustments(font):
    if "GPOS" not in font:
        raise ValueError("palt missing")
    table = font["GPOS"].table
    features = table.FeatureList.FeatureRecord
    indices = [i for i, record in enumerate(features) if record.FeatureTag == "palt"]
    # 日本語 JAN を既定より優先。同一 feature を言語ごとに重ねて適用しない。
    candidates = [[], []]
    for record in table.ScriptList.ScriptRecord:
        if record.ScriptTag not in {"DFLT", "hani", "kana"}:
            continue
        script = record.Script
        candidates[0].extend(
            lang.LangSys for lang in script.LangSysRecord if lang.LangSysTag == "JAN "
        )
        if script.DefaultLangSys:
            candidates[1].append(script.DefaultLangSys)
    for languages in candidates:
        preferred = []
        for lang in languages:
            used = list(lang.FeatureIndex)
            if lang.ReqFeatureIndex != 0xFFFF:
                used.append(lang.ReqFeatureIndex)
            preferred.extend(i for i in indices if i in used and i not in preferred)
        if preferred:
            indices = preferred
            break
    lookups = list(dict.fromkeys(i for f in indices for i in features[f].Feature.LookupListIndex))
    adjustments = {}
    for index in lookups:
        lookup = table.LookupList.Lookup[index]
        # 一つの lookup 内では最初に一致した subtable だけが適用される。
        seen = set()
        for subtable in lookup.SubTable:
            kind = lookup.LookupType
            if kind == 9:
                kind, subtable = subtable.ExtensionLookupType, subtable.ExtSubTable
            if kind != 1:
                raise ValueError(f"unsupported palt lookup type: {kind}")
            if subtable.Format not in {1, 2}:
                raise ValueError("unsupported SinglePos format")
            for i, name in enumerate(subtable.Coverage.glyphs):
                if name in seen:
                    continue
                seen.add(name)
                value = subtable.Value if subtable.Format == 1 else subtable.Value[i]
                x, advance = adjustments.get(name, (0, 0))
                adjustments[name] = (
                    x + (getattr(value, "XPlacement", 0) or 0),
                    advance + (getattr(value, "XAdvance", 0) or 0),
                )
    if not adjustments:
        raise ValueError("palt missing or empty")
    return adjustments


def remove_palt(font):
    table = font["GPOS"].table
    records = table.FeatureList.FeatureRecord
    kept = [i for i, record in enumerate(records) if record.FeatureTag != "palt"]
    remap = {old: new for new, old in enumerate(kept)}
    table.FeatureList.FeatureRecord = [records[i] for i in kept]
    table.FeatureList.FeatureCount = len(kept)
    for record in table.ScriptList.ScriptRecord:
        languages = [lang.LangSys for lang in record.Script.LangSysRecord]
        if record.Script.DefaultLangSys:
            languages.append(record.Script.DefaultLangSys)
        for lang in languages:
            lang.FeatureIndex = [remap[i] for i in lang.FeatureIndex if i in remap]
            lang.FeatureCount = len(lang.FeatureIndex)
            lang.ReqFeatureIndex = remap.get(lang.ReqFeatureIndex, 0xFFFF)
    variations = getattr(table, "FeatureVariations", None)
    if variations:
        for record in variations.FeatureVariationRecord:
            substitution = record.FeatureTableSubstitution
            substitution.SubstitutionRecord = [
                r for r in substitution.SubstitutionRecord if r.FeatureIndex in remap
            ]
            for r in substitution.SubstitutionRecord:
                r.FeatureIndex = remap[r.FeatureIndex]
            substitution.SubstitutionCount = len(substitution.SubstitutionRecord)
    # 未参照の lookup は残す。他 feature が共有する lookup を壊さないため。


def build(source, style, output):
    font = load_noto(source, style)
    try:
        adjustments = palt_adjustments(font)
        glyphset = font.getGlyphSet()
        for name, (shift, delta) in adjustments.items():
            old_advance, lsb = font["hmtx"].metrics[name]
            advance = old_advance + delta
            if "CFF " in font:
                top = font["CFF "].cff.topDictIndex[0]
                # Noto CJK は CID-keyed。各字形の FD ごとの Private を保持する。
                private = top.CharStrings[name].private
                pen = T2CharStringPen(advance, glyphset)
                glyphset[name].draw(TransformPen(pen, (1, 0, 0, 1, shift, 0)))
                top.CharStrings[name] = pen.getCharString(
                    private=private, globalSubrs=top.GlobalSubrs
                )
            elif "glyf" in font:
                pen = TTGlyphPen(glyphset)
                glyphset[name].draw(TransformPen(pen, (1, 0, 0, 1, shift, 0)))
                font["glyf"][name] = pen.glyph()
            else:
                raise ValueError("unsupported Noto outlines")
            font["hmtx"].metrics[name] = (advance, lsb + shift)
        remove_palt(font)
        # hhea/OS2 の縦値は Noto のまま。YPlacement/YAdvance は焼き込まない。
        family = "TA Palt Gothic"
        ps_name = "TAPaltGothic-" + style
        names = {
            1: family,
            2: style,
            3: ps_name + ";1.0",
            4: family + " " + style,
            6: ps_name,
            16: family,
            17: style,
        }
        font["name"].names = [n for n in font["name"].names if n.nameID not in names]
        for name_id, value in names.items():
            font["name"].setName(value, name_id, 3, 1, 0x409)
        if "CFF " in font:
            cff = font["CFF "].cff
            cff.fontNames = [ps_name]
            top = cff.topDictIndex[0]
            top.FamilyName, top.FullName, top.FontName = family, family + " " + style, ps_name
            # CID-keyed の FDArray にも元の名前（NotoSansCJKjp-Regular-…）が残るので付け替える。
            # PDF に元の名前が出ると、代用書体が使われたのか Noto へ落ちたのかを区別できない。
            for fd in getattr(top, "FDArray", None) or []:
                name = getattr(fd, "FontName", None)
                if isinstance(name, str) and "-" in name:
                    fd.FontName = ps_name + "-" + name.rsplit("-", 1)[-1]
        font.save(output / (ps_name + ".otf"))
    finally:
        font.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regular", type=Path, required=True)
    parser.add_argument("--bold", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    marker = args.out / "palt.available"
    marker.unlink(missing_ok=True)
    for style, source in (("Regular", args.regular), ("Bold", args.bold)):
        build(source, style, args.out)
    marker.touch()


if __name__ == "__main__":
    main()
