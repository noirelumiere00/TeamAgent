"""``truncate_graphemes`` の置き場所の契約。

本体は adapters からも使えるよう ``teamagent.util`` に置き、``skills/_shared`` は
再エクスポートだけにする（2 か所に別実装ができると、経路によって切り方が変わる）。
"""

from __future__ import annotations


def test_skills_shared_reexports_the_util_implementation() -> None:
    from teamagent.skills._shared import grapheme_cut as shared
    from teamagent.util import grapheme_cut as util

    assert shared.truncate_graphemes is util.truncate_graphemes
    assert shared.__all__ == ["truncate_graphemes"]
