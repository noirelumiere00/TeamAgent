"""video_algorithm の v1 / v2 プロンプトの関係を固定する。

- v2/system.md（1 本ずつの動画分析）は v1 の複製で、H1 の版表記だけが違う。本番既定は v2、
  env VIDEO_ALGO_PROMPT_VERSION=v1 で戻せるので、片方だけ直すと本番と戻し先で中身がずれる。
  わざと変えるときは、このテストを直して差分を明示すること。
- v2/synthesis.md が「ρ を書いた文はコードで削除される」と約束する欄が、コード
  （synthesis.ground_synthesis で deny=RHO_TERMS を通す欄）と一致すること。
"""

from __future__ import annotations

import re
from pathlib import Path

import teamagent.prompts

_DIR = Path(teamagent.prompts.__file__).parent / "video_algorithm"


def _read(version: str, name: str) -> list[str]:
    return (_DIR / version / f"{name}.md").read_text(encoding="utf-8").splitlines()


def test_v2_system_is_a_copy_of_v1_except_the_title() -> None:
    v1, v2 = _read("v1", "system"), _read("v2", "system")
    assert v1[0].endswith("system prompt v1")
    assert v2[0].endswith("system prompt v2")
    assert v1[0].removesuffix("v1") == v2[0].removesuffix("v2")
    assert v2[1:] == v1[1:]


def test_v2_synthesis_lists_every_field_whose_rho_sentences_are_removed() -> None:
    text = "\n".join(_read("v2", "synthesis"))
    m = re.search(r"（([^（）]*)に ρ を書いた文はコードで削除される）", text)
    assert m is not None
    listed = set(re.findall(r"`(\w+)`", m.group(1)))
    # synthesis.py: headline・creative_brief は項目単位、残りは文単位で ρ を捨てる
    assert listed == {
        "headline",
        "strategy",
        "creative_brief",
        "posting_design",
        "client_pitch",
        "shared_funnel",
    }
