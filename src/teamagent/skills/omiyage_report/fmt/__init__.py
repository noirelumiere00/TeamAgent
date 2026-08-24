"""お土産資料 便1 の FMT レンダラ（deck_spec_fmt_v1 準拠）。

仕様とエンジンの分離:
- 仕様 = ``teamagent/assets/deck_specs/omiyage_fmt_v1.json``（バージョン付き deck spec）
- 入力 = spec の ``input_contract`` に従う計測JSON（deck_meta + slide_plan）。
  文言はエンジン側が作文して渡す（レンダラ無作文原則）。
- 出力 = 1920x1080 セクション列 HTML（画像モードPPTXの原稿）+ 編集用ネイティブPPTX。
"""

from teamagent.skills.omiyage_report.fmt.build import (
    FmtDeckArtifacts,
    build_delivery_comment,
    build_image_pptx,
    render_fmt_deck,
)
from teamagent.skills.omiyage_report.fmt.contract import DeckContent, FmtContractError
from teamagent.skills.omiyage_report.fmt.fonts import FmtFontError
from teamagent.skills.omiyage_report.fmt.spec import FmtDeckSpec, FmtSpecError, load_fmt_spec

__all__ = [
    "DeckContent",
    "FmtContractError",
    "FmtDeckArtifacts",
    "FmtDeckSpec",
    "FmtFontError",
    "FmtSpecError",
    "build_delivery_comment",
    "build_image_pptx",
    "load_fmt_spec",
    "render_fmt_deck",
]
