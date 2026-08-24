"""編集用ネイティブPPTX（stdlib OOXML ライタ）の実出力検証。"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

import pytest

from teamagent.skills.omiyage_report.fmt.build import render_fmt_deck
from teamagent.skills.omiyage_report.fmt.editable import EDIT_MARKER
from teamagent.skills.omiyage_report.fmt.ooxml import contain_box, image_size

from .fmt_fixtures import CTA, make_deck_content, make_png_bytes


@pytest.fixture(scope="module")
def editable_pptx() -> bytes:
    artifacts = render_fmt_deck(make_deck_content(), generated_on="2026-08-24")
    assert EDIT_MARKER in artifacts.editable_filename
    return artifacts.editable_pptx


def test_package_is_valid_zip_with_wellformed_xml(editable_pptx: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(editable_pptx)) as archive:
        names = set(archive.namelist())
        assert "[Content_Types].xml" in names
        assert "ppt/presentation.xml" in names
        assert "ppt/slideMasters/slideMaster1.xml" in names
        assert "ppt/slideLayouts/slideLayout1.xml" in names
        assert "ppt/theme/theme1.xml" in names
        slides = sorted(name for name in names if name.startswith("ppt/slides/slide"))
        assert sum(1 for name in slides if name.endswith(".xml")) == 9
        for name in names:
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(archive.read(name))  # 整形式XML

        # 全 relationship の解決先が実在する
        for name in [n for n in names if n.endswith(".rels")]:
            base = name.rsplit("_rels/", 1)[0]
            tree = ElementTree.fromstring(archive.read(name))
            for rel in tree:
                target = rel.attrib["Target"]
                resolved = target[3:] if target.startswith("../") else target
                if target.startswith("../"):
                    resolved = "ppt/" + resolved
                elif base:
                    resolved = base + resolved
                assert resolved in names, f"{name} -> {target}"


def test_opens_with_python_pptx_and_keeps_fixed_texts(editable_pptx: bytes) -> None:
    pptx = pytest.importorskip("pptx")
    presentation = pptx.Presentation(io.BytesIO(editable_pptx))
    assert len(presentation.slides) == 9
    # 16:9・1920x1080px × 6350 EMU/px
    assert (presentation.slide_width, presentation.slide_height) == (12192000, 6858000)

    def slide_text(index: int) -> str:
        return "\n".join(
            shape.text_frame.text
            for shape in presentation.slides[index].shapes
            if shape.has_text_frame
        )

    assert EDIT_MARKER in slide_text(0)  # 表紙に「編集用（見た目は画像版が正）」
    assert CTA in slide_text(8)  # 総括の結論バンドに CTA 固定文
    assert "提供サムネ" in slide_text(7)  # E カードの画像種別ラベル


def test_pictures_keep_aspect_ratio_contain(editable_pptx: bytes) -> None:
    pptx = pytest.importorskip("pptx")
    presentation = pptx.Presentation(io.BytesIO(editable_pptx))
    picture_shapes = [
        shape
        for slide in presentation.slides
        for shape in slide.shapes
        if shape.shape_type == 13  # PICTURE
    ]
    assert picture_shapes  # 表紙2 + 実例2 + E5
    for shape in picture_shapes:
        ratio = shape.width / shape.height
        assert ratio == pytest.approx(9 / 16, rel=0.02)  # 元比率維持（crop なし）


def test_contain_math_and_image_size_parser() -> None:
    png = make_png_bytes(width=90, height=160)
    assert image_size(png, "png") == (90, 160)
    x, y, w, h = contain_box(90, 160, 0, 0, 236, 420)
    assert w == 236 and h == pytest.approx(419.6, abs=1)  # 幅基準で contain
    assert x == 0 and y == pytest.approx((420 - h) / 2, abs=0.01)
    x, y, w, h = contain_box(160, 90, 0, 0, 236, 420)
    assert w == 236 and h == pytest.approx(132.75, abs=0.01)  # 横長ソースは上下に白余白
