"""レポートの PowerPoint（aico_report_v1）の寸法・色・書体・字の大きさの唯一の正本。

描く側（media worker の ``teamagent.media.deck_render``）は寸法の数字を持たない。ここで組んだ
``TemplateSpec`` を DeckSpec に入れて渡し、描く側はそれどおりにマスターとレイアウトを作る。
寸法を直すときはこのファイルだけを直す。

版面は 16:9（13.333in × 7.5in = 12192000 × 6858000 EMU）。
外部の FMT は土台にしない（色だけ合わせる）。
飾り・ロゴ・左端の帯・グラデーション・影は持たない。
"""

from __future__ import annotations

from functools import lru_cache

from teamagent.media.deck_contracts import (
    BoxSpec,
    LayoutSpec,
    TemplateSpec,
    ThemeSpec,
    TileSpec,
    TypographySpec,
)

TEMPLATE_NAME = "aico_report_v1"

EMU_PER_IN = 914_400
SLIDE_W = 12_192_000
SLIDE_H = 6_858_000

# ── 字の大きさ（pt） ──────────────────────────────────────────────────────────
TITLE_PT = 28
BODY_PT = 14
CONDITION_PT = 12
TABLE_PT_FEW_ROWS = 14  # 6 行以下
TABLE_PT = 12  # 7 行以上（上位 10 本の表もこれ）
APPENDIX_TABLE_PT = 11
CARD_PT = 12  # 動画カードの説明（幅が 2.4in しかないので本文より 1 段小さい）
TAG_PT = 10  # 出どころの札
FOOTER_PT = 10
BIG_NUMBER_PT = 66
SMALL_NUMBER_PT = 32
BODY_SPACE_PT = 14  # 本文の点と点の間（段落の前の空き）
BODY_LINE_PCT = 130  # 本文の行間
CHART_PT = 12  # グラフの軸・値・凡例
TYPOGRAPHY = TypographySpec(
    chart_pt=CHART_PT,
    chart_gap_width=60,
    chart_min_label_share=0.06,
    table_row_in_per_pt=0.034,  # 14pt で約 0.48in
    table_cell_margin_emu=54_864,  # 0.06in
    bullet_indent_emu=228_600,  # 0.25in
    text_inset_emu=45_720,  # 0.05in
)

# ── 字の量 ────────────────────────────────────────────────────────────────────
TITLE_MAX = 34
SUB_MAX = 48
BODY_TARGET = 250
BODY_MAX = 450
CELL_MAX = 24
POINTS_MAX = 3
MAIN_TABLE_ROWS = 6
MAIN_TABLE_COLS = 5
APPENDIX_ROWS = 10
CARDS_MAX = 5

# ── レイアウトの名前（名前で選ぶ） ────────────────────────────────────────────
L_COVER = "R_表紙"
L_CONCLUSION = "R_結論"
L_NUMBERS = "R_数字"
L_CHART = "R_グラフ"
L_TABLE = "R_表"
L_CARDS = "R_動画カード"
L_APPENDIX = "R_付録の表"

# ── 色（テーマ）。1 枚 1 強調色。色だけで伝えず ★・太字・「注意：」を併用する ────
THEME_COLORS: dict[str, str] = {
    "dk1": "1F2937",  # 本文
    "lt1": "FFFFFF",
    "dk2": "2A3351",  # 題
    "lt2": "F3F4F6",  # タイルの地
    "accent1": "2F4B7C",  # 表の見出し行・注目する棒
    "accent2": "B24336",  # 警告だけ
    "accent3": "6B7280",  # 補助の文字・ほかの棒
    "accent4": "D9D9D9",  # ほかの棒・罫線
    "accent5": "9CA3AF",
    "accent6": "4B5563",
    "hlink": "2F4B7C",
    "folHlink": "6B7280",
}
FONT = "游ゴシック"  # 見出し・本文とも（英数も）


def _in(value: float) -> int:
    return round(value * EMU_PER_IN)


# 共通の位置（in）
_L = 0.5
_W = 13.333 - 2 * _L
_COND_Y, _COND_H = 0.26, 0.34
_COND_W = 8.6
_TAG_X = _L + 8.8
_TITLE_Y, _TITLE_H = 0.62, 1.08
_TOP = 1.75
_FOOT_Y, _FOOT_H = 7.02, 0.3


def _box(
    key: str, kind: str, idx: int, name: str, x: float, y: float, w: float, h: float, **kw: object
) -> BoxSpec:
    return BoxSpec.model_validate(
        {
            "key": key,
            "kind": kind,
            "idx": idx,
            "name": name,
            "x": _in(x),
            "y": _in(y),
            "w": _in(w),
            "h": _in(h),
            **kw,
        }
    )


def _title(y: float = _TITLE_Y, w: float = _W, h: float = _TITLE_H) -> BoxSpec:
    return _box(
        "title", "title", 0, "題", _L, y, w, h, font_pt=TITLE_PT, bold=True, color="dk2",
        anchor="t", prompt="題（言い切り 1 行）",
    )  # fmt: skip


def _condition(
    x: float = _L, y: float = _COND_Y, w: float = _COND_W, h: float = _COND_H
) -> BoxSpec:
    return _box(
        "condition", "body", 13, "条件の行", x, y, w, h, font_pt=CONDITION_PT, color="accent3",
        anchor="ctr", prompt="条件（KW・本数・取得日・媒体）",
    )  # fmt: skip


def _source() -> BoxSpec:
    return _box(
        "source", "body", 14, "出どころ", _TAG_X, _COND_Y, _W - 8.8, _COND_H, font_pt=TAG_PT,
        color="accent3", align="r", anchor="ctr", prompt="出どころ",
    )  # fmt: skip


def _footer() -> tuple[BoxSpec, BoxSpec]:
    return (
        _box("footer", "footer", 11, "フッター", _L, _FOOT_Y, 9.0, _FOOT_H, font_pt=FOOTER_PT,
             color="accent3", anchor="ctr"),
        _box("slide_number", "slide_number", 12, "ページ番号", 13.333 - _L - 1.0, _FOOT_Y, 1.0,
             _FOOT_H, font_pt=FOOTER_PT, color="accent3", align="r", anchor="ctr"),
    )  # fmt: skip


def _common() -> tuple[BoxSpec, ...]:
    return (_condition(), _source(), _title(), *_footer())


def _cover() -> LayoutSpec:
    pic_h = 6.2
    pic_w = pic_h * 9 / 16
    pic_x = 13.333 - _L - pic_w
    text_w = pic_x - _L - 0.4
    return LayoutSpec(
        name=L_COVER,
        boxes=(
            _box("addressee", "body", 15, "宛名", _L, 1.5, text_w, 0.5, font_pt=16, color="dk1",
                 anchor="b", prompt="宛名"),
            _title(y=2.15, w=text_w, h=1.75),
            _condition(y=4.05, w=text_w, h=0.6),
            _box("cover", "picture", 16, "表紙の画像", pic_x, 0.55, pic_w, pic_h,
                 prompt="表紙の画像（9:16）"),
            *_footer(),
        ),
    )  # fmt: skip


def _conclusion() -> LayoutSpec:
    return LayoutSpec(
        name=L_CONCLUSION,
        boxes=(
            *_common(),
            _box("body", "body", 1, "本文（3 点）", _L, _TOP, _W, 4.85, font_pt=BODY_PT,
                 color="dk1", bullets=True, shrink_on_overflow=True, prompt="本文",
                 space_before_pt=BODY_SPACE_PT, line_spacing_pct=BODY_LINE_PCT),
        ),
    )  # fmt: skip


def _numbers() -> LayoutSpec:
    tile_x, tile_w, tile_h, gap = 6.6, _W - 6.1, 1.5, 0.15
    boxes: list[BoxSpec] = [
        *_common(),
        _box("big_number", "body", 20, "大きな数字", _L, 1.9, 5.6, 1.55, font_pt=BIG_NUMBER_PT,
             bold=True, color="accent1", anchor="b", prompt="数字"),
        _box("big_label", "body", 21, "大きな数字のラベル", _L, 3.5, 5.6, 1.3, font_pt=BODY_PT,
             color="dk1", prompt="ラベル"),
    ]  # fmt: skip
    tiles: list[TileSpec] = []
    for i in range(3):
        y = _TOP + i * (tile_h + gap)
        tiles.append(TileSpec(x=_in(tile_x), y=_in(y), w=_in(tile_w), h=_in(tile_h)))
        boxes.append(
            _box(f"small_number_{i + 1}", "body", 22 + 2 * i, f"小さな数字 {i + 1}",
                 tile_x + 0.2, y + 0.1, 2.3, tile_h - 0.2, font_pt=SMALL_NUMBER_PT, bold=True,
                 color="dk2", anchor="ctr", prompt="数字")
        )  # fmt: skip
        boxes.append(
            _box(f"small_label_{i + 1}", "body", 23 + 2 * i, f"小さな数字のラベル {i + 1}",
                 tile_x + 2.6, y + 0.1, tile_w - 2.8, tile_h - 0.2, font_pt=BODY_PT,
                 color="dk1", anchor="ctr", prompt="ラベル")
        )  # fmt: skip
    return LayoutSpec(name=L_NUMBERS, boxes=tuple(boxes), tiles=tuple(tiles))


def _chart() -> LayoutSpec:
    chart_w = _W * 2 / 3 - 0.3
    return LayoutSpec(
        name=L_CHART,
        boxes=(
            *_common(),
            _box("chart", "chart", 30, "グラフ", _L, _TOP, chart_w, 4.9, prompt="グラフ"),
            _box("reading", "body", 31, "読み方", _L + chart_w + 0.3, _TOP, _W - chart_w - 0.3,
                 4.9, font_pt=BODY_PT, color="dk1", bullets=True, shrink_on_overflow=True,
                 prompt="読み方", space_before_pt=BODY_SPACE_PT, line_spacing_pct=BODY_LINE_PCT),
        ),
    )  # fmt: skip


def _table() -> LayoutSpec:
    return LayoutSpec(
        name=L_TABLE,
        boxes=(
            *_common(),
            _box("table", "table", 40, "表", _L, _TOP, _W, 4.45, prompt="表"),
            _box("note", "body", 41, "注記", _L, 6.3, _W, 0.5, font_pt=CONDITION_PT,
                 color="accent3", prompt="注記"),
        ),
    )  # fmt: skip


def _cards() -> LayoutSpec:
    pitch = _W / CARDS_MAX
    pic_w = 1.85
    pic_h = pic_w * 16 / 9
    boxes: list[BoxSpec] = list(_common())
    for i in range(CARDS_MAX):
        slot_x = _L + i * pitch
        boxes.append(
            _box(f"card_{i + 1}", "picture", 50 + 2 * i, f"動画 {i + 1} の表紙",
                 slot_x + (pitch - pic_w) / 2, 1.7, pic_w, pic_h, prompt="表紙（9:16）")
        )  # fmt: skip
        boxes.append(
            _box(f"caption_{i + 1}", "body", 51 + 2 * i, f"動画 {i + 1} の説明", slot_x + 0.05,
                 1.7 + pic_h + 0.08, pitch - 0.1, 1.65, font_pt=CARD_PT, color="dk1",
                 align="ctr", shrink_on_overflow=True, prompt="説明")
        )  # fmt: skip
    return LayoutSpec(name=L_CARDS, boxes=tuple(boxes))


def _appendix() -> LayoutSpec:
    return LayoutSpec(
        name=L_APPENDIX,
        boxes=(
            *_common(),
            _box("table", "table", 40, "表", _L, _TOP, _W, 4.95, prompt="表"),
        ),
    )  # fmt: skip


@lru_cache(maxsize=1)
def report_template() -> TemplateSpec:
    """aico_report_v1 のテンプレート（マスター・7 レイアウト・テーマ）。"""
    master = (
        _title(),
        _box("body", "body", 1, "本文", _L, _TOP, _W, 4.85, font_pt=BODY_PT, color="dk1",
             bullets=True, space_before_pt=BODY_SPACE_PT, line_spacing_pct=BODY_LINE_PCT),
        *_footer(),
    )  # fmt: skip
    return TemplateSpec(
        name=TEMPLATE_NAME,
        slide_w=SLIDE_W,
        slide_h=SLIDE_H,
        theme=ThemeSpec(name="Aico Report", colors=THEME_COLORS, major_font=FONT, minor_font=FONT),
        typography=TYPOGRAPHY,
        master_boxes=master,
        layouts=(
            _cover(),
            _conclusion(),
            _numbers(),
            _chart(),
            _table(),
            _cards(),
            _appendix(),
        ),
    )


__all__ = [
    "APPENDIX_ROWS",
    "APPENDIX_TABLE_PT",
    "BODY_MAX",
    "BODY_PT",
    "BODY_TARGET",
    "CARDS_MAX",
    "CELL_MAX",
    "L_APPENDIX",
    "L_CARDS",
    "L_CHART",
    "L_CONCLUSION",
    "L_COVER",
    "L_NUMBERS",
    "L_TABLE",
    "MAIN_TABLE_COLS",
    "MAIN_TABLE_ROWS",
    "POINTS_MAX",
    "SLIDE_H",
    "SLIDE_W",
    "SUB_MAX",
    "TABLE_PT",
    "TABLE_PT_FEW_ROWS",
    "TEMPLATE_NAME",
    "TITLE_MAX",
    "TITLE_PT",
    "report_template",
]
