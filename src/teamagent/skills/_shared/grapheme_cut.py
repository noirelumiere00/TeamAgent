"""``truncate_graphemes`` の再エクスポート（本体は ``teamagent.util.grapheme_cut``）。

本体を util に置いたのは adapters（予定リマインドの件名）からも使うため。adapters は
skills を import できない（import-linter の契約）。既存の import 先を変えずに済むよう、
ここからも同じ関数を出す。
"""

from __future__ import annotations

from teamagent.util.grapheme_cut import truncate_graphemes

__all__ = ["truncate_graphemes"]
