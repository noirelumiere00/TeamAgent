"""全 Skill の description で共有する ``_user_context`` の渡し方（単一情報源）。

なぜ 1 か所に固定するか（2026-09-03 本番実測）:
  OpenClaw のセッション記録 166 ファイル・tool call 363 件のうち 83 件が層1 plugin で
  block され、うち **72 件が ``_user_context must be a plain object``** だった。モデル
  （Bedrock Haiku 4.5）が引数を ``{"arguments": {"_user_context": {...}}}`` と**二重に包んで**
  いた。当時 14 スキルの description は揃って「呼び出し時は **arguments に**
  ``_user_context: …`` を必ず含める」と書いており、この「arguments に」が
  「``arguments`` という名前のラッパーを作れ」と読める＝モデルに包みを作らせる指示に
  なっていた疑いが濃い（description は tool 定義としてそのままモデルへ渡る）。

対策:
  「**引数のトップレベル（他の引数と同じ階層）に置く／包み直さない**」とだけ書き、
  ``arguments`` という語を description から消す。文言は全スキルでこの定数 1 本に統一し、
  スキルごとに書き分けない（tests/skills/test_user_context_description_contract.py が
  「``arguments に`` が 1 本も無い」ことと「統一されている」ことを横断で固定する）。

⚠️ ここを編集するときは、``arguments`` / ``引数オブジェクト`` のような
「入れ子の入れ物」を連想させる語を入れないこと。
"""

from __future__ import annotations

from typing import Final

# description に「これを書いてはいけない」語（回帰テストが全スキル横断で assert する）。
# モデルに ``{"arguments": {...}}`` という包みを作らせた実績のある表現。
FORBIDDEN_WRAPPER_PHRASES: Final[tuple[str, ...]] = (
    "arguments に",
    "arguments には",
    "arguments の中に",
    "arguments 内に",
    "引数の arguments",
)

USER_CONTEXT_RULE: Final[str] = (
    "呼び出し時は `_user_context: {slack_user_id: '<いま話している相手のuser_id>'}` を、"
    "他の引数と同じ**トップレベル**に並べて必ず渡す"
    "（本人解決鍵。入れ子のオブジェクトで包み直さない）。"
)

__all__ = ["FORBIDDEN_WRAPPER_PHRASES", "USER_CONTEXT_RULE"]
