"""デッキごとの woff2 サブセット生成と missing-glyph ハードゲート。

spec ``tokens.typography.font_embedding`` の実装:
- Google Fonts link は media worker の ``_EXTERNAL_HTML_REF`` が拒否するため、
  同梱フル書体（OFL・``teamagent/assets/fonts/``）からデッキ実テキスト+基本
  チャーセットのサブセット woff2 を生成し data:URI で ``@font-face`` 埋め込みする。
- missing glyph 漏れ0をハードゲート化: 漏れ検出=レンダリング失敗の fail-fast
  （勝手な代替字形での続行禁止。画像焼き込みのため豆腐が納品物に固定される）。
"""

from __future__ import annotations

import base64
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_ASSET_FONT_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"

FontRole = Literal["mincho", "gothic", "latin"]

# サブセットへ必ず含める基本チャーセット（数字・約物・K/M/%・括弧・スラッシュ等）
BASE_CHARSET = (
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    " %#&+-*/=.,:;!?°"
    "()[]{}<>\"'@_|\\^"
    "（）「」『』【】〈〉"
    "、。・：；？！〜ー―—…※→↑↓"
    "0１２３４５６７８９％／　"
    "KM億万円本件位"
)


class FmtFontError(RuntimeError):
    """フォント資産の欠落・missing glyph（fail-fast・代替字形での続行禁止）。"""


@dataclass(frozen=True)
class _Face:
    role: FontRole
    family: str
    filename: str
    css_weight: str  # "700" または可変フォントのレンジ "400 700"
    generic_fallback: str  # 名目フォールバック（発火=品質事故と定義）


# 同梱資産（OFL）。ファイル名は google/fonts の配布名に合わせる。
_FACES: tuple[_Face, ...] = (
    _Face("mincho", "Shippori Mincho B1", "ShipporiMinchoB1-SemiBold.ttf", "600", "serif"),
    _Face("mincho", "Shippori Mincho B1", "ShipporiMinchoB1-Bold.ttf", "700", "serif"),
    _Face("mincho", "Shippori Mincho B1", "ShipporiMinchoB1-ExtraBold.ttf", "800", "serif"),
    _Face("gothic", "Zen Kaku Gothic New", "ZenKakuGothicNew-Regular.ttf", "400", "sans-serif"),
    _Face("gothic", "Zen Kaku Gothic New", "ZenKakuGothicNew-Medium.ttf", "500", "sans-serif"),
    _Face("gothic", "Zen Kaku Gothic New", "ZenKakuGothicNew-Bold.ttf", "700", "sans-serif"),
    _Face("gothic", "Zen Kaku Gothic New", "ZenKakuGothicNew-Black.ttf", "900", "sans-serif"),
    _Face("latin", "Instrument Sans", "InstrumentSans-Variable.ttf", "400 700", "sans-serif"),
)

# 便1テンプレートが実際に使う weight（未使用faceは埋め込まずサイズを守る）
BEN1_WEIGHTS: dict[FontRole, tuple[str, ...]] = {
    "mincho": ("700", "800"),
    "gothic": ("400", "500", "700", "900"),
    "latin": ("400 700",),
}

# 各役割スタックのフォールバック順（すべて埋め込みフォント内で完結させる）
STACK_ROLES: dict[FontRole, tuple[FontRole, ...]] = {
    "mincho": ("mincho", "gothic"),
    "gothic": ("gothic",),
    "latin": ("latin", "gothic"),
}


def font_dir() -> Path:
    return _ASSET_FONT_DIR


def _needed_codepoints(text: str) -> set[int]:
    return {ord(ch) for ch in text if not ch.isspace() and ord(ch) >= 0x20}


def _subset_woff2(path: Path, charset: str) -> bytes:
    from fontTools import subset

    options = subset.Options()
    options.flavor = "woff2"
    font = subset.load_font(str(path), options, lazy=True)
    try:
        subsetter = subset.Subsetter(options)
        subsetter.populate(text=charset)
        subsetter.subset(font)
        buffer = io.BytesIO()
        font.save(buffer)
    finally:
        font.close()
    return buffer.getvalue()


def _cmap_codepoints(woff2_bytes: bytes) -> set[int]:
    from fontTools.ttLib import TTFont

    with TTFont(io.BytesIO(woff2_bytes), lazy=True) as font:
        best = font.getBestCmap()
        return set(best.keys())


@dataclass(frozen=True)
class EmbeddedFonts:
    css: str
    total_bytes: int
    families: Mapping[FontRole, str]


def _face_path(base: Path, face: _Face) -> Path:
    path = base / face.filename
    if not path.is_file():
        raise FmtFontError(
            f"font asset missing: {path}（フル3書体の同梱が前提・spec_README 必要改修5）"
        )
    return path


def build_embedded_fonts(
    chars_by_role: Mapping[FontRole, Iterable[str]],
    *,
    base_dir: Path | None = None,
) -> EmbeddedFonts:
    """役割ごとの実テキストからサブセット woff2 を生成し @font-face CSS を返す。

    ゲート: 各役割スタック（例 latin → Instrument Sans + Zen Kaku Gothic New）の
    埋め込み cmap 和集合で全 codepoint が引けなければ ``FmtFontError``。
    """
    base = base_dir or font_dir()

    # 各書体へ渡すチャーセット = その書体を含む全スタックの文字の和集合 + 基本セット
    charset_by_role: dict[FontRole, set[str]] = {"mincho": set(), "gothic": set(), "latin": set()}
    for stack_role, chars in chars_by_role.items():
        text = "".join(chars)
        for member in STACK_ROLES[stack_role]:
            charset_by_role[member].update(text)
    for role in charset_by_role:
        charset_by_role[role].update(BASE_CHARSET)

    css_parts: list[str] = []
    total = 0
    cmap_by_role: dict[FontRole, set[int]] = {}
    for face in _FACES:
        if face.css_weight not in BEN1_WEIGHTS[face.role]:
            continue
        path = _face_path(base, face)
        woff2 = _subset_woff2(path, "".join(sorted(charset_by_role[face.role])))
        total += len(woff2)
        cmap_by_role.setdefault(face.role, set()).update(_cmap_codepoints(woff2))
        encoded = base64.b64encode(woff2).decode("ascii")
        css_parts.append(
            "@font-face{"
            f"font-family:'{face.family}';"
            f"font-style:normal;font-weight:{face.css_weight};"
            f"src:url(data:font/woff2;base64,{encoded}) format('woff2');"
            "}"
        )

    missing: dict[str, set[int]] = {}
    for stack_role, chars in chars_by_role.items():
        needed = _needed_codepoints("".join(chars))
        covered: set[int] = set()
        for member in STACK_ROLES[stack_role]:
            covered |= cmap_by_role.get(member, set())
        gap = needed - covered
        if gap:
            missing[stack_role] = gap
    if missing:
        detail = "; ".join(
            f"{role}: " + ", ".join(f"U+{cp:04X}({chr(cp)})" for cp in sorted(gap)[:10])
            for role, gap in sorted(missing.items())
        )
        raise FmtFontError(f"missing glyphs (fail-fast, no fallback rendering): {detail}")

    families = {role: _family_stack_css(role) for role in STACK_ROLES}
    return EmbeddedFonts(css="".join(css_parts), total_bytes=total, families=families)


def _family_stack_css(stack_role: FontRole) -> str:
    names: list[str] = []
    generic = "sans-serif"
    for member in STACK_ROLES[stack_role]:
        for face in _FACES:
            if face.role == member and f"'{face.family}'" not in names:
                names.append(f"'{face.family}'")
                if member == stack_role:
                    generic = face.generic_fallback
    return ",".join([*names, generic])
