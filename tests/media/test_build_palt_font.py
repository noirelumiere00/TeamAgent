"""実書体を含めず、合成 palt の焼き込みを検証する。"""

import importlib.util
from pathlib import Path

import pytest
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection, TTFont

SCRIPT = Path(__file__).resolve().parents[2] / "infra/docker/render-qa/fonts/build_palt_font.py"
spec = importlib.util.spec_from_file_location("build_palt_font", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def synthetic_font(path, cff=False, features=None, family="Noto Sans CJK JP"):
    builder = FontBuilder(1000, isTTF=not cff)
    order = [".notdef", "A", "B", "C"]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({65: "A", 66: "B", 67: "C"})
    outlines = {}
    for name in order:
        pen = T2CharStringPen(500, None) if cff else TTGlyphPen(None)
        pen.moveTo((100, 0))
        pen.lineTo((300, 0))
        pen.lineTo((300, 600))
        pen.lineTo((100, 600))
        pen.closePath()
        outlines[name] = pen.getCharString() if cff else pen.glyph()
    if cff:
        builder.setupCFF(
            "NotoSansCJKJP-Regular",
            {"FullName": family, "FamilyName": family, "Weight": "Regular"},
            outlines,
            {},
        )
    else:
        builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics({name: (500, 100) for name in order})
    builder.setupHorizontalHeader(ascent=900, descent=-200, lineGap=40)
    builder.setupNameTable(
        {"familyName": family, "styleName": "Regular", "psName": "NotoSansCJKJP-Regular"}
    )
    builder.setupOS2(
        sTypoAscender=900, sTypoDescender=-200, sTypoLineGap=40, usWinAscent=900, usWinDescent=200
    )
    builder.setupPost()
    if features:
        addOpenTypeFeaturesFromString(builder.font, features)
    builder.save(path)
    return path


@pytest.mark.parametrize("cff", [False, True])
@pytest.mark.parametrize("extension", [False, True])
@pytest.mark.parametrize("style", ["Regular", "Bold"])
def test_palt_bakes_horizontal_values_and_preserves_vertical(tmp_path, cff, extension, style):
    use = "useExtension" if extension else ""
    source = synthetic_font(
        tmp_path / "noto.otf",
        cff,
        f"""
        languagesystem DFLT dflt;
        lookup P {use} {{ pos A < -50 80 -100 90 >; pos B < 25 0 -75 0 >; }} P;
        lookup Q {{ pos A < 10 0 -20 0 >; }} Q;
        feature palt {{ lookup P; lookup Q; }} palt;
        feature kern {{ pos A B -15; }} kern;
    """,
    )
    module.build(source, style, tmp_path)
    with TTFont(tmp_path / f"TAPaltGothic-{style}.otf") as font:
        assert font["hmtx"].metrics["A"] == (380, 60)
        assert font["hmtx"].metrics["B"] == (425, 125)
        assert font["hmtx"].metrics["C"] == (500, 100)
        assert (font["hhea"].ascent, font["hhea"].descent, font["hhea"].lineGap) == (900, -200, 40)
        os2 = font["OS/2"]
        assert (
            os2.sTypoAscender,
            os2.sTypoDescender,
            os2.sTypoLineGap,
            os2.usWinAscent,
            os2.usWinDescent,
        ) == (900, -200, 40, 900, 200)
        assert font["name"].getDebugName(1) == "TA Palt Gothic"
        assert font["name"].getDebugName(2) == style
        assert font["name"].getDebugName(6) == f"TAPaltGothic-{style}"
        table = font["GPOS"].table
        assert [r.FeatureTag for r in table.FeatureList.FeatureRecord] == ["kern"]
        assert table.ScriptList.ScriptRecord[0].Script.DefaultLangSys.FeatureIndex == [0]
        glyphset = font.getGlyphSet()
        for name, x in [("A", 60), ("B", 125), ("C", 100)]:
            pen = BoundsPen(glyphset)
            glyphset[name].draw(pen)
            assert pen.bounds == (x, 0, x + 200, 600)


@pytest.mark.parametrize("features", [None, "feature kern { pos A B -20; } kern;"])
def test_missing_palt_fails_without_output(tmp_path, features):
    source = synthetic_font(tmp_path / "noto.otf", features=features)
    with pytest.raises(ValueError, match="palt missing"):
        module.build(source, "Regular", tmp_path)
    assert not (tmp_path / "TAPaltGothic-Regular.otf").exists()


def test_japanese_langsys_preferred_to_default_and_other_languages(tmp_path):
    source = synthetic_font(
        tmp_path / "noto.otf",
        features="""
        languagesystem DFLT dflt;
        languagesystem hani dflt;
        languagesystem hani JAN;
        languagesystem hani ZHS;
        feature palt {
          script DFLT; language dflt; pos A -10;
          script hani; language dflt; pos A -20;
          language JAN exclude_dflt; pos A -100;
          language ZHS exclude_dflt; pos A -200;
        } palt;
    """,
    )
    module.build(source, "Regular", tmp_path)
    with TTFont(tmp_path / "TAPaltGothic-Regular.otf") as font:
        assert font["hmtx"].metrics["A"] == (400, 100)


def test_ttc_selects_jp_face(tmp_path):
    other = synthetic_font(tmp_path / "other.ttf", family="Noto Sans CJK SC")
    jp = synthetic_font(tmp_path / "jp.ttf", features="feature palt { pos A -100; } palt;")
    collection = TTCollection()
    collection.fonts = [TTFont(other), TTFont(jp)]
    source = tmp_path / "noto.ttc"
    collection.save(source)
    collection.close()
    module.build(source, "Regular", tmp_path)
    with TTFont(tmp_path / "TAPaltGothic-Regular.otf") as font:
        assert font["hmtx"].metrics["A"] == (400, 100)


def test_marker_only_after_both_styles_succeed(tmp_path, monkeypatch):
    regular = synthetic_font(
        tmp_path / "regular.ttf", features="feature palt { pos A -100; } palt;"
    )
    bold = synthetic_font(tmp_path / "bold.ttf")
    out = tmp_path / "out"
    out.mkdir()
    marker = out / "palt.available"
    marker.touch()
    monkeypatch.setattr(
        "sys.argv", [str(SCRIPT), "--regular", str(regular), "--bold", str(bold), "--out", str(out)]
    )
    with pytest.raises(ValueError, match="palt missing"):
        module.main()
    assert not marker.exists()
    synthetic_font(bold, features="feature palt { pos A -80; } palt;")
    module.main()
    assert marker.exists()


def test_singlepos_format_one_and_default_langsys(tmp_path):
    source = synthetic_font(
        tmp_path / "noto.otf",
        features="""
        languagesystem DFLT dflt;
        languagesystem latn dflt;
        feature palt {
          script DFLT; language dflt; pos [A B] < -30 0 -60 0 >;
          script latn; language dflt; pos A -200;
        } palt;
    """,
    )
    with TTFont(source) as font:
        assert font["GPOS"].table.LookupList.Lookup[0].SubTable[0].Format == 1
    module.build(source, "Regular", tmp_path)
    with TTFont(tmp_path / "TAPaltGothic-Regular.otf") as font:
        assert font["hmtx"].metrics["A"] == (440, 70)
        assert font["hmtx"].metrics["B"] == (440, 70)


def test_non_singlepos_palt_fails(tmp_path):
    source = synthetic_font(tmp_path / "noto.otf", features="feature palt { pos A B -20; } palt;")
    with pytest.raises(ValueError, match="unsupported palt lookup type"):
        module.build(source, "Regular", tmp_path)
